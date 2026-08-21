#!/usr/bin/env python3
"""Tests for issue #285: sensitivity overlay presets are sourced from config (DP#5).

DP#5: Sensitivity overlays should compose from a base config with small,
auditable diffs the user controls — they are data, not hardcoded source.

Before the fix, the named overlay presets (conservative/moderate/aggressive)
lived in the SENSITIVITY_OVERLAYS code constant. After the fix they are
resolved from the input config under `sensitivity_overlay_presets`, falling
back to the built-in defaults so existing scenarios behave identically.

Tests are relational (compare config-sourced vs default behavior) and use
round numbers per DP#13/DP#15.
"""

import json
import unittest
from pathlib import Path

from optimize import (
    DEFAULT_SENSITIVITY_OVERLAYS,
    SENSITIVITY_OVERLAYS,
    get_sensitivity_overlays,
    apply_sensitivity_overlay,
    compose_preset,
)


class TestOverlayResolution(unittest.TestCase):
    """get_sensitivity_overlays resolves from config with a default fallback."""

    def test_absent_key_falls_back_to_defaults(self):
        """A config without the key yields the built-in defaults (identical behavior)."""
        self.assertIs(get_sensitivity_overlays({}), DEFAULT_SENSITIVITY_OVERLAYS)
        self.assertIs(get_sensitivity_overlays(None), DEFAULT_SENSITIVITY_OVERLAYS)

    def test_backward_compat_alias_equals_defaults(self):
        """SENSITIVITY_OVERLAYS stays importable and equals the defaults."""
        self.assertEqual(SENSITIVITY_OVERLAYS, DEFAULT_SENSITIVITY_OVERLAYS)

    def test_config_presets_take_precedence(self):
        """When the config defines presets, they replace the defaults."""
        cfg = {'sensitivity_overlay_presets': {
            'custom': {'investment_return': 0.06, 'label': 'Custom'},
        }}
        resolved = get_sensitivity_overlays(cfg)
        self.assertIn('custom', resolved)
        self.assertNotIn('conservative', resolved)


class TestApplyOverlayFromConfig(unittest.TestCase):
    """apply_sensitivity_overlay uses config-sourced overlays."""

    def test_default_matches_no_config(self):
        """No config key → same result as applying the built-in default overlay.

        DP#21/#591: the swept rate lands in return_model (the engine's single
        source of truth), not the deprecated assumptions.investment_return
        scalar, which the engine ignores whenever return_model is present.
        """
        cfg = {'assumptions': {'investment_return': 0.07, 'salary_growth': 0.02}}
        result = apply_sensitivity_overlay(dict(cfg, assumptions=dict(cfg['assumptions'])),
                                           'conservative')
        self.assertAlmostEqual(result['return_model']['rate'],
                               DEFAULT_SENSITIVITY_OVERLAYS['conservative']['investment_return'])

    def test_user_defined_overlay_applies(self):
        """A user-defined overlay in config drives the applied value."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'salary_growth': 0.02},
            'sensitivity_overlay_presets': {
                'custom': {'investment_return': 0.06, 'salary_growth': 0.015},
            },
        }
        result = apply_sensitivity_overlay(cfg, 'custom')
        self.assertAlmostEqual(result['return_model']['rate'], 0.06)
        self.assertAlmostEqual(result['assumptions']['salary_growth'], 0.015)

    def test_unknown_overlay_returns_config_unchanged(self):
        cfg = {'assumptions': {'investment_return': 0.07}}
        result = apply_sensitivity_overlay(cfg, 'nonexistent')
        self.assertAlmostEqual(result['assumptions']['investment_return'], 0.07)

    def test_compose_threads_config_overlays(self):
        """compose_preset applies a config-defined overlay when overlays passed."""
        cfg = {'assumptions': {'investment_return': 0.07}}
        overlays = {'custom': {'investment_return': 0.08}}
        result = compose_preset(cfg, overlay_name='custom', overlays=overlays)
        self.assertAlmostEqual(result['return_model']['rate'], 0.08)


# TestSchemaDocumentsKey (test_schema_default_matches_code_default) deleted,
# epic #603 Track C Phase 2b: it asserted the legacy input_schema.json's
# sensitivity_overlay_presets carried the same concrete values as
# DEFAULT_SENSITIVITY_OVERLAYS -- a "no drift" guard against two competing
# declarations of the same fact (#595-shaped). input_schema.json is deleted
# (DP#9); the new contract schema's sensitivity.presets is a real JSON
# Schema shape (additionalProperties: {$ref: sensitivity_preset}), not an
# example instance carrying concrete conservative/moderate/aggressive
# values -- there is no second declaration left to drift, so the guard has
# nothing left to guard.


if __name__ == '__main__':
    unittest.main()
