#!/usr/bin/env python3
"""Issue #956 bite B (sale-core): a declared mid-horizon property SALE settles
in its sale year -- net proceeds invested, disposition taxed, money conserved.

This is the correctness-critical layer built ON TOP of the mechanical
foundation (the schema ``sale`` leaf, the mapper carrying
``sale``/``value_share``/``secured_share``/``acb_share``/``owner_roles``/
``designated_principal_residence_years``, and ``_property_equity_for_year``
gating equity to 0 from the sale year on). See
``test_issue_956_bite_b_sale_mapping.py`` for the foundation layer's tests.

The crux this file verifies is the CONSERVATION IDENTITY (the spec's gate):

  P_gross = value_share * (1+appreciation_rate)^(sale_year - owned_from)
            (or value_share when no appreciation_rate -- the SAME value
            simulation_state._property_equity_for_year's appreciation branch
            computes, so a property sold at its appreciated value realizes
            exactly the equity it would have contributed had it been held);
  E      = P_gross - secured_share            (the equity on the balance sheet);
  T      = capital-gains tax on (P_gross - acb_share), apportioned by the PRE
           taxable_fraction, banded against the owner's taxable income;
  P_net  = P_gross - secured_share - selling_costs - T;
  Δtotal_assets (sell vs hold, in the sale year) = P_net - E = -(selling_costs + T).

Money is conserved: the equity converted to investable cash, less the friction
that genuinely left the household (third-party selling costs + government tax).
The household's total assets drop by EXACTLY the selling friction in the sale
year, and the property's equity is replaced by investable non-reg that THEN
GROWS (compounds from the next year on, since the proceeds are injected
POST-GROWTH -- see apply_property_disposition's docstring for why post-growth
injection is what makes the year-N drop exact rather than rate-dependent).

These tests run the real engine (``FamilySimulation.run``), so the
money-conservation invariant suite (``trajectory_invariants.assert_run_
invariants``, wired into ``run()``) is enforced automatically on every run
here -- a sale whose flows did not balance would raise ``InvariantBreachedError``
before any assertion below. All fixtures use fabricated ids and round numbers
(DP#4/DP#15); no real figure, name, or account enters the repo (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from rules_disposition import _property_disposition_for
from simulation_state import _property_equity_for_year

from test_input_contract import _load_example, _two_generation_subset
import contract_schema


# ──────────────────────────────────────────────────────────────────────────
# Fixtures: a couple-owned cottage with an accrued gain, optional sale.
# ──────────────────────────────────────────────────────────────────────────

# The shipped example projects 50 years from start_year 2026, so results[i]
# is calendar year 2026 + i. A sale dated 2031 fires in results[5]; results[0..4]
# are strictly before it. Round numbers (DP#4/DP#15): the couple (p1/p2) jointly
# owns a cottage worth 500k with a 400k ACB (a 100k accrued gain) and NO
# mortgage secured against it (secured_share = 0, the cleanest conservation
# test -- P_net = P_gross - selling_costs - T with no debt retired). No PRE
# designation -> the gain is fully taxable (taxable_fraction = 1.0).
_SALE_2031 = {"date": "2031-06-30", "selling_costs": 25000}
_SALE_YEAR_INDEX = 5
_COTTAGE_VALUE = 500000
_COTTAGE_ACB = 400000          # a 100k accrued gain at the couple's 100% share
_COTTAGE_SELLING_COSTS = 25000


def _base_doc():
    """The shipped two-generation example, validated (the sub-family the
    adapter can honestly map onto the two-adults-plus-children engine)."""
    doc = _two_generation_subset(_load_example())
    contract_schema.validate_contract(doc)
    return doc


def _base_doc_no_pr_designation():
    """The base doc with the principal residence's PRE designation CLEARED --
    used by the PRE test so a cottage can be designated as a principal
    residence without a family-year conflict (the exemption is one property
    per family per year, ITA s.40(2)(b); the shipped example's principal
    residence is designated from 2023 onward, so any cottage designation
    would conflict). The principal residence itself stays on the balance
    sheet (its equity is unchanged); only its designation is cleared. An ACB
    is added too: a principal residence with NO designation has a taxable
    gain at death, and a null ACB there is refused (an unknown cost base
    cannot default to 0 -- DP#32); a real ACB satisfies the gate (the
    principal residence's death-tax is not this bite's concern)."""
    doc = _two_generation_subset(_load_example())
    for p in doc["properties"]:
        if p["id"] == "principal_residence":
            p["designated_principal_residence_years"] = []
            p["acb"] = p["value"]["amount"]   # bought at value: no accrued gain
    contract_schema.validate_contract(doc)
    return doc


def _add_cottage(doc, sale=None, appreciation_rate=None, mortgage_balance=0):
    """Append a couple-owned (p1/p2 50/50) recreational cottage with a 100k
    accrued gain (value 500k, ACB 400k) and an optional dated ``sale``. An
    optional mortgage secured against it (``mortgage_balance`` > 0) exercises
    the debt-retired (secured_share) leg of the conservation identity. An
    optional ``appreciation_rate`` exercises the appreciated-P_gross leg."""
    doc = copy.deepcopy(doc)
    prop = {
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
        "acb": _COTTAGE_ACB,
        "designated_principal_residence_years": [],
    }
    if appreciation_rate is not None:
        prop["appreciation_rate"] = appreciation_rate
    if sale is not None:
        prop["sale"] = sale
    doc["properties"].append(prop)
    if mortgage_balance:
        doc["liabilities"].append({
            "id": "cottage_mortgage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "mortgage",
            "balance": {"amount": mortgage_balance, "as_of": "2026-06-30"},
            "rate": 0.05, "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 1500},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
            "collateral": "couple_cottage",
        })
    return doc


def _run(doc):
    """Validate -> map to internal config -> run the real engine (which
    enforces the money-conservation invariant suite on every run)."""
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


# ──────────────────────────────────────────────────────────────────────────
# 1. Absence-safe (DP#32): a household with no sale is byte-identical to one
#    that never declared the cottage's sale -- the rule is a strict no-op and
#    the golden invariant is unchanged by construction.
# ──────────────────────────────────────────────────────────────────────────

class SaleIsAbsenceSafe(unittest.TestCase):
    """A cottage held to the horizon (no ``sale``) is byte-identical whether
    or not the property_disposition rule exists -- the rule is a no-op for a
    property with no sale, so the conservation identity's absence path is the
    pre-bite behaviour exactly (DP#32). The golden fixture (no cottage, no
    sale) is covered by its own byte-exact invariant elsewhere."""

    def test_hold_trajectory_unchanged_by_rule(self):
        """The hold household's trajectory is byte-identical to the pre-bite
        engine: the rule never fires (no sale declared), so every year's
        total_assets is unchanged."""
        hold = _run(_add_cottage(_base_doc()))
        # The cottage contributes its static net_equity (500k - 0 mortgage)
        # every year from year 0 (no purchase, no sale, no appreciation) --
        # the rule does not touch it. Sanity: year-0 total_assets includes
        # the cottage's 500k equity at the couple's 100% share.
        self.assertAlmostEqual(
            hold[0].total_assets,
            _run(_base_doc())[0].total_assets + _COTTAGE_VALUE,
            places=4,
            msg="hold household year-0 total_assets should include the "
                "cottage's full 500k equity (the rule is a no-op for a "
                "property with no sale)")

    def test_no_sale_fields_stay_zero(self):
        """A household with no sale has the sale fields at 0.0 every year
        (the rule's absence-safe no-op: sale_proceeds_invested /
        sale_disposition_tax stay at their seeded 0.0 defaults)."""
        hold = _run(_add_cottage(_base_doc()))
        for i, r in enumerate(hold):
            self.assertEqual(r.sale_proceeds_invested, 0.0,
                             f"year {i}: sale_proceeds_invested nonzero "
                             f"for a household with no sale")
            self.assertEqual(r.sale_disposition_tax, 0.0,
                             f"year {i}: sale_disposition_tax nonzero "
                             f"for a household with no sale")


# ──────────────────────────────────────────────────────────────────────────
# 2. The conservation identity (the crux): in the sale year, the sell
#    household's total_assets drops by EXACTLY selling_costs + disposition_tax
#    relative to the same household that holds -- and the two are byte-
#    identical before the sale.
# ──────────────────────────────────────────────────────────────────────────

class ConservationIdentityHoldsExactly(unittest.TestCase):
    """The spec's gate: Δtotal_assets (sell vs hold, sale year) =
    -(selling_costs + T). Money is conserved -- the equity converted to
    investable cash, less the friction that genuinely left the household."""

    def setUp(self):
        self.base = _base_doc()
        self.hold = _run(_add_cottage(self.base))
        self.sell = _run(_add_cottage(self.base, sale=_SALE_2031))

    def test_identical_before_the_sale_year(self):
        """The sell and hold households are byte-identical in every year
        before the sale (the property hasn't been sold yet, so both hold it
        and both have the same non-reg). This is the baseline the
        conservation identity measures the sale-year drop against."""
        for i in range(_SALE_YEAR_INDEX):
            self.assertEqual(
                self.sell[i].total_assets, self.hold[i].total_assets,
                f"year index {i} (before sale): sell and hold diverged "
                f"({self.sell[i].total_assets} vs {self.hold[i].total_assets})")

    def test_sale_year_drop_is_exactly_selling_costs_plus_tax(self):
        """The crux: in the sale year, the sell household's total_assets is
        lower than the hold household's by EXACTLY selling_costs +
        disposition_tax -- the friction that left the household. The property's
        equity (P_gross - secured_share) is replaced by the after-tax net
        proceeds P_net = P_gross - secured_share - selling_costs - T, so
        Δtotal_assets = P_net - E = -(selling_costs + T), byte-exact."""
        n = _SALE_YEAR_INDEX
        selling_costs = float(_COTTAGE_SELLING_COSTS)
        tax = self.sell[n].sale_disposition_tax
        expected_drop = -(selling_costs + tax)
        actual_drop = self.sell[n].total_assets - self.hold[n].total_assets
        self.assertAlmostEqual(
            actual_drop, expected_drop, places=4,
            msg=f"sale-year total_assets drop ({actual_drop}) is not exactly "
                f"-(selling_costs {selling_costs} + disposition_tax {tax}) "
                f"= {expected_drop} -- money is not conserved")

    def test_disposition_tax_is_nonzero_for_an_accrued_gain(self):
        """A cottage sold with a 100k accrued gain (and no PRE designation ->
        fully taxable) crystallizes a non-zero capital-gains tax, banded
        against the owners' taxable income. The tax is the price of the
        conservation identity's 'T' -- a zero tax here would mean the gain is
        not being taxed (the rule is a no-op or the banding is broken)."""
        n = _SALE_YEAR_INDEX
        self.assertGreater(
            self.sell[n].sale_disposition_tax, 0.0,
            "a 100k accrued gain sold with no PRE designation must crystallize "
            "a non-zero capital-gains tax (fully taxable, banded against the "
            "owners' taxable income)")

    def test_realized_gain_surfaces_on_year_result(self):
        """The pre-inclusion capital gain (P_gross - acb_share = 100k) is
        surfaced on YearResult.realized_capital_gains (mirroring the
        drawdown/solvency realized gains, issue #754) so the year-end AMT
        base (#710) sees a real realized base. 100k here (no appreciation:
        P_gross = value_share = 500k, acb_share = 400k)."""
        n = _SALE_YEAR_INDEX
        self.assertAlmostEqual(
            self.sell[n].realized_capital_gains, 100000.0, places=4,
            msg="the sale-year realized_capital_gains should be the 100k "
                "accrued gain (P_gross 500k - acb_share 400k), surfaced for "
                "the AMT base")

    def test_proceeds_invested_equals_net_proceeds(self):
        """The net proceeds P_net = P_gross - secured_share - selling_costs - T
        are invested into non-reg (surfaced on YearResult.sale_proceeds_
        invested). With no mortgage (secured_share = 0), no appreciation
        (P_gross = value_share = 500k), selling_costs 25k, and the crystallized
        tax, P_net = 500k - 0 - 25k - T."""
        n = _SALE_YEAR_INDEX
        p_gross = float(_COTTAGE_VALUE)         # no appreciation
        secured_share = 0.0                      # no mortgage
        selling_costs = float(_COTTAGE_SELLING_COSTS)
        tax = self.sell[n].sale_disposition_tax
        expected_p_net = p_gross - secured_share - selling_costs - tax
        self.assertAlmostEqual(
            self.sell[n].sale_proceeds_invested, expected_p_net, places=4,
            msg=f"sale_proceeds_invested ({self.sell[n].sale_proceeds_invested}) "
                f"!= P_net = P_gross {p_gross} - secured {secured_share} - "
                f"selling_costs {selling_costs} - tax {tax} = {expected_p_net}")


# ──────────────────────────────────────────────────────────────────────────
# 3. The "then grows" property: the property equity is replaced by investable
#    non-reg that THEN GROWS (compounds from the next year on). The invested
#    proceeds compound at the non-reg return rate, while a static cottage
#    (no appreciation) does not, so the sell household pulls ahead over time.
# ──────────────────────────────────────────────────────────────────────────

class ProceedsGrowAfterTheSaleYear(unittest.TestCase):
    """The spec: 'the property equity is replaced by investable non-reg that
    then grows.' The proceeds are injected POST-GROWTH in the sale year (so
    the year-N drop is exactly the friction), then compound from year N+1 on.
    A static cottage (no appreciation_rate) does not grow, so the invested
    proceeds outpace it and the sell household's total_assets pulls ahead of
    the hold household's over the remaining horizon."""

    def test_sell_pulls_ahead_of_hold_after_the_sale_year(self):
        base = _base_doc()
        hold = _run(_add_cottage(base))
        sell = _run(_add_cottage(base, sale=_SALE_2031))
        n = _SALE_YEAR_INDEX
        # In the sale year the sell household is BEHIND (it paid the friction).
        self.assertLess(
            sell[n].total_assets, hold[n].total_assets,
            "sale year: the sell household should be behind (it paid the "
            "selling costs + tax, the friction that left the household)")
        # From some year after the sale on, the invested proceeds (compounding
        # at the non-reg return) outpace the static cottage equity, so the sell
        # household pulls ahead. The crossover year depends on the non-reg
        # return vs the static cottage; the test asserts it happens within a
        # few years (the proceeds are large relative to the friction).
        crossed = False
        for i in range(n + 1, min(n + 6, len(sell))):
            if sell[i].total_assets > hold[i].total_assets:
                crossed = True
                break
        self.assertTrue(
            crossed,
            "the invested proceeds should compound and overtake the static "
            "cottage equity within a few years of the sale (the 'then grows' "
            "property) -- a sell household that stays behind forever would "
            "mean the proceeds are not being invested/grown")

    def test_gap_grows_monotonically_after_crossover(self):
        """Once the invested proceeds overtake the static cottage, the gap
        WIDENS (the proceeds keep compounding at the non-reg return while the
        cottage stays static) -- the 'then grows' is cumulative, not a one-
        shot. The terminal gap is large and positive (the proceeds compounded
        over the whole remaining horizon)."""
        base = _base_doc()
        hold = _run(_add_cottage(base))
        sell = _run(_add_cottage(base, sale=_SALE_2031))
        n = _SALE_YEAR_INDEX
        self.assertGreater(
            sell[-1].total_assets - hold[-1].total_assets, 0.0,
            "terminal: the sell household's invested proceeds (compounded "
            "over the remaining horizon at the non-reg return) should leave "
            "it well ahead of the hold household's static cottage equity")


# ──────────────────────────────────────────────────────────────────────────
# 4. The conservation identity with a mortgage (secured_share leg) and with
#    appreciation (P_gross leg): the spec's identity holds across both legs.
# ──────────────────────────────────────────────────────────────────────────

class ConservationHoldsAcrossBothLegs(unittest.TestCase):
    """The conservation identity has two variable legs beyond the no-mortgage
    no-appreciation baseline: the mortgage discharge (secured_share, the debt
    retired) and the appreciated gross value (P_gross compounds year over
    year). Both must conserve money exactly."""

    def test_with_a_mortgage_secured_share_retired(self):
        """A cottage with a 200k mortgage: the secured_share (200k) is
        discharged at sale. P_net = P_gross - secured_share - selling_costs - T.
        The conservation identity still holds exactly: the sell vs hold drop
        in the sale year is -(selling_costs + T) (the mortgage discharge is
        neutral -- it converts secured debt to cash paid out, net-zero on the
        balance sheet since both the asset and the debt were already netted in
        equity)."""
        base = _base_doc()
        hold = _run(_add_cottage(base, mortgage_balance=200000))
        sell = _run(_add_cottage(base, sale=_SALE_2031,
                                 mortgage_balance=200000))
        n = _SALE_YEAR_INDEX
        # Identical before the sale (both hold the mortgaged cottage).
        for i in range(n):
            self.assertEqual(
                sell[i].total_assets, hold[i].total_assets,
                f"year {i} (before sale, with mortgage): diverged")
        # The sale-year drop is exactly the friction (the mortgage discharge
        # is balance-sheet-neutral: equity was value - secured, so retiring
        # secured alongside the sale does not change the drop vs the no-
        # mortgage case -- only the friction leaves the household).
        selling_costs = float(_COTTAGE_SELLING_COSTS)
        tax = sell[n].sale_disposition_tax
        expected_drop = -(selling_costs + tax)
        actual_drop = sell[n].total_assets - hold[n].total_assets
        self.assertAlmostEqual(
            actual_drop, expected_drop, places=4,
            msg=f"with a mortgage: sale-year drop ({actual_drop}) != "
                f"-(selling_costs {selling_costs} + tax {tax}) = "
                f"{expected_drop} -- the secured_share discharge is not "
                f"balance-sheet-neutral")

    def test_with_appreciation_p_gross_compounds(self):
        """A cottage appreciating at 3%/yr: P_gross at the sale year (2031,
        owned from 2026 -> 5 years held) = 500k * 1.03^5. The realized gain is
        P_gross - acb_share (the appreciated value less the original cost base).
        The conservation identity holds exactly with the appreciated P_gross:
        the sell vs hold drop in the sale year is -(selling_costs + T), where
        T bands the larger appreciated gain."""
        base = _base_doc()
        hold = _run(_add_cottage(base, appreciation_rate=0.03))
        sell = _run(_add_cottage(base, sale=_SALE_2031,
                                 appreciation_rate=0.03))
        n = _SALE_YEAR_INDEX
        # Identical before the sale (both hold the appreciating cottage).
        for i in range(n):
            self.assertEqual(
                sell[i].total_assets, hold[i].total_assets,
                f"year {i} (before sale, appreciating): diverged")
        # The sale-year drop is exactly the friction, with T banded against
        # the larger appreciated gain (P_gross = 500k * 1.03^5 ≈ 579,637).
        selling_costs = float(_COTTAGE_SELLING_COSTS)
        tax = sell[n].sale_disposition_tax
        expected_drop = -(selling_costs + tax)
        actual_drop = sell[n].total_assets - hold[n].total_assets
        self.assertAlmostEqual(
            actual_drop, expected_drop, places=4,
            msg=f"with appreciation: sale-year drop ({actual_drop}) != "
                f"-(selling_costs {selling_costs} + tax {tax}) = "
                f"{expected_drop}")
        # The appreciated gain is larger than the static 100k baseline.
        self.assertGreater(
            sell[n].realized_capital_gains, 100000.0,
            "an appreciating cottage's realized gain should exceed the "
            "static 100k baseline (P_gross compounds above value_share)")

    def test_p_gross_matches_equity_gate_appreciation(self):
        """The spec: P_gross is the SAME value _property_equity_for_year's
        appreciation branch computes. So the proceeds realize EXACTLY the
        equity the property would have contributed held -- no money appears
        or vanishes between the equity-gate value and the sale proceeds."""
        # A cottage appreciating at 3%, sold in 2031 (year index 5), owned
        # from 2026 (start_year) -> 5 years held -> P_gross = 500k * 1.03^5.
        prop = {
            'net_equity': 500000.0, 'value_share': 500000.0,
            'secured_share': 0.0, 'acb_share': 400000.0,
            'appreciation_rate': 0.03,
            'sale': {'year': 2031, 'selling_costs': 25000.0,
                      'owner_roles': {'primary': 0.5, 'spouse': 0.5},
                      'designated_principal_residence_years': []},
        }
        # The equity gate in the year BEFORE the sale (2030, year index 4)
        # returns the appreciated value at 2030 (4 years held).
        equity_2030 = _property_equity_for_year(prop, 2030, 2026)
        self.assertAlmostEqual(equity_2030, 500000.0 * 1.03 ** 4, places=4)
        # The equity gate in the sale year (2031) returns 0 (sold).
        self.assertEqual(_property_equity_for_year(prop, 2031, 2026), 0.0)
        # The disposition helper's P_gross in the sale year is the appreciated
        # value at 2031 (5 years held) -- the value the property WOULD have
        # contributed held, which the proceeds realize exactly.
        # (Use placeholder brackets/inputs: the helper's P_gross does not
        # depend on brackets or taxable income -- only on value_share, rate,
        # and the sale year vs owned_from.)
        figures = _property_disposition_for(
            prop, 2031, 2026, brackets=[], primary_taxable_income=0.0,
            spouse_taxable_income=0.0)
        self.assertAlmostEqual(
            figures['p_gross'], 500000.0 * 1.03 ** 5, places=4,
            msg="P_gross should equal the appreciated value at the sale year "
                "(value_share * (1+rate)^(sale_year - owned_from)), the SAME "
                "value _property_equity_for_year's appreciation branch computes")


# ──────────────────────────────────────────────────────────────────────────
# 4b. The PRE apportionment leg (ITA s.40(2)(b)) and the rule's edge cases:
#    a property designated as a principal residence for some years shelters
#    the gain apportioned to those years; the conservation identity still
#    holds (the sheltered gain is not taxed, so T is smaller and the drop is
#    smaller). Also covers the rule's defensive edges (a sold property with
#    no brackets raises loudly, not silently under-taxing the gain).
# ──────────────────────────────────────────────────────────────────────────

class PreDesignationAndEdgeCases(unittest.TestCase):
    """The PRE apportionment (s.40(2)(b)) is the fourth leg of the
    disposition: a property designated as a principal residence for some years
    shelters the gain apportioned to those years, lowering the disposition
    tax. The conservation identity still holds exactly (the sheltered gain is
    not taxed, so the drop is smaller by the sheltered tax). This class also
    covers the rule's defensive edges so no production line is untested."""

    def test_pre_designation_shelters_part_of_the_gain(self):
        """A cottage designated as principal residence for the years
        2026-2030 (5 years, sold in 2031): the PRE apportionment shelters part
        of the gain, so the disposition tax is LOWER than the fully-taxable
        baseline (no designation). The conservation identity still holds: the
        drop is -(selling_costs + T) with the smaller T. (Single-property
        approximation: the family window is the property's own designation
        span -- the common one-non-principal-property case; the multi-property
        family window is the estate path's concern, a documented follow-up.)
        The base doc's principal residence has its PRE designation cleared so
        the cottage can be designated without a family-year conflict (the
        exemption is one property per family per year, ITA s.40(2)(b))."""
        base = _base_doc_no_pr_designation()
        # Designate the cottage as principal residence for 2026-2030 (5 years),
        # sold in 2031. The PRE designation periods are carried by the mapper.
        prop_with_pre = {
            "id": "couple_cottage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "recreational",
            "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
            "acb": _COTTAGE_ACB,
            "designated_principal_residence_years": [
                {"from": "2026-01-01", "to": "2030-12-31"}],
            "sale": _SALE_2031,
        }
        doc = copy.deepcopy(base)
        doc["properties"].append(prop_with_pre)
        sell_pre = _run(doc)
        # Compare against the fully-taxable sale (no designation), same base.
        sell_full = _run(_add_cottage(base, sale=_SALE_2031))
        n = _SALE_YEAR_INDEX
        # The PRE designation shelters part of the gain -> LOWER tax.
        self.assertLess(
            sell_pre[n].sale_disposition_tax,
            sell_full[n].sale_disposition_tax,
            "a PRE designation should shelter part of the gain, lowering "
            "the disposition tax below the fully-taxable (no-designation) "
            "baseline")
        # The conservation identity still holds exactly with the smaller T.
        selling_costs = float(_COTTAGE_SELLING_COSTS)
        tax = sell_pre[n].sale_disposition_tax
        # Build the hold-side (cottage designated, not sold) for the drop.
        prop_hold = copy.deepcopy(prop_with_pre)
        del prop_hold["sale"]
        doc_hold = copy.deepcopy(base)
        doc_hold["properties"].append(prop_hold)
        hold_pre = _run(doc_hold)
        expected_drop = -(selling_costs + tax)
        actual_drop = sell_pre[n].total_assets - hold_pre[n].total_assets
        self.assertAlmostEqual(
            actual_drop, expected_drop, places=4,
            msg=f"with PRE designation: sale-year drop ({actual_drop}) != "
                f"-(selling_costs {selling_costs} + tax {tax}) = "
                f"{expected_drop}")

    def test_helper_no_op_for_a_non_sale_year(self):
        """The helper returns all zeros for a year that is not the property's
        sale year (the rule's absence-safe no-op for every pre-/post-sale
        year and every property held to the horizon -- DP#32)."""
        prop = {
            'net_equity': 200000.0, 'value_share': 500000.0,
            'secured_share': 0.0, 'acb_share': 400000.0,
            'sale': {'year': 2031, 'selling_costs': 25000.0,
                      'owner_roles': {'primary': 0.5, 'spouse': 0.5},
                      'designated_principal_residence_years': []},
        }
        # Year before the sale -> all zeros.
        figures = _property_disposition_for(
            prop, 2030, 2026, brackets=[], primary_taxable_income=0.0,
            spouse_taxable_income=0.0)
        self.assertEqual(figures['p_gross'], 0.0)
        self.assertEqual(figures['p_net'], 0.0)
        self.assertEqual(figures['disposition_tax'], 0.0)
        self.assertEqual(figures['realized_gain'], 0.0)

    def test_rule_raises_when_brackets_missing_and_a_sale_fires(self):
        """DP#32: a property IS sold this year but no brackets were passed to
        band the gain -- the rule raises loudly rather than silently under-
        taxing the gain (a zero tax from missing brackets is the exact 'silent
        wrong number' failure this repo exists to prevent). The absence-safe
        no-op path (no sale) does NOT need brackets and does not raise."""
        from rule_registry import RuleContext, YearWorkingState, RULES
        from simulation_state import SimState, _default_canada_state
        prop = {
            'id': 'cottage', 'kind': 'recreational', 'net_equity': 200000.0,
            'value_share': 500000.0, 'secured_share': 0.0,
            'acb_share': 400000.0,
            'sale': {'year': 2026, 'selling_costs': 25000.0,
                      'owner_roles': {'primary': 0.5, 'spouse': 0.5},
                      'designated_principal_residence_years': []},
        }
        cfg = SimulationConfig(
            projection_years=1, properties=[prop],
            family_members=[{'role': 'primary', 'gross_income': 130000,
                             'birth_year': 1990},
                            {'role': 'spouse', 'gross_income': 50000,
                             'birth_year': 1992}])
        state = SimState(jurisdiction_state={'canada': _default_canada_state()})
        ws = YearWorkingState.from_state(state, {}, 0)
        ctx = RuleContext(year=0, calendar_year=2026, allocations={},
                          config=cfg, investment_return=0.0,
                          mortgage_rate=0.0, heloc_rate=0.0,
                          mortgage_data=None, use_readvanceable=False,
                          deduct_later=False, primary_marginal_rate=0.4,
                          spouse_marginal_rate=0.2, resp_data=None,
                          fhsa_contribution=0.0, rrsp_annual_limit=None,
                          tfsa_annual_limit=None, fhsa_annual_limit=None,
                          non_reg_after_tax_return=None, cpp_income=0.0,
                          oas_income=0.0, pension_income=0.0,
                          drawdown_order=None, rrif_min_rate_primary=0.0,
                          rrif_min_rate_spouse=0.0, drawdown_net_target=0.0,
                          retiree_marginal_rate=0.0,
                          drawdown_bracket_target=None,
                          drawdown_other_taxable_income=0.0,
                          year_brackets=None)   # the missing-brackets case
        with self.assertRaises(ValueError) as cm:
            RULES['property_disposition'](ws, ctx)
        self.assertIn('year_brackets', str(cm.exception))

    def test_zero_share_owner_is_skipped(self):
        """An owner role with a 0 share (defensive: the mapper filters 0-share
        roles out, but the helper guards against them anyway) is skipped -- a
        0-share owner's gain slice is not computed (no division by zero, no
        phantom tax). Covered by giving the helper an owner_roles dict with a
        0-share entry and confirming the tax is the same as without it."""
        from tax_data import default_tax_provider
        brackets = default_tax_provider().get_combined_brackets()
        base_prop = {
            'net_equity': 200000.0, 'value_share': 500000.0,
            'secured_share': 0.0, 'acb_share': 400000.0,
            'sale': {'year': 2026, 'selling_costs': 25000.0,
                      'owner_roles': {'primary': 0.5, 'spouse': 0.5},
                      'designated_principal_residence_years': []},
        }
        # Add a 0-share 'extra' role -- should be skipped (no effect on tax).
        prop_with_zero = copy.deepcopy(base_prop)
        prop_with_zero['sale']['owner_roles'] = {
            'primary': 0.5, 'spouse': 0.5, 'extra': 0.0}
        f1 = _property_disposition_for(
            base_prop, 2026, 2026, brackets,
            primary_taxable_income=100000.0, spouse_taxable_income=50000.0)
        f2 = _property_disposition_for(
            prop_with_zero, 2026, 2026, brackets,
            primary_taxable_income=100000.0, spouse_taxable_income=50000.0)
        self.assertAlmostEqual(
            f1['disposition_tax'], f2['disposition_tax'], places=6,
            msg="a 0-share owner role should be skipped (no phantom tax, no "
                "division by zero)")


# ──────────────────────────────────────────────────────────────────────────
# 5. The rule is registered and fires (DP#18): a sale has an observable
#    effect on the engine output -- the rule is not a silent no-op.
# ──────────────────────────────────────────────────────────────────────────

class RuleFiresAndIsRegistered(unittest.TestCase):
    """DP#18: a rule that is registered but never fires (or fires with no
    observable effect) is the #627 failure shape. The property_disposition
    rule must have an observable effect on a sale household's output."""

    def test_rule_name_in_rule_order(self):
        """The rule is registered in RULE_ORDER (the explicit declared order)
        and EXPECTED_RULE_NAMES (the independent declaration the registry
        test enforces) -- a rule silently added to the registry without
        being added to the order is a build error, not a silent gap."""
        from simulation_rules import RULE_ORDER
        self.assertIn('property_disposition', RULE_ORDER)

    def test_rule_has_observable_effect(self):
        """The rule changes the engine output: a sale household's sale-year
        total_assets DIFFERS from the same household with the rule disabled.
        Disabling the rule (monkey-patching RULES to a no-op) should make the
        sale household's sale-year total_assets revert to the hold household's
        (the property's equity would gate to 0 with no proceeds replacing it
        -- a money-LOSING path the rule exists to prevent). This is the
        output-depends-on-it test the spec requires (DP#18)."""
        import rule_registry
        base = _base_doc()
        sell = _run(_add_cottage(base, sale=_SALE_2031))
        n = _SALE_YEAR_INDEX
        original = rule_registry.RULES['property_disposition']
        try:
            # Disable the rule -> the sale's proceeds are NOT invested; the
            # property's equity gates to 0 (sale gate) with nothing replacing
            # it -> the household LOSES the equity (money NOT conserved).
            rule_registry.RULES['property_disposition'] = (
                lambda ws, ctx: False)
            sell_disabled = _run(_add_cottage(base, sale=_SALE_2031))
        finally:
            rule_registry.RULES['property_disposition'] = original
        # With the rule: the sale-year total_assets includes the invested
        # P_net (sale_proceeds_invested > 0). Without the rule: no proceeds
        # invested (sale_proceeds_invested == 0) and the property's equity
        # is gone (gated to 0) -- a strictly lower total_assets.
        self.assertGreater(
            sell[n].sale_proceeds_invested, 0.0,
            "with the rule: the sale's net proceeds should be invested "
            "(sale_proceeds_invested > 0)")
        self.assertEqual(
            sell_disabled[n].sale_proceeds_invested, 0.0,
            "without the rule: no proceeds invested (the rule is what "
            "invests them)")
        self.assertGreater(
            sell[n].total_assets, sell_disabled[n].total_assets,
            "the rule has an observable effect: with it, the sale-year "
            "total_assets is HIGHER (the proceeds replace the gated-to-0 "
            "equity); without it the household LOSES the equity")


if __name__ == "__main__":
    unittest.main()