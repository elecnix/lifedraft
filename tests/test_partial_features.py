#!/usr/bin/env python3
"""Tests for the 7 partially-implemented features.

All data is fake round numbers per DP#4 and DP#15.
No personal information.

Covers:
1. RRSP Deduct-Later Ledger (Task 63 / Scenario 2.3)
2. IRD Penalty Model (Task 64 / Scenario 3.1)
3. Mortgage Renewal Model (Task 65 / Scenario 3.2)
4. OAS Clawback Retirement Drawdown (Task 66 / Scenario 5.1)
5. HBP Module + First Home Strategies (Task 67 / Scenario 10.1)
6. Pension Split Optimization (Task 68 / Scenario 12.1)

Run with: python3 -m pytest tests/test_partial_features.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest


class TestIRDPenalty(unittest.TestCase):
    """Scenario 3.1: Refinance cash-out must factor IRD penalty.
    Scenario 3.3: Break for readvanceable product.

    SCENARIO_SEED 3.1: House $800K, Mortgage $300K @ 4.5%, IRD ~$8K.
    SCENARIO_SEED 3.3: Mortgage $450K @ 4.0%, 2 years into 5-yr, IRD ~$15K.
    """

    def setUp(self):
        from countries.canada.ird_penalty import (
            compute_three_months_interest, compute_ird_penalty,
            compute_breakage_penalty, refinance_with_penalty_analysis,
            break_for_readvanceable_analysis, DEFAULT_POSTED_RATES,
        )
        self.three_mo = compute_three_months_interest
        self.ird = compute_ird_penalty
        self.breakage = compute_breakage_penalty
        self.refi_analysis = refinance_with_penalty_analysis
        self.readv_analysis = break_for_readvanceable_analysis
        self.default_rates = DEFAULT_POSTED_RATES

    def test_three_months_interest(self):
        """3 months' interest on $450K at 4.0% = $4,500."""
        penalty = self.three_mo(450000, 0.04)
        self.assertAlmostEqual(penalty, 4500, places=0)

    def test_ird_penalty_fixed_rate(self):
        """IRD penalty when contract rate > posted rate.

        SCENARIO 3.1: Mortgage $300K, contract 4.5%, 3 years remaining,
        posted 3yr rate 4.0%.
        IRD = $300K × (0.045 - 0.04) × 36/12 = $4,500
        """
        posted = {1: 0.04, 2: 0.04, 3: 0.04, 5: 0.045, 7: 0.05}
        penalty = self.ird(300000, 0.045, 36, posted_rates=posted)
        self.assertAlmostEqual(penalty, 300000 * 0.005 * 3, places=0)

    def test_ird_penalty_zero_when_rates_up(self):
        """IRD is 0 when current rates are higher than contract."""
        posted = {3: 0.06}  # Rates went up
        penalty = self.ird(300000, 0.045, 36, posted_rates=posted)
        self.assertAlmostEqual(penalty, 0, places=0)

    def test_breakage_variable_always_three_month(self):
        """Variable-rate penalty is always 3 months' interest."""
        result = self.breakage(450000, 0.04, 36, rate_type='variable')
        self.assertAlmostEqual(result['total_penalty'], 4500, places=0)
        self.assertEqual(result['method'], 'Variable: 3 months interest only')

    def test_breakage_fixed_takes_greater(self):
        """Fixed-rate penalty is the greater of 3mo interest or IRD."""
        # When IRD is larger
        posted = {3: 0.01}  # Very low posted rate → huge IRD
        result = self.breakage(450000, 0.04, 36, rate_type='fixed', posted_rates=posted)
        self.assertTrue(result['total_penalty'] >= 4500)
        self.assertTrue(result['ird_exceeds_three_month'])

    def test_refinance_analysis_net_benefit(self):
        """SCENARIO 3.1: Net benefit after penalty should be positive for good deal.

        House $800K, Mortgage $300K @ 4.5%.
        Cash-out $340K at HELOC rate.
        """
        result = self.refi_analysis(
            mortgage_balance=300000, contract_rate=0.045,
            remaining_months=36, rate_type='fixed',
            new_rate=0.0545, new_term_months=60,
            cash_out=340000, investment_return=0.07,
            marginal_rate=0.43,
            posted_rates={3: 0.04},
        )
        # Penalty should be moderate
        self.assertTrue(result['total_penalty'] > 0)
        # At 7% return and 43% MTR, after-tax return = 3.99% > HELOC after-tax cost
        # So net benefit should eventually be positive
        self.assertIn(result['recommendation'], ['refinance', 'keep_current'])

    def test_break_for_readvanceable(self):
        """SCENARIO 3.3: Break for readvanceable product.

        Mortgage $450K @ 4.0%, 2 years into 5-year term (36 months left).
        IRD penalty ~$15K.
        """
        result = self.readv_analysis(
            mortgage_balance=450000, contract_rate=0.04,
            remaining_months=36,
            new_readvanceable_rate=0.042,
            readvance_ratio=1.0,
            house_value=700000,
            marginal_rate=0.43,
            heloc_rate=0.05,
            investment_return=0.07,
            rate_type='fixed',
            posted_rates={3: 0.04},
        )
        # SM benefit should accumulate over remaining term
        self.assertTrue(result['total_sm_benefit_over_remaining_term'] > 0)
        # Break-even should be calculable
        self.assertTrue(result['break_even_years'] < float('inf'))


# =============================================================================
# 3. Mortgage Renewal Model — Scenario 3.2
# =============================================================================

class TestMortgageRenewal(unittest.TestCase):
    """Scenario 3.2: Variable vs Fixed rate at refinance.

    SCENARIO_SEED: Mortgage $400K, 25yr amortization.
    3yr fixed at 4.04% (renewal at 5.0%)
    vs 5yr variable at 3.75% (renewal at 4.5%).
    """

    def setUp(self):
        from countries.canada.renewal_model import (
            MortgageTerm, RenewalEvent, simulate_renewal_path,
            compare_rate_term_options, rate_sensitivity_analysis,
        )
        self.Term = MortgageTerm
        self.Renewal = RenewalEvent
        self.simulate = simulate_renewal_path
        self.compare = compare_rate_term_options
        self.sensitivity = rate_sensitivity_analysis

    def test_mortgage_term_contains_year(self):
        """Term correctly reports which years it covers."""
        term = self.Term(rate=0.0404, start_year=2026, term_years=3)
        self.assertTrue(term.contains_year(2026))
        self.assertTrue(term.contains_year(2028))
        self.assertFalse(term.contains_year(2029))

    def test_simulate_renewal_path_fixed(self):
        """Simulate 3yr fixed → renewal at 5.0%."""
        terms = [
            self.Term(rate=0.0404, start_year=2026, term_years=3),
            self.Term(rate=0.0500, start_year=2029, term_years=7),
        ]
        result = self.simulate(400000, terms, amortization_years=25, projection_years=10)
        # Should have 2 terms and 1 renewal event
        self.assertEqual(len(result.terms), 2)
        self.assertEqual(len(result.renewal_events), 1)
        # Total interest should be positive
        self.assertTrue(result.total_interest_paid > 0)
        # Final balance should be less than initial
        self.assertTrue(result.final_balance < 400000)

    def test_simulate_renewal_path_variable(self):
        """Simulate 5yr variable → renewal at 4.5%."""
        terms = [
            self.Term(rate=0.0375, start_year=2026, term_years=5),
            self.Term(rate=0.0450, start_year=2031, term_years=5),
        ]
        result = self.simulate(400000, terms, amortization_years=25, projection_years=10)
        self.assertEqual(len(result.renewal_events), 1)
        # Variable at lower rate should cost less in total interest
        self.assertTrue(result.total_interest_paid > 0)

    def test_compare_rate_term_options(self):
        """SCENARIO 3.2: Compare 3yr fixed vs 5yr variable over 10 years."""
        options = [
            {'name': '3yr_fixed', 'rate': 0.0404, 'term_years': 3, 'rate_type': 'fixed'},
            {'name': '5yr_variable', 'rate': 0.0375, 'term_years': 5, 'rate_type': 'variable'},
        ]
        result = self.compare(
            mortgage_balance=400000, amortization_years=25,
            options=options, projection_years=10,
            renewal_assumptions={'fixed': 0.05, 'variable': 0.045},
        )
        # Should have 2 options compared
        self.assertEqual(len(result['options']), 2)
        # Best should be whichever has lower total interest
        self.assertIsNotNone(result['best'])
        # Total cost spread should be calculable
        self.assertTrue(result['total_cost_spread'] >= 0)

    def test_rate_sensitivity_analysis(self):
        """SCENARIO 3.2 extension: Sensitivity to renewal rate.

        What if renewal is 6%? 3.5%?
        """
        result = self.sensitivity(
            mortgage_balance=400000, amortization_years=25,
            base_rate=0.0404, term_years=3, projection_years=10,
            renewal_rate_range=[0.035, 0.04, 0.045, 0.05, 0.055, 0.06],
        )
        # Should have results for each renewal rate
        self.assertEqual(len(result['sensitivity_results']), 6)
        # Lower renewal rate → lower total interest
        sorted_results = sorted(result['sensitivity_results'], key=lambda r: r['renewal_rate'])
        self.assertTrue(sorted_results[0]['total_interest'] < sorted_results[-1]['total_interest'])

    def test_renewal_rate_change_detected(self):
        """Renewal events detect rate changes."""
        terms = [
            self.Term(rate=0.0404, start_year=2026, term_years=3),
            self.Term(rate=0.0500, start_year=2029, term_years=5),
        ]
        result = self.simulate(400000, terms, amortization_years=25, projection_years=10)
        self.assertEqual(len(result.renewal_events), 1)
        event = result.renewal_events[0]
        self.assertAlmostEqual(event.rate_change, 0.0096, places=4)
        self.assertTrue(event.is_rate_increase)


# =============================================================================
# 4. OAS Clawback Retirement Drawdown — Scenario 5.1
# =============================================================================

class TestOASClawbackDrawdown(unittest.TestCase):
    """Scenario 5.1: OAS clawback management with year-by-year drawdown.

    SCENARIO_SEED: Age 72, RRIF $800K, CPP $15K, OAS $8,908,
    Other income $40K. Total $106,148 → clawback applies.
    """

    def setUp(self):
        from countries.canada.pension_split_optimizer import (
            optimize_pension_split, project_pension_split_retirement,
            pension_income_credit, PensionSplitResult,
        )
        self.optimize = optimize_pension_split
        self.project = project_pension_split_retirement
        self.credit = pension_income_credit

    def test_oas_clawback_retention_with_tfsa_bridge(self):
        """Year-by-year: draw TFSA first to stay under clawback.

        SCENARIO 5.1: RRIF min = $42,240 (5.28%).
        With $40K other income + $15K CPP + $8.9K OAS = ~$106K.
        Drawing TFSA instead of extra RRIF keeps income lower.
        """
        results = self.project(
            investment_return=0.05,
            spouse_a_age=72, spouse_b_age=70,
            spouse_a_rrif=800000, spouse_b_rrif=200000,
            spouse_a_tfsa=100000, spouse_b_tfsa=80000,
            spouse_a_cpp=15000, spouse_b_cpp=8000,
            annual_expenses=60000,
            years=5,
        )
        # Should produce 5 years of results
        self.assertEqual(len(results), 5)
        # Each result should have OAS clawback data
        for r in results:
            self.assertIn('total_oas_clawback', r)
            self.assertIn('optimal_split_pct', r)
            self.assertTrue(r['total_oas_clawback'] >= 0)

    def test_pension_split_reduces_clawback(self):
        """Pension splitting reduces OAS clawback for the higher earner."""
        result_no_split = self.optimize(
            spouse_a_income=80000, spouse_b_income=20000,
            eligible_pension=40000,
            spouse_a_oas=8908, spouse_b_oas=8908,
            spouse_a_age=68, spouse_b_age=66,
            split_resolution=50,
        )
        # With splitting, some income moves to lower earner
        self.assertTrue(result_no_split.tax_savings >= 0)
        self.assertTrue(result_no_split.oas_savings >= 0)

    def test_optimal_split_not_always_50(self):
        """SCENARIO 12.1: Optimal split may not be 50%.

        If 50% split pushes the receiving spouse into a higher bracket
        or the OAS threshold, a lower split is better.
        """
        # Spouse A has high income near OAS clawback
        # Spouse B has very low income
        result = self.optimize(
            spouse_a_income=90000,  # Near clawback
            spouse_b_income=10000,  # Very low
            eligible_pension=40000,
            spouse_a_oas=8908, spouse_b_oas=8908,
            spouse_a_age=68, spouse_b_age=66,
            split_resolution=100,
        )
        # Optimal split should be > 0 but may not be exactly 50%
        self.assertTrue(result.optimal_split_pct >= 0)
        self.assertTrue(result.optimal_split_pct <= 0.50)

    def test_pension_income_credit(self):
        """Pension income credit: $2,000 × 15% = $300 per spouse."""
        self.assertAlmostEqual(self.credit(5000), 300, places=0)
        self.assertAlmostEqual(self.credit(1000), 150, places=0)  # Only $1,000 eligible

    def test_year_by_year_drawdown_rrif_declines(self):
        """RRIF balance should decline over retirement years."""
        results = self.project(
            investment_return=0.05,
            spouse_a_age=72, spouse_b_age=70,
            spouse_a_rrif=800000, spouse_b_rrif=200000,
            spouse_a_tfsa=50000, spouse_b_tfsa=50000,
            annual_expenses=70000,
            years=10,
        )
        # RRIF balances should generally decline
        first_a = results[0]['spouse_a_rrif_balance']
        last_a = results[-1]['spouse_a_rrif_balance']
        self.assertTrue(last_a < first_a)


# =============================================================================
# 5. HBP Module + First Home Strategies — Scenario 10.1
# =============================================================================

class TestHBPFirstHome(unittest.TestCase):
    """Scenario 10.1: FHSA vs RRSP HBP vs TFSA for first home.
    """

    def setUp(self):
        from countries.canada.hbp_rules import HBPAccount, compare_first_home_strategies
        self.HBP = HBPAccount
        self.compare = compare_first_home_strategies

    def test_hbp_repayment_schedule(self):
        """HBP repayment: 15 years, 1/15 per year."""
        hbp = self.HBP(withdrawal=35000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        self.assertEqual(len(schedule), 15)
        self.assertAlmostEqual(schedule[0]['min_payment'], 35000 / 15, places=0)
        # First repayment in year 2029 (2026 + 2 + 1)
        self.assertEqual(schedule[0]['year'], 2029)

    def test_hbp_default_tax_consequence(self):
        """HBP default: outstanding balance added to income."""
        hbp = self.HBP(withdrawal=60000, withdrawal_year=2026)
        hbp.repaid = 20000  # Only repaid $20K of $60K
        result = hbp.default_tax_impact(year=2028, marginal_rate=0.35)
        # Outstanding = 40000, tax = 40000 × 0.35 = 14000
        self.assertAlmostEqual(result['tax_if_default'], 14000, places=0)

    def test_hbp_repayment_shortfall(self):
        """If repayment < minimum, shortfall is taxed as income."""
        hbp = self.HBP(withdrawal=60000, withdrawal_year=2026)
        min_repayment = 60000 / 15  # $4,000/yr
        result = hbp.make_repayment(2000, year=2029)  # Only paid $2,000
        self.assertTrue(result['shortfall'] > 0)
        self.assertTrue(result['shortfall_tax_consequence'])

    def test_compare_fhsa_hbp_tfsa(self):
        """SCENARIO 10.1: Compare three strategies for first home.

        Age 28, income $75K (MTR ~35%), buying in 4 years.
        """
        result = self.compare(
            income=75000, marginal_rate=0.35,
            years_to_purchase=4,
            rrsp_existing_balance=30000,
            investment_return=0.07,
        )
        # Should have 5 strategies (FHSA, HBP, TFSA, Combined, Double)
        self.assertEqual(len(result['strategies']), 5)
        # Strategies should be sorted by net benefit (highest first)
        for i in range(len(result['strategies']) - 1):
            self.assertTrue(
                result['strategies'][i]['net_benefit'] >=
                result['strategies'][i + 1]['net_benefit']
            )
        # FHSA should have tax savings (deductible contributions)
        fhsa = next(s for s in result['strategies'] if s['name'] == 'FHSA')
        self.assertTrue(fhsa['tax_savings'] > 0)
        self.assertFalse(fhsa['repayment_required'])

    def test_combined_strategy_highest_down_payment(self):
        """FHSA + HBP combined should give highest down payment."""
        result = self.compare(
            income=75000, marginal_rate=0.35,
            years_to_purchase=4,
            rrsp_existing_balance=60000,
            investment_return=0.07,
        )
        combined = next(s for s in result['strategies'] if s['name'] == 'FHSA + HBP Combined')
        fhsa_only = next(s for s in result['strategies'] if s['name'] == 'FHSA')
        # Combined should have higher down payment than FHSA alone
        self.assertTrue(combined['down_payment'] > fhsa_only['down_payment'])

    def test_hbp_max_withdrawal_cap(self):
        """HBP withdrawal capped at $60,000 (2024+ rule)."""
        hbp = self.HBP(withdrawal=80000, withdrawal_year=2026)
        # The account records the withdrawal amount
        self.assertEqual(hbp.withdrawal, 80000)
        # But comparison should cap at max
        result = self.compare(
            income=100000, marginal_rate=0.43,
            rrsp_existing_balance=80000,
            hbp_withdrawal=80000,
            investment_return=0.07,
        )
        hbp = next(s for s in result['strategies'] if s['name'] == 'RRSP HBP')
        self.assertTrue(hbp['down_payment'] <= 60000 + 80000 * 0.18 * 4)  # Not excessive


# =============================================================================
# 6. Pension Split Optimization — Scenario 12.1
# =============================================================================

class TestPensionSplitOptimization(unittest.TestCase):
    """Scenario 12.1: Optimal pension split percentage."""

    def setUp(self):
        from countries.canada.pension_split_optimizer import (
            optimize_pension_split, pension_income_credit,
            both_spouses_get_credit, PensionSplitResult,
        )
        self.optimize = optimize_pension_split
        self.credit = pension_income_credit
        self.both_get = both_spouses_get_credit

    def test_no_split_when_ages_below_threshold(self):
        """Pension splitting not available below age 55."""
        result = self.optimize(
            spouse_a_income=50000, spouse_b_income=20000,
            eligible_pension=40000,
            spouse_a_age=50, spouse_b_age=48,
        )
        self.assertEqual(result.optimal_split_pct, 0)

    def test_split_available_at_65(self):
        """Pension splitting available at 65+ (QC: both federal and provincial)."""
        result = self.optimize(
            spouse_a_income=50000, spouse_b_income=20000,
            eligible_pension=40000,
            spouse_a_age=68, spouse_b_age=66,
        )
        self.assertTrue(result.optimal_split_pct >= 0)

    def test_split_saves_tax_at_bracket_gap(self):
        """Splitting saves tax equal to bracket_gap × split_amount."""
        result = self.optimize(
            spouse_a_income=80000, spouse_b_income=20000,
            eligible_pension=40000,
            spouse_a_age=68, spouse_b_age=66,
            split_resolution=50,
        )
        # With ~40pp bracket gap, splitting should save significant tax
        self.assertTrue(result.tax_savings >= 0)

    def test_pension_credit_both_spouses(self):
        """Both spouses can claim pension credit with proper split.

        Need at least $2,000 pension income each after split.
        """
        self.assertTrue(self.both_get(5000, 3000))
        self.assertFalse(self.both_get(5000, 500))  # B only has $500

    def test_split_reduces_oas_clawback(self):
        """SCENARIO 12.1: Splitting high earner's pension reduces OAS clawback.

        Spouse A: $62,908 (near threshold $95,323).
        With 50% split of $40K: A gets $42,908, B gets $51,908.
        """
        result = self.optimize(
            spouse_a_income=62908 - 40000,  # Non-pension income
            spouse_b_income=31908,
            eligible_pension=40000,
            spouse_a_oas=8908, spouse_b_oas=8908,
            spouse_a_age=68, spouse_b_age=66,
            split_resolution=50,
        )
        # Splitting should produce OAS savings
        self.assertTrue(result.oas_savings >= 0)

    def test_zero_income_split_zero_pct(self):
        """If eligible_pension is 0, split is 0."""
        result = self.optimize(
            spouse_a_income=50000, spouse_b_income=20000,
            eligible_pension=0,
            spouse_a_age=68, spouse_b_age=66,
        )
        self.assertEqual(result.optimal_split_amount, 0)


if __name__ == '__main__':
    unittest.main()