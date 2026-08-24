#!/usr/bin/env python3
"""Issue #697 (epic #690, bite 6, final): a SHORT-TERM rental (Airbnb-style) is a
legally and fiscally different animal from the long-term rental of #693.

Three modelled differences, each exercised below:

1. **Business income, not property income.** STR income with material services is
   ACTIVE business income (ITA s.9), classified distinctly from #693's passive
   property income (T776). The net-income arithmetic is the SAME (DP#9); the
   CLASSIFICATION and the obligations that ride on it differ.
2. **GST/HST/QST small-supplier threshold.** Short-term accommodation is a
   taxable supply: gross revenue EXCEEDING $30,000/year (ETA s.148) triggers a
   registration obligation a long-term rental (an exempt supply) never has.
3. **A jurisdictional legality gate.** The engine REFUSES (loud, DP#32) to model
   positive STR income where it cannot confirm the use is permitted -- a
   zoning-banned Montreal borough, or an STR with no CITQ registration.

The legality/tax rules live in the Canada jurisdiction module (DP#25):
``countries/canada/short_term_rental``. The contract adapter only invokes them.

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
from countries.canada.short_term_rental import (
    GST_HST_SMALL_SUPPLIER_THRESHOLD,
    ShortTermRentalNotPermitted,
    assess_str_legality,
    classify_str_income,
    require_str_permitted,
)

from test_input_contract import _load_example, _two_generation_subset
import contract_schema


def _add_owned_str(doc, gross_rent, expenses, jurisdiction, citq_registered,
                   mortgage_balance=0, rate=0.05):
    """A short-term rental condo the PRIMARY COUPLE jointly owns, declaring T776
    rent/expense facts, a `short_term` legality block, and (optionally) a
    mortgage secured against it."""
    doc = copy.deepcopy(doc)
    doc["properties"].append({
        "id": "couple_airbnb",
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
            "short_term": {
                "jurisdiction": jurisdiction,
                "citq_registered": citq_registered,
            },
        },
    })
    if mortgage_balance:
        doc["liabilities"].append({
            "id": "str_mortgage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "mortgage",
            "balance": {"amount": mortgage_balance, "as_of": "2026-06-30"},
            "rate": rate,
            "rate_type": "fixed",
            "amortization": {"years": 25, "payment_monthly": 1200},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
            "collateral": "couple_airbnb",
        })
    return doc


def _run(doc):
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


def _first_year(doc):
    return _run(doc)[0]


# ── the tax law, in isolation (countries/canada/short_term_rental) ──────────
class ShortTermRentalTaxLaw(unittest.TestCase):
    def test_income_is_classified_as_business_income(self):
        eff = classify_str_income(40000, 8000, 0.0)
        self.assertEqual(eff.income_type, "business_income")

    def test_net_business_income_matches_the_t776_net_arithmetic(self):
        # gross 40000 - expenses 8000 - deductible interest 10000 = 22000.
        eff = classify_str_income(40000, 8000, 10000)
        self.assertAlmostEqual(eff.net_business_income, 22000.0)

    def test_gst_hst_required_above_the_small_supplier_threshold(self):
        # $40k > $30k -> must register for GST/HST/QST.
        above = classify_str_income(40000, 8000, 0.0)
        self.assertTrue(above.gst_hst_registration_required)

    def test_gst_hst_not_required_at_or_below_the_threshold(self):
        # Exactly $30k and below -> small supplier, no registration obligation.
        at = classify_str_income(GST_HST_SMALL_SUPPLIER_THRESHOLD, 5000, 0.0)
        below = classify_str_income(20000, 5000, 0.0)
        self.assertFalse(at.gst_hst_registration_required)
        self.assertFalse(below.gst_hst_registration_required)


# ── the legality gate, in isolation ─────────────────────────────────────────
class ShortTermRentalLegalityGate(unittest.TestCase):
    def test_banned_borough_is_not_permitted(self):
        ruling = assess_str_legality("montreal_lachine", citq_registered=True)
        self.assertFalse(ruling.permitted)

    def test_unregistered_str_is_not_permitted(self):
        ruling = assess_str_legality("montreal_verdun", citq_registered=False)
        self.assertFalse(ruling.permitted)

    def test_registered_str_in_unbanned_borough_is_permitted(self):
        ruling = assess_str_legality("montreal_verdun", citq_registered=True)
        self.assertTrue(ruling.permitted)

    def test_require_permitted_raises_for_banned_borough(self):
        with self.assertRaises(ShortTermRentalNotPermitted):
            require_str_permitted("montreal_saint_laurent", citq_registered=True)

    def test_require_permitted_raises_for_unregistered(self):
        with self.assertRaises(ShortTermRentalNotPermitted):
            require_str_permitted("montreal_verdun", citq_registered=False)

    def test_require_permitted_admits_a_registered_unbanned_str(self):
        require_str_permitted("montreal_verdun", citq_registered=True)  # no raise


# ── the wired engine path ───────────────────────────────────────────────────
class ShortTermRentalReachesTheEngine(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())  # no rental
        contract_schema.validate_contract(self.base)

    def test_permitted_str_carries_business_income_and_gst_flag(self):
        doc = _add_owned_str(self.base, gross_rent=40000, expenses=8000,
                             jurisdiction="montreal_verdun", citq_registered=True)
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        st = next(p["rental"]["short_term"] for p in legacy["properties"]
                  if p.get("rental", {}).get("short_term"))
        self.assertEqual(st["income_type"], "business_income")
        self.assertTrue(st["gst_hst_registration_required"])
        self.assertEqual(st["jurisdiction"], "montreal_verdun")

    def test_str_income_surfaces_on_year_result(self):
        """A permitted STR's net income shows as business income, and the year
        flags the GST/HST registration obligation. The net income is real, taxed
        ordinary income, so it is also inside net_rental_income (not double)."""
        doc = _add_owned_str(self.base, gross_rent=40000, expenses=8000,
                             jurisdiction="montreal_verdun", citq_registered=True)
        contract_schema.validate_contract(doc)
        yr = _first_year(doc)
        self.assertAlmostEqual(yr.str_business_income, 32000.0, places=6)
        self.assertTrue(yr.gst_hst_registration_required)
        self.assertAlmostEqual(yr.net_rental_income, 32000.0, places=6)

    def test_below_threshold_str_does_not_flag_gst(self):
        """DP#17 pair: the SAME permitted STR below the $30k threshold surfaces
        income but NOT the registration obligation -- the flag is the discriminant
        between the two cases, not the income."""
        doc = _add_owned_str(self.base, gross_rent=20000, expenses=5000,
                             jurisdiction="montreal_verdun", citq_registered=True)
        contract_schema.validate_contract(doc)
        yr = _first_year(doc)
        self.assertAlmostEqual(yr.str_business_income, 15000.0, places=6)
        self.assertFalse(yr.gst_hst_registration_required)

    def test_banned_borough_str_is_refused_at_load(self):
        doc = _add_owned_str(self.base, gross_rent=40000, expenses=8000,
                             jurisdiction="montreal_lachine", citq_registered=True)
        contract_schema.validate_contract(doc)  # schema-valid; the REFUSAL is a legality rule
        with self.assertRaises(ShortTermRentalNotPermitted):
            ic.to_internal_config(doc)

    def test_unregistered_str_is_refused_at_load(self):
        doc = _add_owned_str(self.base, gross_rent=40000, expenses=8000,
                             jurisdiction="montreal_verdun", citq_registered=False)
        contract_schema.validate_contract(doc)
        with self.assertRaises(ShortTermRentalNotPermitted):
            ic.to_internal_config(doc)


class TestAbsenceIsNoOp(unittest.TestCase):
    """DP#32: a household with no STR must be byte-identical to today -- a plain
    (long-term) rental carries no `short_term` marker, never sets the GST flag,
    and the golden household (no rental at all) is unchanged."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())  # principal only
        contract_schema.validate_contract(self.base)

    def test_long_term_rental_has_no_short_term_marker(self):
        doc = copy.deepcopy(self.base)
        doc["properties"].append({
            "id": "couple_rental",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "rental",
            "value": {"amount": 500000, "as_of": "2026-06-30"},
            "acb": 400000,
            "designated_principal_residence_years": [],
            "rental": {"gross_rent_annual": 40000, "expenses_annual": 8000,
                       "as_of": "2026-06-30"},
        })
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        for prop in legacy["properties"]:
            self.assertNotIn("short_term", prop.get("rental", {}))
        yr = _first_year(doc)
        self.assertEqual(yr.str_business_income, 0.0)
        self.assertFalse(yr.gst_hst_registration_required)

    def test_golden_household_str_facts_are_inert(self):
        legacy = ic.to_internal_config(self.base)
        cfg = SimulationConfig.from_dict(legacy)
        results = FamilySimulation(cfg).run()
        self.assertTrue(all(r.str_business_income == 0.0 for r in results))
        self.assertTrue(
            all(r.gst_hst_registration_required is False for r in results))

    def test_golden_invariant_unchanged(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests"))
        from test_golden_trajectory_581 import golden_household_config, _run as _grun
        self.assertEqual(
            repr(_grun(golden_household_config())[-1].total_assets),
            "9709753.139463063")


if __name__ == "__main__":
    unittest.main()
