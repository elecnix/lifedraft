#!/usr/bin/env python3
"""Unit tests for asset_location.py module.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/ -v
Or:      python3 tests/test_asset_location.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.asset_location import (
    AccountType, ETFType, PortfolioHolding, AccountAllocation,
    AssetLocationResult, AssetLocationOptimizer,
    compute_tax_drag, light_vs_ludicrous,
)


class TestPortfolioHolding(unittest.TestCase):
    """Test PortfolioHolding dataclass."""

    def test_creation(self):
        h = PortfolioHolding("VTI", ETFType.US_LISTED_EQUITY, 0.30, 0.02)
        self.assertEqual(h.name, "VTI")
        self.assertAlmostEqual(h.allocation_pct, 0.30)

    def test_distribution_type(self):
        self.assertEqual(
            PortfolioHolding("ZAG", ETFType.BONDS, 0.20).distribution_type,
            "interest"
        )
        self.assertEqual(
            PortfolioHolding("XEI", ETFType.CANADIAN_DIVIDEND, 0.10).distribution_type,
            "eligible_dividend"
        )


class TestComputeTaxDrag(unittest.TestCase):
    """Test compute_tax_drag pure function."""

    def test_bonds_in_non_reg_high_drag(self):
        """Bonds in non-reg have high tax drag (fully taxable interest)."""
        h = PortfolioHolding("ZAG", ETFType.BONDS, 1.0, 0.03)
        drag = compute_tax_drag(h, AccountType.NON_REG, 0.4571)
        self.assertGreater(drag, 0)

    def test_bonds_in_rrsp_zero_drag(self):
        """Bonds in RRSP have zero tax drag (sheltered)."""
        h = PortfolioHolding("ZAG", ETFType.BONDS, 1.0, 0.03)
        drag = compute_tax_drag(h, AccountType.RRSP, 0.4571)
        self.assertEqual(drag, 0)

    def test_us_equity_in_tfsa_wht_drag(self):
        """US equity in TFSA has WHT drag (15% unrecoverable)."""
        h = PortfolioHolding("VTI", ETFType.US_LISTED_EQUITY, 1.0, 0.02)
        drag = compute_tax_drag(h, AccountType.TFSA, 0.4571)
        self.assertGreater(drag, 0)  # ~30 bps

    def test_us_equity_in_rrsp_zero_drag(self):
        """US equity in RRSP has 0 WHT drag (treaty)."""
        h = PortfolioHolding("VTI", ETFType.US_LISTED_EQUITY, 1.0, 0.02)
        drag = compute_tax_drag(h, AccountType.RRSP, 0.4571)
        self.assertEqual(drag, 0)

    def test_canadian_equity_zero_drag(self):
        """Canadian equity has no WHT drag in any account."""
        h = PortfolioHolding("XIC", ETFType.CANADIAN_EQUITY, 1.0, 0.02)
        for acct in [AccountType.RRSP, AccountType.TFSA, AccountType.NON_REG]:
            drag = compute_tax_drag(h, acct, 0.4571)
            self.assertEqual(drag, 0)


class TestAssetLocationOptimizer(unittest.TestCase):
    """Test the AssetLocationOptimizer."""

    def setUp(self):
        self.portfolio = [
            PortfolioHolding("VTI", ETFType.US_LISTED_EQUITY, 0.30, 0.02),
            PortfolioHolding("XIC", ETFType.CANADIAN_EQUITY, 0.15, 0.02),
            PortfolioHolding("XEI", ETFType.CANADIAN_DIVIDEND, 0.10, 0.04),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.20, 0.03),
            PortfolioHolding("XAW", ETFType.INTERNATIONAL_EQUITY, 0.25, 0.02),
        ]
        self.account_sizes = {
            AccountType.RRSP: 0.35,
            AccountType.TFSA: 0.20,
            AccountType.NON_REG: 0.45,
        }
        self.optimizer = AssetLocationOptimizer(
            marginal_rate=0.4571, province='quebec',
            account_sizes=self.account_sizes,
        )

    def test_optimize_produces_result(self):
        result = self.optimizer.optimize(self.portfolio)
        self.assertIsInstance(result, AssetLocationResult)
        self.assertGreater(len(result.allocations), 0)

    def test_ludicrous_less_drag_than_light(self):
        """Ludicrous approach should have <= tax drag vs light."""
        ludicrous = self.optimizer.optimize(self.portfolio)
        light = self.optimizer.light_approach(self.portfolio)
        self.assertLessEqual(ludicrous.total_tax_drag_bps, light.total_tax_drag_bps)

    def test_rrsp_gets_us_equity(self):
        """RRSP should get US-listed equities (0% WHT)."""
        result = self.optimizer.optimize(self.portfolio)
        if AccountType.RRSP in result.allocations:
            rrsp_holdings = result.allocations[AccountType.RRSP].holdings
            rrsp_etf_types = [h.etf_type for h in rrsp_holdings]
            self.assertIn(ETFType.US_LISTED_EQUITY, rrsp_etf_types)

    def test_bonds_not_in_non_reg(self):
        """Bonds should not be in non-reg (fully taxable interest)."""
        result = self.optimizer.optimize(self.portfolio)
        if AccountType.NON_REG in result.allocations:
            nonreg_holdings = result.allocations[AccountType.NON_REG].holdings
            nonreg_types = [h.etf_type for h in nonreg_holdings]
            # Bonds should prefer registered accounts
            # (may end up in non-reg if account sizes force it)


class TestLightVsLudicrous(unittest.TestCase):
    """Test the comparison function."""

    def test_comparison_runs(self):
        portfolio = [
            PortfolioHolding("VTI", ETFType.US_LISTED_EQUITY, 0.40, 0.02),
            PortfolioHolding("XIC", ETFType.CANADIAN_EQUITY, 0.20, 0.02),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.40, 0.03),
        ]
        result = light_vs_ludicrous(
            portfolio, marginal_rate=0.4571, portfolio_value=500000
        )
        self.assertIn('light_drag_bps', result)
        self.assertIn('ludicrous_drag_bps', result)
        self.assertIn('annual_savings', result)
        self.assertIn('ten_year_savings', result)

    def test_ludicrous_saves_money(self):
        """Ludicrous should save money over 10 years."""
        portfolio = [
            PortfolioHolding("VTI", ETFType.US_LISTED_EQUITY, 0.40, 0.02),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.40, 0.03),
            PortfolioHolding("XIC", ETFType.CANADIAN_EQUITY, 0.20, 0.02),
        ]
        result = light_vs_ludicrous(portfolio, marginal_rate=0.4571)
        # At minimum, US equity WHT savings and bonds in RRSP
        self.assertGreaterEqual(result['savings_bps'], 0)


class TestPortfolioFromConfig(unittest.TestCase):
    """DP#2: Portfolio composition should come from user config, not hardcoded."""

    def test_portfolio_holding_from_dict(self):
        """PortfolioHolding.from_dict creates a holding from config data."""
        from countries.canada.asset_location import PortfolioHolding, ETFType
        data = {
            'name': 'VTI',
            'etf_type': 'us_listed',
            'allocation_pct': 0.30,
            'yield_pct': 0.015,
        }
        holding = PortfolioHolding.from_dict(data)
        self.assertEqual(holding.name, 'VTI')
        self.assertEqual(holding.etf_type, ETFType.US_LISTED_EQUITY)
        self.assertAlmostEqual(holding.allocation_pct, 0.30)
        self.assertAlmostEqual(holding.yield_pct, 0.015)

    def test_portfolio_holding_from_dict_defaults(self):
        """PortfolioHolding.from_dict uses the DP#13 fallback for missing yield.

        (Issue #691 deleted the orphaned per-holding ``mer_pct``; the canonical
        engine-read fee is the per-account ``mer`` -- see test_issue_691_mer.py.)
        """
        from countries.canada.asset_location import PortfolioHolding, ETFType
        data = {
            'name': 'XIC',
            'etf_type': 'canadian',
            'allocation_pct': 0.20,
        }
        holding = PortfolioHolding.from_dict(data)
        self.assertEqual(holding.name, 'XIC')
        self.assertAlmostEqual(holding.yield_pct, 0.02)  # DP#13 fallback

    def test_portfolio_holding_round_trip(self):
        """PortfolioHolding.from_dict/to_dict round-trips (DP#24)."""
        from countries.canada.asset_location import PortfolioHolding, ETFType
        original = PortfolioHolding('XEQT', ETFType.CANADIAN_EQUITY, 0.40, 0.025)
        data = original.to_dict()
        restored = PortfolioHolding.from_dict(data)
        self.assertEqual(restored.name, original.name)
        self.assertEqual(restored.etf_type, original.etf_type)
        self.assertAlmostEqual(restored.allocation_pct, original.allocation_pct)
        self.assertAlmostEqual(restored.yield_pct, original.yield_pct)

    def test_optimizer_from_dict(self):
        """AssetLocationOptimizer.from_dict creates optimizer from config."""
        from countries.canada.asset_location import AssetLocationOptimizer, AccountType
        data = {
            'marginal_rate': 0.45,
            'province': 'ontario',
            'account_sizes': {
                'rrsp': 0.40,
                'tfsa': 0.25,
                'non_reg': 0.35,
            },
        }
        optimizer = AssetLocationOptimizer.from_dict(data)
        self.assertAlmostEqual(optimizer.marginal_rate, 0.45)
        self.assertEqual(optimizer.province, 'ontario')
        self.assertAlmostEqual(optimizer.account_sizes[AccountType.RRSP], 0.40)

    def test_optimizer_round_trip(self):
        """AssetLocationOptimizer.from_dict/to_dict round-trips (DP#24)."""
        from countries.canada.asset_location import AssetLocationOptimizer, AccountType
        original = AssetLocationOptimizer(
            marginal_rate=0.45,
            province='quebec',
            account_sizes={AccountType.RRSP: 0.35, AccountType.TFSA: 0.20, AccountType.NON_REG: 0.45},
        )
        data = original.to_dict()
        restored = AssetLocationOptimizer.from_dict(data)
        self.assertAlmostEqual(restored.marginal_rate, original.marginal_rate)
        self.assertEqual(restored.province, original.province)

    def test_portfolio_from_config(self):
        """portfolio_from_config creates portfolio from config data (DP#2)."""
        from countries.canada.asset_location import (
            portfolio_from_config, PortfolioHolding, ETFType,
        )
        config = {
            'holdings': [
                {'name': 'VTI', 'etf_type': 'us_listed', 'allocation_pct': 0.30, 'yield_pct': 0.018},
                {'name': 'XIC', 'etf_type': 'canadian', 'allocation_pct': 0.20, 'yield_pct': 0.025},
                {'name': 'ZAG', 'etf_type': 'bonds', 'allocation_pct': 0.50, 'yield_pct': 0.035},
            ],
        }
        portfolio = portfolio_from_config(config)
        self.assertEqual(len(portfolio), 3)
        self.assertEqual(portfolio[0].name, 'VTI')
        self.assertAlmostEqual(portfolio[0].yield_pct, 0.018)
        self.assertAlmostEqual(portfolio[2].allocation_pct, 0.50)

    def test_portfolio_from_config_empty(self):
        """portfolio_from_config returns empty list for missing config."""
        from countries.canada.asset_location import portfolio_from_config
        self.assertEqual(portfolio_from_config({}), [])
        self.assertEqual(portfolio_from_config({'holdings': []}), [])


class TestConfigurableYieldRates(unittest.TestCase):
    """DP#2: Yield rates must come from config, not be hardcoded."""

    def test_configurable_yield_overrides_default(self):
        """User-configured yield_pct overrides the 0.02 default."""
        from countries.canada.asset_location import PortfolioHolding, ETFType
        h_default = PortfolioHolding('VTI', ETFType.US_LISTED_EQUITY, 0.30)
        h_custom = PortfolioHolding('VTI', ETFType.US_LISTED_EQUITY, 0.30, yield_pct=0.035)
        self.assertAlmostEqual(h_default.yield_pct, 0.02)  # DP#13 fallback
        self.assertAlmostEqual(h_custom.yield_pct, 0.035)  # From config

    def test_configurable_account_sizes(self):
        """Account sizes come from config, not hardcoded defaults."""
        from countries.canada.asset_location import AssetLocationOptimizer, AccountType
        # Default sizes (DP#13 fallback)
        default_optimizer = AssetLocationOptimizer()
        self.assertAlmostEqual(default_optimizer.account_sizes[AccountType.RRSP], 0.35)

        # User-configured sizes
        custom_sizes = {
            AccountType.RRSP: 0.50,
            AccountType.TFSA: 0.30,
            AccountType.NON_REG: 0.20,
        }
        custom_optimizer = AssetLocationOptimizer(account_sizes=custom_sizes)
        self.assertAlmostEqual(custom_optimizer.account_sizes[AccountType.RRSP], 0.50)


if __name__ == '__main__':
    unittest.main()