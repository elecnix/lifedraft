#!/usr/bin/env python3
"""Unit tests for quebec_deduction.py module.

Run with: python3 -m pytest tests/ -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.provinces.quebec.quebec_deduction import (
    quebec_interest_deduction,
    QuebecDeductionTracker,
    quebec_sm_portfolio_optimization,
)


class TestQuebecInterestDeduction(unittest.TestCase):
    """Test the pure function for Quebec interest deduction limit."""

    def test_federal_full_deduction(self):
        """Federal: full interest is deductible."""
        result = quebec_interest_deduction(10000, 3000)
        self.assertAlmostEqual(result['federal_deductible'], 10000)

    def test_quebec_limited_by_income(self):
        """Quebec: deduction limited to investment income."""
        result = quebec_interest_deduction(10000, 3000)
        self.assertAlmostEqual(result['qc_deductible'], 3000)
        self.assertAlmostEqual(result['new_carry_forward'], 7000)

    def test_quebec_when_income_exceeds_interest(self):
        """Quebec: full deduction when income exceeds interest."""
        result = quebec_interest_deduction(5000, 10000)
        self.assertAlmostEqual(result['qc_deductible'], 5000)
        self.assertAlmostEqual(result['new_carry_forward'], 0)

    def test_carry_forward_used(self):
        """Quebec: carry-forward from prior year increases this year's limit."""
        result = quebec_interest_deduction(10000, 3000, carry_forward_prior=5000)
        # Limit = $3k income + $5k carry-forward = $8k
        self.assertAlmostEqual(result['qc_deductible'], 8000)
        self.assertAlmostEqual(result['new_carry_forward'], 2000)

    def test_capital_gains_count_as_income(self):
        """Quebec: realized capital gains count toward deduction limit."""
        result = quebec_interest_deduction(
            10000, 3000, capital_gains_realized=10000)
        # Income = $3k + ($10k × 50% CG) = $8k
        self.assertAlmostEqual(result['qc_deductible'], 8000)
        self.assertAlmostEqual(result['new_carry_forward'], 2000)

    def test_zero_interest(self):
        """Zero interest: zero deduction."""
        result = quebec_interest_deduction(0, 3000)
        self.assertEqual(result['federal_deductible'], 0)
        self.assertEqual(result['qc_deductible'], 0)

    def test_zero_income_no_carry(self):
        """Zero income and no carry-forward: Quebec deducts nothing."""
        result = quebec_interest_deduction(10000, 0)
        self.assertAlmostEqual(result['qc_deductible'], 0)
        self.assertAlmostEqual(result['new_carry_forward'], 10000)


class TestQuebecDeductionTracker(unittest.TestCase):
    """Test year-by-year Quebec deduction tracking."""

    def test_year1_income_deficit(self):
        """Year 1: income deficit carries forward."""
        tracker = QuebecDeductionTracker()
        result = tracker.process_year(2026, 10000, 3000)
        self.assertAlmostEqual(result['qc_deductible'], 3000)
        self.assertAlmostEqual(tracker.carry_forward, 7000)

    def test_year2_carry_forward_used(self):
        """Year 2: carry-forward from year 1 is used."""
        tracker = QuebecDeductionTracker()
        tracker.process_year(2026, 10000, 3000)
        # Year 2: interest $10k, income $10k, carry-forward $7k
        result = tracker.process_year(2027, 10000, 10000)
        # Limit = $10k + $7k carry = $17k, but interest only $10k
        self.assertAlmostEqual(result['qc_deductible'], 10000)
        self.assertAlmostEqual(tracker.carry_forward, 0)

    def test_multi_year_shortfall(self):
        """Multi-year tracking of shortfall."""
        tracker = QuebecDeductionTracker()
        tracker.process_year(2026, 8000, 2000)
        tracker.process_year(2027, 8000, 3000)
        # Total QC shortfall across years
        self.assertGreater(tracker.total_qc_deduction_shortfall(), 0)

    def test_summary_runs(self):
        """Summary produces output."""
        tracker = QuebecDeductionTracker()
        tracker.process_year(2026, 10000, 3000)
        summary = tracker.summary()
        self.assertIn("QUEBEC", summary)
        self.assertIn("2026", summary)


class TestQuebecSMPortfolioOptimization(unittest.TestCase):
    """Test SM portfolio optimization for Quebec deduction."""

    def test_income_covers_interest(self):
        """When income covers interest, no change needed."""
        result = quebec_sm_portfolio_optimization(
            heloc_interest=5000,
            current_dividend_income=5000,
        )
        self.assertTrue(result['qc_deduction_fully_covered'])
        self.assertEqual(result['income_gap'], 0)

    def test_income_gap_needs_dividends(self):
        """Income gap: dividend ETF recommendation."""
        result = quebec_sm_portfolio_optimization(
            heloc_interest=10000,
            current_dividend_income=2000,
            current_interest_income=1000,
        )
        self.assertFalse(result['qc_deduction_fully_covered'])
        self.assertGreater(result['income_gap'], 0)
        self.assertEqual(len(result['options']), 2)  # dividend + realize CG

    def test_zero_current_income(self):
        """Zero current income: full gap."""
        result = quebec_sm_portfolio_optimization(
            heloc_interest=8000,
        )
        self.assertFalse(result['qc_deduction_fully_covered'])
        self.assertAlmostEqual(result['income_gap'], 8000)


class TestQuebecScheduleL(unittest.TestCase):
    """Test Schedule L line-by-line mapping (DP#66)."""

    def test_schedule_l_lines_present(self):
        """Schedule L line items are included in the result."""
        result = quebec_interest_deduction(
            heloc_interest=10000, investment_income=3000,
            eligible_dividend_income=2000,
            interest_income=1000,
        )
        self.assertIn('schedule_l', result)
        sl = result['schedule_l']
        self.assertEqual(sl['line_1_interest_on_borrowed_money'], 10000)
        self.assertEqual(sl['line_2_eligible_dividends'], 2000)
        self.assertEqual(sl['line_3_non_eligible_dividends'], 0)
        self.assertEqual(sl['line_4_interest_and_other'], 1000)
        self.assertEqual(sl['line_5_taxable_capital_gains'], 0)
        self.assertEqual(sl['line_6_net_rental_income'], 0)
        self.assertEqual(sl['line_7_total_investment_income'], 3000)  # 2000 + 0 + 1000 + 0 + 0
        self.assertEqual(sl['line_8_deductible_interest'], 3000)  # Min(interest, income)
        self.assertEqual(sl['line_9_carry_forward'], 7000)

    def test_schedule_l_with_capital_gains(self):
        """Schedule L correctly maps capital gains to line 5."""
        result = quebec_interest_deduction(
            heloc_interest=10000, investment_income=3000,
            capital_gains_realized=8000, capital_gains_inclusion=0.50,
        )
        sl = result['schedule_l']
        self.assertAlmostEqual(sl['line_5_taxable_capital_gains'], 4000)  # 50% of 8000
        self.assertAlmostEqual(sl['line_7_total_investment_income'], 7000)  # 3000 + 4000

    def test_schedule_l_with_detailed_income(self):
        """Schedule L correctly separates income types when detailed breakdown provided."""
        result = quebec_interest_deduction(
            heloc_interest=15000, investment_income=0,  # Aggregate overridden
            eligible_dividend_income=4000,
            non_eligible_dividend_income=1000,
            interest_income=2000,
            rental_income_net=3000,
            capital_gains_realized=10000,
        )
        sl = result['schedule_l']
        self.assertEqual(sl['line_2_eligible_dividends'], 4000)
        self.assertEqual(sl['line_3_non_eligible_dividends'], 1000)
        self.assertEqual(sl['line_4_interest_and_other'], 2000)
        self.assertAlmostEqual(sl['line_5_taxable_capital_gains'], 5000)  # 50% of 10000
        self.assertEqual(sl['line_6_net_rental_income'], 3000)
        self.assertAlmostEqual(sl['line_7_total_investment_income'], 15000)  # 4+1+2+5+3
        self.assertAlmostEqual(sl['line_8_deductible_interest'], 15000)  # Income covers interest
        self.assertAlmostEqual(sl['line_9_carry_forward'], 0)

    def test_schedule_l_with_rental_income(self):
        """Rental income is included in the deduction limit."""
        result = quebec_interest_deduction(
            heloc_interest=10000, investment_income=0,
            rental_income_net=8000,
        )
        self.assertAlmostEqual(result['qc_deductible'], 8000)
        self.assertAlmostEqual(result['new_carry_forward'], 2000)

    def test_carry_forward_two_step_consumption(self):
        """Carry-forward from year 1 is fully consumed in year 2 (two-step)."""
        tracker = QuebecDeductionTracker()
        # Year 1: $10k interest, $3k income → $3k deductible, $7k carry forward
        r1 = tracker.process_year(2026, 10000, 3000)
        self.assertAlmostEqual(r1['qc_deductible'], 3000)
        self.assertAlmostEqual(tracker.carry_forward, 7000)
        
        # Year 2: $10k interest, $10k income + $7k carry = $17k limit → $10k deductible
        # All carry-forward is consumed
        r2 = tracker.process_year(2027, 10000, 10000)
        self.assertAlmostEqual(r2['qc_deductible'], 10000)
        self.assertAlmostEqual(tracker.carry_forward, 0)
        # Total deducted: $3k + $10k = $13k out of $20k total interest

    def test_zero_investment_income_maximum_carry_forward(self):
        """Zero investment income with large HELOC interest: maximum carry-forward."""
        result = quebec_interest_deduction(
            heloc_interest=15000,
            investment_income=0,
            carry_forward_prior=0,
        )
        self.assertAlmostEqual(result['qc_deductible'], 0)
        self.assertAlmostEqual(result['new_carry_forward'], 15000)
        self.assertAlmostEqual(result['income_deficit'], 15000)

    def test_capital_gains_with_no_other_income(self):
        """Capital gains in a year with no other investment income."""
        result = quebec_interest_deduction(
            heloc_interest=10000,
            investment_income=0,
            capital_gains_realized=20000,
            capital_gains_inclusion=0.50,
        )
        # Taxable CG = $10k, so $10k of $10k interest is deductible
        self.assertAlmostEqual(result['qc_deductible'], 10000)
        self.assertAlmostEqual(result['new_carry_forward'], 0)

    def test_eligible_dividends_cover_interest(self):
        """Eligible dividends fully cover HELOC interest deduction."""
        result = quebec_interest_deduction(
            heloc_interest=5000,
            investment_income=0,
            eligible_dividend_income=5000,
        )
        self.assertAlmostEqual(result['qc_deductible'], 5000)
        self.assertAlmostEqual(result['new_carry_forward'], 0)

    def test_mixed_income_types(self):
        """Mixed income types: dividends + interest + CG + rental."""
        result = quebec_interest_deduction(
            heloc_interest=20000,
            investment_income=0,  # Overridden by detailed breakdown
            eligible_dividend_income=3000,
            non_eligible_dividend_income=1000,
            interest_income=2000,
            rental_income_net=1500,
            capital_gains_realized=10000,
        )
        sl = result['schedule_l']
        # Total income = 3000 + 1000 + 2000 + 5000 + 1500 = 12500
        self.assertAlmostEqual(sl['line_7_total_investment_income'], 12500)
        # Deductible = min(20000, 12500) = 12500
        self.assertAlmostEqual(sl['line_8_deductible_interest'], 12500)
        # Carry forward = 20000 - 12500 = 7500
        self.assertAlmostEqual(sl['line_9_carry_forward'], 7500)


if __name__ == '__main__':
    unittest.main()
