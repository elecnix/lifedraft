#!/usr/bin/env python3
"""Issue #956 bite E (principal-residence disposition): a declared mid-horizon
SALE of the PRINCIPAL residence settles in its sale year -- the home + its
mortgage + any HELOC/SM secured against it leave the balance sheet, the net
proceeds are invested into the portfolio, and money is conserved.

This is the correctness-critical layer built ON TOP of the mechanical
foundation (the schema ``sale`` leaf on a ``kind="principal"`` property -- the
SAME ``property_sale`` block Bite B defines, reused verbatim -- the
``contract_principal._map_principal_sale`` mapper carrying the sale onto
``cfg['property']['principal_sale']``, and the
``SimulationConfig.principal_sale`` field). The crux this file verifies is the
CONSERVATION IDENTITY (the spec's gate), adapted from Bite B for the principal
residence's DIFFERENT balance-sheet position.

## Why the principal's identity is on net_assets, not total_assets

The principal residence is deliberately excluded from ``config.properties``
(``_map_owned_properties`` skips the principal): its value reaches the annual
side via ``house_value`` / LTV / charge math, NOT via ``property_equities`` in
``total_assets``. So the principal's gross value is OFF the balance sheet --
only its mortgage/HELOC appear as DEBT. This is the existing model (see
``SimState.total_assets``'s docstring: the home's value is not in there).

Bite B's non-principal identity ``Δtotal_assets = -(selling_costs + T)`` works
because the non-principal's equity IS in ``total_assets`` (selling converts
equity -> cash, minus friction, net-zero on the balance sheet). For the
principal, the value was NEVER in ``total_assets``, so selling ADDS the gross
value (as proceeds) and REMOVES the debt (discharged), net of friction. The
honest conservation identity is therefore on NET_ASSETS:

  V              = value_share (the principal's gross value at the couple's
                  share; the engine holds house_value FIXED -- no appreciation
                  model for the principal this bite);
  discharged_debt= mortgage_balance + heloc_balance + sm_heloc at the sale
                  year (the LIVE year-N balances, read off YearWorkingState --
                  the principal's mortgage AMORTIZES, so a config-time
                  snapshot would under-state the retired debt and break the
                  identity);
  T              = capital-gains tax on (value_share - acb_share), apportioned
                  by the PRE taxable_fraction (ITA s.40(2)(b)), banded against
                  each owner's taxable income;
  P_net          = V - discharged_debt - selling_costs - T;
  Δtotal_assets  (sell vs hold, sale year) = P_net
                  (the proceeds invested -- the hold household has 0 proceeds);
  Δtotal_debt    (sell vs hold, sale year) = -discharged_debt
                  (the hold household keeps the debt; the sell household
                  discharges it);
  Δnet_assets    (sell vs hold, sale year) = P_net + discharged_debt
                  = V - selling_costs - T.

Money is conserved: the off-balance-sheet home converts to on-balance-sheet
assets (proceeds + debt retired), less the friction that genuinely left the
household (third-party selling costs + government tax). For a
fully-PRE-designated principal (the common case), T ≈ 0, so
Δnet_assets ≈ V - selling_costs.

These tests run the real engine (``FamilySimulation.run``), so the
money-conservation invariant suite (``trajectory_invariants.assert_run_
invariants``, wired into ``run()``) is enforced automatically on every run
here -- a sale whose flows did not balance would raise ``InvariantBreachedError``
before any assertion below. All fixtures use fabricated ids and round numbers
(DP#4/DP#15); no real figure, name, or account enters the repo (DP#15).

## Rent as an ongoing living cost (P1 scope note)

The sell-and-rent case sells the principal and invests the full equity; the
household then RENTS going forward. This bite does NOT invent a new rent
field: the contract already has a dated living-costs mechanism
(``household_budget.expense_segments`` -- #760), so a post-sale rent is
DECLARED by the user as a dated living-cost segment starting in the sale year.
This bite's scope is the disposition (the home leaves, the proceeds enter the
portfolio); the ongoing rent is a living-cost the user declares separately.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from rules_disposition import _principal_disposition_for

from test_input_contract import _load_example, _two_generation_subset
import contract_errors
import contract_schema


# ──────────────────────────────────────────────────────────────────────────
# Fixtures: the shipped example's principal residence (joint p1/p2, value
# 650k, mortgage + HELOC secured against it), with an optional dated SALE.
# ──────────────────────────────────────────────────────────────────────────

# The shipped example projects 50 years from start_year 2026, so results[i]
# is calendar year 2026 + i. A sale dated 2031 fires in results[5]; results[0..4]
# are strictly before it. Round numbers (DP#4/DP#15): the couple (p1/p2) jointly
# owns the principal residence; the example's mortgage (340k) + HELOC are
# secured against it.
_SALE_2031 = {"year": 2031, "selling_costs": 30000}
_SALE_YEAR_INDEX = 5
_PRINCIPAL_VALUE = 650_000      # the shipped example's principal residence value
_PRINCIPAL_SELLING_COSTS = 30_000


def _base_doc():
    """The shipped two-generation example, validated (the sub-family the
    adapter can honestly map onto the two-adults-plus-children engine)."""
    doc = _two_generation_subset(_load_example())
    contract_schema.validate_contract(doc)
    return doc


def _add_principal_sale(doc, sale):
    """Add a ``sale`` block to the principal residence (the kind=principal
    property). Reuses the SAME ``property_sale`` schema block Bite B defines
    -- the principal is a property in ``doc["properties"]``, and the schema
    permits ``sale`` on any property; the mapper carries it onto
    ``cfg['property']['principal_sale']`` (the principal's own seam, distinct
    from the non-principal ``properties[]`` path)."""
    doc = copy.deepcopy(doc)
    for prop in doc["properties"]:
        if prop["kind"] == "principal":
            prop["sale"] = sale
            break
    return doc


def _run(doc):
    """Validate -> map to internal config -> run the real engine (which
    enforces the money-conservation invariant suite on every run)."""
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


# ──────────────────────────────────────────────────────────────────────────
# 1. Absence-safe (DP#32): a household with no principal sale is byte-identical
#    to one that never declared the sale -- the rule is a strict no-op and the
#    golden invariant is unchanged by construction.
# ──────────────────────────────────────────────────────────────────────────

class SaleIsAbsenceSafe(unittest.TestCase):
    """A principal held to the horizon (no ``sale``) is byte-identical whether
    or not the principal_disposition rule exists -- the rule is a no-op for a
    principal with no sale, so the conservation identity's absence path is the
    pre-bite behaviour exactly (DP#32). The golden fixture (no principal sale)
    is covered by its own byte-exact invariant elsewhere (the golden
    household builds SimulationConfig.from_dict straight from a legacy dict
    that never carries ``principal_sale``)."""

    def test_hold_trajectory_unchanged_by_rule(self):
        """The hold household's trajectory is byte-identical to the pre-bite
        engine: the rule never fires (no sale declared), so every year's
        total_assets / total_debt is unchanged."""
        hold = _run(_base_doc())
        # The principal_disposition rule never fires -> the principal_sale_*
        # surfaced fields stay at their seeded 0.0 defaults every year.
        for i, r in enumerate(hold):
            self.assertEqual(r.principal_sale_proceeds_invested, 0.0,
                             f"year {i}: principal_sale_proceeds_invested "
                             f"nonzero for a household with no principal sale")
            self.assertEqual(r.principal_sale_disposition_tax, 0.0,
                             f"year {i}: principal_sale_disposition_tax "
                             f"nonzero for a household with no principal sale")
            self.assertEqual(r.principal_sale_discharged_debt, 0.0,
                             f"year {i}: principal_sale_discharged_debt "
                             f"nonzero for a household with no principal sale")

    def test_no_principal_sale_means_config_field_is_none(self):
        """A household with no principal sale maps to ``principal_sale=None``
        on the SimulationConfig (the absence-safe fast path -- DP#32)."""
        cfg = SimulationConfig.from_dict(ic.to_internal_config(_base_doc()))
        self.assertIsNone(cfg.principal_sale,
                           "a household with no principal sale must map to "
                           "principal_sale=None (the rule's no-op fast path)")


# ──────────────────────────────────────────────────────────────────────────
# 2. The conservation identity (the crux): in the sale year, the sell
#    household's NET_ASSETS rises by EXACTLY value_share - selling_costs - tax
#    relative to the same household that holds -- and the two are byte-
#    identical before the sale.
# ──────────────────────────────────────────────────────────────────────────

class ConservationIdentityHoldsExactly(unittest.TestCase):
    """The spec's gate (adapted for the principal's off-balance-sheet value):
    Δnet_assets (sell vs hold, sale year) = V - selling_costs - T. Money is
    conserved -- the off-balance-sheet home converts to on-balance-sheet
    assets (proceeds + debt retired), less the friction that genuinely left
    the household. For the shipped example's principal (fully PRE-designated,
    ACB=value -> no accrued gain), T = 0, so Δnet_assets = V - selling_costs."""

    def setUp(self):
        self.base = _base_doc()
        self.hold = _run(self.base)
        self.sell = _run(_add_principal_sale(self.base, _SALE_2031))

    def test_identical_before_the_sale_year(self):
        """The sell and hold households are byte-identical in every year
        before the sale (the principal hasn't been sold yet, so both hold it
        and both have the same mortgage/HELOC/portfolio). This is the baseline
        the conservation identity measures the sale-year change against."""
        for i in range(_SALE_YEAR_INDEX):
            self.assertEqual(
                self.sell[i].total_assets, self.hold[i].total_assets,
                f"year index {i} (before sale): sell and hold total_assets "
                f"diverged ({self.sell[i].total_assets} vs "
                f"{self.hold[i].total_assets})")
            self.assertEqual(
                self.sell[i].total_debt, self.hold[i].total_debt,
                f"year index {i} (before sale): sell and hold total_debt "
                f"diverged ({self.sell[i].total_debt} vs "
                f"{self.hold[i].total_debt})")

    def test_sale_year_net_assets_rise_is_exactly_value_less_friction(self):
        """The crux: in the sale year, the sell household's NET_ASSETS is
        HIGHER than the hold household's by EXACTLY value_share - selling_costs
        - tax (the home's gross value realized, less the friction). The
        principal's value was OFF the balance sheet (not in total_assets), so
        selling ADDS it (as proceeds + debt retired), less friction. With the
        shipped example's fully-PRE-designated principal (tax = 0) and ACB =
        value (no accrued gain), T = 0, so Δnet_assets = V - selling_costs
        = 650k - 30k = 620k."""
        n = _SALE_YEAR_INDEX
        selling_costs = float(_PRINCIPAL_SELLING_COSTS)
        tax = self.sell[n].principal_sale_disposition_tax
        V = float(_PRINCIPAL_VALUE)   # couple's 100% share (joint p1/p2)
        expected_rise = V - selling_costs - tax
        sell_net = self.sell[n].total_assets - self.sell[n].total_debt
        hold_net = self.hold[n].total_assets - self.hold[n].total_debt
        actual_rise = sell_net - hold_net
        self.assertAlmostEqual(
            actual_rise, expected_rise, places=2,
            msg=f"sale-year net_assets rise ({actual_rise}) is not exactly "
                f"V {V} - selling_costs {selling_costs} - tax {tax} "
                f"= {expected_rise} -- money is not conserved")

    def test_sale_year_total_assets_rise_is_net_proceeds(self):
        """The total_assets leg: the sell household's total_assets rises by
        EXACTLY P_net (the net proceeds invested into non-reg), since the hold
        household has 0 proceeds. P_net = V - discharged_debt - selling_costs
        - T."""
        n = _SALE_YEAR_INDEX
        p_net = self.sell[n].principal_sale_proceeds_invested
        actual_ta_rise = self.sell[n].total_assets - self.hold[n].total_assets
        self.assertAlmostEqual(
            actual_ta_rise, p_net, places=2,
            msg=f"sale-year total_assets rise ({actual_ta_rise}) != P_net "
                f"{p_net} (the net proceeds invested into non-reg)")

    def test_sale_year_total_debt_drop_is_discharged_debt(self):
        """The total_debt leg: the sell household's total_debt DROPS by EXACTLY
        the discharged secured debt (mortgage + HELOC + SM-HELOC at the sale
        year), since the hold household keeps it."""
        n = _SALE_YEAR_INDEX
        discharged = self.sell[n].principal_sale_discharged_debt
        actual_td_drop = self.hold[n].total_debt - self.sell[n].total_debt
        self.assertAlmostEqual(
            actual_td_drop, discharged, places=2,
            msg=f"sale-year total_debt drop ({actual_td_drop}) != "
                f"discharged_debt {discharged} (the secured debt retired at "
                f"the sale)")

    def test_proceeds_plus_discharged_equals_value_less_friction(self):
        """The two legs sum to the home's gross value less friction:
        P_net + discharged_debt = (V - discharged - sc - T) + discharged
        = V - selling_costs - T -- the identity the net_assets check asserts,
        decomposed into its two observable legs."""
        n = _SALE_YEAR_INDEX
        p_net = self.sell[n].principal_sale_proceeds_invested
        discharged = self.sell[n].principal_sale_discharged_debt
        tax = self.sell[n].principal_sale_disposition_tax
        selling_costs = float(_PRINCIPAL_SELLING_COSTS)
        V = float(_PRINCIPAL_VALUE)
        self.assertAlmostEqual(
            p_net + discharged, V - selling_costs - tax, places=2,
            msg=f"P_net {p_net} + discharged {discharged} != V {V} - "
                f"selling_costs {selling_costs} - tax {tax}")

    def test_disposition_tax_is_zero_for_fully_pre_designated_principal(self):
        """The shipped example's principal is designated from 2023 onward
        (a ``to: null`` period) -> fully PRE-exempt (ITA s.40(2)(b)) -> the
        disposition tax is 0. A non-zero tax here would mean the PRE
        apportionment is not being applied (the rule is a no-op or the
        designation is not carried)."""
        n = _SALE_YEAR_INDEX
        self.assertEqual(
            self.sell[n].principal_sale_disposition_tax, 0.0,
            "a fully-PRE-designated principal (designated 2023 onward) must "
            "crystallize 0 capital-gains tax (the gain is fully sheltered by "
            "the principal-residence exemption, ITA s.40(2)(b))")

    def test_secured_debt_zero_from_sale_year_on(self):
        """The home + its mortgage + any HELOC/SM secured against it leave the
        balance sheet from the sale year on: mortgage_balance and
        heloc_balance are 0 in the sale year AND every subsequent year (the
        amortization schedule's scheduled end_balance does not resurrect a
        paid-off mortgage -- the rule force-zeros it every post-sale year)."""
        for i in range(_SALE_YEAR_INDEX, len(self.sell)):
            self.assertEqual(
                self.sell[i].mortgage_balance, 0.0,
                f"year {i} (sale year and after): mortgage_balance should be "
                f"0 (the principal's mortgage is discharged at the sale)")
            self.assertEqual(
                self.sell[i].heloc_balance, 0.0,
                f"year {i} (sale year and after): heloc_balance should be 0 "
                f"(any HELOC/SM secured against the principal is discharged)")

    def test_realized_gain_surfaces_on_year_result(self):
        """The pre-inclusion capital gain (value_share - acb_share) is
        surfaced on YearResult.realized_capital_gains so the year-end AMT base
        (#710) sees a real realized base. The shipped example's principal has
        ACB = null -> value_share (bought at value) -> gain = 0 (no accrued
        gain), so realized_capital_gains from the sale is 0 (the
        principal_sale_realized_gain leg)."""
        n = _SALE_YEAR_INDEX
        # principal_sale_realized_gain is the gain leg (0 for a bought-at-
        # value principal). Surfaced separately AND folded into
        # realized_capital_gains for the AMT base.
        self.assertEqual(
            self.sell[n].principal_sale_realized_gain, 0.0,
            "the shipped example's principal (ACB=value) has no accrued "
            "gain -> principal_sale_realized_gain is 0")


# ──────────────────────────────────────────────────────────────────────────
# 3. The "then grows" property: the net proceeds invested into non-reg THEN
#    GROW (compound from the next year on). The sell household's net_assets
#    pulls ahead of the hold household's over the remaining horizon (the
#    invested proceeds compound, while the hold household keeps amortizing
#    its mortgage).
# ──────────────────────────────────────────────────────────────────────────

class ProceedsGrowAfterTheSaleYear(unittest.TestCase):
    """The spec: the net proceeds are 'investable non-reg that then grows.'
    The proceeds are injected POST-GROWTH in the sale year (so the year-N
    identity holds exactly), then compound from year N+1 on. The sell
    household's net_assets pulls ahead of the hold household's over the
    remaining horizon (the proceeds compound at the non-reg return, while the
    hold household keeps paying mortgage interest)."""

    def test_sell_net_assets_pulls_ahead_after_the_sale_year(self):
        base = _base_doc()
        hold = _run(base)
        sell = _run(_add_principal_sale(base, _SALE_2031))
        n = _SALE_YEAR_INDEX
        # In the sale year the sell household is AHEAD (it realized the home's
        # value minus friction; the hold household still carries the debt and
        # the off-sheet home).
        sell_net_n = sell[n].total_assets - sell[n].total_debt
        hold_net_n = hold[n].total_assets - hold[n].total_debt
        self.assertGreater(
            sell_net_n, hold_net_n,
            "sale year: the sell household's net_assets should be AHEAD (it "
            "realized the home's value less friction; the hold household "
            "still carries the mortgage debt and the off-sheet home)")
        # The gap WIDENS over the remaining horizon (the proceeds compound at
        # the non-reg return while the hold household keeps paying mortgage
        # interest). The terminal gap is larger than the sale-year gap.
        sell_net_terminal = sell[-1].total_assets - sell[-1].total_debt
        hold_net_terminal = hold[-1].total_assets - hold[-1].total_debt
        gap_n = sell_net_n - hold_net_n
        gap_terminal = sell_net_terminal - hold_net_terminal
        self.assertGreater(
            gap_terminal, gap_n,
            "terminal: the sell household's net-assets advantage should "
            "WIDEN over the horizon (the invested proceeds compound while "
            "the hold household keeps amortizing its mortgage) -- a gap that "
            "shrinks would mean the proceeds are not being invested/grown")


# ──────────────────────────────────────────────────────────────────────────
# 4. The conservation identity with no selling costs (selling_costs = 0) and
#    with a partial-PRE principal (tax > 0): the spec's identity holds across
#    both legs, and the PRE apportionment prices a non-zero tax for a
#    partially-designated principal.
# ──────────────────────────────────────────────────────────────────────────

class ConservationHoldsAcrossBothLegs(unittest.TestCase):
    """The conservation identity has two variable legs beyond the baseline
    (selling_costs and the PRE-apportioned tax). Both must conserve money
    exactly. The PRE apportionment (ITA s.40(2)(b)) is the fourth leg: a
    principal designated for only SOME years shelters part of the gain,
    lowering the disposition tax below the fully-taxable baseline."""

    def test_with_zero_selling_costs(self):
        """A sale with selling_costs = 0 (a real declarable value -- DP#32:
        null `selling_costs` is a real $0, not an unknown): the conservation
        identity holds with the full V realized (no friction). The
        `selling_costs` leaf is optional in the schema; omitting it is a $0
        cost (the mapper reads null as 0.0 explicitly, never `x or DEFAULT`)."""
        base = _base_doc()
        hold = _run(base)
        sale = {"year": 2031, "selling_costs": 0}
        sell = _run(_add_principal_sale(base, sale))
        n = _SALE_YEAR_INDEX
        # No selling costs -> the full V is realized (less tax, which is 0 for
        # the fully-PRE-designated principal).
        V = float(_PRINCIPAL_VALUE)
        tax = sell[n].principal_sale_disposition_tax
        expected_rise = V - 0.0 - tax
        sell_net = sell[n].total_assets - sell[n].total_debt
        hold_net = hold[n].total_assets - hold[n].total_debt
        actual_rise = sell_net - hold_net
        self.assertAlmostEqual(
            actual_rise, expected_rise, places=2,
            msg=f"with selling_costs=0: sale-year net_assets rise "
                f"({actual_rise}) != V {V} - 0 - tax {tax} = {expected_rise}")

    def test_with_null_selling_costs_is_zero(self):
        """A sale with no `selling_costs` key (the schema's optional leaf) is
        a real $0 in disposition costs, distinct from a sale that has not been
        priced (DP#32). The mapper reads null/absent as 0.0 explicitly."""
        base = _base_doc()
        hold = _run(base)
        sale = {"year": 2031}   # no selling_costs key -> $0
        sell = _run(_add_principal_sale(base, sale))
        n = _SALE_YEAR_INDEX
        V = float(_PRINCIPAL_VALUE)
        tax = sell[n].principal_sale_disposition_tax
        expected_rise = V - 0.0 - tax
        sell_net = sell[n].total_assets - sell[n].total_debt
        hold_net = hold[n].total_assets - hold[n].total_debt
        actual_rise = sell_net - hold_net
        self.assertAlmostEqual(
            actual_rise, expected_rise, places=2,
            msg=f"with no selling_costs key: sale-year net_assets rise "
                f"({actual_rise}) != V {V} - 0 - tax {tax} = {expected_rise}")

    def test_partial_pre_designation_crystallizes_nonzero_tax(self):
        """A principal designated for only SOME years (not its whole ownership)
        shelters part of the gain, crystallizing a NON-ZERO disposition tax
        (the PRE apportionment prices a taxable_fraction > 0). The conservation
        identity still holds: Δnet_assets = V - selling_costs - T with the
        non-zero T. Built with a NON-CONTIGUOUS designation (2026 and 2030, but
        not 2027-2029) -> count=2, window=5 (the span 2026..2030) -> the "+1"
        bonus year exempts 3/5 of the gain -> taxable_fraction = 0.4 -> a
        non-zero tax on a 250k gain. The single-property approximation the
        disposition helper uses (window = the property's OWN designation span)
        means a CONTIGUOUS designation covering its own span is always fully
        exempt (count=N, window=N -> exempt = min(1, (N+1)/N) = 1); a
        non-contiguous designation is the way to exercise the partial leg
        here (the multi-property family window is the estate path's concern,
        a documented follow-up)."""
        base = _base_doc()
        # Give the principal an accrued gain: ACB < value (bought below the
        # current value). And a NON-CONTIGUOUS PRE designation (2026 and 2030,
        # not the years between) -> partly sheltered, partly taxed.
        doc = copy.deepcopy(base)
        for prop in doc["properties"]:
            if prop["kind"] == "principal":
                prop["acb"] = 400_000   # bought at 400k, now worth 650k -> 250k gain
                prop["designated_principal_residence_years"] = [
                    {"from": "2026-01-01", "to": "2026-12-31"},
                    {"from": "2030-01-01", "to": "2030-12-31"}]
                prop["sale"] = {"year": 2031, "selling_costs": 30_000}
                break
        sell = _run(doc)
        n = _SALE_YEAR_INDEX
        # The partial designation (count=2, window=5 -> taxable_fraction 0.4)
        # + the 250k accrued gain -> a NON-ZERO tax.
        self.assertGreater(
            sell[n].principal_sale_disposition_tax, 0.0,
            "a principal with a 250k accrued gain and a non-contiguous PRE "
            "designation (2026 + 2030, not 2027-2029 -> count=2, window=5 -> "
            "taxable_fraction 0.4) must crystallize a NON-ZERO capital-gains "
            "tax (the gain is partly sheltered, partly taxed)")
        # The realized gain is the full 250k (pre-inclusion, pre-PRE).
        self.assertAlmostEqual(
            sell[n].principal_sale_realized_gain, 250_000.0, places=2,
            msg="the realized gain should be value_share - acb_share = "
                "650k - 400k = 250k (pre-inclusion, pre-PRE apportionment)")
        # Conservation still holds: Δnet_assets = V - selling_costs - T. Build
        # the hold-side (same partial designation, no sale) for the rise.
        doc_hold = copy.deepcopy(base)
        for prop in doc_hold["properties"]:
            if prop["kind"] == "principal":
                prop["acb"] = 400_000
                prop["designated_principal_residence_years"] = [
                    {"from": "2026-01-01", "to": "2026-12-31"},
                    {"from": "2030-01-01", "to": "2030-12-31"}]
                break
        hold = _run(doc_hold)
        V = float(_PRINCIPAL_VALUE)
        selling_costs = 30_000.0
        tax = sell[n].principal_sale_disposition_tax
        expected_rise = V - selling_costs - tax
        sell_net = sell[n].total_assets - sell[n].total_debt
        hold_net = hold[n].total_assets - hold[n].total_debt
        actual_rise = sell_net - hold_net
        self.assertAlmostEqual(
            actual_rise, expected_rise, places=2,
            msg=f"with partial PRE: sale-year net_assets rise ({actual_rise}) "
                f"!= V {V} - selling_costs {selling_costs} - tax {tax} "
                f"= {expected_rise}")

    def test_helper_no_op_for_a_pre_sale_year(self):
        """The helper returns all zeros for a year BEFORE the property's sale
        year (the rule's absence-safe no-op for every pre-sale year -- DP#32)."""
        sale = {
            'year': 2031, 'selling_costs': 30_000.0,
            'owner_roles': {'primary': 0.5, 'spouse': 0.5},
            'designated_principal_residence_years': [
                {'from': '2023-01-01', 'to': None}],
            'value_share': 650_000.0, 'acb_share': 650_000.0,
        }
        # Year before the sale -> all zeros (pre-sale: home held, nothing
        # discharged, no proceeds).
        figures = _principal_disposition_for(
            sale, 2030, mortgage_balance=300_000.0, heloc_balance=50_000.0,
            sm_heloc=0.0, brackets=[], primary_taxable_income=0.0,
            spouse_taxable_income=0.0)
        self.assertEqual(figures['discharged_debt'], 0.0)
        self.assertEqual(figures['disposition_tax'], 0.0)
        self.assertEqual(figures['realized_gain'], 0.0)
        self.assertEqual(figures['p_net'], 0.0)

    def test_helper_no_designation_is_fully_taxable(self):
        """A principal with NO PRE designation (``designated_principal_
        residence_years == []``) is FULLY taxable (taxable_fraction = 1.0) --
        the helper's ``designated_count <= 0`` branch. With an accrued gain
        (value_share > acb_share) and no designation, the disposition tax is
        the full 50%-included gain banded against the owners' taxable income
        (a non-zero tax, distinct from the fully-PRE-designated 0)."""
        from tax_data import default_tax_provider
        brackets = default_tax_provider().get_combined_brackets()
        sale = {
            'year': 2026, 'selling_costs': 30_000.0,
            'owner_roles': {'primary': 0.5, 'spouse': 0.5},
            'designated_principal_residence_years': [],   # no designation
            'value_share': 650_000.0, 'acb_share': 400_000.0,  # 250k gain
        }
        figures = _principal_disposition_for(
            sale, 2026, mortgage_balance=300_000.0, heloc_balance=50_000.0,
            sm_heloc=0.0, brackets=brackets,
            primary_taxable_income=130_000.0, spouse_taxable_income=50_000.0)
        # No designation -> fully taxable -> non-zero tax on the 250k gain.
        self.assertGreater(
            figures['disposition_tax'], 0.0,
            "a principal with no PRE designation and a 250k gain must "
            "crystallize a non-zero tax (taxable_fraction = 1.0, the gain "
            "is fully taxable, banded against the owners' income)")
        # P_net = V - discharged - selling_costs - T.
        V = 650_000.0
        discharged = 300_000.0 + 50_000.0
        self.assertAlmostEqual(
            figures['p_net'],
            V - discharged - 30_000.0 - figures['disposition_tax'], places=2)

    def test_helper_zero_share_owner_is_skipped(self):
        """An owner role with a 0 share (defensive: the mapper filters 0-share
        roles out, but the helper guards against them anyway) is skipped -- a
        0-share owner's gain slice is not computed (no division by zero, no
        phantom tax). Mirrors Bite B's ``test_zero_share_owner_is_skipped``."""
        from tax_data import default_tax_provider
        brackets = default_tax_provider().get_combined_brackets()
        base_sale = {
            'year': 2026, 'selling_costs': 30_000.0,
            'owner_roles': {'primary': 0.5, 'spouse': 0.5},
            'designated_principal_residence_years': [],
            'value_share': 650_000.0, 'acb_share': 400_000.0,
        }
        # Add a 0-share 'extra' role -- should be skipped (no effect on tax).
        sale_with_zero = copy.deepcopy(base_sale)
        sale_with_zero['owner_roles'] = {
            'primary': 0.5, 'spouse': 0.5, 'extra': 0.0}
        f1 = _principal_disposition_for(
            base_sale, 2026, mortgage_balance=300_000.0, heloc_balance=0.0,
            sm_heloc=0.0, brackets=brackets,
            primary_taxable_income=130_000.0, spouse_taxable_income=50_000.0)
        f2 = _principal_disposition_for(
            sale_with_zero, 2026, mortgage_balance=300_000.0, heloc_balance=0.0,
            sm_heloc=0.0, brackets=brackets,
            primary_taxable_income=130_000.0, spouse_taxable_income=50_000.0)
        self.assertAlmostEqual(
            f1['disposition_tax'], f2['disposition_tax'], places=6,
            msg="a 0-share owner role should be skipped (no phantom tax, no "
                "division by zero)")

    def test_disposition_gain_tax_zero_couple_share_is_zero_tax(self):
        """The shared gain-banding spine ``_disposition_gain_tax`` returns 0
        when the owner_roles sum to 0 (no taxable owner -- a defensive guard
        the mapper never produces, but the helper must handle without dividing
        by zero). A sale with empty owner_roles -> couple_share = 0 -> 0 tax,
        regardless of the gain."""
        from rules_disposition import _disposition_gain_tax
        sale = {
            'year': 2026, 'selling_costs': 30_000.0,
            'owner_roles': {},   # no owners -> couple_share = 0
            'designated_principal_residence_years': [],
        }
        tax = _disposition_gain_tax(
            250_000.0, sale, 2026, brackets=[],
            primary_taxable_income=130_000.0, spouse_taxable_income=50_000.0)
        self.assertEqual(
            tax, 0.0,
            "a sale with no taxable owners (couple_share = 0) must return 0 "
            "tax (no division by zero, no phantom tax)")

    def test_helper_post_sale_year_zeroes_debt_no_proceeds(self):
        """The helper returns discharged_debt = (live balances, which are 0
        post-sale since the rule zeroed them last year) and no proceeds/tax/
        gain for a year AFTER the sale year (the sale settled once, in its
        sale year; post-sale years only re-zero the debt so the amortization
        schedule's scheduled balance does not resurrect a paid-off mortgage)."""
        sale = {
            'year': 2031, 'selling_costs': 30_000.0,
            'owner_roles': {'primary': 0.5, 'spouse': 0.5},
            'designated_principal_residence_years': [
                {'from': '2023-01-01', 'to': None}],
            'value_share': 650_000.0, 'acb_share': 650_000.0,
        }
        # A post-sale year: the live balances are 0 (the rule zeroed them
        # last year and they carried forward at 0). No proceeds/tax/gain (the
        # sale settled once, in its sale year).
        figures = _principal_disposition_for(
            sale, 2032, mortgage_balance=0.0, heloc_balance=0.0, sm_heloc=0.0,
            brackets=[], primary_taxable_income=0.0,
            spouse_taxable_income=0.0)
        self.assertEqual(figures['discharged_debt'], 0.0)
        self.assertEqual(figures['p_net'], 0.0)
        self.assertEqual(figures['disposition_tax'], 0.0)
        self.assertEqual(figures['realized_gain'], 0.0)

    def test_rule_raises_when_brackets_missing_and_a_sale_fires(self):
        """DP#32: the principal IS sold this year but no brackets were passed
        to band the gain -- the rule raises loudly rather than silently
        under-taxing the gain. The no-sale / pre-sale / post-sale paths do NOT
        need brackets and do not raise."""
        from rule_registry import RuleContext, YearWorkingState, RULES
        from simulation_state import SimState, _default_canada_state
        sale = {
            'year': 2026, 'selling_costs': 30_000.0,
            'owner_roles': {'primary': 0.5, 'spouse': 0.5},
            'designated_principal_residence_years': [],
            'value_share': 650_000.0, 'acb_share': 400_000.0,  # a 250k gain
        }
        cfg = SimulationConfig(
            projection_years=1, house_value=650_000, principal_sale=sale,
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
            RULES['principal_disposition'](ws, ctx)
        self.assertIn('year_brackets', str(cm.exception))


# ──────────────────────────────────────────────────────────────────────────
# 5. The rule is registered and fires (DP#18): a principal sale has an
#    observable effect on the engine output -- the rule is not a silent no-op.
# ──────────────────────────────────────────────────────────────────────────

class RuleFiresAndIsRegistered(unittest.TestCase):
    """DP#18: a rule that is registered but never fires (or fires with no
    observable effect) is the #627 failure shape. The principal_disposition
    rule must have an observable effect on a sale household's output."""

    def test_rule_name_in_rule_order(self):
        """The rule is registered in RULE_ORDER (the explicit declared order)
        and EXPECTED_RULE_NAMES (the independent declaration the registry
        test enforces) -- a rule silently added to the registry without being
        added to the order is a build error, not a silent gap."""
        from simulation_rules import RULE_ORDER
        self.assertIn('principal_disposition', RULE_ORDER)

    def test_rule_has_observable_effect(self):
        """The rule changes the engine output: a sale household's sale-year
        total_assets DIFFERS from the same household with the rule disabled.
        Disabling the rule (monkey-patching RULES to a no-op) should leave the
        sale household's sale-year total_assets WITHOUT the invested proceeds
        -- a money-LOSING path the rule exists to prevent. This is the
        output-depends-on-it test the spec requires (DP#18)."""
        import rule_registry
        base = _base_doc()
        sell = _run(_add_principal_sale(base, _SALE_2031))
        n = _SALE_YEAR_INDEX
        original = rule_registry.RULES['principal_disposition']
        try:
            # Disable the rule -> the sale's proceeds are NOT invested and
            # the mortgage/HELOC are NOT discharged (the amortization schedule
            # keeps running); the household does NOT realize the home's value.
            rule_registry.RULES['principal_disposition'] = (
                lambda ws, ctx: False)
            sell_disabled = _run(_add_principal_sale(base, _SALE_2031))
        finally:
            rule_registry.RULES['principal_disposition'] = original
        # With the rule: the sale-year total_assets includes the invested
        # P_net (principal_sale_proceeds_invested > 0). Without the rule: no
        # proceeds invested and the mortgage/HELOC stay (the household keeps
        # the debt and never realizes the home's value).
        self.assertGreater(
            sell[n].principal_sale_proceeds_invested, 0.0,
            "with the rule: the sale's net proceeds should be invested "
            "(principal_sale_proceeds_invested > 0)")
        self.assertEqual(
            sell_disabled[n].principal_sale_proceeds_invested, 0.0,
            "without the rule: no proceeds invested (the rule is what "
            "invests them)")
        # The sell household's sale-year NET_ASSETS is HIGHER with the rule
        # (it realized the home's value less friction); without the rule the
        # household keeps the off-sheet home AND the mortgage debt (the
        # mortgage_balance stays > 0 without the rule).
        # WITH the rule: the sale-year mortgage is DISCHARGED (0). WITHOUT the
        # rule: the mortgage keeps amortizing (> 0). The rule is what
        # discharges it -- the observable effect.
        self.assertEqual(
            sell[n].mortgage_balance, 0.0,
            "with the rule: the sale-year mortgage is discharged (0)")
        self.assertGreater(
            sell_disabled[n].mortgage_balance, 0.0,
            "without the rule: the mortgage keeps amortizing (the rule is "
            "what discharges it at the sale)")
        # The net_assets observable: WITH the rule the household realized the
        # home's value (net_assets higher); WITHOUT the rule it did not.
        sell_net = sell[n].total_assets - sell[n].total_debt
        disabled_net = sell_disabled[n].total_assets - sell_disabled[n].total_debt
        self.assertGreater(
            sell_net, disabled_net,
            "the rule has an observable effect: with it, the sale-year "
            "net_assets is HIGHER (the home's value is realized less "
            "friction); without it the household keeps the off-sheet home "
            "AND the mortgage debt (net_assets lower)")


# ──────────────────────────────────────────────────────────────────────────
# 6. The contract surface: the principal's `sale` is mapped onto
#    cfg['property']['principal_sale'] and round-trips (DP#24).
# ──────────────────────────────────────────────────────────────────────────

class ContractSurfaceMapsAndRoundTrips(unittest.TestCase):
    """The principal residence's declared `sale` (the SAME `property_sale`
    schema block Bite B defines, on a `kind="principal"` property) is mapped
    by `contract_principal._map_principal_sale` onto
    `cfg['property']['principal_sale']`, read by `SimulationConfig.from_dict`
    into the `principal_sale` field, and re-emitted by `to_dict` (DP#24:
    a load -> modify -> save cycle does not silently drop the sale)."""

    def test_sale_maps_onto_principal_sale_field(self):
        """A principal with a declared `sale` maps onto the
        `principal_sale` config field (the principal's own seam, distinct
        from the non-principal `properties[]` path)."""
        doc = _add_principal_sale(_base_doc(), _SALE_2031)
        legacy = ic.to_internal_config(doc)
        self.assertIn('principal_sale', legacy['property'],
                      "the principal's sale must map onto "
                      "cfg['property']['principal_sale']")
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIsNotNone(cfg.principal_sale)
        self.assertEqual(cfg.principal_sale['year'], 2031)
        # selling_costs is the couple's share (joint p1/p2 50/50 -> 100%
        # couple share -> full selling_costs).
        self.assertAlmostEqual(
            cfg.principal_sale['selling_costs'], 30_000.0, places=2)
        # owner_roles carries each taxed member's share.
        self.assertIn('primary', cfg.principal_sale['owner_roles'])
        self.assertIn('spouse', cfg.principal_sale['owner_roles'])
        # value_share is the couple's share of the gross value (100%).
        self.assertAlmostEqual(
            cfg.principal_sale['value_share'], 650_000.0, places=2)
        # acb_share: null ACB -> value_share (no accrued gain, DP#32).
        self.assertAlmostEqual(
            cfg.principal_sale['acb_share'], 650_000.0, places=2)

    def test_sale_round_trips_through_to_dict(self):
        """DP#24: a load -> to_dict cycle re-emits the principal_sale so a
        subsequent from_dict reloads it byte-identically (the sale is not
        silently dropped)."""
        doc = _add_principal_sale(_base_doc(), _SALE_2031)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        out = cfg.to_dict()
        self.assertIn('principal_sale', out['property'],
                      "to_dict must re-emit principal_sale (DP#24: a load -> "
                      "save cycle must not silently drop the sale)")
        # Reload from the to_dict output -> the sale round-trips.
        cfg2 = SimulationConfig.from_dict(out)
        self.assertIsNotNone(cfg2.principal_sale)
        self.assertEqual(cfg2.principal_sale['year'], 2031)

    def test_no_sale_does_not_emit_principal_sale_in_to_dict(self):
        """DP#24/DP#32: a household with no principal sale does NOT emit a
        `principal_sale` key in to_dict (None round-trips to 'absent', not a
        literal null a naive from_dict would treat as 'declared but empty')."""
        cfg = SimulationConfig.from_dict(ic.to_internal_config(_base_doc()))
        out = cfg.to_dict()
        self.assertNotIn('principal_sale', out['property'],
                          "a household with no principal sale must not emit "
                          "principal_sale in to_dict (None -> absent, DP#32)")

    def test_no_principal_property_maps_to_none(self):
        """A document with NO ``kind="principal"`` property (the household
        rents -- no principal residence declared) maps to
        ``principal_sale=None`` (the helper's ``principal is None`` fast
        path). The engine's no-property path: house_value=0, no mortgage, no
        sale -- a strict no-op."""
        doc = copy.deepcopy(_base_doc())
        # Remove the principal residence property entirely.
        doc["properties"] = [p for p in doc["properties"]
                             if p["kind"] != "principal"]
        # The mortgage/HELOC were secured against the principal; remove them
        # too (a household with no property has no secured charge -- the
        # contract adapter refuses a mortgage against an absent property).
        doc["liabilities"] = [l for l in doc["liabilities"]
                              if l.get("collateral") != "principal_residence"]
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        # No principal -> no principal_sale (the helper returned None).
        self.assertNotIn('principal_sale', legacy['property'],
                          "a document with no principal property must not "
                          "carry a principal_sale (the helper's no-principal "
                          "fast path)")
        cfg = SimulationConfig.from_dict(legacy)
        self.assertIsNone(cfg.principal_sale)

    def test_zero_couple_share_principal_sale_is_refused(self):
        """DP#32: a principal owned ENTIRELY by someone outside the couple
        (couple_share == 0) declaring a sale is REFUSED LOUDLY -- the
        household cannot sell a home it does not own, and silently carrying a
        0-share sale (no proceeds, no debt discharged) would be the exact
        no-op-masquerading-as-a-disposition silent-zero failure this repo
        exists to prevent. Built by re-assigning the principal's owner to
        ``ca`` (a child, not in the couple p1/p2 -> couple_share = 0)."""
        doc = copy.deepcopy(_base_doc())
        for prop in doc["properties"]:
            if prop["kind"] == "principal":
                # Re-assign to ca (a child) -> the couple's share is 0.
                prop["owner"] = "ca"
                prop["sale"] = {"year": 2031, "selling_costs": 30000}
                break
        # The mortgage/heloc were owned by p1/p2 against the principal; a
        # child-owned principal with a couple-owned mortgage is incoherent
        # for this test's purpose, so remove the secured liabilities.
        doc["liabilities"] = [l for l in doc["liabilities"]
                              if l.get("collateral") != "principal_residence"]
        contract_schema.validate_contract(doc)
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            ic.to_internal_config(doc)
        self.assertIn("0 share", str(cm.exception))


if __name__ == "__main__":
    unittest.main()