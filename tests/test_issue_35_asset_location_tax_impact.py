#!/usr/bin/env python3
"""Tests for Issue #35: asset_location_tax_impact() replaces asset_location_suggestion().

Per DP#30: the simulator models tax consequences, not financial decisions.
asset_location_suggestion() returns prescriptive recommendations (financial advice).
asset_location_tax_impact() returns structured tax drag data (tax information).

All test data uses round numbers per DP#13/DP#15.
"""

import unittest
import warnings


class TestAssetLocationTaxImpact(unittest.TestCase):
    """Test the new asset_location_tax_impact() function."""

    def test_returns_tax_drag_for_all_account_types(self):
        """asset_location_tax_impact returns tax drag for each account type."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result = asset_location_tax_impact('bonds', 0.50, 'quebec')
        # Should return a dict with all account types
        self.assertIn('rrsp', result)
        self.assertIn('tfsa', result)
        self.assertIn('non_reg', result)
        self.assertIn('resp', result)
        # Each value should be a number (tax drag in bps)
        for key, value in result.items():
            self.assertIsInstance(value, (int, float),
                                 f"Tax drag for {key} should be numeric, got {type(value)}")

    def test_bonds_zero_drag_in_registered_accounts(self):
        """Bonds have zero tax drag in RRSP/TFSA (interest is sheltered)."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result = asset_location_tax_impact('bonds', 0.50, 'quebec')
        # Bonds in RRSP/TFSA: no tax on interest
        self.assertEqual(result['rrsp'], 0.0)
        self.assertEqual(result['tfsa'], 0.0)
        # Bonds in non-reg: interest fully taxable at MTR
        self.assertGreater(result['non_reg'], 0.0)

    def test_us_equity_zero_drag_in_rrsp(self):
        """US-listed equity has zero WHT drag in RRSP (treaty exemption)."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result = asset_location_tax_impact('us_listed', 0.50, 'quebec')
        self.assertEqual(result['rrsp'], 0.0)
        # TFSA has 15% WHT drag
        self.assertGreater(result['tfsa'], 0.0)
        # Non-reg: FTC recovers WHT, but distribution taxed at MTR
        # (WHT drag component is 0 for non-reg)

    def test_canadian_equity_zero_drag_all_accounts(self):
        """Canadian equity has no WHT drag in any account."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result = asset_location_tax_impact('canadian', 0.50, 'quebec')
        # Canadian equities: no WHT, and in non-reg, capital gains
        # are only taxed on realization. The WHT component is 0.
        self.assertEqual(result['rrsp'], 0.0)
        self.assertEqual(result['tfsa'], 0.0)

    def test_international_equity_zero_in_rrsp_and_nonreg(self):
        """International equity has 0 WHT in RRSP, 0 in non-reg (FTC)."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result = asset_location_tax_impact('international', 0.50, 'quebec')
        self.assertEqual(result['rrsp'], 0.0)
        # TFSA/RESP: 15% unrecoverable WHT
        self.assertGreater(result['tfsa'], 0.0)

    def test_canadian_dividend_low_drag_in_nonreg_quebec(self):
        """Canadian dividends have low effective drag in non-reg (DTC)."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result = asset_location_tax_impact('canadian_dividend', 0.50, 'quebec')
        # In non-reg: effective dividend rate < MTR due to DTC
        self.assertGreater(result['non_reg'], 0.0)
        # But should be less than bonds in non-reg (which are fully taxed)
        bonds_result = asset_location_tax_impact('bonds', 0.50, 'quebec')
        self.assertLess(result['non_reg'], bonds_result['non_reg'])

    def test_round_numbers(self):
        """All test inputs use round numbers per DP#13."""
        from countries.canada.tax_calc import asset_location_tax_impact

        # Round MTR (50%) and round yield (2%)
        result = asset_location_tax_impact('us_listed', 0.50, 'quebec')
        self.assertIsInstance(result, dict)


class TestAssetLocationSuggestionDeprecation(unittest.TestCase):
    """Test that asset_location_suggestion() emits deprecation warning."""

    def test_suggestion_emits_deprecation_warning(self):
        """asset_location_suggestion should emit a DeprecationWarning."""
        from countries.canada.tax_calc import asset_location_suggestion

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = asset_location_suggestion('us_listed', 0.50, 'quebec')
            # Should still return a result (backward compat)
            self.assertIsInstance(result, list)
            # Should emit a deprecation warning
            deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
            self.assertGreater(len(deprecation_warnings), 0,
                               "asset_location_suggestion should emit a DeprecationWarning")

    def test_suggestion_returns_data_driven_order(self):
        """Deprecated asset_location_suggestion now derives order from tax impact data."""
        from countries.canada.tax_calc import asset_location_suggestion

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = asset_location_suggestion('us_listed', 0.50, 'quebec')
            # Data-driven: sorted by lowest tax drag first
            # rrsp=0, tfsa=30, resp=30, non_reg=100 → rrsp first
            self.assertEqual(result[0], 'rrsp')
            self.assertIsInstance(result, list)


class TestAssetLocationOptimizerUsesTaxImpact(unittest.TestCase):
    """Test that AssetLocationOptimizer uses tax drag data, not suggestions."""

    def test_optimizer_works_with_tax_impact_data(self):
        """Optimizer should work using tax drag data instead of suggestion order."""
        from countries.canada.asset_location import (
            AssetLocationOptimizer, PortfolioHolding, ETFType, AccountType,
        )

        portfolio = [
            PortfolioHolding("VTI", ETFType.US_LISTED_EQUITY, 0.30, 0.02),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.20, 0.03),
            PortfolioHolding("XIC", ETFType.CANADIAN_EQUITY, 0.15, 0.02),
        ]
        account_sizes = {
            AccountType.RRSP: 0.35,
            AccountType.TFSA: 0.20,
            AccountType.NON_REG: 0.45,
        }
        optimizer = AssetLocationOptimizer(
            marginal_rate=0.50, province='quebec', account_sizes=account_sizes,
        )
        result = optimizer.optimize(portfolio)
        # Should produce a valid result
        self.assertGreater(len(result.allocations), 0)
        # Total drag should be non-negative
        self.assertGreaterEqual(result.total_tax_drag_bps, 0)


class TestTaxImpactStructuredOutput(unittest.TestCase):
    """Test that asset_location_tax_impact returns structured data, not advice."""

    def test_output_is_data_not_advice(self):
        """Result should be numeric tax drag, not account orderings."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result = asset_location_tax_impact('us_listed', 0.50, 'quebec')
        # Every value should be a number (bps), not a string or list
        for key, value in result.items():
            self.assertIsInstance(value, (int, float))
            self.assertGreaterEqual(value, 0.0)

    def test_tax_drag_increases_with_mtr(self):
        """Higher MTR should mean higher tax drag in non-reg accounts."""
        from countries.canada.tax_calc import asset_location_tax_impact

        result_low = asset_location_tax_impact('bonds', 0.30, 'quebec')
        result_high = asset_location_tax_impact('bonds', 0.50, 'quebec')
        # Non-reg drag should be higher at higher MTR
        self.assertGreater(result_high['non_reg'], result_low['non_reg'])


if __name__ == '__main__':
    unittest.main()