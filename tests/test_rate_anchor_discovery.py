"""Tests for rate anchor discovery from input.json.

DP#5: Anchors represent actual decisions the user is weighing.
This module tests that refinance_options and renewal_options
from input.json are auto-discovered and generate valid anchor presets.
"""

import json
import pytest
from copy import deepcopy

from optimize import discover_rate_anchors, apply_anchor_preset, compose_preset, run_rate_comparison, print_rate_comparison


# ── Fixtures ────────────────────────────────────────────────────────────────

def _base_config():
    """Minimal config with property, assumptions, and heloc sections."""
    return {
        'property': {
            'house_value': 500000,
            'mortgage_balance': 200000,
            'mortgage_rate': 0.0495,
            'ltv_max': 0.80,
            'margin_available': 100000,
        },
        'assumptions': {
            'investment_return': 0.07,
            'heloc_rate': 0.0495,
        },
        'heloc': {
            'balance': 100000,
            'rate': 0.0495,
            'rate_type': 'variable',
        },
    }


def _config_with_bnc_rates():
    """Config with BNC refinance and renewal options."""
    cfg = _base_config()
    cfg['property']['renewal_options'] = [
        {
            'name': '3-year fixed renewal',
            'rate': 0.0379,
            'term_years': 3,
            'type': 'fixed',
            'renewal_rate_assumption': 0.05,
        },
        {
            'name': '5-year variable renewal',
            'rate': 0.035,
            'term_years': 5,
            'type': 'variable',
            'renewal_rate_assumption': 0.045,
        },
    ]
    cfg['property']['refinance_options'] = [
        {
            'name': '3-year fixed',
            'rate': 0.0404,
            'term_years': 3,
            'type': 'fixed',
            'renewal_rate_assumption': 0.05,
        },
        {
            'name': '5-year variable',
            'rate': 0.0375,
            'term_years': 5,
            'type': 'variable',
            'renewal_rate_assumption': 0.045,
        },
    ]
    return cfg


# ── discover_rate_anchors tests ────────────────────────────────────────────

class TestDiscoverRateAnchors:
    """Test auto-discovery of rate anchors from input.json."""

    def test_discovers_current_rate(self):
        """Current rate is always discovered from property.mortgage_rate."""
        cfg = _base_config()
        anchors = discover_rate_anchors(cfg)
        assert 'renew_current' in anchors
        assert anchors['renew_current']['mortgage_rate'] == 0.0495
        assert anchors['renew_current']['source'] == 'current'

    def test_discovers_renewal_options(self):
        """Renewal options are discovered from property.renewal_options."""
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        assert '3_year_fixed_renewal' in anchors
        assert anchors['3_year_fixed_renewal']['mortgage_rate'] == 0.0379
        assert anchors['3_year_fixed_renewal']['source'] == 'renewal'
        assert anchors['3_year_fixed_renewal']['type'] == 'fixed'

    def test_discovers_refinance_options(self):
        """Refinance options are discovered from property.refinance_options."""
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        assert '3_year_fixed' in anchors
        assert anchors['3_year_fixed']['mortgage_rate'] == 0.0404
        assert anchors['3_year_fixed']['source'] == 'refinance'
        assert anchors['3_year_fixed']['term_years'] == 3

    def test_discovers_all_bnc_rates(self):
        """All BNC rates (2 renewal + 2 refinance + 1 current) are discovered."""
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        # Current + 2 renewal + 2 refinance = 5 anchors
        assert len(anchors) == 5
        assert 'renew_current' in anchors
        assert '3_year_fixed_renewal' in anchors
        assert '5_year_variable_renewal' in anchors
        assert '3_year_fixed' in anchors
        assert '5_year_variable' in anchors

    def test_no_options_returns_current_only(self):
        """Without renewal/refinance options, only current rate is discovered."""
        cfg = _base_config()
        anchors = discover_rate_anchors(cfg)
        assert len(anchors) == 1
        assert 'renew_current' in anchors

    def test_heloc_rate_matches_mortgage_rate(self):
        """Discovered anchors set heloc_rate equal to mortgage_rate."""
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        for name, anchor in anchors.items():
            assert 'heloc_rate' in anchor
            assert anchor['heloc_rate'] == anchor['mortgage_rate']

    def test_renewal_rate_assumption_preserved(self):
        """renewal_rate_assumption is preserved in discovered anchors."""
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        assert anchors['3_year_fixed']['renewal_rate_assumption'] == 0.05
        assert anchors['5_year_variable']['renewal_rate_assumption'] == 0.045

    def test_anchor_key_sanitization(self):
        """Anchor keys are sanitized (spaces/dashes → underscores)."""
        cfg = _base_config()
        cfg['property']['refinance_options'] = [
            {'name': 'BNC Variable 5yr', 'rate': 0.0375, 'term_years': 5, 'type': 'variable'}
        ]
        anchors = discover_rate_anchors(cfg)
        assert 'bnc_variable_5yr' in anchors


# ── apply_anchor_preset tests ──────────────────────────────────────────────

class TestApplyAnchorPreset:
    """Test applying discovered anchor presets to config."""

    def test_hardcoded_preset_still_works(self):
        """Hardcoded ANCHOR_PRESETS still work when no discovered anchors."""
        cfg = _base_config()
        result = apply_anchor_preset(cfg, 'renew_current')
        # Should use hardcoded value (5%) since no discovered anchors override it
        assert result['property']['mortgage_rate'] == 0.05

    def test_discovered_anchor_overrides_rate(self):
        """Discovered anchors override mortgage and HELOC rates.

        assumptions.heloc_rate is the one spelling the engine actually
        reads (scenario_discovery.py). The apply_anchor_preset write into
        cfg['heloc']['rate'] was deleted in epic #603 Track C Phase 2
        (DP#9/#595-B): heloc.rate never had a production reader even before
        the whole heloc.* block was deleted outright, so writing it was
        always a no-op duplicate of the assumptions.heloc_rate write below.
        """
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        result = apply_anchor_preset(cfg, '3_year_fixed', discovered_anchors=anchors)
        assert result['property']['mortgage_rate'] == 0.0404
        assert result['assumptions']['heloc_rate'] == 0.0404

    def test_discovered_anchor_does_not_mutate_original(self):
        """Applying an anchor preset does not mutate the original config."""
        cfg = _config_with_bnc_rates()
        original_rate = cfg['property']['mortgage_rate']
        original_heloc = cfg['heloc']['rate']
        anchors = discover_rate_anchors(cfg)
        result = apply_anchor_preset(cfg, '5_year_variable', discovered_anchors=anchors)
        assert cfg['property']['mortgage_rate'] == original_rate
        assert cfg['heloc']['rate'] == original_heloc

    def test_missing_anchor_returns_unchanged(self):
        """Applying a non-existent anchor name returns config unchanged."""
        cfg = _base_config()
        result = apply_anchor_preset(cfg, 'nonexistent_anchor')
        assert result == cfg


# ── compose_preset tests ───────────────────────────────────────────────────

class TestComposePresetWithDiscovered:
    """Test compose_preset with discovered anchors."""

    def test_anchor_with_overlay(self):
        """Compose preset with discovered anchor + sensitivity overlay."""
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        result = compose_preset(cfg, anchor_name='3_year_fixed',
                                overlay_name='conservative',
                                discovered_anchors=anchors)
        assert result['property']['mortgage_rate'] == 0.0404
        # DP#21/#591: the swept rate lands in return_model, the engine's
        # single source of truth (not the deprecated assumptions scalar).
        assert result['return_model']['rate'] == 0.05

    def test_anchor_only(self):
        """Compose preset with discovered anchor only (no overlay)."""
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        result = compose_preset(cfg, anchor_name='5_year_variable',
                                discovered_anchors=anchors)
        assert result['property']['mortgage_rate'] == 0.0375
        # Investment return should be unchanged
        assert result['assumptions']['investment_return'] == 0.07

    def test_overlay_only(self):
        """Compose preset with overlay only (no anchor)."""
        cfg = _config_with_bnc_rates()
        result = compose_preset(cfg, overlay_name='aggressive')
        # Mortgage rate should be unchanged
        assert result['property']['mortgage_rate'] == 0.0495
        assert result['return_model']['rate'] == 0.09


# ── CLI integration tests ──────────────────────────────────────────────────

class TestCLIIntegration:
    """Test CLI argument parsing and --compare-rates mode."""

    def test_list_anchors_flag(self):
        """--list-anchors should list all available anchor presets."""
        # Test discover_rate_anchors directly (CLI subprocess depends on local paths)
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        assert 'renew_current' in anchors
        assert '3_year_fixed' in anchors
        assert '5_year_variable' in anchors
        assert '3_year_fixed_renewal' in anchors
        assert '5_year_variable_renewal' in anchors

    def test_compare_rates_flag(self):
        """--compare-rates should produce a rate comparison with valid structure."""
        # Test the rate comparison structure directly (full optimizer needs complete config)
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        
        # Verify we can compose each anchor preset
        results = []
        for anchor_name, anchor in anchors.items():
            cfg_anchor = compose_preset(cfg, anchor_name=anchor_name,
                                         discovered_anchors=anchors)
            results.append({
                'anchor_name': anchor_name,
                'mortgage_rate': cfg_anchor['property']['mortgage_rate'],
                'heloc_rate': cfg_anchor['heloc']['rate'],
                'label': anchor.get('label', anchor_name),
            })
        
        # Verify all anchors produce valid configs
        assert len(results) == 5  # current + 2 renewal + 2 refinance
        for r in results:
            assert r['mortgage_rate'] > 0
            assert r['heloc_rate'] > 0
            assert r['label']
        
        # Verify ordering: lowest rate first
        sorted_results = sorted(results, key=lambda r: r['mortgage_rate'])
        assert sorted_results[0]['mortgage_rate'] == 0.035  # 5-year variable renewal
        assert sorted_results[-1]['mortgage_rate'] == 0.0495  # current rate

    def test_anchor_with_discovered_name(self):
        """--anchor with a discovered name should apply the correct rate."""
        # Test apply_anchor_preset directly (CLI subprocess depends on local paths)
        cfg = _config_with_bnc_rates()
        anchors = discover_rate_anchors(cfg)
        result = apply_anchor_preset(cfg, '5_year_variable', discovered_anchors=anchors)
        assert result['property']['mortgage_rate'] == 0.0375
        # assumptions.heloc_rate is the one spelling the engine reads; the
        # cfg['heloc']['rate'] write was deleted (epic #603 Track C Phase 2,
        # DP#9/#595-B) -- see test_discovered_anchor_overrides_rate.
        assert result['assumptions']['heloc_rate'] == 0.0375