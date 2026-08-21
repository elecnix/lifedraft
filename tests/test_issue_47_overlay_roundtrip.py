#!/usr/bin/env python3
"""Tests for DP#24: ScenarioOverlay round-trip serialization.

Tests verify:
- to_dict() produces a serializable dict
- from_dict() reconstructs the overlay
- Round-trip: to_dict() → from_dict() produces equivalent overlay
- to_json() produces valid JSON
- build_overlay_config attaches _overlay metadata for round-trip
"""

import json
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_config import ScenarioOverlay, build_overlay_config
from copy import deepcopy


class TestScenarioOverlayToDict(unittest.TestCase):
    """DP#24: to_dict() produces a serializable dict."""

    def test_minimal_overlay(self):
        overlay = ScenarioOverlay(label="base")
        d = overlay.to_dict()
        self.assertEqual(d['label'], 'base')
        self.assertEqual(d['mortgage_rate'], 0.05)
        # Zero/False/None defaults should be omitted
        self.assertNotIn('cash_out', d)
        self.assertNotIn('use_readvanceable', d)
        self.assertNotIn('deduct_later', d)
        self.assertNotIn('investment_return', d)
        self.assertNotIn('ltv', d)

    def test_full_overlay(self):
        overlay = ScenarioOverlay(
            label="SM+", cash_out=200000, resp_cash_out=10000,
            primary_income=160000, spouse_income=70000, mortgage_rate=0.06,
            use_readvanceable=True, deduct_later=True,
            investment_return=0.05, ltv=0.45,
        )
        d = overlay.to_dict()
        self.assertEqual(d['label'], 'SM+')
        self.assertEqual(d['cash_out'], 200000)
        self.assertEqual(d['resp_cash_out'], 10000)
        self.assertEqual(d['primary_income'], 160000)
        self.assertEqual(d['spouse_income'], 70000)
        self.assertEqual(d['mortgage_rate'], 0.06)
        self.assertTrue(d['use_readvanceable'])
        self.assertTrue(d['deduct_later'])
        self.assertEqual(d['investment_return'], 0.05)
        self.assertEqual(d['ltv'], 0.45)


class TestScenarioOverlayFromDict(unittest.TestCase):
    """DP#24: from_dict() reconstructs overlay from dict."""

    def test_minimal_round_trip(self):
        original = ScenarioOverlay(label="base")
        d = original.to_dict()
        restored = ScenarioOverlay.from_dict(d)
        self.assertEqual(original.label, restored.label)
        self.assertEqual(original.cash_out, restored.cash_out)
        self.assertEqual(original.mortgage_rate, restored.mortgage_rate)

    def test_full_round_trip(self):
        original = ScenarioOverlay(
            label="SM+", cash_out=200000, resp_cash_out=10000,
            primary_income=160000, spouse_income=70000, mortgage_rate=0.06,
            use_readvanceable=True, deduct_later=True,
            investment_return=0.05, ltv=0.45,
        )
        d = original.to_dict()
        restored = ScenarioOverlay.from_dict(d)
        self.assertEqual(original.label, restored.label)
        self.assertEqual(original.cash_out, restored.cash_out)
        self.assertEqual(original.resp_cash_out, restored.resp_cash_out)
        self.assertEqual(original.primary_income, restored.primary_income)
        self.assertEqual(original.spouse_income, restored.spouse_income)
        self.assertEqual(original.mortgage_rate, restored.mortgage_rate)
        self.assertEqual(original.use_readvanceable, restored.use_readvanceable)
        self.assertEqual(original.deduct_later, restored.deduct_later)
        self.assertEqual(original.investment_return, restored.investment_return)
        self.assertEqual(original.ltv, restored.ltv)

    def test_from_dict_with_explicit_defaults(self):
        """from_dict() with all fields explicitly set."""
        data = {
            'label': 'explicit',
            'cash_out': 0.0,
            'resp_cash_out': 0.0,
            'primary_income': 0.0,
            'spouse_income': 0.0,
            'mortgage_rate': 0.05,
            'use_readvanceable': False,
            'deduct_later': False,
            'investment_return': None,
            'ltv': None,
        }
        overlay = ScenarioOverlay.from_dict(data)
        self.assertEqual(overlay.label, 'explicit')
        self.assertEqual(overlay.cash_out, 0.0)
        self.assertIsNone(overlay.investment_return)


class TestScenarioOverlayToJson(unittest.TestCase):
    """DP#24: to_json() produces valid JSON."""

    def test_json_is_valid(self):
        overlay = ScenarioOverlay(label="test", cash_out=100000)
        json_str = overlay.to_json()
        parsed = json.loads(json_str)
        self.assertEqual(parsed['label'], 'test')
        self.assertEqual(parsed['cash_out'], 100000)

    def test_json_round_trip(self):
        original = ScenarioOverlay(
            label="SM+", cash_out=200000, use_readvanceable=True, ltv=0.35,
        )
        json_str = original.to_json()
        parsed = json.loads(json_str)
        restored = ScenarioOverlay.from_dict(parsed)
        self.assertEqual(original.label, restored.label)
        self.assertEqual(original.cash_out, restored.cash_out)
        self.assertEqual(original.use_readvanceable, restored.use_readvanceable)
        self.assertEqual(original.ltv, restored.ltv)


class TestBuildOverlayConfigRoundTrip(unittest.TestCase):
    """DP#24: build_overlay_config attaches _overlay for round-trip."""

    def _base_cfg(self):
        return {
            'family': {
                'members': [
                    {'role': 'primary', 'name': 'P', 'gross_income': 160000,
                     'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 80000},
                    {'role': 'spouse', 'name': 'S', 'gross_income': 70000,
                     'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 70000},
                ],
                'children': []
            },
            'property': {
                'house_value': 900000,
                'mortgage_balance': 400000,
                'mortgage_rate': 0.0495,
                'margin_available': 50000,
                'ltv_max': 0.80,
            },
            'accounts': {
                'resp_current_balance': 30000,
            },
            'assumptions': {
                'investment_return': 0.07,
                'inflation': 0.02,
                'projection_years': 25,
            },
            'scenarios': {
                'income': [],
                'mortgage': [],
            },
        }

    def test_derived_config_has_overlay_metadata(self):
        base = self._base_cfg()
        overlay = ScenarioOverlay(label="SM+", cash_out=200000, use_readvanceable=True,
                                   refinance_amortization_years=25)
        derived = build_overlay_config(base, overlay)
        self.assertIn('_overlay', derived)
        self.assertEqual(derived['_overlay']['label'], 'SM+')
        self.assertEqual(derived['_overlay']['cash_out'], 200000)

    def test_overlay_metadata_round_trips(self):
        base = self._base_cfg()
        overlay = ScenarioOverlay(
            label="test", cash_out=150000, primary_income=180000,
            mortgage_rate=0.06, use_readvanceable=True,
            investment_return=0.05, ltv=0.44,
            refinance_amortization_years=25,
        )
        derived = build_overlay_config(base, overlay)
        
        # Recover overlay from metadata
        recovered = ScenarioOverlay.from_dict(derived['_overlay'])
        
        self.assertEqual(recovered.label, overlay.label)
        self.assertEqual(recovered.cash_out, overlay.cash_out)
        self.assertEqual(recovered.primary_income, overlay.primary_income)
        self.assertEqual(recovered.mortgage_rate, overlay.mortgage_rate)
        self.assertEqual(recovered.use_readvanceable, overlay.use_readvanceable)
        self.assertEqual(recovered.investment_return, overlay.investment_return)
        self.assertAlmostEqual(recovered.ltv, overlay.ltv)

    def test_overlay_metadata_is_json_serializable(self):
        base = self._base_cfg()
        overlay = ScenarioOverlay(label="test", cash_out=100000, deduct_later=True,
                                   refinance_amortization_years=25)
        derived = build_overlay_config(base, overlay)
        
        # The entire derived config should be JSON-serializable
        json_str = json.dumps(derived['_overlay'])
        parsed = json.loads(json_str)
        self.assertEqual(parsed['label'], 'test')

    def test_base_config_not_mutated(self):
        base = self._base_cfg()
        overlay = ScenarioOverlay(label="test", cash_out=200000, refinance_amortization_years=25)
        derived = build_overlay_config(base, overlay)
        # Base should not have _overlay
        self.assertNotIn('_overlay', base)
        # Base should not be mutated
        self.assertEqual(base['property']['mortgage_balance'], 400000)


if __name__ == '__main__':
    unittest.main()