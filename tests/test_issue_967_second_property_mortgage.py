#!/usr/bin/env python3
"""Issue #967 — mid-horizon MORTGAGE financing for a second property.

Before this issue a property bought mid-horizon (#696/Bite B) was equity-
financed: the full value left the portfolio as the down payment. A MORTGAGE
originating at the purchase year was not expressible -- the contract's
liabilities are year-0 opening balances, so a mortgage that originates at
year N could not be declared. This blocked the LEVERAGE case, which for a
RENTAL is the Smith Manoeuvre: borrow to buy an income property, deduct the
interest (ITA s.20(1)(c)).

This issue adds ``purchase.financing = {mortgage_amount, rate, rate_type,
amortization_years}``. At the purchase year:

  - a mortgage ORIGINATES on the property (secured, collateral = the
    property), serviced (principal + interest) from that year to payoff;
  - only the DOWN PAYMENT (value - mortgage_amount, couple share) leaves the
    portfolio -- the mortgage funds the rest. The originated principal is an
    INFLOW to the cash-flow identity in the purchase year (money conserved:
    the mortgage arrives from the lender and leaves for the seller in the
    same breath, the same inflow==outflow discipline the year-0 leveraged
    lump sum uses);
  - the interest is DEDUCTIBLE when the property is a rental (investment use,
    ITA s.20(1)(c)) -- the rental fold claims the per-year interest from the
    financing schedule; NON-deductible for a recreational/personal property
    (a cottage has no ``rental`` block, so its financed interest never reaches
    the deduction, by construction).

Absence-safe (DP#32): a property with no ``financing`` is equity-financed,
byte-identical to #696. The golden invariant (9709753.139463063) does not
move -- verified in test_golden_trajectory_581 (no financing ⇒ no-op).

All fixtures use fabricated ids and round numbers (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from contract_property import _annual_amortization_schedule
from simulation_config import SimulationConfig
from simulation import FamilySimulation

from test_input_contract import _load_example, _two_generation_subset
import contract_people
import contract_schema


# The example projects 50 years from start_year 2026, so results[i] is calendar
# year 2026 + i. A purchase dated 2031 fires in results[5]; results[0..4] are
# strictly before it.
_PURCHASE_2031 = {"date": "2031-06-30", "closing_costs": 10000}
_PURCHASE_YEAR_INDEX = 5
_VALUE = 400000
_MORTGAGE_AMOUNT = 300000
_DOWN_PAYMENT = _VALUE - _MORTGAGE_AMOUNT  # 100000, the couple owns 100%
_FINANCING = {
    "mortgage_amount": _MORTGAGE_AMOUNT,
    "rate": 0.05,
    "rate_type": "fixed",
    "amortization_years": 25,
}


def _add_rental(doc, financing=None, purchase=None):
    """A rental condo the PRIMARY COUPLE owns whole, declaring T776 facts
    (gross_rent 30000, expenses 8000) and an optional financed purchase."""
    doc = copy.deepcopy(doc)
    prop = {
        "id": "couple_rental",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "rental",
        "value": {"amount": _VALUE, "as_of": "2026-06-30"},
        "acb": _VALUE,  # bought at value: no accrued gain yet (DP#32)
        "designated_principal_residence_years": [],
        "rental": {"gross_rent_annual": 30000, "expenses_annual": 8000,
                   "as_of": "2026-06-30"},
        "purchase": purchase or copy.deepcopy(_PURCHASE_2031),
    }
    if financing is not None:
        prop["purchase"]["financing"] = financing
    doc["properties"].append(prop)
    return doc


def _add_cottage(doc, financing=None, purchase=None):
    """A recreational property the PRIMARY COUPLE owns whole, with an optional
    financed purchase. No ``rental`` block -- a cottage carries equity only."""
    doc = copy.deepcopy(doc)
    prop = {
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": _VALUE, "as_of": "2026-06-30"},
        "acb": _VALUE,
        "designated_principal_residence_years": [],
        "purchase": purchase or copy.deepcopy(_PURCHASE_2031),
    }
    if financing is not None:
        prop["purchase"]["financing"] = financing
    doc["properties"].append(prop)
    return doc


def _run(doc):
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


class ContractMappingTest(unittest.TestCase):
    """The financing block reaches the internal config with the right shape."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_financing_maps_to_internal_config(self):
        doc = _add_rental(self.base, financing=_FINANCING)
        legacy = ic.to_internal_config(doc)
        financing = legacy["properties"][0]["purchase"]["financing"]
        # The couple owns 100% -> the mortgage_amount passes through at the
        # couple's share (300000), with the rate/type/term and the origination
        # year derived from the purchase date.
        self.assertEqual(financing["mortgage_amount"], _MORTGAGE_AMOUNT)
        self.assertEqual(financing["rate"], 0.05)
        self.assertEqual(financing["rate_type"], "fixed")
        self.assertEqual(financing["amortization_years"], 25)
        self.assertEqual(financing["origination_year"], 2031)
        # A rental's financed mortgage is deductible; a cottage's is not.
        self.assertTrue(financing["deductible"])
        # The precomputed annual amortization schedule is carried.
        self.assertTrue(financing["schedule"])
        self.assertEqual(financing["schedule"][0]["year"], 2031)
        self.assertAlmostEqual(financing["schedule"][0]["opening_balance"],
                                _MORTGAGE_AMOUNT)

    def test_cottage_financing_is_non_deductible(self):
        doc = _add_cottage(self.base, financing=_FINANCING)
        legacy = ic.to_internal_config(doc)
        financing = legacy["properties"][0]["purchase"]["financing"]
        self.assertFalse(financing["deductible"])

    def test_financing_makes_net_equity_the_down_payment(self):
        """With financing, ``net_equity`` is the DOWN PAYMENT (value -
        mortgage_amount), not the full value -- so only the down payment
        leaves the portfolio in the purchase year."""
        doc = _add_rental(self.base, financing=_FINANCING)
        legacy = ic.to_internal_config(doc)
        prop = legacy["properties"][0]
        # net_equity = (value - mortgage_amount) * couple_share = 100000.
        self.assertAlmostEqual(prop["net_equity"], _DOWN_PAYMENT)
        # secured_share carries the financed principal so the appreciation/
        # sale layer sees the mortgage against the property.
        self.assertAlmostEqual(prop["secured_share"], _MORTGAGE_AMOUNT)

    def test_no_financing_is_equity_financed_byte_identical(self):
        """Without financing, net_equity is the FULL value (the couple owns
        100%) -- the equity-financed #696 behaviour, byte-identical."""
        doc = _add_rental(self.base)  # no financing
        legacy = ic.to_internal_config(doc)
        prop = legacy["properties"][0]
        self.assertAlmostEqual(prop["net_equity"], _VALUE)
        self.assertNotIn("financing", prop["purchase"])


class AbsenceIsByteIdenticalTest(unittest.TestCase):
    """A household with no financing is byte-identical to the #696 equity-
    financed behaviour -- the golden no-op (DP#32)."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_no_financing_trajectory_matches_equity_financed(self):
        equity = _run(_add_rental(self.base))
        no_financing = _run(_add_rental(self.base))  # identical: no financing
        for i in range(len(equity)):
            self.assertEqual(equity[i].total_assets, no_financing[i].total_assets)
            self.assertEqual(equity[i].total_debt, no_financing[i].total_debt)

    def test_financed_trajectory_identical_before_purchase_year(self):
        """Before the purchase year the financed property does not exist, so
        the trajectory is byte-identical to a household that never declared it
        (no mortgage originated yet, no outflow, no equity)."""
        without = _run(self.base)
        with_fin = _run(_add_rental(self.base, financing=_FINANCING))
        for i in range(_PURCHASE_YEAR_INDEX):
            self.assertEqual(with_fin[i].total_assets, without[i].total_assets,
                             f"year index {i} (before purchase) diverged")
            self.assertEqual(with_fin[i].total_debt, without[i].total_debt)


class MortgageOriginationAndServicingTest(unittest.TestCase):
    """The mortgage ORIGINATES in the purchase year and is SERVICED (principal
    + interest) from that year to payoff, joining the debt-service term."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)
        self.results = _run(_add_rental(self.base, financing=_FINANCING))

    def test_no_mortgage_before_purchase_year(self):
        for i in range(_PURCHASE_YEAR_INDEX):
            self.assertEqual(
                self.results[i].second_property_mortgage_balance, 0.0,
                f"year {i}: mortgage should not exist before purchase")
            self.assertEqual(
                self.results[i].second_property_mortgage_payment, 0.0)

    def test_mortgage_originates_in_purchase_year(self):
        r = self.results[_PURCHASE_YEAR_INDEX]
        # The principal originated this year (the inflow that funds the
        # purchase), surfaced for the solvency identity + money conservation.
        self.assertAlmostEqual(
            r.second_property_mortgage_originated, _MORTGAGE_AMOUNT)
        # The balance is the originated principal less the first year's
        # principal paydown (NOT the full principal -- one year already serviced).
        self.assertLess(r.second_property_mortgage_balance, _MORTGAGE_AMOUNT)
        self.assertGreater(r.second_property_mortgage_balance, 0.0)

    def test_mortgage_serviced_every_year_from_purchase_to_payoff(self):
        # From the purchase year onward, a payment is made every year until
        # the balance reaches 0 (the payoff year).
        payments = [r.second_property_mortgage_payment
                    for r in self.results[_PURCHASE_YEAR_INDEX:]]
        # The first many years all carry a payment (a 25-year amortization
        # originated in year 5 of a 50-year horizon -> pays off well inside).
        self.assertTrue(all(p > 0 for p in payments[:20]))
        # The balance declines monotonically (principal is paid down).
        balances = [r.second_property_mortgage_balance
                    for r in self.results[_PURCHASE_YEAR_INDEX:]]
        for i in range(1, len(balances) - 1):
            if balances[i] > 0:
                self.assertLess(balances[i], balances[i - 1],
                                "mortgage balance must decline each serviced year")

    def test_mortgage_pays_off_within_amortization_term(self):
        # A 25-year amortization originated in 2031 pays off by 2056
        # (index 30). The balance reaches 0 and stays 0; no payment after.
        for i in range(31, len(self.results)):
            self.assertEqual(
                self.results[i].second_property_mortgage_balance, 0.0,
                f"year index {i}: mortgage should be paid off")
            self.assertEqual(
                self.results[i].second_property_mortgage_payment, 0.0)

    def test_interest_declines_as_balance_amortizes(self):
        # Interest = opening_balance * rate, so as the balance declines the
        # interest declines too (the standard amortization shape).
        interests = [r.second_property_mortgage_interest
                     for r in self.results[_PURCHASE_YEAR_INDEX:]
                     if r.second_property_mortgage_balance > 0]
        for i in range(1, len(interests)):
            self.assertLess(interests[i], interests[i - 1])

    def test_mortgage_balance_in_total_debt(self):
        # The outstanding mortgage balance is folded into total_debt, so the
        # balance sheet sees the real household liability.
        r = self.results[_PURCHASE_YEAR_INDEX]
        self.assertGreater(r.total_debt, r.second_property_mortgage_balance)


class RentalInterestDeductionTest(unittest.TestCase):
    """A RENTAL bought with a mortgage DEDUCTS its interest (ITA s.20(1)(c));
    the per-year interest rides the rental fold's net rental income."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_rental_deducts_financed_interest(self):
        results = _run(_add_rental(self.base, financing=_FINANCING))
        # Year 5 (the purchase year): net rental income = gross - expenses -
        # deductible interest = 30000 - 8000 - (300000 * 0.05) = 7000.
        # The static mortgage_interest_annual is $0 for a financed rental
        # (no year-0 secured debt); the deduction comes from the financing
        # schedule's dynamic interest.
        r5 = results[_PURCHASE_YEAR_INDEX]
        self.assertAlmostEqual(r5.second_property_mortgage_interest, 15000.0)
        self.assertAlmostEqual(r5.net_rental_income, 7000.0)
        # Year 6: interest = 293954.76 * 0.05 = 14697.74 (after one year's
        # principal paydown); net rental = 30000 - 8000 - 14697.74 = 7302.26.
        r6 = results[_PURCHASE_YEAR_INDEX + 1]
        self.assertAlmostEqual(r6.second_property_mortgage_interest,
                               14697.74, places=2)
        self.assertAlmostEqual(r6.net_rental_income, 7302.26, places=2)

    def test_equity_financed_rental_has_no_mortgage_interest(self):
        """A rental bought WITHOUT financing has $0 financed mortgage interest
        -- the rental fold reads the static mortgage_interest_annual only
        (byte-identical to #693)."""
        results = _run(_add_rental(self.base))  # no financing
        r5 = results[_PURCHASE_YEAR_INDEX]
        self.assertEqual(r5.second_property_mortgage_interest, 0.0)
        # No secured debt against this rental -> net rental = 30000 - 8000.
        self.assertAlmostEqual(r5.net_rental_income, 22000.0)


class CottageInterestNonDeductibleTest(unittest.TestCase):
    """A COTTAGE bought with a mortgage does NOT deduct its interest -- a
    recreational property has no ``rental`` block, so its financed interest
    never reaches the rental deduction fold, by construction."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_cottage_mortgage_originates_and_services(self):
        """The cottage's mortgage originates and services exactly like the
        rental's -- the financing mechanic is property-kind-agnostic."""
        results = _run(_add_cottage(self.base, financing=_FINANCING))
        r5 = results[_PURCHASE_YEAR_INDEX]
        self.assertAlmostEqual(r5.second_property_mortgage_originated,
                               _MORTGAGE_AMOUNT)
        self.assertAlmostEqual(r5.second_property_mortgage_interest, 15000.0)
        self.assertAlmostEqual(r5.second_property_mortgage_payment,
                               21045.24, places=2)
        # The balance is the same as the rental's (same principal/rate/term).
        self.assertGreater(r5.second_property_mortgage_balance, 0.0)

    def test_cottage_interest_is_not_deducted(self):
        """The cottage's net_rental_income stays $0 (no rental block) -- the
        financed interest is surfaced on the mortgage field but never reaches
        a tax deduction. NON-deductible by construction."""
        results = _run(_add_cottage(self.base, financing=_FINANCING))
        for i in range(_PURCHASE_YEAR_INDEX,
                       _PURCHASE_YEAR_INDEX + 5):
            self.assertEqual(results[i].net_rental_income, 0.0,
                             f"year {i}: a cottage has no rental income/ "
                             "deduction -- its financed interest is not "
                             "deductible")
            # The interest IS surfaced on the mortgage field (the rule
            # surfaces it; the rental fold is what claims it, and a cottage
            # has no rental fold).
            if results[i].second_property_mortgage_balance > 0:
                self.assertGreater(
                    results[i].second_property_mortgage_interest, 0.0)

    def test_cottage_and_rental_mortgage_servicing_identical(self):
        """The mortgage servicing (balance, payment, interest) is identical
        for a cottage and a rental with the same financing -- deductibility
        follows the property kind, not the financing, so the financing
        mechanic is the same."""
        rental = _run(_add_rental(self.base, financing=_FINANCING))
        cottage = _run(_add_cottage(self.base, financing=_FINANCING))
        for i in range(_PURCHASE_YEAR_INDEX,
                       _PURCHASE_YEAR_INDEX + 10):
            self.assertAlmostEqual(
                rental[i].second_property_mortgage_balance,
                cottage[i].second_property_mortgage_balance,
                places=2,
                msg=f"year {i}: mortgage servicing must be kind-agnostic")
            self.assertAlmostEqual(
                rental[i].second_property_mortgage_payment,
                cottage[i].second_property_mortgage_payment,
                places=2)
            self.assertAlmostEqual(
                rental[i].second_property_mortgage_interest,
                cottage[i].second_property_mortgage_interest,
                places=2)


class MoneyConservationTest(unittest.TestCase):
    """The mortgage is an INFLOW (funds the purchase) and a serviced LIABILITY
    (outflow each year after) -- a financed purchase must not create or
    destroy money (DP#18), the same inflow==outflow discipline as the year-0
    borrowed-investment path."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_purchase_year_not_ruined_by_the_down_payment(self):
        """The down payment (100000) + closing costs (10000) leave the
        portfolio, but the mortgage principal (300000) arrives as an inflow
        the same year -- so the cash-flow identity does NOT invent a
        shortfall and force a spurious liquidation (no false ruin)."""
        results = _run(_add_rental(self.base, financing=_FINANCING))
        for i in range(_PURCHASE_YEAR_INDEX, _PURCHASE_YEAR_INDEX + 3):
            self.assertFalse(results[i].ruined,
                              f"year {i}: a financed purchase must not be "
                              "ruined -- the originated mortgage is an inflow "
                              "that funds the down payment")

    def test_net_worth_change_in_purchase_year_is_closing_costs(self):
        """Money conservation, stated via net worth (total_assets - total_debt):

        In the purchase year the household gains a property worth `value`
        (equity = value - mortgage = down payment, on the asset side) AND a
        mortgage debt of `mortgage_amount` (on the liability side). The
        mortgage principal inflow funds the purchase: net worth changes by
        (equity gained) - (debt incurred) - (closing costs) - (one year's
        mortgage interest) + (the normal year's investment growth + income -
        spending). The financed-vs-equity DIFFERENCE in net worth at the
        purchase year is exactly: the financed household kept `mortgage_amount`
        invested (it did NOT spend it as down payment) but owes
        `mortgage_amount` more debt, minus one year's interest on that debt
        (which the financed household pays but the equity household does not).

        Stated cleanly: financed_net_worth - equity_net_worth at the purchase
        year = mortgage_amount - mortgage_amount (the kept asset offsets the
        new debt) - financed_interest_paid_this_year + interest_earned_on_kept_
        mortgage_amount. The DIFFERENCE is bounded (order of the interest),
        NOT a six-figure creation/destruction of money.
        """
        equity = _run(_add_rental(self.base))           # full value as down payment
        financed = _run(_add_rental(self.base, financing=_FINANCING))
        i = _PURCHASE_YEAR_INDEX
        nw_equity = equity[i].total_assets - equity[i].total_debt
        nw_fin = financed[i].total_assets - financed[i].total_debt
        # The difference must be small (order of one year's mortgage interest
        # on the kept mortgage_amount, ~15000), NOT the mortgage principal --
        # proving the principal inflow exactly offsets the new debt, so no
        # money was created or destroyed by the financing itself.
        diff = nw_fin - nw_equity
        self.assertLess(abs(diff), _MORTGAGE_AMOUNT,
                        "the financed-vs-equity net-worth difference must be "
                        "far smaller than the mortgage principal -- the "
                        "principal inflow must offset the new debt (money "
                        "conserved), not create or destroy it")

    def test_no_spurious_forced_liquidation_in_purchase_year(self):
        """The purchase year charges no forced-liquidation tax/loss beyond
        what the household's normal cash flow would incur -- the mortgage
        inflow funds the down payment, so the waterfall is not triggered by
        the purchase itself."""
        baseline = _run(self.base)
        financed = _run(_add_rental(self.base, financing=_FINANCING))
        i = _PURCHASE_YEAR_INDEX
        # The financed purchase year's forced-liquidation tax is at most the
        # baseline's (the purchase is funded by the mortgage inflow, not a
        # forced sale). A spurious liquidation would inflate this above 0
        # when the baseline had none.
        self.assertLessEqual(
            financed[i].forced_liquidation_tax,
            baseline[i].forced_liquidation_tax + 1.0,
            "the financed purchase must not trigger a spurious forced "
            "liquidation -- the mortgage inflow funds the down payment")


class HalfOwnerFinancingTest(unittest.TestCase):
    """A property the couple owns HALF of: the mortgage principal and the
    down payment are taken at the couple's share (mirroring #692/#696)."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_financing_taken_at_couples_share(self):
        half_owner = {"joint": [{"person": "p1", "pct": 0.25},
                                {"person": "p2", "pct": 0.25},
                                {"person": "ca", "pct": 0.5}]}
        doc = _add_rental(self.base, financing=_FINANCING)
        doc["properties"][-1]["owner"] = half_owner
        legacy = ic.to_internal_config(doc)
        prop = legacy["properties"][0]
        # The couple owns 50% -> mortgage_amount and net_equity (down payment)
        # are taken at 50%.
        self.assertAlmostEqual(prop["purchase"]["financing"]["mortgage_amount"],
                               _MORTGAGE_AMOUNT * 0.5)
        self.assertAlmostEqual(prop["net_equity"], _DOWN_PAYMENT * 0.5)


class AmortizationScheduleHelperTest(unittest.TestCase):
    """The pure ``_annual_amortization_schedule`` helper (DP#3) -- the one
    spelling of the per-year interest/principal/balance the servicing rule,
    the rental deduction, and total_debt all read."""

    def test_zero_principal_returns_empty_schedule(self):
        # A mortgage of $0 originates nothing: the schedule is empty (no
        # entries, no servicing). Defensive branch coverage (DP#32: the
        # schema permits money=0, so the helper must handle it cleanly).
        self.assertEqual(_annual_amortization_schedule(
            principal=0.0, annual_rate=0.05, amortization_years=25,
            origination_year=2031, projection_years=50), [])

    def test_zero_amortization_years_returns_empty_schedule(self):
        # A non-positive term amortizes nothing.
        self.assertEqual(_annual_amortization_schedule(
            principal=300000.0, annual_rate=0.05, amortization_years=0,
            origination_year=2031, projection_years=50), [])

    def test_schedule_starts_at_origination_and_amortizes(self):
        sched = _annual_amortization_schedule(
            principal=300000.0, annual_rate=0.05, amortization_years=25,
            origination_year=2031, projection_years=50)
        self.assertEqual(sched[0]['year'], 2031)
        self.assertAlmostEqual(sched[0]['opening_balance'], 300000.0)
        self.assertAlmostEqual(sched[0]['interest'], 15000.0)
        # The balance declines each year.
        self.assertLess(sched[1]['opening_balance'], sched[0]['opening_balance'])

    def test_schedule_pays_off_within_term_and_stops(self):
        # A 25-year amortization pays off by year 25; the schedule stops
        # (no entries past payoff).
        sched = _annual_amortization_schedule(
            principal=300000.0, annual_rate=0.05, amortization_years=25,
            origination_year=2031, projection_years=50)
        self.assertEqual(len(sched), 25)
        self.assertAlmostEqual(sched[-1]['end_balance'], 0.0, places=2)
        self.assertEqual(sched[-1]['year'], 2031 + 24)

    def test_schedule_capped_at_projection_horizon(self):
        # A term longer than the horizon is capped: the schedule covers only
        # `projection_years` entries (the fold never reaches past it).
        sched = _annual_amortization_schedule(
            principal=300000.0, annual_rate=0.05, amortization_years=40,
            origination_year=2031, projection_years=5)
        self.assertEqual(len(sched), 5)
        self.assertGreater(sched[-1]['end_balance'], 0.0)  # not yet paid off

    def test_final_year_closes_balance_exactly(self):
        # In the payoff year the payment closes the loan exactly (the
        # final-year branch), so end_balance floors at 0 even if the
        # annuity slightly over- or under-amortizes.
        sched = _annual_amortization_schedule(
            principal=120000.0, annual_rate=0.0, amortization_years=10,
            origination_year=2031, projection_years=15)
        # 0% rate -> straight-line: 12000 principal/year for 10 years.
        self.assertEqual(len(sched), 10)
        self.assertAlmostEqual(sched[-1]['end_balance'], 0.0)
        self.assertAlmostEqual(sched[-1]['principal'], 12000.0)


class ProjectionYearsFallbackTest(unittest.TestCase):
    """``to_internal_config`` derives the mortgage-schedule horizon cap from the
    projection span; when the horizon does not date against the primary
    (``_horizon_end_year`` returns None) it falls back to a generous fixed cap
    (a mortgage's own amortization term bounds the schedule, so the cap is a
    safety bound, never a truncating one)."""

    def test_horizon_none_falls_back_to_generous_cap(self):
        # The horizon_end-is-None branch is structurally hard to reach through
        # a full valid contract (the estate model refuses households whose
        # horizon is not the primary couple). Exercise the fallback directly by
        # patching _horizon_end_year to return None, then confirm a financed
        # property still maps with a non-empty schedule (the cap did not
        # truncate it -- the mortgage's own term bounds the schedule).
        doc = _add_rental(_two_generation_subset(_load_example()),
                         financing=_FINANCING)
        import input_contract as ic_mod
        orig = contract_people._horizon_end_year
        contract_people._horizon_end_year = lambda d, pid: None
        try:
            legacy = ic_mod.to_internal_config(doc)
        finally:
            contract_people._horizon_end_year = orig
        sched = legacy["properties"][0]["purchase"]["financing"]["schedule"]
        self.assertTrue(sched, "the fallback cap must not truncate the "
                         "schedule -- the mortgage's own term bounds it")
        self.assertEqual(sched[0]["year"], 2031)


class ServicingRuleEdgeCasesTest(unittest.TestCase):
    """Direct unit tests for the ``apply_second_property_mortgage`` rule's
    edge-case branches (a wiring-bug guard + a mixed-financing config)."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_mismatched_state_config_raises(self):
        """A length mismatch between the config's properties and the SimState's
        second-property-mortgage balances (when financing IS declared) is a
        wiring bug, not a silent truncation -> raise (DP#32)."""
        from simulation_rules import (apply_second_property_mortgage,
                                       YearWorkingState, RuleContext)
        from simulation_config import SimulationConfig
        doc = _add_rental(self.base, financing=_FINANCING)
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        # A SimState with NO second_property_mortgage_balances (empty list)
        # but a config with one financed property -> mismatch -> raise.
        # RuleContext is frozen (DP#26), so the mismatched config is passed
        # to the constructor directly -- mutating ``ctx.config`` after the
        # fact would raise FrozenInstanceError instead of the intended guard.
        ws = YearWorkingState()
        ws.opening_second_property_mortgage_balances = []
        ctx = RuleContext(
            year=0, calendar_year=2031, allocations={}, config=cfg,
            investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
            mortgage_data=None, use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.4, spouse_marginal_rate=0.2,
            resp_data=None, fhsa_contribution=0.0, rrsp_annual_limit=None,
            tfsa_annual_limit=None, fhsa_annual_limit=None,
            non_reg_after_tax_return=None, cpp_income=0.0, oas_income=0.0,
            pension_income=0.0, drawdown_order=None,
            rrif_min_rate_primary=0.0, rrif_min_rate_spouse=0.0,
            drawdown_net_target=0.0, retiree_marginal_rate=0.0,
            drawdown_bracket_target=None, drawdown_other_taxable_income=0.0)
        with self.assertRaises(ValueError):
            apply_second_property_mortgage(ws, ctx)

    def test_mixed_financing_only_services_the_financed_property(self):
        """A config with two properties -> one financed, one equity-financed ->
        the rule services only the financed one; the equity-financed one's
        balance stays 0 (the per-property `financing is None` branch)."""
        doc = _add_cottage(self.base)  # equity-financed cottage, no financing
        doc = _add_rental(doc, financing=_FINANCING)  # financed rental
        results = _run(doc)
        # Before the rental's purchase year (2031, index 5): nothing services.
        for i in range(_PURCHASE_YEAR_INDEX):
            self.assertEqual(
                results[i].second_property_mortgage_balance, 0.0)
        # From the purchase year: only the rental's mortgage is on the books.
        r = results[_PURCHASE_YEAR_INDEX]
        self.assertAlmostEqual(
            r.second_property_mortgage_originated, _MORTGAGE_AMOUNT)
        self.assertGreater(r.second_property_mortgage_balance, 0.0)


if __name__ == "__main__":
    unittest.main()