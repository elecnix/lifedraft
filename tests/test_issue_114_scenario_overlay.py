#!/usr/bin/env python3
"""Tests for Issue #114: ScenarioOverlay null-or-delta overlay pattern (DP#18).

Per DP#18: overlays modify a base config — they don't replace it.
Income fields default to None (meaning "no change from base") rather than 0.0
(which would silently zero out base income).

All test data uses round numbers per DP#13/DP#15.
"""

import unittest

from scenario_overlay import ScenarioOverlay, build_overlay_config


def _base_cfg():
    """Minimal base config for overlay tests. Round numbers per DP#13."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 150000},
                {'role': 'spouse', 'gross_income': 75000},
            ],
        },
        'property': {
            'house_value': 500000,
            'mortgage_balance': 200000,
            'margin_available': 100000,
            'mortgage_rate': 0.05,
        },
        'accounts': {
            'resp_current_balance': 30000,
        },
        'assumptions': {
            'investment_return': 0.07,
        },
    }


class TestScenarioOverlayNullDelta(unittest.TestCase):
    """Test that ScenarioOverlay uses None to mean 'no change from base'."""

    def test_income_defaults_to_none(self):
        """Per DP#18: income fields default to None, not 0.0."""
        overlay = ScenarioOverlay(label="test")
        self.assertIsNone(overlay.primary_income)
        self.assertIsNone(overlay.spouse_income)

    def test_none_income_preserves_base(self):
        """Per DP#18: None income means 'keep base', not 'set to zero'."""
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test")
        derived = build_overlay_config(base, overlay)
        # Base incomes preserved, not zeroed
        self.assertEqual(derived['family']['members'][0]['gross_income'], 150000)
        self.assertEqual(derived['family']['members'][1]['gross_income'], 75000)

    def test_explicit_income_overrides_base(self):
        """When income is explicitly set, it overrides base."""
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", primary_income=180000, spouse_income=90000)
        derived = build_overlay_config(base, overlay)
        self.assertEqual(derived['family']['members'][0]['gross_income'], 180000)
        self.assertEqual(derived['family']['members'][1]['gross_income'], 90000)

    def test_partial_income_override(self):
        """Can override just primary income while preserving spouse income."""
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", primary_income=200000)
        derived = build_overlay_config(base, overlay)
        self.assertEqual(derived['family']['members'][0]['gross_income'], 200000)
        # Spouse income preserved from base
        self.assertEqual(derived['family']['members'][1]['gross_income'], 75000)

    def test_zero_income_is_explicit(self):
        """Explicitly setting income to 0 should override (not be confused with None)."""
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", primary_income=0, spouse_income=0)
        derived = build_overlay_config(base, overlay)
        # Explicit zero overrides base
        self.assertEqual(derived['family']['members'][0]['gross_income'], 0)
        self.assertEqual(derived['family']['members'][1]['gross_income'], 0)

    def test_overlay_does_not_mutate_base(self):
        """Per DP#18: overlay produces derived config without mutating base."""
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", primary_income=180000)
        build_overlay_config(base, overlay)
        # Base should not be mutated
        self.assertEqual(base['family']['members'][0]['gross_income'], 150000)

    def test_mortgage_rate_override_with_none_income(self):
        """Can override mortgage rate while keeping base incomes."""
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", mortgage_rate=0.06)
        derived = build_overlay_config(base, overlay)
        self.assertEqual(derived['property']['mortgage_rate'], 0.06)
        # Incomes preserved
        self.assertEqual(derived['family']['members'][0]['gross_income'], 150000)
        self.assertEqual(derived['family']['members'][1]['gross_income'], 75000)


if __name__ == '__main__':
    unittest.main()