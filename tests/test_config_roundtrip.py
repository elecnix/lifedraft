#!/usr/bin/env python3
"""Tests for SimulationConfig round-trip: to_dict, to_json, overlay_diff (DP#24).

Per DP#24: config round-trips enable save–modify–re-run workflows.
to_dict() is the inverse of from_dict(); overlay_diff shows what changed.
"""

import sys
import os
import json
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from dataclasses import replace

from simulation import SimulationConfig


def _make_config():
    return SimulationConfig(
        projection_years=5, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
        mortgage_balance=100000, mortgage_rate=0.05,
        house_value=400000, ltv_max=0.80,
        deduct_later_bracket_target=117045,
    )


class TestConfigRoundTrip(unittest.TestCase):
    """DP#24: to_dict/from_dict must round-trip without loss."""

    def test_to_dict_from_dict_roundtrip(self):
        """to_dict() → from_dict() produces equivalent config."""
        cfg = _make_config()
        d = cfg.to_dict()
        cfg2 = SimulationConfig.from_dict(d)
        self.assertEqual(cfg.projection_years, cfg2.projection_years)
        self.assertEqual(cfg.investment_return, cfg2.investment_return)
        self.assertEqual(cfg.mortgage_balance, cfg2.mortgage_balance)
        self.assertEqual(cfg.house_value, cfg2.house_value)
        self.assertEqual(cfg.ltv_max, cfg2.ltv_max)
        self.assertEqual(cfg.deduct_later_bracket_target, cfg2.deduct_later_bracket_target)

    def test_roundtrip_preserves_tax_retirement_fields(self):
        """from_dict(to_dict()) preserves capital_gains_inclusion/tfsa_growth (#247).

        retirement_years dropped from this pin in epic #603 Track C Phase 2
        (DP#9): the field itself was deleted -- parsed onto
        SimulationConfig.retirement_years, only round-tripped via to_dict(),
        never read for a decision (#593's DEAD_ALLOWLIST).
        """
        cfg = replace(
            _make_config(),
            capital_gains_inclusion=0.66, tfsa_growth=0.05,
        )
        cfg2 = SimulationConfig.from_dict(cfg.to_dict())
        self.assertEqual(
            (cfg2.capital_gains_inclusion, cfg2.tfsa_growth),
            (cfg.capital_gains_inclusion, cfg.tfsa_growth),
        )

    def test_to_dict_contains_all_sections(self):
        """to_dict() contains all main sections."""
        cfg = _make_config()
        d = cfg.to_dict()
        self.assertIn('assumptions', d)
        self.assertIn('property', d)
        self.assertIn('family', d)
        self.assertIn('accounts', d)
        self.assertIn('savings', d)

    def test_to_dict_includes_deduct_later_bracket_target(self):
        """to_dict() includes the deduct_later_bracket_target field (DP#45)."""
        cfg = _make_config()
        d = cfg.to_dict()
        self.assertIn('deduct_later_bracket_target', d['accounts'])
        # DP#13: _make_config sets it to 117045 explicitly
        self.assertEqual(d['accounts']['deduct_later_bracket_target'], 117045)

    def test_from_dict_default_deduct_later(self):
        """from_dict() uses default deduct_later_bracket_target if missing (DP#13: 0 = auto-detect)."""
        cfg = SimulationConfig.from_dict({
            'assumptions': {'projection_years': 3, 'investment_return': 0.07},
            'savings': {'rate': 0.0},
            'property': {'house_value': 400000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120000},
            ]},
            'accounts': {},
        })
        # DP#13: default is 0 (auto-detect from tax brackets), not 117045
        self.assertEqual(cfg.deduct_later_bracket_target, 0)
        # But explicitly setting it works
        cfg_explicit = SimulationConfig.from_dict({
            'assumptions': {'projection_years': 3, 'investment_return': 0.07,
                            'deduct_later_bracket_target': 117045},
            'savings': {'rate': 0.0},
            'property': {'house_value': 400000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120000},
            ]},
            'accounts': {},
        })
        self.assertEqual(cfg_explicit.deduct_later_bracket_target, 117045)


class TestToJson(unittest.TestCase):
    """DP#24: to_json() produces valid JSON and optionally writes to file."""

    def test_to_json_returns_valid_json(self):
        """to_json() returns a parseable JSON string."""
        cfg = _make_config()
        j = cfg.to_json()
        parsed = json.loads(j)
        self.assertEqual(parsed['assumptions']['projection_years'], 5)

    def test_to_json_writes_to_file(self):
        """to_json(path=...) writes JSON to a file."""
        cfg = _make_config()
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
            path = f.name
        try:
            cfg.to_json(path=path)
            with open(path) as f:
                parsed = json.load(f)
            self.assertEqual(parsed['assumptions']['projection_years'], 5)
        finally:
            os.unlink(path)

    def test_to_json_roundtrip_with_from_dict(self):
        """to_json() → json.loads → from_dict() round-trips."""
        cfg = _make_config()
        j = cfg.to_json()
        parsed = json.loads(j)
        cfg2 = SimulationConfig.from_dict(parsed)
        self.assertEqual(cfg.projection_years, cfg2.projection_years)
        self.assertEqual(cfg.deduct_later_bracket_target, cfg2.deduct_later_bracket_target)


class TestOverlayDiff(unittest.TestCase):
    """DP#18, DP#24: overlay_diff shows what changed between configs."""

    def test_same_config_no_diff(self):
        """Identical configs produce empty overlays."""
        cfg = _make_config()
        diff = SimulationConfig.overlay_diff(cfg, cfg)
        self.assertEqual(diff['n_changes'], 0)
        self.assertEqual(len(diff['overlays']), 0)

    def test_ltv_overlay_shows_in_diff(self):
        """LTV overlay changes appear in the diff (DP#18)."""
        cfg = _make_config()
        cfg2 = replace(cfg, ltv_max=0.50)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        self.assertGreater(diff['n_changes'], 0)
        self.assertIn('property.ltv_max', diff['overlays'])

    def test_mortgage_overlay_shows_in_diff(self):
        """Mortgage balance overlay appears in the diff."""
        cfg = _make_config()
        cfg2 = replace(cfg, mortgage_balance=200000)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        self.assertIn('property.mortgage_balance', diff['overlays'])
        self.assertEqual(diff['overlays']['property.mortgage_balance']['from'], 100000)
        self.assertEqual(diff['overlays']['property.mortgage_balance']['to'], 200000)

    def test_deduct_later_overlay_shows_in_diff(self):
        """deduct_later_bracket_target overlay appears in the diff."""
        cfg = _make_config()
        cfg2 = replace(cfg, deduct_later_bracket_target=58523)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        self.assertIn('accounts.deduct_later_bracket_target', diff['overlays'])
        self.assertEqual(diff['overlays']['accounts.deduct_later_bracket_target']['from'], 117045)
        self.assertEqual(diff['overlays']['accounts.deduct_later_bracket_target']['to'], 58523)

    def test_overlay_diff_auditable(self):
        """Overlay diff is human-readable — can be inspected to see what changed."""
        cfg = _make_config()
        cfg2 = replace(cfg, ltv_max=0.65, mortgage_balance=250000)
        diff = SimulationConfig.overlay_diff(cfg, cfg2)
        # Each overlay shows 'from' and 'to' values
        for key, change in diff['overlays'].items():
            self.assertIn('from', change)
            self.assertIn('to', change)


if __name__ == '__main__':
    unittest.main()