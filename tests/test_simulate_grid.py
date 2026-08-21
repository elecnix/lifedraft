#!/usr/bin/env python3
"""Tests for enumerate_scenarios — DP#5, DP#14, DP#18 scenarios compose from base.

Tests verify:
- ScenarioOverlay creation and defaults
- build_overlay_config produces valid derived configs
- enumerate_overlays generates all decision combos
- evaluate_overlay calls simulation correctly
- Sensitivity overlays work correctly
"""
from tax_data import default_tax_provider
import json
import pytest
from copy import deepcopy
from simulation import ScenarioOverlay, build_overlay_config
from simulate import (
    enumerate_overlays,
    evaluate_overlay,
)


def _base_cfg():
    """Minimal base config for testing."""
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


class TestScenarioOverlay:
    """Test ScenarioOverlay dataclass defaults."""

    def test_default_values(self):
        overlay = ScenarioOverlay(label="base")
        assert overlay.cash_out == 0.0
        assert overlay.resp_cash_out == 0.0
        # DP#18: income defaults to None (no change from base)
        assert overlay.primary_income is None
        assert overlay.spouse_income is None
        assert overlay.mortgage_rate == 0.05  # DP#13: round-number placeholder
        assert overlay.use_readvanceable is False
        assert overlay.deduct_later is False
        assert overlay.investment_return is None
        assert overlay.ltv is None

    def test_custom_values(self):
        overlay = ScenarioOverlay(
            label="SM+", cash_out=200000, use_readvanceable=True,
            deduct_later=True, investment_return=0.06,
        )
        assert overlay.cash_out == 200000
        assert overlay.use_readvanceable is True
        assert overlay.deduct_later is True
        assert overlay.investment_return == 0.06


class TestBuildOverlayConfig:
    """Test that overlay produces valid derived configs from base (DP#18)."""

    def test_overlay_is_deepcopy_not_mutation(self):
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", primary_income=180000)
        derived = build_overlay_config(base, overlay)
        # Base should not be mutated
        assert base['family']['members'][0]['gross_income'] == 160000
        # Derived should have overridden income
        assert derived['family']['members'][0]['gross_income'] == 180000

    def test_spouse_income_override(self):
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", spouse_income=90000)
        derived = build_overlay_config(base, overlay)
        assert derived['family']['members'][1]['gross_income'] == 90000

    def test_mortgage_rate_override(self):
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", mortgage_rate=0.06)
        derived = build_overlay_config(base, overlay)
        assert derived['property']['mortgage_rate'] == 0.06

    def test_cashout_increases_mortgage_and_shrinks_margin(self):
        """issue #257: a cash-out refinance is a MORTGAGE increase, its
        proceeds invested via cash_out.

        issue #664: margin_available represents pre-existing undrawn HELOC
        room sharing ONE registered charge with the mortgage -- it must
        shrink dollar-for-dollar by the cash-out booked (not stay untouched;
        that was the #664 bug -- letting the household draw the FULL
        pre-existing HELOC limit *in addition to* the refinanced mortgage).
        """
        base = _base_cfg()
        orig_margin = base['property']['margin_available']
        orig_mortgage = base['property']['mortgage_balance']
        cash_out = 150000
        overlay = ScenarioOverlay(label="test", cash_out=cash_out,
                                   refinance_amortization_years=25)  # #655
        derived = build_overlay_config(base, overlay)
        # margin_available shrinks by exactly the cash-out booked (#664)
        assert derived['property']['margin_available'] == max(0.0, orig_margin - cash_out)
        # mortgage carries the cash-out (the refinance)
        assert derived['property']['mortgage_balance'] == orig_mortgage + cash_out
        # cash_out recorded so its proceeds are invested in the lump sum
        assert derived['property']['cash_out'] == cash_out

    def test_cashout_computes_ltv(self):
        base = _base_cfg()
        cash_out = 150000
        overlay = ScenarioOverlay(label="test", cash_out=cash_out,
                                   refinance_amortization_years=25)  # #655
        derived = build_overlay_config(base, overlay)
        expected_ltv = (base['property']['mortgage_balance'] + cash_out) / base['property']['house_value']
        assert abs(derived['property']['ltv_max'] - expected_ltv) < 0.001

    def test_resp_cashout_reduces_balance(self):
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", resp_cash_out=10000)
        derived = build_overlay_config(base, overlay)
        # RESP is zeroed out; net proceeds added as free_cash (not margin debt)
        assert derived['accounts']['resp_current_balance'] == 0
        assert derived['property']['free_cash'] == 10000

    def test_investment_return_overlay(self):
        # #260: return_model is the single source of truth; the overlay writes the
        # swept rate there (not the deprecated assumptions.investment_return scalar).
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", investment_return=0.05)
        derived = build_overlay_config(base, overlay)
        assert derived['return_model']['rate'] == 0.05

    def test_no_income_overlay_preserves_original(self):
        """DP#18: When income is not set in overlay, base income is preserved."""
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test")  # n_income=None, spouse_income=None
        derived = build_overlay_config(base, overlay)
        # Base income should be preserved, not zeroed out
        assert derived['family']['members'][0]['gross_income'] == 160000
        assert derived['family']['members'][1]['gross_income'] == 70000

    def test_no_investment_return_overlay_preserves_original(self):
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test")  # investment_return=None
        derived = build_overlay_config(base, overlay)
        assert derived['assumptions']['investment_return'] == 0.07

    def test_zero_cashout_preserves_original(self):
        base = _base_cfg()
        overlay = ScenarioOverlay(label="test", cash_out=0)
        derived = build_overlay_config(base, overlay)
        assert derived['property']['margin_available'] == base['property']['margin_available']
        assert derived['property']['mortgage_balance'] == base['property']['mortgage_balance']


class TestEnumerateOverlays:
    """Test that enumerate_overlays generates correct decision combos (DP#5)."""

    def test_overlay_count_matches_combinations(self):
        base = _base_cfg()
        from scenario_discovery import discover_anchors
        anchors = discover_anchors(base)

        # Mirror the expansion logic from enumerate_overlays
        sm_options = list(anchors['sm_options'])
        dl_options = list(anchors['deduct_later_options'])
        # enumerate_overlays expands SM when property has equity
        if True not in sm_options and base['property'].get('margin_available', 0) > 0:
            sm_options = [True, False]
        # enumerate_overlays expands DL when bracket gap exists
        if True not in dl_options:
            from tax_calculator import marginal_rate
            brackets = default_tax_provider().get_combined_brackets()
            members = base['family']['members']
            p_inc = next((m['gross_income'] for m in members if m['role'] == 'primary'), 0)
            s_inc = next((m['gross_income'] for m in members if m['role'] == 'spouse'), 0)
            if marginal_rate(p_inc, brackets) > marginal_rate(s_inc, brackets):
                dl_options = [True, False]

        expected = (len(anchors['refinance']) * len(anchors['income']) *
                   len(anchors['mortgage']) * len(sm_options) * len(dl_options))
        # Plus RESP overlays (only non-'keep' actions)
        resp_count = sum(1 for a in anchors['resp_action'] if a != 'keep')
        resp_overlays = (resp_count * len(anchors['refinance']) *
                        len(anchors['income']) * 1 * len(sm_options) * len(dl_options))
        expected += resp_overlays
        overlays = enumerate_overlays(base)
        assert len(overlays) == expected

    def test_all_refinance_levels_present(self):
        base = _base_cfg()
        overlays = enumerate_overlays(base)
        labels = [o.label for o in overlays]
        # Check we have the 3 refi levels in labels (actual labels from input.json)
        refi_parts = set()
        for l in labels:
            parts = l.split('|')
            if len(parts) >= 3:
                refi_parts.add(parts[2].strip())
        assert any('Renew current mortgage' in p or 'No Refinance' in p for p in refi_parts)

    def test_smith_and_deduct_variations(self):
        base = _base_cfg()
        overlays = enumerate_overlays(base)
        smith_count = sum(1 for o in overlays if o.use_readvanceable)
        dl_count = sum(1 for o in overlays if o.deduct_later)
        assert smith_count > 0
        assert dl_count > 0
        assert smith_count == len(overlays) // 2  # half have SM

    def test_base_income_used_when_no_scenarios(self):
        base = _base_cfg()
        overlays = enumerate_overlays(base)
        # All should use base income
        for o in overlays:
            assert o.primary_income == 160000  # primary from base config
            assert o.spouse_income == 70000   # spouse from base config

    def test_uses_discover_anchors(self):
        base = _base_cfg()
        from scenario_discovery import discover_anchors
        anchors = discover_anchors(base)
        overlays = enumerate_overlays(base)

        # Verify refi levels from anchors are represented in overlay labels
        _refi_abbrevs = {
            'no_refinance': 'No Refinance',
            'min_refi': 'Fill Registered Room',
            'max_refi': 'Maximum Refinance (80%)',
        }
        expected_refi_labels = set(
            _refi_abbrevs.get(r['id'], r['label']) for r in anchors['refinance']
        )
        actual_refi_labels = set(o.label.split('|')[2].strip() for o in overlays)
        assert expected_refi_labels == actual_refi_labels

        # Verify SM options from anchors are represented (overlays may expand)
        sm_values = set(o.use_readvanceable for o in overlays)
        assert set(anchors['sm_options']).issubset(sm_values)

    def test_ltv_computed_for_each_overlay(self):
        base = _base_cfg()
        overlays = enumerate_overlays(base)
        for o in overlays:
            assert o.ltv is not None
            if o.cash_out == 0:
                # No refi: LTV should be original mortgage / house value
                expected = base['property']['mortgage_balance'] / base['property']['house_value']
                assert abs(o.ltv - expected) < 0.001


class TestEvaluateOverlay:
    """Test evaluate_overlay integration with simulation (DP#14)."""

    @pytest.fixture
    def real_cfg(self):
        """Load the actual input.json for full integration tests."""
        try:
            with open('input.json') as f:
                return json.load(f)
        except FileNotFoundError:
            pytest.skip('input.json not available')

    def test_evaluate_no_refi_returns_result(self, real_cfg):
        overlay = ScenarioOverlay(label="no refi", cash_out=0, use_readvanceable=False, deduct_later=False)
        result = evaluate_overlay(real_cfg, overlay)
        assert result is not None
        assert 'net_benefit' in result
        assert 'label' in result

    def test_evaluate_overlay_preserves_metadata(self, real_cfg):
        overlay = ScenarioOverlay(label="test SM", cash_out=0, use_readvanceable=True, deduct_later=True)
        result = evaluate_overlay(real_cfg, overlay)
        assert result['use_readvanceable'] is True
        assert result['deduct_later'] is True
        assert result['cash_out'] == 0

    def test_cash_out_scenario_has_higher_debt(self, real_cfg):
        no_refi = ScenarioOverlay(label="no refi", cash_out=0)
        # Get LTV from base config
        house = real_cfg['property']['house_value']
        mortgage = real_cfg['property']['mortgage_balance']
        max_cashout = max(0, house * real_cfg['property'].get('ltv_max', 0.80) - mortgage)
        # Use a modest cash out
        moderate_cashout = min(100000, max_cashout) if max_cashout > 0 else 0
        with_refi = ScenarioOverlay(label="refi", cash_out=moderate_cashout,
                                       ltv=(mortgage + moderate_cashout) / house)
        result_no = evaluate_overlay(real_cfg, no_refi)
        result_with = evaluate_overlay(real_cfg, with_refi)
        # Cash-out scenario should have higher total debt
        assert result_with['total_debt'] >= result_no['total_debt']

    def test_strategy_alloc_used_in_evaluate(self, real_cfg):
        """Verify evaluate_overlay uses strategy_alloc when provided."""
        overlay = ScenarioOverlay(label="test alloc", cash_out=0,
                                  use_readvanceable=False, deduct_later=False)

        # Default: auto-discover from base_cfg
        result_default = evaluate_overlay(real_cfg, overlay)

        # Custom: heavy RRSP allocation
        custom_alloc = {
            'rrsp_pct': 0.60,
            'spousal_rrsp_pct': 0.10,
            'tfsa_pct': 0.05,
            'fhsa_pct': 0.0,
            'resp_pct': 0.0,
            'non_reg_pct': 0.25,
        }
        result_custom = evaluate_overlay(real_cfg, overlay, strategy_alloc=custom_alloc)

        # Custom alloc should produce different (higher) RRSP than default
        assert result_custom['RRSP'] != result_default['RRSP']
        assert result_custom['RRSP'] > result_default['RRSP']
        # And different TFSA
        assert result_custom['TFSA'] != result_default['TFSA']


class TestScenarioSectionsOverride:
    """Test that scenarios sections in config override auto-discovery."""

    def test_scenario_sections_override_discovery(self):
        base = _base_cfg()
        from scenario_discovery import discover_anchors

        # Without override: auto-discovery returns 1 income scenario
        anchors_auto = discover_anchors(base)
        assert len(anchors_auto['income']) == 1
        assert anchors_auto['income'][0]['id'] == 'current'

        # With override: provide custom income scenario
        overridden = deepcopy(base)
        overridden['scenarios']['income'] = [{
            'id': 'raise',
            'label': '5% raise',
            'members': [
                {'role': 'primary', 'gross_income': 168000,
                 'kind': 'employment', 'from': '2026-01-01', 'to': None},
                {'role': 'spouse', 'gross_income': 70000,
                 'kind': 'employment', 'from': '2026-01-01', 'to': None},
            ],
        }]
        anchors_override = discover_anchors(overridden)
        assert len(anchors_override['income']) == 1
        assert anchors_override['income'][0]['id'] == 'raise'
        assert anchors_override['income'][0]['primary_income'] == 168000

        # Verify overlays use the overridden income
        overlays = enumerate_overlays(overridden)
        for o in overlays:
            assert o.primary_income == 168000

class TestScenarioOverlayFields:
    """ScenarioOverlay uses primary_income/spouse_income field names."""

    def test_primary_income_set_and_get(self):
        overlay = ScenarioOverlay(label="test", primary_income=180000)
        assert overlay.primary_income == 180000

    def test_spouse_income_set_and_get(self):
        overlay = ScenarioOverlay(label="test", spouse_income=90000)
        assert overlay.spouse_income == 90000

    def test_primary_income_no_warning(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            overlay = ScenarioOverlay(label="test", primary_income=180000)
            assert overlay.primary_income == 180000
            assert len(w) == 0

    def test_spouse_income_no_warning(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            overlay = ScenarioOverlay(label="test", spouse_income=90000)
            assert overlay.spouse_income == 90000
            assert len(w) == 0
