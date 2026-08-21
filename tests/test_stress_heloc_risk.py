#!/usr/bin/env python3
"""Tests for HELOC call risk, forced liquidation, and LTV-triggered stress events.

Per DP#17: every rule path gets a test. Per DP#3: pure functions.
Per DP#6: models mechanism, not product names.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from stress_scenarios import (
    StressPath, STRESS_2008_CRASH, STRESS_BASELINE,
    compute_heloc_call_risk, ltv_triggered_stress_path,
    forced_liquidation_impact, compute_heloc_call_series,
    HELoCCallEvent,
)


class TestHELoCCallRisk(unittest.TestCase):
    """Test compute_heloc_call_risk — DP#17: two rule paths."""

    def test_no_call_when_ltv_below_threshold(self):
        """LTV below 80% → no call triggered (rule path: no call)."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=100000, ltv_threshold=0.80,
        )
        # Total debt = 300000, house value = 500000, LTV = 60% < 80%
        self.assertEqual(event.call_amount, 0)
        self.assertEqual(event.forced_liquidation, 0)

    def test_call_when_ltv_exceeds_threshold(self):
        """LTV above 80% → call triggered for excess amount (rule path: call)."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
        )
        # Total debt = 450000, house = 500000, LTV = 90% > 80%
        # Max debt at 80% = 400000, excess = 50000
        self.assertGreater(event.call_amount, 0)
        # Call amount should not exceed HELOC balance
        self.assertLessEqual(event.call_amount, 250000)

    def test_call_with_house_decline(self):
        """House value decline pushes LTV above threshold."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=150000, ltv_threshold=0.80,
            house_decline_pct=-0.15,  # 15% house value drop
        )
        # Original LTV = 350000/500000 = 70% — no call
        # After decline: 350000/425000 = 82.4% → call triggered
        self.assertGreater(event.call_amount, 0)

    def test_no_call_when_heloc_zero(self):
        """No HELOC balance → no call possible (DP#7: mechanism check)."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=400000,
            heloc_balance=0, ltv_threshold=0.80,
        )
        self.assertEqual(event.call_amount, 0)
        self.assertEqual(event.forced_liquidation, 0)

    def test_forced_liquidation_covers_call(self):
        """Forced liquidation covers the call amount."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=200000, portfolio_cost_basis=100000,
            marginal_rate=0.4571,
        )
        # Call should trigger forced liquidation
        if event.call_amount > 0:
            self.assertGreater(event.forced_liquidation, 0)
            # Liquidation should not exceed portfolio value
            self.assertLessEqual(event.forced_liquidation, 200000)

    def test_call_amount_limited_to_heloc(self):
        """Call amount cannot exceed HELOC balance (lender can't call mortgage)."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=300000, ltv_threshold=0.80,
        )
        # Total debt = 500000, house = 500000, LTV = 100%
        # Max debt at 80% = 400000, excess = 100000
        # But call can only be for HELOC portion
        self.assertLessEqual(event.call_amount, 300000)


class TestHELoCCallRiskBoundaryConditions(unittest.TestCase):
    """Boundary tests for compute_heloc_call_risk — DP#17: every conditional branch.

    Tests LTV exactly at threshold, just above/below, zero portfolio,
    zero cost basis, positive/negative capital gains, and other edge cases.
    """

    def test_ltv_exactly_at_threshold_no_call(self):
        """LTV exactly at 80% threshold → no call (boundary: at threshold)."""
        # house=500000, total_debt=400000 → LTV = 80% exactly
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=300000,
            heloc_balance=100000, ltv_threshold=0.80,
        )
        self.assertEqual(event.call_amount, 0)
        self.assertAlmostEqual(event.ltv_before, 0.80)

    def test_ltv_just_above_threshold_triggers_call(self):
        """LTV just above 80% → call triggered (boundary: just above)."""
        # house=500000, total_debt=400001 → LTV = 80.0002%
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=300000,
            heloc_balance=100001, ltv_threshold=0.80,
        )
        self.assertGreater(event.call_amount, 0)

    def test_ltv_just_below_threshold_no_call(self):
        """LTV just below 80% → no call (boundary: just below)."""
        # house=500000, total_debt=399999 → LTV = 79.9998%
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=300000,
            heloc_balance=99999, ltv_threshold=0.80,
        )
        self.assertEqual(event.call_amount, 0)

    def test_zero_portfolio_no_liquidation(self):
        """Zero portfolio → no forced liquidation even with a call."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=0, portfolio_cost_basis=0,
        )
        # Call still triggered but no portfolio to liquidate
        self.assertGreater(event.call_amount, 0)
        self.assertEqual(event.forced_liquidation, 0)
        self.assertEqual(event.liquidation_tax, 0)

    def test_zero_cost_basis_positive_gain(self):
        """Zero cost basis → full liquidation proceeds are capital gain."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=200000, portfolio_cost_basis=0,
            marginal_rate=0.4571,
        )
        self.assertGreater(event.call_amount, 0)
        self.assertGreater(event.forced_liquidation, 0)
        # All proceeds are gain when cost basis is zero
        self.assertGreater(event.liquidation_tax, 0)

    def test_positive_capital_gain_triggers_tax(self):
        """Positive capital gain → liquidation tax applied."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=200000, portfolio_cost_basis=100000,
            marginal_rate=0.50,
        )
        self.assertGreater(event.call_amount, 0)
        self.assertGreater(event.forced_liquidation, 0)
        self.assertGreater(event.liquidation_tax, 0)

    def test_negative_capital_gain_no_tax(self):
        """Negative capital gain (portfolio below cost basis) → no tax."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=100000, portfolio_cost_basis=200000,
            marginal_rate=0.50,
        )
        self.assertGreater(event.call_amount, 0)
        self.assertGreater(event.forced_liquidation, 0)
        # Portfolio value < cost basis → capital loss, no tax
        self.assertEqual(event.liquidation_tax, 0)

    def test_portfolio_decline_with_call(self):
        """Portfolio decline during call → opportunity cost added to net loss."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=200000, portfolio_cost_basis=100000,
            portfolio_decline_pct=-0.30,  # 30% portfolio crash
            marginal_rate=0.4571,
        )
        # Call should still trigger
        self.assertGreater(event.call_amount, 0)
        # Net loss should include opportunity cost from forced sale at crashed prices
        self.assertGreater(event.net_loss_from_liquidation, 0)

    def test_call_capped_at_heloc_balance(self):
        """Call amount cannot exceed HELOC balance."""
        # Extreme case: mortgage + HELOC = 100% of house value
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=400000,
            heloc_balance=100000, ltv_threshold=0.80,
        )
        # Total debt = 500000, LTV = 100%, max at 80% = 400000
        # Excess = 100000, but capped at HELOC balance of 100000
        self.assertLessEqual(event.call_amount, 100000)

    def test_custom_ltv_threshold(self):
        """Custom LTV threshold (e.g., 65% for some readvanceable products)."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=200000, ltv_threshold=0.65,
        )
        # LTV = 400000/500000 = 80% > 65%
        self.assertGreater(event.call_amount, 0)
        self.assertAlmostEqual(event.call_threshold, 0.65)

    def test_zero_house_value_no_call(self):
        """Zero house value → no call (edge case: division by zero protection)."""
        event = compute_heloc_call_risk(
            house_value=0, mortgage_balance=100000,
            heloc_balance=50000, ltv_threshold=0.80,
        )
        self.assertEqual(event.call_amount, 0)
        self.assertEqual(event.ltv_before, 0)

    def test_cost_basis_equals_portfolio_value(self):
        """Cost basis equals portfolio value → zero capital gain, no tax."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=200000, portfolio_cost_basis=200000,
            marginal_rate=0.50,
        )
        self.assertGreater(event.call_amount, 0)
        self.assertGreater(event.forced_liquidation, 0)
        # Gain = 0 when cost basis equals portfolio value
        self.assertEqual(event.liquidation_tax, 0)

    def test_house_decline_without_portfolio_decline(self):
        """House decline alone can trigger call with no portfolio decline."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=150000, ltv_threshold=0.80,
            house_decline_pct=-0.20,  # 20% house value drop
            investment_portfolio=100000, portfolio_cost_basis=80000,
            marginal_rate=0.45,
        )
        # Original LTV = 70%, after decline LTV = 350000/400000 = 87.5%
        self.assertGreater(event.call_amount, 0)
        # No portfolio decline → net loss = call amount + liquidation tax only
        self.assertGreater(event.forced_liquidation, 0)

    def test_portfolio_100_percent_decline(self):
        """100% portfolio decline → portfolio value goes to zero (edge case)."""
        event = compute_heloc_call_risk(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=200000, portfolio_cost_basis=100000,
            portfolio_decline_pct=-1.0,  # Total loss
            marginal_rate=0.4571,
        )
        # Portfolio after crash = 0, so no liquidation possible
        self.assertGreater(event.call_amount, 0)
        self.assertEqual(event.forced_liquidation, 0)
        self.assertEqual(event.liquidation_tax, 0)

    def test_pure_function_same_inputs(self):
        """DP#3: Same inputs produce same outputs."""
        kwargs = dict(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=250000, ltv_threshold=0.80,
            investment_portfolio=200000, portfolio_cost_basis=100000,
            marginal_rate=0.4571,
        )
        e1 = compute_heloc_call_risk(**kwargs)
        e2 = compute_heloc_call_risk(**kwargs)
        self.assertEqual(e1.call_amount, e2.call_amount)
        self.assertEqual(e1.forced_liquidation, e2.forced_liquidation)
        self.assertEqual(e1.liquidation_tax, e2.liquidation_tax)
        self.assertEqual(e1.net_loss_from_liquidation, e2.net_loss_from_liquidation)


class TestForcedLiquidation(unittest.TestCase):
    """Test forced_liquidation_impact — DP#19: cost basis tracking."""

    def test_liquidation_with_gain(self):
        """Liquidating at a gain triggers capital gains tax (DP#27)."""
        result = forced_liquidation_impact(
            investment_portfolio=200000, portfolio_cost_basis=100000,
            liquidation_pct=0.50, marginal_rate=0.4571,
        )
        # Liquidate 50% = 100000, cost basis recovered = 50000
        # Gain = 100000 - 50000 = 50000
        # Tax = 50000 × 0.50 × 0.4571 = 11427.50
        self.assertAlmostEqual(result['liquidation_amount'], 100000)
        self.assertAlmostEqual(result['capital_gain'], 50000)
        self.assertAlmostEqual(result['tax_on_gain'], 11427.50, places=1)
        self.assertAlmostEqual(result['portfolio_remaining'], 100000)
        self.assertAlmostEqual(result['cost_basis_remaining'], 50000)

    def test_liquidation_with_loss(self):
        """Liquidating at a loss: no tax, capital loss for offset (DP#27)."""
        result = forced_liquidation_impact(
            investment_portfolio=100000, portfolio_cost_basis=150000,
            liquidation_pct=0.50, marginal_rate=0.4571,
        )
        # Liquidate 50% = 50000, cost basis recovered = 75000
        # Loss = 50000 - 75000 = -25000
        # No tax on loss
        self.assertAlmostEqual(result['liquidation_amount'], 50000)
        self.assertAlmostEqual(result['capital_gain'], 0)
        self.assertAlmostEqual(result['capital_loss'], 25000)
        self.assertAlmostEqual(result['tax_on_gain'], 0)

    def test_zero_liquidation(self):
        """Zero liquidation pct → no impact."""
        result = forced_liquidation_impact(
            investment_portfolio=200000, portfolio_cost_basis=100000,
            liquidation_pct=0.0,
        )
        self.assertEqual(result['liquidation_amount'], 0)
        self.assertEqual(result['tax_on_gain'], 0)
        self.assertAlmostEqual(result['portfolio_remaining'], 200000)


class TestLTVTriggeredStressPath(unittest.TestCase):
    """Test ltv_triggered_stress_path overlay composition (DP#18)."""

    def test_overlay_adds_decline_to_returns(self):
        """LTV trigger overlay composes with base path (DP#18: overlays modify)."""
        path = ltv_triggered_stress_path(
            STRESS_BASELINE,
            house_decline_pct=-0.15,
            portfolio_decline_pct=-0.30,
            rate_increase=0.02,
        )
        # Base return year 1 is 0.07, overlay adds -0.30 → -0.23
        self.assertAlmostEqual(path.investment_return_path[0], 0.07 + (-0.30))
        # Base rate year 1 is 0.0495, overlay adds 0.02 → 0.0695
        self.assertAlmostEqual(path.heloc_rate_path[0], 0.0495 + 0.02)
        self.assertIn("house", path.name.lower())

    def test_overlay_preserves_base_after_stress(self):
        """After the stress overlay years, base path returns (DP#5: overlays are temporary)."""
        path = ltv_triggered_stress_path(
            STRESS_2008_CRASH,
            house_decline_pct=-0.10,
            portfolio_decline_pct=-0.20,
            rate_increase=0.01,
        )
        # Year 1 of 2008 crash is -0.40, plus overlay -0.20 → -0.60
        self.assertAlmostEqual(path.investment_return_path[0], -0.40 + (-0.20))
        # Later years should be the base crash recovery values, not modified
        self.assertEqual(path.investment_return_path[5], 0.07)  # Year 6 = base recovery


class TestHELoCCallSeries(unittest.TestCase):
    """Test compute_heloc_call_series — multi-year projection (DP#26: fold)."""

    def test_no_calls_when_ltv_low(self):
        """With low LTV, no calls over the projection period."""
        events = compute_heloc_call_series(
            house_value=500000, mortgage_balance=100000,
            heloc_balance=50000, investment_portfolio=100000,
            portfolio_cost_basis=80000, stress_path=STRESS_BASELINE,
            projection_years=5,
        )
        # LTV = 150000/500000 = 30% — should never trigger a call
        for event in events:
            self.assertEqual(event.call_amount, 0)

    def test_calls_when_ltv_high(self):
        """With high LTV, calls appear in stress scenarios."""
        events = compute_heloc_call_series(
            house_value=400000, mortgage_balance=200000,
            heloc_balance=150000, investment_portfolio=100000,
            portfolio_cost_basis=80000, stress_path=STRESS_2008_CRASH,
            ltv_threshold=0.80, projection_years=3,
        )
        # LTV = 350000/400000 = 87.5% → should trigger calls
        call_years = [e for e in events if e.call_amount > 0]
        self.assertGreater(len(call_years), 0, "Should have at least one call year")

    def test_series_length_matches_projection(self):
        """Series length equals projection years."""
        events = compute_heloc_call_series(
            house_value=500000, mortgage_balance=200000,
            heloc_balance=100000, investment_portfolio=200000,
            portfolio_cost_basis=100000, stress_path=STRESS_BASELINE,
            projection_years=5,
        )
        self.assertEqual(len(events), 5)


if __name__ == '__main__':
    unittest.main()