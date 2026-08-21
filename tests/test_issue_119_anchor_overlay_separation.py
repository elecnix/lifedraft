#!/usr/bin/env python3
"""Tests for Issue #119: Separate anchors from overlays in FORECAST_PRESETS (DP#5).

Per DP#5: anchor decisions (mortgage rate, cash-out) are real choices the user
is weighing. Sensitivity overlays (investment return, inflation, salary growth)
are uncertain parameters applied across all anchors. These should be separate.

All test data uses round numbers per DP#13/DP#15.
"""

import unittest

from optimize import (
    ANCHOR_PRESETS, SENSITIVITY_OVERLAYS, FORECAST_PRESETS,
    apply_preset, apply_anchor_preset, apply_sensitivity_overlay, compose_preset,
)


class TestAnchorPresets(unittest.TestCase):
    """Test ANCHOR_PRESETS structure and apply_anchor_preset."""

    def test_anchor_presets_exist(self):
        """ANCHOR_PRESETS should contain refinance decision options."""
        self.assertIn('renew_current', ANCHOR_PRESETS)
        self.assertIn('renew_low', ANCHOR_PRESETS)
        self.assertIn('refinance_fixed', ANCHOR_PRESETS)

    def test_anchor_presets_only_contain_decisions(self):
        """Anchors should only contain decision variables (mortgage_rate), not sensitivities."""
        for name, anchor in ANCHOR_PRESETS.items():
            self.assertIn('mortgage_rate', anchor, f"Anchor {name} missing mortgage_rate")
            self.assertNotIn('investment_return', anchor, f"Anchor {name} should not contain investment_return (DP#5)")
            self.assertNotIn('inflation', anchor, f"Anchor {name} should not contain inflation (DP#5)")

    def test_anchor_presets_have_labels(self):
        """Each anchor preset should have a human-readable label."""
        for name, anchor in ANCHOR_PRESETS.items():
            self.assertIn('label', anchor, f"Anchor {name} missing label")

    def test_apply_anchor_preset_overrides_mortgage_rate(self):
        """Applying an anchor should override only mortgage_rate."""
        cfg = {
            'property': {'house_value': 500000, 'mortgage_balance': 100000, 'mortgage_rate': 0.05},
            'assumptions': {'investment_return': 0.07, 'salary_growth': 0.02},
        }
        result = apply_anchor_preset(cfg, 'renew_current')
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.05)
        # Should NOT change investment_return
        self.assertAlmostEqual(result['assumptions']['investment_return'], 0.07)

    def test_apply_anchor_preset_does_not_modify_original(self):
        """apply_anchor_preset should return a deep copy."""
        cfg = {
            'property': {'house_value': 500000, 'mortgage_balance': 100000, 'mortgage_rate': 0.05},
            'assumptions': {'investment_return': 0.07},
        }
        apply_anchor_preset(cfg, 'renew_current')
        self.assertAlmostEqual(cfg['property']['mortgage_rate'], 0.05)  # Unchanged

    def test_apply_unknown_anchor_returns_original(self):
        """Unknown anchor name returns config unchanged."""
        cfg = {'property': {'mortgage_rate': 0.05}}
        result = apply_anchor_preset(cfg, 'nonexistent')
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.05)


class TestSensitivityOverlays(unittest.TestCase):
    """Test SENSITIVITY_OVERLAYS structure and apply_sensitivity_overlay."""

    def test_sensitivity_overlays_exist(self):
        """SENSITIVITY_OVERLAYS should contain return/growth/inflation scenarios."""
        self.assertIn('conservative', SENSITIVITY_OVERLAYS)
        self.assertIn('moderate', SENSITIVITY_OVERLAYS)
        self.assertIn('aggressive', SENSITIVITY_OVERLAYS)

    def test_overlays_only_contain_sensitivities(self):
        """Overlays should only contain uncertain parameters, not anchor decisions."""
        for name, overlay in SENSITIVITY_OVERLAYS.items():
            self.assertIn('investment_return', overlay, f"Overlay {name} missing investment_return")
            self.assertNotIn('mortgage_rate', overlay, f"Overlay {name} should not contain mortgage_rate (DP#5)")

    def test_conservative_lower_return(self):
        """Conservative overlay has lower investment return than moderate."""
        self.assertLess(
            SENSITIVITY_OVERLAYS['conservative']['investment_return'],
            SENSITIVITY_OVERLAYS['moderate']['investment_return'],
        )

    def test_apply_overlay_overrides_return(self):
        """Applying an overlay should override only sensitivity parameters."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'salary_growth': 0.02},
            'property': {'house_value': 500000, 'mortgage_balance': 100000, 'mortgage_rate': 0.05},
        }
        result = apply_sensitivity_overlay(cfg, 'conservative')
        # DP#21/#591: swept rate lands in return_model, the engine's single
        # source of truth (assumptions.investment_return is deprecated and
        # silently ignored by the engine once return_model exists).
        self.assertAlmostEqual(result['return_model']['rate'], 0.05)
        # Should NOT change mortgage_rate
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.05)

    def test_apply_overlay_does_not_modify_original(self):
        """apply_sensitivity_overlay should return a deep copy."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'salary_growth': 0.02},
            'property': {'mortgage_rate': 0.05},
        }
        apply_sensitivity_overlay(cfg, 'conservative')
        self.assertAlmostEqual(cfg['assumptions']['investment_return'], 0.07)  # Unchanged

    def test_apply_unknown_overlay_returns_original(self):
        """Unknown overlay name returns config unchanged."""
        cfg = {'assumptions': {'investment_return': 0.07}}
        result = apply_sensitivity_overlay(cfg, 'nonexistent')
        self.assertAlmostEqual(result['assumptions']['investment_return'], 0.07)


class TestComposePreset(unittest.TestCase):
    """Test compose_preset: combining anchor + overlay."""

    def test_compose_anchor_and_overlay(self):
        """compose_preset applies both anchor and overlay."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'salary_growth': 0.02},
            'property': {'house_value': 500000, 'mortgage_balance': 100000, 'mortgage_rate': 0.05},
        }
        result = compose_preset(cfg, anchor_name='renew_current', overlay_name='conservative')
        # Anchor should override mortgage_rate
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.05)
        # Overlay should override the return_model rate (DP#21/#591: the
        # engine's single source of truth, not the deprecated scalar)
        self.assertAlmostEqual(result['return_model']['rate'], 0.05)

    def test_compose_anchor_only(self):
        """compose_preset with only anchor."""
        cfg = {
            'assumptions': {'investment_return': 0.07},
            'property': {'mortgage_rate': 0.05},
        }
        result = compose_preset(cfg, anchor_name='renew_low')
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.035)
        # investment_return should remain unchanged
        self.assertAlmostEqual(result['assumptions']['investment_return'], 0.07)

    def test_compose_overlay_only(self):
        """compose_preset with only overlay."""
        cfg = {
            'assumptions': {'investment_return': 0.07},
            'property': {'mortgage_rate': 0.05},
        }
        result = compose_preset(cfg, overlay_name='aggressive')
        self.assertAlmostEqual(result['return_model']['rate'], 0.09)
        # mortgage_rate should remain unchanged
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.05)

    def test_compose_neither_returns_copy(self):
        """compose_preset with no anchor or overlay returns a copy."""
        cfg = {'assumptions': {'investment_return': 0.07}}
        result = compose_preset(cfg)
        self.assertAlmostEqual(result['assumptions']['investment_return'], 0.07)
        # Should be a different object
        self.assertIsNot(result, cfg)


class TestForecastPresetsBackwardCompat(unittest.TestCase):
    """Test that FORECAST_PRESETS still works for backward compatibility."""

    def test_legacy_presets_still_exist(self):
        """FORECAST_PRESETS should still be available for backward compat."""
        self.assertIn('conservative', FORECAST_PRESETS)
        self.assertIn('moderate', FORECAST_PRESETS)
        self.assertIn('aggressive', FORECAST_PRESETS)

    def test_apply_preset_still_works(self):
        """apply_preset should still work for backward compat."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'salary_growth': 0.02},
            'property': {'house_value': 500000, 'mortgage_balance': 100000, 'mortgage_rate': 0.05},
        }
        result = apply_preset(cfg, 'conservative')
        self.assertAlmostEqual(result['return_model']['rate'], 0.05)
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.055)


if __name__ == '__main__':
    unittest.main()