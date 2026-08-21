#!/usr/bin/env python3
"""Unit tests for retirement.py module.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/ -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.retirement import (
    oas_clawback, cpp_benefit, rrif_minimum_withdrawal,
    pension_splitting_available, RetirementState,
    DrawdownOptimizer, project_retirement,
    OAS_ANNUAL_MAX, OAS_CLAWBACK_THRESHOLD,
)


class TestOASClawback(unittest.TestCase):
    """Test OAS clawback calculation."""

    def test_below_threshold_no_clawback(self):
        """Income below threshold: no clawback."""
        result = oas_clawback(80000)
        self.assertEqual(result['clawback_amount'], 0)
        self.assertEqual(result['net_oas'], OAS_ANNUAL_MAX)

    def test_above_threshold_partial_clawback(self):
        """Income above threshold: 15% clawback on excess."""
        result = oas_clawback(100000)
        excess = 100000 - OAS_CLAWBACK_THRESHOLD
        expected_clawback = min(OAS_ANNUAL_MAX, excess * 0.15)
        self.assertAlmostEqual(result['clawback_amount'], expected_clawback, places=0)
        self.assertGreater(result['clawback_amount'], 0)
        self.assertLess(result['net_oas'], OAS_ANNUAL_MAX)

    def test_very_high_income_full_clawback(self):
        """Very high income: OAS fully clawed back."""
        result = oas_clawback(200000)
        self.assertAlmostEqual(result['clawback_amount'], OAS_ANNUAL_MAX, places=0)
        self.assertAlmostEqual(result['net_oas'], 0, places=0)

    def test_custom_threshold(self):
        """Custom threshold parameter works."""
        result = oas_clawback(90000, threshold=80000)
        self.assertGreater(result['clawback_amount'], 0)


class TestCPPBenefit(unittest.TestCase):
    """Test CPP benefit calculation."""

    def test_cpp_at_65(self):
        """CPP at 65: full benefit."""
        benefit = cpp_benefit(65, year=2026)
        self.assertGreater(benefit, 0)

    def test_cpp_at_60_reduced(self):
        """CPP at 60: 36% reduction (0.6% × 60 months)."""
        benefit_65 = cpp_benefit(65, year=2026)
        benefit_60 = cpp_benefit(60, year=2026)
        expected_ratio = 1 - 60 * 0.006  # 0.64
        self.assertAlmostEqual(benefit_60 / benefit_65, expected_ratio, places=2)

    def test_cpp_at_70_increased(self):
        """CPP at 70: 42% increase (0.7% × 60 months)."""
        benefit_65 = cpp_benefit(65, year=2026)
        benefit_70 = cpp_benefit(70, year=2026)
        expected_ratio = 1 + 60 * 0.007  # 1.42
        self.assertAlmostEqual(benefit_70 / benefit_65, expected_ratio, places=2)

    def test_cpp_clamped_to_60_70(self):
        """CPP start age is clamped to 60-70."""
        benefit_55 = cpp_benefit(55, year=2026)  # Treated as 60
        benefit_75 = cpp_benefit(75, year=2026)  # Treated as 70
        self.assertEqual(benefit_55, cpp_benefit(60, year=2026))
        self.assertEqual(benefit_75, cpp_benefit(70, year=2026))


class TestRRIFMinimumWithdrawal(unittest.TestCase):
    """Test RRIF minimum withdrawal calculation."""

    def test_age_72(self):
        """Age 72: 5.40% minimum withdrawal."""
        withdrawal = rrif_minimum_withdrawal(500000, 72)
        self.assertAlmostEqual(withdrawal, 500000 * 0.0540, places=0)

    def test_age_71(self):
        """Age 71: 5.28% mandatory conversion."""
        withdrawal = rrif_minimum_withdrawal(500000, 71)
        self.assertAlmostEqual(withdrawal, 500000 * 0.0528, places=0)

    def test_age_80(self):
        """Age 80: 6.72%."""
        withdrawal = rrif_minimum_withdrawal(500000, 80)
        self.assertAlmostEqual(withdrawal, 500000 * 0.0672, places=0)

    def test_zero_balance(self):
        """Zero balance: zero withdrawal."""
        self.assertEqual(rrif_minimum_withdrawal(0, 72), 0)


class TestPensionSplitting(unittest.TestCase):
    """Test pension income splitting availability."""

    def test_available_at_65_quebec(self):
        """Quebec: available at 65 with qualifying income."""
        result = pension_splitting_available(65, 'quebec')
        self.assertTrue(result['federal_available'])
        self.assertTrue(result['provincial_available'])

    def test_not_available_at_60_quebec(self):
        """Quebec: NOT available at 60 for provincial (need 65)."""
        result = pension_splitting_available(60, 'quebec')
        self.assertTrue(result['federal_available'])  # Federal: 55+
        self.assertFalse(result['provincial_available'])  # Quebec: 65+

    def test_ontario_55_plus(self):
        """Ontario: both federal and provincial at 55+."""
        result = pension_splitting_available(55, 'ontario')
        self.assertTrue(result['federal_available'])
        self.assertTrue(result['provincial_available'])

    def test_no_qualifying_income(self):
        """No qualifying income: splitting not available."""
        result = pension_splitting_available(65, 'quebec', has_qualifying_income=False)
        self.assertFalse(result['federal_available'])


class TestRetirementState(unittest.TestCase):
    """Test RetirementState dataclass."""

    def test_total_assets(self):
        state = RetirementState(rrif_balance=500000, tfsa_balance=100000,
                                non_reg_balance=200000)
        self.assertAlmostEqual(state.total_assets, 800000)

    def test_compute_cpp(self):
        state = RetirementState(cpp_start_age=65, year=2026)
        cpp = state.compute_cpp()
        self.assertGreater(cpp, 0)


class TestDrawdownOptimizer(unittest.TestCase):
    """Test drawdown optimization."""

    def test_tax_first_minimizes_oas_clawback(self):
        """Tax-first strategy should minimize OAS clawback."""
        state = RetirementState(
            age=67, rrif_balance=600000, tfsa_balance=150000,
            non_reg_balance=200000, non_reg_acb=100000,
            annual_expenses=60000,
            year=2026,
        )
        optimizer = DrawdownOptimizer(investment_return=0.05)
        result = optimizer.optimize_year(state)
        self.assertIn('strategy', result)
        self.assertIn('total_tax', result)
        self.assertIn('oas_clawback', result)

    def test_all_strategies_produce_result(self):
        """All three strategies produce valid results."""
        state = RetirementState(
            age=70, rrif_balance=500000, tfsa_balance=100000,
            non_reg_balance=150000, annual_expenses=50000,
            year=2026,
        )
        optimizer = DrawdownOptimizer(investment_return=0.05)
        for method in ['tax_first', 'deferred_first', 'balanced']:
            if method == 'tax_first':
                result = optimizer._tax_first_strategy(state, 30000)
            elif method == 'deferred_first':
                result = optimizer._deferred_first_strategy(state, 30000)
            else:
                result = optimizer._balanced_strategy(state, 30000)
            self.assertIn('total_tax', result)


class TestProjectRetirement(unittest.TestCase):
    """Test multi-year retirement projection."""

    def test_10_year_projection(self):
        """10-year projection runs and produces results."""
        state = RetirementState(
            age=65, rrif_balance=500000, tfsa_balance=100000,
            non_reg_balance=200000, annual_expenses=50000,
        )
        results = project_retirement(state, years=10, investment_return=0.05)
        self.assertEqual(len(results), 10)
        # Assets should decrease each year (spending > income)
        self.assertLess(results[-1]['total_assets'], state.total_assets)

    def test_projection_includes_age(self):
        """Each year includes age."""
        state = RetirementState(age=65, rrif_balance=300000, annual_expenses=40000)
        results = project_retirement(state, years=5, investment_return=0.05)
        for i, r in enumerate(results):
            self.assertEqual(r['age'], 65 + i)


if __name__ == '__main__':
    unittest.main()

class TestYearVersionedOAS(unittest.TestCase):
    """Test DP#20: year-versioned OAS/CPP data (fallback defaults)."""

    def test_oas_year_2026(self):
        """2026 OAS uses 2026 threshold and amount."""
        result_2026 = oas_clawback(100000, year=2026)
        self.assertEqual(result_2026['threshold'], 95323)
        self.assertEqual(result_2026['threshold'], 95323)

    def test_oas_year_2023_lower_threshold(self):
        """2023 OAS had a lower threshold."""
        result_2023 = oas_clawback(90000, year=2023)
        # 2023 threshold was 83917, so 90000 > 83917 → some clawback
        self.assertEqual(result_2023['threshold'], 83917)
        self.assertTrue(result_2023['clawback_amount'] > 0)

    def test_oas_year_specific_clawback_different(self):
        """Same income, different year → different clawback."""
        result_2023 = oas_clawback(90000, year=2023)
        result_2026 = oas_clawback(90000, year=2026)
        # 2023 has lower threshold, so more clawback
        self.assertGreater(result_2023['clawback_amount'], result_2026['clawback_amount'])

    def test_oas_default_2026_fallback(self):
        """Without year parameter, uses 2026 defaults (DP#13)."""
        result_default = oas_clawback(100000)
        result_2026 = oas_clawback(100000, year=2026)
        self.assertEqual(result_default['threshold'], result_2026['threshold'])

    def test_oas_explicit_override(self):
        """Explicit threshold/amount override year parameter."""
        result = oas_clawback(100000, threshold=80000, oas_amount=7000, year=2026)
        self.assertEqual(result['threshold'], 80000)
        self.assertEqual(result['net_oas'], 7000 - 0.15 * 20000)

    def test_oas_unknown_year(self):
        """Unknown year falls back to 2026 defaults."""
        result = oas_clawback(100000, year=2019)
        # Should use default constants
        self.assertEqual(result['threshold'], 95323)

    def test_cpp_year_2026(self):
        """2026 CPP max benefit at 65."""
        benefit = cpp_benefit(65, year=2026)
        self.assertAlmostEqual(benefit, 18092, places=0)

    def test_cpp_year_2023(self):
        """2023 CPP had lower max benefit."""
        benefit_2023 = cpp_benefit(65, year=2023)
        benefit_2026 = cpp_benefit(65, year=2026)
        self.assertLess(benefit_2023, benefit_2026)

    def test_cpp_default_fallback(self):
        """Without year parameter, raises ValueError (DP#13: year required)."""
        with self.assertRaises(ValueError):
            cpp_benefit(65)
