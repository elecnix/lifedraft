#!/usr/bin/env python3
"""Issue #696 (epic #690, bite 5): a dated MID-HORIZON PROPERTY PURCHASE.

Before this bite every property the annual engine could see was held from year
0 of the projection (#692 carried each non-principal property's static net
equity onto the balance sheet from the opening state). A household could ask
"what if I already owned a rental from day one?" but NOT "should I buy one in
five years?" -- the decision the epic exists to evaluate (DP#30).

This bite adds a dated purchase. A property declaring ``purchase`` (a date +
closing costs) is BOUGHT that year, not held from year 0:

  - it contributes NO equity, rent, or CCA before its purchase year -- it does
    not yet exist (``simulation_state._property_equity_for_year``, and the rental
    gate in ``simulation._rental_income_for``);
  - in the purchase year the household funds the DOWN PAYMENT (the couple's
    net_equity = value less the mortgage originated against it) plus the couple's
    share of CLOSING COSTS, drawn through the solvency liquidation waterfall
    (``simulation_rules.apply_solvency``) -- a real, fully-sourced outflow, never
    a clamped reduction of contributions that could let the payment vanish;
  - its equity enters the balance sheet, its mortgage originates, and it earns
    rent / claims CCA normally FROM the purchase year onward.

Scope of THIS bite: the dated purchase EVENT (equity gating + rental/CCA gating
+ the sourced cash outflow), building on #692's ``properties[]`` seam and
#693/#694's rental machinery. STR (#697) is a later bite.

All fixtures use fabricated ids and round numbers (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from simulation_state import SimState, _property_equity_for_year

from test_input_contract import _load_example, _two_generation_subset
import contract_people
import contract_property
import contract_schema


def _add_cottage(doc, value=300000, mortgage_balance=0, purchase=None,
                 owner=None):
    """A recreational property the PRIMARY COUPLE (p1/p2) jointly owns, with an
    optional mortgage secured against it and an optional dated ``purchase``."""
    doc = copy.deepcopy(doc)
    prop = {
        "id": "couple_cottage",
        "owner": owner or {"joint": [{"person": "p1", "pct": 0.5},
                                     {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": value, "as_of": "2026-06-30"},
        "acb": 150000,
        "designated_principal_residence_years": [],
    }
    if purchase is not None:
        prop["purchase"] = purchase
    doc["properties"].append(prop)
    if mortgage_balance:
        doc["liabilities"].append({
            "id": "cottage_mortgage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "mortgage",
            "balance": {"amount": mortgage_balance, "as_of": "2026-06-30"},
            "rate": 0.05,
            "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 1500},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
            "collateral": "couple_cottage",
        })
    return doc


def _add_rental(doc, purchase=None):
    """A rental condo the PRIMARY COUPLE owns, declaring T776 rent/expense
    facts (net rental income 30000 - 8000 = 22000) and an optional purchase."""
    doc = copy.deepcopy(doc)
    prop = {
        "id": "couple_rental",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "rental",
        "value": {"amount": 400000, "as_of": "2026-06-30"},
        "acb": 300000,
        "designated_principal_residence_years": [],
        "rental": {"gross_rent_annual": 30000, "expenses_annual": 8000,
                   "as_of": "2026-06-30"},
    }
    if purchase is not None:
        prop["purchase"] = purchase
    doc["properties"].append(prop)
    return doc


def _run(doc):
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


# The example projects 50 years from start_year 2026, so results[i] is calendar
# year 2026 + i. A purchase dated 2031 fires in results[5]; results[0..4] are
# strictly before it.
_PURCHASE_2031 = {"date": "2031-06-30", "closing_costs": 10000}
_PURCHASE_YEAR_INDEX = 5


class PurchaseReachesInternalConfig(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_purchase_maps_year_and_closing_costs(self):
        doc = _add_cottage(self.base, purchase=_PURCHASE_2031)
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        purchases = [p.get("purchase") for p in legacy["properties"]]
        # Couple owns 100% -> closing costs pass through undivided.
        self.assertEqual(purchases, [{"year": 2031, "closing_costs": 10000.0}])

    def test_closing_costs_taken_at_the_couples_share(self):
        """A property the couple owns HALF of (a child owns the other half): the
        couple funds only its share of the closing costs, the same share its
        net_equity is taken at (#692)."""
        half_owner = {"joint": [{"person": "p1", "pct": 0.25},
                                {"person": "p2", "pct": 0.25},
                                {"person": "ca", "pct": 0.5}]}
        doc = _add_cottage(self.base, purchase=_PURCHASE_2031, owner=half_owner)
        primary_id, spouse_id = contract_people._find_primary_and_spouse(doc)
        owned = contract_property._map_owned_properties(doc, primary_id, spouse_id)
        self.assertEqual(owned[0]["purchase"],
                         {"year": 2031, "closing_costs": 5000.0})


class PropertyEquityGatedByPurchaseYear(unittest.TestCase):
    """The pure gate: a purchased property is worth nothing on the balance sheet
    until the year it is bought; a property with no purchase is unconditional."""

    def test_zero_before_purchase_year(self):
        prop = {"net_equity": 60000.0, "purchase": {"year": 2031}}
        self.assertEqual(_property_equity_for_year(prop, 2030, 2026), 0.0)

    def test_full_equity_from_purchase_year_onward(self):
        prop = {"net_equity": 60000.0, "purchase": {"year": 2031}}
        self.assertEqual(_property_equity_for_year(prop, 2031, 2026), 60000.0)
        self.assertEqual(_property_equity_for_year(prop, 2040, 2026), 60000.0)

    def test_no_purchase_is_unconditional(self):
        prop = {"net_equity": 60000.0}
        self.assertEqual(_property_equity_for_year(prop, 2026, 2026), 60000.0)


class PurchasedPropertyIsInertBeforePurchase(unittest.TestCase):
    """The property genuinely does not exist before its purchase date: the whole
    trajectory up to the purchase year is byte-identical to a household that
    never declared it (no equity, no rent, no cash outflow)."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_trajectory_identical_to_absent_before_purchase(self):
        without = _run(self.base)
        with_buy = _run(_add_cottage(self.base, value=300000,
                                     mortgage_balance=240000,
                                     purchase=_PURCHASE_2031))
        for i in range(_PURCHASE_YEAR_INDEX):
            self.assertEqual(with_buy[i].total_assets, without[i].total_assets,
                             f"year index {i} (before purchase) diverged")

    def test_equity_absent_before_but_present_from_purchase_year(self):
        without = _run(self.base)
        with_buy = _run(_add_cottage(self.base, value=300000,
                                     mortgage_balance=0,
                                     purchase=_PURCHASE_2031))
        # Before: identical. From the purchase year on: the household owns the
        # cottage, so its balance sheet differs.
        self.assertEqual(with_buy[_PURCHASE_YEAR_INDEX - 1].total_assets,
                         without[_PURCHASE_YEAR_INDEX - 1].total_assets)
        self.assertNotEqual(with_buy[_PURCHASE_YEAR_INDEX].total_assets,
                            without[_PURCHASE_YEAR_INDEX].total_assets)


class RentalIncomeStartsAtPurchaseYear(unittest.TestCase):
    """A rental bought mid-horizon earns NO rent before its purchase year and its
    full net rental income (30000 - 8000 = 22000) from that year onward -- proof
    the property is truly absent before, not merely masked."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_no_rent_before_purchase_full_rent_after(self):
        results = _run(_add_rental(self.base, purchase=_PURCHASE_2031))
        for i in range(_PURCHASE_YEAR_INDEX):
            self.assertEqual(results[i].net_rental_income, 0.0,
                             f"year index {i} earned rent before purchase")
        for i in range(_PURCHASE_YEAR_INDEX, _PURCHASE_YEAR_INDEX + 3):
            self.assertAlmostEqual(results[i].net_rental_income, 22000.0,
                                   places=6)

    def test_a_rental_held_from_year_zero_earns_from_year_zero(self):
        """Control: without a purchase date the same rental earns from year 0."""
        results = _run(_add_rental(self.base))
        self.assertAlmostEqual(results[0].net_rental_income, 22000.0, places=6)


class DownPaymentAndClosingCostsAreCharged(unittest.TestCase):
    """Money conservation (DP#18): the purchase-year cash outflow -- the DOWN
    PAYMENT (net_equity) plus CLOSING COSTS -- is charged in full to the cash-flow
    identity (``apply_solvency``), which funds it from real inflows/assets. It is
    charged EXACTLY ONCE, in the purchase year alone, and never clamped away (the
    defect the issue calls out: reducing contributions floors at zero and lets a
    down payment larger than the year's savings vanish). Asserted on the identity's
    own spending figure, so the tax cost of whatever the waterfall liquidates does
    not muddy the charge itself."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_full_outflow_is_charged_in_the_purchase_year_only(self):
        # value 300000, mortgage 240000 -> down payment (net_equity) 60000;
        # closing costs 20000 -> total purchase outflow 80000.
        without = _run(self.base)
        with_buy = _run(_add_cottage(
            self.base, value=300000, mortgage_balance=240000,
            purchase={"date": "2031-06-30", "closing_costs": 20000}))
        K = _PURCHASE_YEAR_INDEX
        # Before AND after the purchase year: the same spending as the household
        # that made no purchase (a one-time event, not a recurring cost).
        self.assertAlmostEqual(
            with_buy[K - 1].solvency_spending_outflow,
            without[K - 1].solvency_spending_outflow, places=6)
        self.assertAlmostEqual(
            with_buy[K + 1].solvency_spending_outflow,
            without[K + 1].solvency_spending_outflow, places=6)
        # In the purchase year: the full 80000 outflow on top of ordinary spend.
        self.assertAlmostEqual(
            with_buy[K].solvency_spending_outflow
            - without[K].solvency_spending_outflow, 80000.0, places=6)


class TestAbsenceIsNoOp(unittest.TestCase):
    """DP#32: a property with no ``purchase`` (held from year 0) is byte-identical
    to before this bite -- no ``purchase`` key in the internal config, and a
    trajectory unchanged by the fold's per-year equity recompute."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_no_purchase_key_emitted(self):
        doc = _add_cottage(self.base, value=400000, mortgage_balance=250000)
        legacy = ic.to_internal_config(doc)
        for prop in legacy["properties"]:
            self.assertNotIn("purchase", prop)

    def test_held_from_year_zero_trajectory_unchanged(self):
        """A cottage held from year 0 must still add exactly its static net
        equity to the terminal balance sheet (the #692 invariant): the #696
        per-year recompute is a no-op when no purchase is declared."""
        without = _run(self.base)
        held = _run(_add_cottage(self.base, value=400000,
                                 mortgage_balance=250000))
        delta = held[-1].total_assets - without[-1].total_assets
        self.assertAlmostEqual(delta, 150000.0, places=6)
        # And identical every year, not just terminally.
        for i in range(len(without)):
            self.assertAlmostEqual(held[i].total_assets - without[i].total_assets,
                                   150000.0, places=6)


if __name__ == "__main__":
    unittest.main()
