#!/usr/bin/env python3
"""Tests for issue #279: replace investment_return=float defaults with required parameter.

DP#13/DP#21: Hardcoded 7%/5% investment return defaults mask configuration errors
and make multi-scenario analysis unreliable. All Canada module functions must
require explicit investment_return, raising ValueError if omitted.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest


class TestInvestmentReturnRequired(unittest.TestCase):
    """Verify that investment_return is now a required parameter,
    not an opinionated default, in all Canada module functions."""

    def test_cashout_compute_per_dollar_benefit_requires_return(self):
        from countries.canada.cashout_optimizer import compute_per_dollar_benefit
        with self.assertRaises(ValueError):
            compute_per_dollar_benefit(1.0, 0.50)

    def test_cashout_compute_tfsa_per_dollar_requires_return(self):
        from countries.canada.cashout_optimizer import compute_tfsa_per_dollar
        with self.assertRaises(ValueError):
            compute_tfsa_per_dollar()

    def test_cashout_compute_fhsa_per_dollar_requires_return(self):
        from countries.canada.cashout_optimizer import compute_fhsa_per_dollar
        with self.assertRaises(ValueError):
            compute_fhsa_per_dollar(1.0, 0.50)

    def test_cashout_compute_nonreg_per_dollar_requires_return(self):
        from countries.canada.cashout_optimizer import compute_nonreg_per_dollar
        with self.assertRaises(ValueError):
            compute_nonreg_per_dollar()

    def test_cashout_compute_min_extraction_requires_return(self):
        from countries.canada.cashout_optimizer import compute_min_extraction
        with self.assertRaises(ValueError):
            compute_min_extraction({'property': {'house_value': 500000, 'mortgage_balance': 300000}})

    def test_fhsa_double_deduction_requires_return(self):
        from countries.canada.fhsa import fhsa_double_deduction_analysis
        with self.assertRaises(ValueError):
            fhsa_double_deduction_analysis(130000, 0.4571)

    def test_ird_refinance_requires_return(self):
        from countries.canada.ird_penalty import refinance_with_penalty_analysis
        with self.assertRaises(ValueError):
            refinance_with_penalty_analysis(300000, 0.05, 36)

    def test_ird_readvanceable_requires_return(self):
        from countries.canada.ird_penalty import break_for_readvanceable_analysis
        with self.assertRaises(ValueError):
            break_for_readvanceable_analysis(400000, 0.05, 36)

    def test_pension_split_requires_return(self):
        from countries.canada.pension_split_optimizer import project_pension_split_retirement
        with self.assertRaises((ValueError, TypeError)):
            project_pension_split_retirement(spouse_a_age=72, spouse_b_age=70, spouse_a_rrif=400000, spouse_b_rrif=100000, spouse_a_tfsa=50000, spouse_b_tfsa=50000)

    def test_retirement_drawdown_requires_return(self):
        from countries.canada.retirement import DrawdownOptimizer
        with self.assertRaises(ValueError):
            DrawdownOptimizer()

    def test_retirement_project_requires_return(self):
        from countries.canada.retirement import project_retirement, RetirementState
        with self.assertRaises(ValueError):
            project_retirement(RetirementState())

    def test_hbp_first_home_requires_return(self):
        from countries.canada.hbp_rules import compare_first_home_strategies
        with self.assertRaises(ValueError):
            compare_first_home_strategies(income=75000, marginal_rate=0.35)

    def test_explicit_return_works(self):
        """When investment_return is explicitly provided, functions work correctly."""
        from countries.canada.cashout_optimizer import compute_per_dollar_benefit
        result = compute_per_dollar_benefit(1.0, 0.50, investment_return=0.07)
        self.assertGreater(result, 0)

    def test_explicit_tfsa_return_works(self):
        from countries.canada.cashout_optimizer import compute_tfsa_per_dollar
        result = compute_tfsa_per_dollar(investment_return=0.07)
        self.assertGreater(result, 1)

    def test_source_no_hardcoded_defaults(self):
        """Verify source files no longer contain opinionated investment_return defaults."""
        import countries.canada.cashout_optimizer as co
        import countries.canada.fhsa as fh
        import countries.canada.ird_penalty as ird
        import countries.canada.pension_split_optimizer as pso
        import countries.canada.retirement as ret
        import countries.canada.hbp_rules as hbp

        for module_name, module in [
            ('cashout_optimizer', co),
            ('fhsa', fh),
            ('ird_penalty', ird),
            ('pension_split_optimizer', pso),
            ('retirement', ret),
            ('hbp_rules', hbp),
        ]:
            with open(module.__file__) as f:
                source = f.read()
            # Check that "= 0.07" and "= 0.05" don't appear as investment_return defaults
            self.assertNotIn("investment_return: float = 0.07", source,
                             f"{module_name} still has investment_return=0.07 default")
            self.assertNotIn("investment_return: float = 0.05", source,
                             f"{module_name} still has investment_return=0.05 default")


if __name__ == '__main__':
    unittest.main()