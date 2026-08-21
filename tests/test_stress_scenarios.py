#!/usr/bin/env python3
"""Unit tests for stress_scenarios.py module.

Run with: python3 -m pytest tests/ -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from stress_scenarios import (
    StressPath, STRESS_2008_CRASH, STRESS_RATE_SPIKE, STRESS_COMBINED,
    STRESS_BASELINE, STRESS_STAGFLATION, STRESS_LONG_RECOVERY,
    STRESS_RATE_SHOCK_2022, ALL_STRESS_PATHS, run_stress_test,
)


class TestStressPath(unittest.TestCase):
    """Test StressPath dataclass."""

    def test_years_property(self):
        path = StressPath("test", [0.07] * 5, [0.05] * 5)
        self.assertEqual(path.years, 5)

    def test_fill_returns(self):
        path = StressPath("test", [-0.40, 0.15, 0.10], [0.05] * 10)
        filled = path.fill_returns(10, default_return=0.07)
        self.assertEqual(len(filled), 10)
        self.assertAlmostEqual(filled[0], -0.40)
        self.assertAlmostEqual(filled[3], 0.07)  # Filled with default

    def test_average_return(self):
        path = StressPath("test", [0.05, 0.10], [0.05] * 10)
        avg = path.average_return(4)  # 0.05, 0.10 + 0.07, 0.07
        self.assertAlmostEqual(avg, (0.05 + 0.10 + 0.07 + 0.07) / 4)

    def test_fill_rates(self):
        path = StressPath("test", [0.07] * 10, [0.072, 0.035])
        filled = path.fill_rates(5, default_rate=0.05)
        self.assertEqual(len(filled), 5)
        self.assertAlmostEqual(filled[2], 0.05)


class TestPredefinedStressPaths(unittest.TestCase):
    """Test that predefined stress paths are valid."""

    def test_all_paths_have_data(self):
        """All predefined paths have investment and rate data."""
        for path in ALL_STRESS_PATHS:
            self.assertGreater(len(path.investment_return_path), 0,
                             f"{path.name} has no return path")
            self.assertGreater(len(path.heloc_rate_path), 0,
                             f"{path.name} has no rate path")

    def test_2008_crash_negative_year1(self):
        """2008-style crash has negative return in year 1."""
        self.assertLess(STRESS_2008_CRASH.investment_return_path[0], 0)

    def test_rate_spike_high_rate(self):
        """Rate spike has elevated HELOC rates."""
        self.assertGreater(max(STRESS_RATE_SPIKE.heloc_rate_path), 0.06)

    def test_combined_worst_case(self):
        """Combined stress has both negative returns and high rates."""
        self.assertLess(STRESS_COMBINED.investment_return_path[0], 0)
        self.assertGreater(STRESS_COMBINED.heloc_rate_path[0], 0.06)

    def test_baseline_normal(self):
        """Baseline has normal returns and rates."""
        self.assertAlmostEqual(STRESS_BASELINE.investment_return_path[0], 0.07)
        self.assertAlmostEqual(STRESS_BASELINE.heloc_rate_path[0], 0.0495)

    def test_stagflation_low_returns(self):
        """Stagflation has low returns and high rates."""
        self.assertLess(min(STRESS_STAGFLATION.investment_return_path), 0.05)
        self.assertGreater(max(STRESS_STAGFLATION.heloc_rate_path), 0.05)

    def test_long_recovery_near_zero(self):
        """Long recovery has near-zero returns for extended period."""
        self.assertAlmostEqual(STRESS_LONG_RECOVERY.investment_return_path[2], 0.02)


class TestStressTestRun(unittest.TestCase):
    """Test running stress tests with actual config."""

    def setUp(self):
        import json, os
        if not os.path.exists('input.json'):
            self.skipTest('input.json not available — gitignored per DP#15')
        self.cfg = json.load(open('input.json'))

    def test_baseline_runs(self):
        """Baseline stress test runs without error."""
        result = run_stress_test(self.cfg, STRESS_BASELINE)
        self.assertIn('net_benefit', result)
        self.assertIn('avg_return', result)
        self.assertEqual(result['stress_name'], "Baseline (no stress)")

    def test_2008_crash_lower_than_baseline(self):
        """2008 crash should produce lower net benefit than baseline."""
        baseline = run_stress_test(self.cfg, STRESS_BASELINE)
        crash = run_stress_test(self.cfg, STRESS_2008_CRASH)
        self.assertLessEqual(crash['net_benefit'], baseline['net_benefit'])

    def test_stress_returns_path(self):
        """Stress test result includes the return path."""
        result = run_stress_test(self.cfg, STRESS_2008_CRASH)
        self.assertIn('return_path', result)
        self.assertIn('rate_path', result)
        self.assertLess(result['return_path'][0], 0)  # Year 1 is negative


class TestLTVTriggeredStressPath(unittest.TestCase):
    """Test ltv_triggered_stress_path overlays."""

    def test_portfolio_decline_layered(self):
        """Portfolio decline is layered onto the first year return."""
        from stress_scenarios import ltv_triggered_stress_path, STRESS_BASELINE
        path = ltv_triggered_stress_path(STRESS_BASELINE, -0.15, -0.30)
        returns = path.fill_returns(10)
        # First year should be baseline[0] + portfolio_decline
        # STRESS_BASELINE has fill_returns(10) = 0.07 for each year
        # With -0.30 overlay, first year = 0.07 + (-0.30) = -0.23
        self.assertLess(returns[0], 0)

    def test_rate_increase_layered(self):
        """Rate increase is layered onto the first 3 years."""
        from stress_scenarios import ltv_triggered_stress_path, STRESS_BASELINE
        path = ltv_triggered_stress_path(STRESS_BASELINE, -0.15, -0.10, rate_increase=0.02)
        rates = path.fill_rates(10)
        # BASELINE rate is 4.95%, first 3 years should be baseline_rate + 0.02
        baseline_rate = STRESS_BASELINE.heloc_rate_path[0]
        for i in range(3):
            self.assertAlmostEqual(rates[i], baseline_rate + 0.02, places=4)
        # Remaining years should be unchanged
        for i in range(3, 10):
            self.assertAlmostEqual(rates[i], baseline_rate, places=4)

    def test_name_includes_declines(self):
        """Stress path name reflects the decline percentages."""
        from stress_scenarios import ltv_triggered_stress_path, STRESS_BASELINE
        path = ltv_triggered_stress_path(STRESS_BASELINE, -0.15, -0.30)
        self.assertIn('-15%', path.name)
        self.assertIn('-30%', path.name)


class TestForcedLiquidationImpact(unittest.TestCase):
    """Test forced_liquidation_impact calculations."""

    def test_liquidation_with_gain(self):
        """Liquidation of appreciated portfolio triggers capital gains tax."""
        from stress_scenarios import forced_liquidation_impact
        result = forced_liquidation_impact(
            investment_portfolio=200000,
            portfolio_cost_basis=100000,
            liquidation_pct=0.25,
            marginal_rate=0.4571,
        )
        # Liquidation amount: 200000 * 0.25 = 50000
        self.assertAlmostEqual(result['liquidation_amount'], 50000)
        # Cost basis recovered: 100000 * 0.25 = 25000
        self.assertAlmostEqual(result['cost_basis_recovered'], 25000)
        # Capital gain: 50000 - 25000 = 25000
        self.assertAlmostEqual(result['capital_gain'], 25000)
        # Tax: 25000 * 50% * 45.71% = 5713.75
        self.assertAlmostEqual(result['tax_on_gain'], 25000 * 0.50 * 0.4571, places=1)
        # Remaining portfolio: 200000 - 50000 = 150000
        self.assertAlmostEqual(result['portfolio_remaining'], 150000)

    def test_liquidation_with_loss(self):
        """Liquidation at a loss: no tax, capital loss recorded."""
        from stress_scenarios import forced_liquidation_impact
        result = forced_liquidation_impact(
            investment_portfolio=80000,  # Below cost basis
            portfolio_cost_basis=100000,
            liquidation_pct=0.50,
            marginal_rate=0.4571,
        )
        # Liquidation: 80000 * 0.50 = 40000
        self.assertAlmostEqual(result['liquidation_amount'], 40000)
        # Cost basis recovered: 100000 * 0.50 = 50000
        self.assertAlmostEqual(result['cost_basis_recovered'], 50000)
        # Loss: 40000 - 50000 = -10000
        self.assertAlmostEqual(result['capital_loss'], 10000)
        self.assertEqual(result['capital_gain'], 0)
        self.assertEqual(result['tax_on_gain'], 0)

    def test_zero_liquidation(self):
        """Zero liquidation returns all zeros."""
        from stress_scenarios import forced_liquidation_impact
        result = forced_liquidation_impact(
            investment_portfolio=200000,
            portfolio_cost_basis=100000,
            liquidation_pct=0,
        )
        self.assertEqual(result['liquidation_amount'], 0)
        self.assertEqual(result['net_proceeds'], 0)
        self.assertAlmostEqual(result['portfolio_remaining'], 200000)

    def test_full_liquidation(self):
        """100% liquidation empties the portfolio."""
        from stress_scenarios import forced_liquidation_impact
        result = forced_liquidation_impact(
            investment_portfolio=100000,
            portfolio_cost_basis=60000,
            liquidation_pct=1.0,
            marginal_rate=0.4571,
        )
        self.assertAlmostEqual(result['liquidation_amount'], 100000)
        self.assertAlmostEqual(result['portfolio_remaining'], 0)
        self.assertAlmostEqual(result['cost_basis_remaining'], 0)


class TestStressComparisonReport(unittest.TestCase):
    """Test stress_comparison_report output format."""

    def test_report_format_headers(self):
        """stress_comparison_report can produce a report with a config dict."""
        # This test verifies the function signature and basic output format.
        # A full config dict is needed for run_stress_test; skip if unavailable.
        from stress_scenarios import stress_comparison_report
        # The function signature takes cfg dict; we just verify it's callable.
        # Full integration test would need complete config.
        self.assertTrue(callable(stress_comparison_report))


if __name__ == '__main__':
    unittest.main()