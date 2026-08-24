#!/usr/bin/env python3
"""Issue #693 (epic #690, bite 2): a declared rental property produces NET RENTAL
INCOME (gross rent - operating expenses) taxable at the owner's marginal rate,
and the mortgage interest on the income-producing property is DEDUCTIBLE under
ITA s.20(1)(c).

Before this bite the rental facts (``properties[kind=rental].rental``) reached
the engine only as static equity (#692); no rent, no expense and no interest
deduction were ever computed. This bite carries the T776 facts into taxable
income: the operating income (gross - expenses) is added to the owner's taxable
income, and the mortgage interest is subtracted (s.20(1)(c)) -- the same
deductibility rule the household's RRSP/TFSA borrowing is expressly DENIED
(s.18(11)).

Scope of THIS bite: net rental income + s.20(1)(c) interest deductibility. CCA /
recapture (#694), the per-year PRE allocation (#695), a mid-horizon purchase
(#696) and STR business-income treatment (#697) are LATER bites, deliberately
out of scope.

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
from countries.canada.rental_income import (
    classify_rental_income, net_rental_income)

from test_input_contract import _load_example, _two_generation_subset
import contract_schema


def _add_owned_rental(doc, gross_rent, expenses, mortgage_balance, rate=0.05):
    """A rental condo the PRIMARY COUPLE (p1/p2) jointly owns, declaring T776
    rent/expense facts and (optionally) a mortgage secured against it."""
    doc = copy.deepcopy(doc)
    doc["properties"].append({
        "id": "couple_rental",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "rental",
        "value": {"amount": 500000, "as_of": "2026-06-30"},
        "acb": 400000,
        "designated_principal_residence_years": [],
        "rental": {
            "gross_rent_annual": gross_rent,
            "expenses_annual": expenses,
            "as_of": "2026-06-30",
        },
    })
    if mortgage_balance:
        doc["liabilities"].append({
            "id": "rental_mortgage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "mortgage",
            "balance": {"amount": mortgage_balance, "as_of": "2026-06-30"},
            "rate": rate,
            "rate_type": "fixed",
            "amortization": {"years": 25, "payment_monthly": 1200},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
            "collateral": "couple_rental",
        })
    return doc


def _run(doc):
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


def _first_year(doc):
    return _run(doc)[0]


# ── the tax law, in isolation (countries/canada/rental_income) ──────────────
class RentalIncomeTaxLaw(unittest.TestCase):
    def test_net_income_is_gross_less_expenses_less_deductible_interest(self):
        # 30000 rent - 8000 expenses - 10000 interest = 12000 taxable.
        self.assertAlmostEqual(net_rental_income(30000, 8000, 10000), 12000.0)

    def test_mortgage_free_rental_has_no_interest_deduction(self):
        # Same rent/expenses, no financing -> net = gross - expenses.
        self.assertAlmostEqual(net_rental_income(30000, 8000, 0.0), 22000.0)

    def test_interest_is_flagged_deductible_under_s20_1_c(self):
        eff = classify_rental_income(30000, 8000, 10000)
        # The full mortgage interest is deductible (s.20(1)(c) proportion 1.0).
        self.assertAlmostEqual(eff.deductible_interest, 10000.0)
        self.assertAlmostEqual(eff.operating_income, 22000.0)

    def test_rental_can_run_at_a_loss(self):
        # Expenses + interest above rent -> a deductible rental loss (negative).
        self.assertAlmostEqual(net_rental_income(10000, 8000, 10000), -8000.0)


# ── the wired engine path (DP#17 rule-path pair) ────────────────────────────
class RentalIncomeReachesTaxableIncome(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())  # no rental
        contract_schema.validate_contract(self.base)

    def test_net_rental_income_is_gross_less_expenses_less_interest(self):
        """A rental: 30000 rent, 8000 expenses, 200000 mortgage @ 5% (10000
        interest) -> net rental income 12000, of which 10000 is the s.20(1)(c)
        interest deduction."""
        doc = _add_owned_rental(self.base, gross_rent=30000, expenses=8000,
                                mortgage_balance=200000, rate=0.05)
        contract_schema.validate_contract(doc)
        yr = _first_year(doc)
        self.assertAlmostEqual(yr.net_rental_income, 12000.0, places=6)
        self.assertAlmostEqual(yr.rental_interest_deductible, 10000.0, places=6)

    def test_mortgage_free_rental_deducts_nothing_and_ends_richer(self):
        """The SAME property mortgage-free has no interest deduction: its net
        rental income is higher by exactly the interest (deductibility depends on
        the debt, not the property). Because net rental income is real, taxed
        cash the household reinvests, the higher-net-income (mortgage-free)
        rental ends WEALTHIER than the mortgaged one, and both end wealthier than
        a household with no rental at all (the income is realised and taxed, then
        compounds)."""
        mortgaged = _run(_add_owned_rental(
            self.base, 30000, 8000, mortgage_balance=200000, rate=0.05))
        free = _run(_add_owned_rental(
            self.base, 30000, 8000, mortgage_balance=0))
        base = _run(self.base)
        self.assertAlmostEqual(free[0].net_rental_income, 22000.0, places=6)
        self.assertAlmostEqual(mortgaged[0].net_rental_income, 12000.0, places=6)
        # The mortgage-free rental has NO deduction; the mortgaged one deducts.
        self.assertAlmostEqual(free[0].rental_interest_deductible, 0.0, places=6)
        self.assertAlmostEqual(mortgaged[0].rental_interest_deductible, 10000.0,
                               places=6)
        # Net rental income is realised, taxed, and reinvested -> both rentals
        # end wealthier than no rental, and more net income ends wealthier still.
        self.assertGreater(mortgaged[-1].total_assets, base[-1].total_assets)
        self.assertGreater(free[-1].total_assets, mortgaged[-1].total_assets)

    def test_zero_expense_zero_interest_rental_is_fully_taxable(self):
        """DP#32: gross rent with no expenses and no mortgage still shows up as
        taxable net rental income -- not silently netted to zero."""
        yr = _first_year(_add_owned_rental(
            self.base, gross_rent=24000, expenses=0, mortgage_balance=0))
        self.assertAlmostEqual(yr.net_rental_income, 24000.0, places=6)
        self.assertAlmostEqual(yr.rental_interest_deductible, 0.0, places=6)

    def test_rental_facts_reach_the_internal_config(self):
        doc = _add_owned_rental(self.base, 30000, 8000, mortgage_balance=200000,
                                rate=0.05)
        legacy = ic.to_internal_config(doc)
        rental = next(p["rental"] for p in legacy["properties"] if "rental" in p)
        self.assertEqual(rental["gross_rent_annual"], 30000)
        self.assertEqual(rental["expenses_annual"], 8000)
        self.assertAlmostEqual(rental["mortgage_interest_annual"], 10000.0)
        self.assertEqual(rental["owner_roles"], {"primary": 0.5, "spouse": 0.5})


class TestAbsenceIsNoOp(unittest.TestCase):
    """DP#32: a household that declares no rental (the golden path) must be
    byte-identical to today -- no rental facts in the internal config, and no
    rental income on any YearResult."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())  # principal only
        contract_schema.validate_contract(self.base)

    def test_no_rental_block_in_internal_config(self):
        legacy = ic.to_internal_config(self.base)
        for prop in legacy.get("properties", []):
            self.assertNotIn("rental", prop)

    def test_year_results_have_zero_rental_income(self):
        legacy = ic.to_internal_config(self.base)
        cfg = SimulationConfig.from_dict(legacy)
        results = FamilySimulation(cfg).run()
        self.assertTrue(all(r.net_rental_income == 0.0 for r in results))
        self.assertTrue(all(r.rental_interest_deductible == 0.0 for r in results))

    def test_non_rental_property_carries_no_rental_facts(self):
        """A cottage (kind=recreational) reaches the config as equity only, with
        no rental facts -- rental income is a kind=rental thing."""
        doc = copy.deepcopy(self.base)
        doc["properties"].append({
            "id": "couple_cottage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "recreational",
            "value": {"amount": 300000, "as_of": "2026-06-30"},
            "acb": 150000,
            "designated_principal_residence_years": [],
        })
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertTrue(all("rental" not in p for p in legacy["properties"]))
        self.assertEqual(_first_year(doc).net_rental_income, 0.0)


if __name__ == "__main__":
    unittest.main()
