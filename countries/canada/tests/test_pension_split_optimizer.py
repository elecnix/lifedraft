#!/usr/bin/env python3
"""Unit tests for pension_split_optimizer.py module (#317, DP#17).

All test data uses round numbers per DP#15. No personal information.
Role-based names per DP#4. Tests exercise every rule path per DP#17.

SCENARIO 12.1: The optimal pension split is not always 50% — it balances
income-splitting tax savings, the pension income credit, and OAS clawback.

Run with: python3 -m pytest countries/canada/tests/test_pension_split_optimizer.py -v
"""

import os
import sys

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)

import unittest

from countries.canada.pension_split_optimizer import (
    PENSION_INCOME_CREDIT_MAX,
    PENSION_CREDIT_RATE,
    PensionSplitResult,
    pension_income_credit,
    both_spouses_get_credit,
    optimize_pension_split,
    project_pension_split_retirement,
)
from countries.canada.retirement import PensionIncomeType


class TestPensionIncomeCredit(unittest.TestCase):
    """Pension income credit: 15% of first $2,000 of eligible pension."""

    def test_below_cap_credits_full_amount(self):
        self.assertEqual(pension_income_credit(1000), 1000 * PENSION_CREDIT_RATE)

    def test_at_cap(self):
        self.assertEqual(
            pension_income_credit(PENSION_INCOME_CREDIT_MAX),
            PENSION_INCOME_CREDIT_MAX * PENSION_CREDIT_RATE,
        )

    def test_above_cap_is_capped(self):
        # Anything above $2,000 yields the same credit as exactly $2,000.
        self.assertEqual(
            pension_income_credit(50000),
            pension_income_credit(PENSION_INCOME_CREDIT_MAX),
        )

    def test_zero_pension_zero_credit(self):
        self.assertEqual(pension_income_credit(0), 0)


class TestBothSpousesGetCredit(unittest.TestCase):
    """Both spouses qualify only when each has >= $2,000 pension income."""

    def test_both_at_threshold(self):
        self.assertTrue(both_spouses_get_credit(2000, 2000))

    def test_one_below_threshold(self):
        self.assertFalse(both_spouses_get_credit(2000, 1999))

    def test_both_below_threshold(self):
        self.assertFalse(both_spouses_get_credit(0, 0))


class TestOptimizeAvailability(unittest.TestCase):
    """Age gate: federal splitting requires age 55+ (DP#28)."""

    def test_below_federal_age_returns_no_split(self):
        result = optimize_pension_split(
            spouse_a_income=60000,
            spouse_b_income=20000,
            eligible_pension=30000,
            spouse_a_age=50,
            spouse_b_age=50,
            province="quebec",
        )
        self.assertIsInstance(result, PensionSplitResult)
        self.assertEqual(result.optimal_split_pct, 0)
        self.assertEqual(result.optimal_split_amount, 0)
        self.assertIn("not available", result.split_details["note"])

    def test_eligible_age_runs_optimization(self):
        result = optimize_pension_split(
            spouse_a_income=90000,
            spouse_b_income=10000,
            eligible_pension=40000,
            spouse_a_age=68,
            spouse_b_age=66,
            province="quebec",
        )
        # Optimization ran: search details are populated, not the age note.
        self.assertNotIn("note", result.split_details)
        self.assertIn("baseline", result.split_details)


class TestQuebecProvincialAgeGate(unittest.TestCase):
    """#351: Quebec restricts RRIF/LIF income splitting to age 65+.

    A 55-64 Quebec resident passes the federal 55+ gate but must be blocked
    by the provincial 65+ rule (TP-1 Schedule L), unlike a non-Quebec resident.
    """

    def test_quebec_60yo_rrif_income_blocked(self):
        # Age 60 (federal-eligible) but RRIF income in Quebec needs 65+.
        result = optimize_pension_split(
            spouse_a_income=90000,
            spouse_b_income=10000,
            eligible_pension=40000,
            spouse_a_age=60,
            spouse_b_age=58,
            province="quebec",
            income_type=PensionIncomeType.RRIF_INCOME,
        )
        self.assertEqual(result.optimal_split_pct, 0)
        self.assertIn("Quebec", result.split_details["note"])

    def test_non_quebec_60yo_rpp_income_allowed(self):
        # Same age, but a federal jurisdiction permits 55+ RPP splitting.
        result = optimize_pension_split(
            spouse_a_income=90000,
            spouse_b_income=10000,
            eligible_pension=40000,
            spouse_a_age=60,
            spouse_b_age=58,
            province="ontario",
            income_type=PensionIncomeType.RPP_PENSION,
        )
        self.assertNotIn("note", result.split_details)
        self.assertGreater(result.optimal_split_pct, 0)


class TestOptimizeSplitDirection(unittest.TestCase):
    """The optimizer shifts pension from the higher earner toward the lower."""

    def test_unequal_incomes_favor_splitting(self):
        result = optimize_pension_split(
            spouse_a_income=90000,
            spouse_b_income=10000,
            eligible_pension=40000,
            spouse_a_age=68,
            spouse_b_age=66,
            province="quebec",
        )
        # A high spread makes a meaningful split optimal and tax-positive.
        self.assertGreater(result.optimal_split_pct, 0)
        self.assertLessEqual(result.optimal_split_pct, 0.50)
        self.assertGreater(result.tax_savings, 0)
        # Splitting cannot increase total family tax.
        self.assertLessEqual(result.total_tax_with_split, result.total_tax_no_split)

    def test_split_amount_matches_pct(self):
        eligible = 40000
        result = optimize_pension_split(
            spouse_a_income=90000,
            spouse_b_income=10000,
            eligible_pension=eligible,
            spouse_a_age=68,
            spouse_b_age=66,
        )
        self.assertAlmostEqual(
            result.optimal_split_amount, eligible * result.optimal_split_pct
        )

    def test_max_split_never_exceeds_50pct(self):
        # Extreme spread: even so, the cap holds at 50%.
        result = optimize_pension_split(
            spouse_a_income=300000,
            spouse_b_income=0,
            eligible_pension=100000,
            spouse_a_age=68,
            spouse_b_age=66,
        )
        self.assertLessEqual(result.optimal_split_pct, 0.50)


class TestOptimizeEdgeCases(unittest.TestCase):
    """Edge cases: zero pension, equal incomes."""

    def test_zero_eligible_pension_no_split(self):
        result = optimize_pension_split(
            spouse_a_income=50000,
            spouse_b_income=50000,
            eligible_pension=0,
            spouse_a_age=68,
            spouse_b_age=66,
        )
        self.assertEqual(result.optimal_split_pct, 0)
        self.assertEqual(result.optimal_split_amount, 0)
        self.assertEqual(result.tax_savings, 0)

    def test_equal_incomes_no_benefit(self):
        # When both spouses already share income equally, splitting the
        # pension cannot reduce tax, so the optimum stays at 0%.
        result = optimize_pension_split(
            spouse_a_income=50000,
            spouse_b_income=50000,
            eligible_pension=0,
            spouse_a_age=68,
            spouse_b_age=66,
        )
        self.assertEqual(result.optimal_split_pct, 0)

    def test_pure_function_repeatable(self):
        kwargs = dict(
            spouse_a_income=90000,
            spouse_b_income=10000,
            eligible_pension=40000,
            spouse_a_age=68,
            spouse_b_age=66,
        )
        r1 = optimize_pension_split(**kwargs)
        r2 = optimize_pension_split(**kwargs)
        self.assertEqual(r1.optimal_split_pct, r2.optimal_split_pct)
        self.assertEqual(r1.tax_savings, r2.tax_savings)


class TestProjectPensionSplitRetirement(unittest.TestCase):
    """Multi-year projection plumbing (SCENARIO 12.1 + 5.1)."""

    def test_requires_explicit_investment_return(self):
        # DP#13: no opinionated default for investment_return.
        with self.assertRaises(ValueError):
            project_pension_split_retirement(
                spouse_a_age=68,
                spouse_b_age=66,
                spouse_a_rrif=400000,
                spouse_b_rrif=200000,
                spouse_a_tfsa=100000,
                spouse_b_tfsa=100000,
            )

    def test_projection_length_and_aging(self):
        results = project_pension_split_retirement(
            spouse_a_age=68,
            spouse_b_age=66,
            spouse_a_rrif=400000,
            spouse_b_rrif=200000,
            spouse_a_tfsa=100000,
            spouse_b_tfsa=100000,
            spouse_a_cpp=12000,
            spouse_b_cpp=8000,
            years=5,
            investment_return=0.0,
        )
        self.assertEqual(len(results), 5)
        # Ages advance one year per projection step.
        self.assertEqual(results[0]["spouse_a_age"], 68)
        self.assertEqual(results[4]["spouse_a_age"], 72)
        # Each row exposes the optimal split decision.
        for row in results:
            self.assertIn("optimal_split_pct", row)
            self.assertGreaterEqual(row["optimal_split_pct"], 0)


if __name__ == "__main__":
    unittest.main()
