#!/usr/bin/env python3
"""Tests for ird_penalty.py module — IRD Penalty / Mortgage Breakage (DP#17).

Issue #57: The IRD penalty module had zero test coverage. Every rule path
was untested. This test suite exercises all implemented rule paths:

1. Three months' interest penalty (variable rate)
2. IRD penalty calculation (fixed rate)
3. Breakage penalty = max(3mo interest, IRD)
4. Discounted vs posted rate IRD comparison
5. Refinance break-even analysis
6. Readvanceable break-even analysis
7. Edge cases: zero balance, negative rate diff, expired term

Run with: python3 -m pytest tests/test_ird_penalty.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.ird_penalty import (
    compute_three_months_interest,
    compute_ird_penalty,
    compute_breakage_penalty,
    refinance_with_penalty_analysis,
    break_for_readvanceable_analysis,
    DEFAULT_POSTED_RATES,
    _find_closest_term,
)


class TestThreeMonthsInterest(unittest.TestCase):
    """Test compute_three_months_interest (variable rate penalty)."""

    def test_standard_calculation(self):
        """3 months' interest on $400k at 5%."""
        result = compute_three_months_interest(400000, 0.05)
        expected = 400000 * 0.05 * (3 / 12)  # $5,000
        self.assertAlmostEqual(result, expected)

    def test_zero_balance(self):
        """Zero balance = zero penalty."""
        result = compute_three_months_interest(0, 0.05)
        self.assertAlmostEqual(result, 0.0)

    def test_high_rate(self):
        """High interest rate increases penalty proportionally."""
        result = compute_three_months_interest(300000, 0.10)
        expected = 300000 * 0.10 * 0.25  # $7,500
        self.assertAlmostEqual(result, expected)

    def test_small_balance(self):
        """Small balance with normal rate."""
        result = compute_three_months_interest(50000, 0.045)
        expected = 50000 * 0.045 * 0.25  # $562.50
        self.assertAlmostEqual(result, expected)

    def test_variable_rate_penalty(self):
        """Variable rate: penalty is always 3 months' interest."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.0495,
            remaining_months=30,
            rate_type='variable',
        )
        expected_3mo = 450000 * 0.0495 * 0.25
        self.assertAlmostEqual(result['three_months_interest'], expected_3mo)
        self.assertAlmostEqual(result['ird_penalty'], 0.0)
        self.assertAlmostEqual(result['total_penalty'], expected_3mo)
        self.assertEqual(result['method'], 'Variable: 3 months interest only')


class TestIRDPenalty(unittest.TestCase):
    """Test compute_ird_penalty (Interest Rate Differential)."""

    def test_basic_ird_calculation(self):
        """IRD when contract rate is higher than posted rate."""
        result = compute_ird_penalty(
            mortgage_balance=450000,
            contract_rate=0.05,
            remaining_months=36,
            posted_rates={3: 0.035},  # Rates dropped to 3.5%
        )
        # rate_diff = 0.05 - 0.035 = 0.015
        # IRD = 450000 * 0.015 * (36/12) = $20,250
        expected = 450000 * 0.015 * 3
        self.assertAlmostEqual(result, expected)

    def test_rates_went_up_zero_ird(self):
        """IRD is zero when current rates are higher than contract rate."""
        result = compute_ird_penalty(
            mortgage_balance=450000,
            contract_rate=0.04,
            remaining_months=36,
            posted_rates={3: 0.05},  # Rates went UP
        )
        # rate_diff = 0.04 - 0.05 = -0.01 → negative, IRD = 0
        self.assertAlmostEqual(result, 0.0)

    def test_zero_remaining_months(self):
        """No IRD when term is expired."""
        result = compute_ird_penalty(
            mortgage_balance=450000,
            contract_rate=0.05,
            remaining_months=0,
            posted_rates={5: 0.04},
        )
        self.assertAlmostEqual(result, 0.0)

    def test_discounted_rate_ird(self):
        """IRD with discounted rate: lender uses your discount against you."""
        result = compute_ird_penalty(
            mortgage_balance=400000,
            contract_rate=0.04,  # You got 1% discount from posted 5%
            remaining_months=36,
            posted_rates={3: 0.05},
            use_discounted_rate=True,
            discount_from_posted=0.01,  # Your original discount
        )
        # comparable_rate = 0.05 - 0.01 = 0.04
        # rate_diff = 0.04 - 0.04 = 0 → IRD = 0
        self.assertAlmostEqual(result, 0.0)

    def test_discounted_rate_with_rate_drop(self):
        """IRD with discount: rates dropped since you signed."""
        result = compute_ird_penalty(
            mortgage_balance=400000,
            contract_rate=0.04,  # Discounted from 5%
            remaining_months=36,
            posted_rates={3: 0.04},  # Posted dropped to 4%
            use_discounted_rate=True,
            discount_from_posted=0.01,
        )
        # effective_comparable = 0.04 - 0.01 = 0.03
        # rate_diff = 0.04 - 0.03 = 0.01
        # IRD = 400000 * 0.01 * (36/12) = $12,000
        expected = 400000 * 0.01 * 3
        self.assertAlmostEqual(result, expected)

    def test_default_posted_rates_used(self):
        """IRD calculation falls back to DEFAULT_POSTED_RATES."""
        result = compute_ird_penalty(
            mortgage_balance=300000,
            contract_rate=0.06,
            remaining_months=36,
        )
        # Should use DEFAULT_POSTED_RATES[3] = 0.05
        # rate_diff = 0.06 - 0.05 = 0.01
        # IRD = 300000 * 0.01 * 3 = $9,000
        expected = 300000 * 0.01 * 3
        self.assertAlmostEqual(result, expected)


class TestBreakagePenalty(unittest.TestCase):
    """Test compute_breakage_penalty (the main entry point)."""

    def test_fixed_ird_exceeds_three_months(self):
        """Fixed rate: IRD penalty exceeds 3 months' interest."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.05,
            remaining_months=36,
            rate_type='fixed',
            posted_rates={3: 0.035},
        )
        three_mo = 450000 * 0.05 * 0.25  # $5,625
        ird = 450000 * 0.015 * 3  # $20,250
        self.assertAlmostEqual(result['three_months_interest'], three_mo)
        self.assertAlmostEqual(result['ird_penalty'], ird)
        self.assertAlmostEqual(result['total_penalty'], ird)  # IRD is greater
        self.assertTrue(result['ird_exceeds_three_month'])

    def test_fixed_three_months_exceeds_ird(self):
        """Fixed rate: 3 months' interest exceeds IRD (rates close together)."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.05,
            remaining_months=36,
            rate_type='fixed',
            posted_rates={3: 0.048},  # Only 0.2% difference
        )
        three_mo = 450000 * 0.05 * 0.25  # $5,625
        # IRD = 450000 * 0.002 * 3 = $2,700
        self.assertAlmostEqual(result['three_months_interest'], three_mo)
        self.assertAlmostEqual(result['total_penalty'], three_mo)  # 3mo > IRD
        self.assertFalse(result['ird_exceeds_three_month'])

    def test_variable_penalty(self):
        """Variable rate: always 3 months' interest, never IRD."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.0495,
            remaining_months=30,
            rate_type='variable',
        )
        self.assertEqual(result['rate_type'], 'variable')
        self.assertAlmostEqual(result['ird_penalty'], 0.0)

    def test_returns_remaining_months(self):
        """Breakage result includes remaining months."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.05,
            remaining_months=42,
            rate_type='fixed',
            posted_rates={4: 0.035},
        )
        self.assertEqual(result['remaining_months'], 42)


class TestFindClosestTerm(unittest.TestCase):
    """Test _find_closest_term helper."""

    def test_exact_match(self):
        """Exact term available."""
        result = _find_closest_term(5, DEFAULT_POSTED_RATES)
        self.assertEqual(result, 5)

    def test_between_terms(self):
        """Remaining years between available terms."""
        result = _find_closest_term(4.5, DEFAULT_POSTED_RATES)
        # 4.5 is between 4 and 5; closest is 5 (0.5 away vs 0.5 from 4)
        self.assertIn(result, [4, 5])  # Either 4 or 5 is acceptable

    def test_short_remaining(self):
        """Less than 1 year remaining."""
        result = _find_closest_term(0.5, DEFAULT_POSTED_RATES)
        self.assertEqual(result, 1)  # Shortest available term

    def test_long_remaining(self):
        """More than longest available term."""
        result = _find_closest_term(15, DEFAULT_POSTED_RATES)
        self.assertEqual(result, 10)  # Longest available term


class TestRefinanceAnalysis(unittest.TestCase):
    """Test refinance_with_penalty_analysis."""

    def test_refinance_beneficial(self):
        """Cash-out refinance where benefits exceed penalty."""
        result = refinance_with_penalty_analysis(
            mortgage_balance=300000,
            contract_rate=0.05,
            remaining_months=36,
            rate_type='variable',
            new_rate=0.045,
            cash_out=100000,
            investment_return=0.07,
            marginal_rate=0.43,
        )
        # Variable: penalty is 3 months' interest
        self.assertGreater(result['total_penalty'], 0)
        # Should recommend refinance if net benefit > 0
        self.assertIn(result['recommendation'], ['refinance', 'keep_current'])

    def test_penalty_always_included(self):
        """Refinance analysis always includes penalty."""
        result = refinance_with_penalty_analysis(
            mortgage_balance=500000,
            contract_rate=0.06,
            remaining_months=24,
            rate_type='fixed',
            posted_rates={2: 0.04},
            investment_return=0.07,
        )
        self.assertGreater(result['total_penalty'], 0)
        self.assertIn('break_even_months', result)
        self.assertIn('net_benefit_after_penalty', result)

    def test_rate_savings_calculation(self):
        """Refinancing at lower rate saves on interest."""
        result = refinance_with_penalty_analysis(
            mortgage_balance=400000,
            contract_rate=0.05,
            remaining_months=24,
            new_rate=0.04,
            rate_type='variable',
            investment_return=0.07,
        )
        # Rate savings should be positive when new_rate < contract_rate
        self.assertGreater(result['interest_savings_remaining'], 0)

    def test_higher_new_rate_no_savings(self):
        """Refinancing at higher rate costs more in interest."""
        result = refinance_with_penalty_analysis(
            mortgage_balance=400000,
            contract_rate=0.04,
            remaining_months=24,
            new_rate=0.05,
            rate_type='variable',
            investment_return=0.07,
        )
        # Interest savings should be negative when new_rate > contract_rate
        self.assertLess(result['interest_savings_remaining'], 0)


class TestReadvanceableAnalysis(unittest.TestCase):
    """Test break_for_readvanceable_analysis."""

    def test_break_even_analysis(self):
        """Readvanceable analysis produces break-even years."""
        result = break_for_readvanceable_analysis(
            mortgage_balance=400000,
            contract_rate=0.05,
            remaining_months=36,
            new_readvanceable_rate=0.0495,
            house_value=500000,
            investment_return=0.07,
        )
        self.assertIn('break_even_years', result)
        self.assertIn('recommendation', result)
        self.assertIn(result['recommendation'],
                      ['switch_to_readvanceable', 'stay_current'])

    def test_penalty_included_in_analysis(self):
        """Readvanceable analysis includes penalty calculation."""
        result = break_for_readvanceable_analysis(
            mortgage_balance=400000,
            contract_rate=0.05,
            remaining_months=36,
            investment_return=0.07,
        )
        self.assertIn('penalty', result)
        self.assertGreater(result['total_penalty'], 0)

    def test_sm_benefit_grows_over_time(self):
        """SM benefit accumulates as more principal is readvanced."""
        result = break_for_readvanceable_analysis(
            mortgage_balance=400000,
            contract_rate=0.05,
            remaining_months=60,
            readvance_ratio=1.0,
            house_value=500000,
            investment_return=0.07,
        )
        self.assertGreater(result['total_sm_benefit_over_remaining_term'], 0)

    def test_partial_readvance(self):
        """Partial readvance ratio (0.65) produces less benefit than full readvance."""
        partial_result = break_for_readvanceable_analysis(
            mortgage_balance=400000,
            contract_rate=0.05,
            remaining_months=60,
            readvance_ratio=0.65,
            house_value=500000,
            investment_return=0.07,
        )
        full_result = break_for_readvanceable_analysis(
            mortgage_balance=400000,
            contract_rate=0.05,
            remaining_months=60,
            readvance_ratio=1.0,
            investment_return=0.07,
            house_value=500000,
        )
        self.assertLess(partial_result['total_sm_benefit_over_remaining_term'],
                        full_result['total_sm_benefit_over_remaining_term'])


class TestOpenTermNoPenalty(unittest.TestCase):
    """Issue #651 (DP#17/DP#32): an OPEN term can be broken at any time with
    NO prepayment penalty. Openness is orthogonal to fixed/variable, so the
    zero must hold for both. It must be a DECLARED zero (is_open=True), never
    an accidental one -- and absence of the flag must be a pure no-op (a term
    is closed unless it says otherwise), so the whole existing suite above
    still describes closed-term behaviour unchanged.
    """

    def test_open_fixed_term_has_zero_penalty(self):
        """Open + fixed: no IRD, no 3-months' interest — a declared zero."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.05,
            remaining_months=36,
            rate_type='fixed',
            posted_rates={3: 0.035},  # big drop → closed IRD would be five figures
            is_open=True,
        )
        self.assertEqual(result['total_penalty'], 0.0)
        self.assertEqual(result['ird_penalty'], 0.0)
        self.assertEqual(result['three_months_interest'], 0.0)
        self.assertTrue(result['is_open'])

    def test_open_variable_term_has_zero_penalty(self):
        """Open + variable: the 3-months'-interest charge is suppressed too."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.0495,
            remaining_months=30,
            rate_type='variable',
            is_open=True,
        )
        self.assertEqual(result['total_penalty'], 0.0)
        self.assertEqual(result['three_months_interest'], 0.0)
        self.assertTrue(result['is_open'])

    def test_absence_is_no_op(self):
        """Not declaring is_open == declaring is_open=False (closed): the
        result must be byte-for-byte the incumbent closed-term result."""
        kwargs = dict(
            mortgage_balance=450000,
            contract_rate=0.05,
            remaining_months=36,
            rate_type='fixed',
            posted_rates={3: 0.035},
        )
        default = compute_breakage_penalty(**kwargs)
        explicit_closed = compute_breakage_penalty(**kwargs, is_open=False)
        self.assertEqual(default, explicit_closed)
        # And the closed penalty is genuinely non-zero here, so the open case
        # above is suppressing a real charge, not measuring an empty one.
        self.assertGreater(default['total_penalty'], 0.0)


class TestEdgeCases(unittest.TestCase):
    """Edge cases and boundary conditions."""

    def test_zero_balance_no_penalty(self):
        """Zero mortgage balance: no penalty."""
        result = compute_breakage_penalty(
            mortgage_balance=0,
            contract_rate=0.05,
            remaining_months=36,
            rate_type='fixed',
            posted_rates={3: 0.04},
        )
        self.assertAlmostEqual(result['total_penalty'], 0.0)

    def test_zero_rate_no_penalty(self):
        """Zero contract rate: 3 months' interest = 0."""
        result = compute_breakage_penalty(
            mortgage_balance=450000,
            contract_rate=0.0,
            remaining_months=36,
            rate_type='fixed',
            posted_rates={3: 0.04},
        )
        # IRD = 0 - 0.04 = negative → 0
        # 3mo = 450000 * 0 * 0.25 = 0
        self.assertAlmostEqual(result['total_penalty'], 0.0)

    def test_equal_rates_no_ird(self):
        """Contract rate equals posted rate: IRD = 0, penalty = 3mo interest."""
        result = compute_breakage_penalty(
            mortgage_balance=300000,
            contract_rate=0.05,
            remaining_months=36,
            rate_type='fixed',
            posted_rates={3: 0.05},  # Same rate
        )
        self.assertAlmostEqual(result['ird_penalty'], 0.0)
        self.assertAlmostEqual(result['three_months_interest'],
                                300000 * 0.05 * 0.25)

    def test_one_month_remaining(self):
        """Only 1 month remaining: very small IRD."""
        result = compute_ird_penalty(
            mortgage_balance=400000,
            contract_rate=0.05,
            remaining_months=1,
            posted_rates={1: 0.04},
        )
        # IRD = 400000 * 0.01 * (1/12) = $333.33
        expected = 400000 * 0.01 / 12
        self.assertAlmostEqual(result, expected, places=0)

    def test_large_rate_differential(self):
        """Large rate drop: significant IRD penalty."""
        result = compute_breakage_penalty(
            mortgage_balance=500000,
            contract_rate=0.07,
            remaining_months=60,
            rate_type='fixed',
            posted_rates={5: 0.03},  # 4% rate drop!
        )
        # IRD = 500000 * 0.04 * 5 = $100,000
        three_mo = 500000 * 0.07 * 0.25  # $8,750
        self.assertGreater(result['ird_penalty'], three_mo)
        self.assertAlmostEqual(result['total_penalty'], result['ird_penalty'])

    def test_default_posted_rates_structure(self):
        """DEFAULT_POSTED_RATES has expected term structure."""
        self.assertIn(1, DEFAULT_POSTED_RATES)
        self.assertIn(5, DEFAULT_POSTED_RATES)
        self.assertIn(10, DEFAULT_POSTED_RATES)
        # Rates should be positive
        for term, rate in DEFAULT_POSTED_RATES.items():
            self.assertGreater(rate, 0)
            self.assertLess(rate, 0.20)  # Less than 20%


if __name__ == '__main__':
    unittest.main()