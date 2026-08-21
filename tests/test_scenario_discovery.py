"""Unit tests for scenario_discovery.discover_anchors().

All data is fabricated with round numbers — no personal data (DP#4, DP#15).
Tests verify trigger-based discovery: modules activate when their trigger
data is present, and disappear when it is absent (DP#16).
"""

import pytest
from scenario_discovery import discover_anchors
# DP#25 (#998): scenario_discovery no longer imports the simulation layer at
# runtime; its simulation callables (marginal_rate, strategy types/engine, rate
# resolvers) are injected. Importing simulation_deps configures the injection
# point at import time, the same way the entry points (simulate/optimize) do.
import simulation_deps  # noqa: F401  (import side-effect: injects SimulationDeps)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_cfg():
    """Return a minimal valid config with fabricated round numbers."""
    return {
        'family': {
            'members': [
                {
                    'role': 'primary',
                    'gross_income': 150000,
                    'rrsp_room_accumulated': 30000,
                    'tfsa_room_accumulated': 40000,
                    'fhsa_first_time_buyer_since': None,
                    'fhsa_room_accumulated': 0,
                },
                {
                    'role': 'spouse',
                    'gross_income': 70000,
                    'rrsp_room_accumulated': 20000,
                    'tfsa_room_accumulated': 40000,
                    'fhsa_first_time_buyer_since': None,
                    'fhsa_room_accumulated': 0,
                },
            ],
            'children': [],
        },
        'property': {
            'house_value': 800000,
            'mortgage_balance': 300000,
            'mortgage_rate': 0.05,
            'margin_available': 50000,
            'ltv_max': 0.80,
            'heloc_readvance': True,
        },
        'accounts': {'resp_current_balance': 0},
        'assumptions': {'investment_return': 0.07},
        'scenarios': {},
    }


# ---------------------------------------------------------------------------
# 1. TestDiscoverAnchorsBasic
# ---------------------------------------------------------------------------

class TestDiscoverAnchorsBasic:
    """Smoke test: all keys present, all lists non-empty (for base config)."""

    EXPECTED_KEYS = [
        'income', 'mortgage', 'refinance', 'strategy',
        'resp_action', 'sm_options', 'deduct_later_options', 'child_accounts',
    ]

    def test_all_keys_present(self):
        anchors = discover_anchors(_base_cfg())
        for key in self.EXPECTED_KEYS:
            assert key in anchors, f"Missing key: {key}"

    def test_income_non_empty(self):
        anchors = discover_anchors(_base_cfg())
        assert len(anchors['income']) > 0

    def test_mortgage_non_empty(self):
        anchors = discover_anchors(_base_cfg())
        assert len(anchors['mortgage']) > 0

    def test_refinance_non_empty(self):
        anchors = discover_anchors(_base_cfg())
        assert len(anchors['refinance']) > 0

    def test_strategy_non_empty(self):
        anchors = discover_anchors(_base_cfg())
        assert len(anchors['strategy']) > 0

    def test_resp_action_non_empty(self):
        anchors = discover_anchors(_base_cfg())
        assert len(anchors['resp_action']) > 0

    def test_sm_options_non_empty(self):
        anchors = discover_anchors(_base_cfg())
        assert len(anchors['sm_options']) > 0

    def test_deduct_later_options_non_empty(self):
        anchors = discover_anchors(_base_cfg())
        assert len(anchors['deduct_later_options']) > 0

    def test_child_accounts_is_list(self):
        anchors = discover_anchors(_base_cfg())
        assert isinstance(anchors['child_accounts'], list)


# ---------------------------------------------------------------------------
# 2. TestIncomeDiscovery
# ---------------------------------------------------------------------------

class TestIncomeDiscovery:
    """Auto from members vs scenarios.income override, role-based matching."""

    def test_auto_uses_member_incomes(self):
        anchors = discover_anchors(_base_cfg())
        inc = anchors['income']
        assert len(inc) == 1
        assert inc[0]['primary_income'] == 150000
        assert inc[0]['spouse_income'] == 70000

    def test_auto_id_is_current(self):
        anchors = discover_anchors(_base_cfg())
        assert anchors['income'][0]['id'] == 'current'

    def test_override_replaces_auto(self):
        cfg = _base_cfg()
        cfg['scenarios']['income'] = [
            {
                'id': 'job_switch',
                'label': 'Job switch',
                'members': [
                    {'role': 'primary', 'gross_income': 180000,
                     'kind': 'employment', 'from': '2026-01-01', 'to': None},
                    {'role': 'spouse', 'gross_income': 70000,
                     'kind': 'employment', 'from': '2026-01-01', 'to': None},
                ],
            }
        ]
        anchors = discover_anchors(cfg)
        inc = anchors['income']
        assert len(inc) == 1
        assert inc[0]['id'] == 'job_switch'
        assert inc[0]['primary_income'] == 180000
        assert inc[0]['spouse_income'] == 70000

    def test_role_based_matching_primary(self):
        cfg = _base_cfg()
        cfg['scenarios']['income'] = [
            {
                'id': 'promo',
                'label': 'Promotion',
                'members': [
                    {'role': 'spouse', 'gross_income': 90000,
                     'kind': 'employment', 'from': '2026-01-01', 'to': None},
                    {'role': 'primary', 'gross_income': 200000,
                     'kind': 'employment', 'from': '2026-01-01', 'to': None},
                ],
            }
        ]
        anchors = discover_anchors(cfg)
        assert anchors['income'][0]['primary_income'] == 200000
        assert anchors['income'][0]['spouse_income'] == 90000


# ---------------------------------------------------------------------------
# 3. TestMortgageDiscovery
# ---------------------------------------------------------------------------

class TestMortgageDiscovery:
    """Auto from refinance_options/renewal_options vs scenarios.mortgage override,
    rate_path='current'."""

    def test_auto_fallback_to_current_rate(self):
        """No refinance_options or renewal_options → one entry with current rate."""
        anchors = discover_anchors(_base_cfg())
        mtg = anchors['mortgage']
        assert len(mtg) == 1
        assert mtg[0]['rate'] == 0.05
        assert mtg[0]['id'] == 'current'

    def test_auto_from_refinance_options(self):
        cfg = _base_cfg()
        cfg['property']['refinance_options'] = [
            {'name': '3yr Fixed', 'rate': 0.045, 'type': 'fixed', 'term_years': 3},
        ]
        anchors = discover_anchors(cfg)
        mtg = anchors['mortgage']
        assert any(m['id'] == 'refi_3yr_fixed' for m in mtg)
        refi = next(m for m in mtg if m['id'] == 'refi_3yr_fixed')
        assert refi['rate'] == 0.045
        assert refi['type'] == 'fixed'
        assert refi['term_years'] == 3

    def test_auto_from_renewal_options(self):
        cfg = _base_cfg()
        cfg['property']['renewal_options'] = [
            {'name': '5yr Variable', 'rate': 0.055, 'type': 'variable', 'term_years': 5},
        ]
        anchors = discover_anchors(cfg)
        mtg = anchors['mortgage']
        assert any(m['id'] == 'renew_5yr_variable' for m in mtg)
        renew = next(m for m in mtg if m['id'] == 'renew_5yr_variable')
        assert renew['rate'] == 0.055

    def test_override_annotates_auto(self):
        """#890/DP#33: a declared mortgage ANNOTATES the auto sweep, it does not
        replace it. _base_cfg has no renewal/refinance options, so auto is the
        single 'current' rung; the declaration is unioned over it and marked."""
        cfg = _base_cfg()
        cfg['scenarios']['mortgage'] = [
            {'id': 'fixed_5yr', 'label': '5yr Fixed', 'rate': 0.045, 'type': 'fixed', 'term_years': 5},
        ]
        anchors = discover_anchors(cfg)
        ids = {m['id'] for m in anchors['mortgage']}
        assert {'current', 'fixed_5yr'} <= ids  # auto rung survives alongside the declaration
        declared = next(m for m in anchors['mortgage'] if m['id'] == 'fixed_5yr')
        assert declared['rate'] == 0.045
        assert declared['declared'] is True

    def test_override_rate_path_current(self):
        """When rate_path='current', the declared option's rate comes from
        property.mortgage_rate (unioned over the auto sweep, #890)."""
        cfg = _base_cfg()
        cfg['scenarios']['mortgage'] = [
            {'id': 'current_path', 'label': 'Current Rate Path', 'rate_path': 'current', 'type': 'variable', 'term_years': 1},
        ]
        anchors = discover_anchors(cfg)
        declared = next(m for m in anchors['mortgage'] if m['id'] == 'current_path')
        assert declared['rate'] == 0.05  # from property.mortgage_rate


# ---------------------------------------------------------------------------
# 4. TestRefinanceDiscovery
# ---------------------------------------------------------------------------

class TestRefinanceDiscovery:
    """Auto LTV levels (continuous) vs scenarios.refinance override, ltv computed from cash_out."""

    def test_auto_multiple_ltv_levels(self):
        """Should auto-generate LTV levels from current to 80% in 5% steps."""
        anchors = discover_anchors(_base_cfg())
        refi = anchors['refinance']
        ids = [r['id'] for r in refi]
        # Current LTV = 300000/800000 = 37.5%
        # Should have: no_refinance, ltv_40pct, ltv_45pct, ..., ltv_80pct
        assert 'no_refinance' in ids
        assert 'ltv_40pct' in ids
        assert 'ltv_80pct' in ids
        assert len(refi) > 3  # Multiple levels, not just 3

    def test_auto_no_refi_ltv(self):
        anchors = discover_anchors(_base_cfg())
        no_refi = next(r for r in anchors['refinance'] if r['id'] == 'no_refinance')
        assert no_refi['cash_out'] == 0
        # ltv = mortgage_balance / house_value = 300000 / 800000 = 0.375
        assert no_refi['ltv'] == pytest.approx(0.375, abs=0.001)

    def test_auto_generates_5_percent_increments(self):
        """LTv levels should be in 5% increments."""
        anchors = discover_anchors(_base_cfg())
        refi = anchors['refinance']
        for r in refi:
            if r['id'] != 'no_refinance':
                # Check that ltv is approximately a multiple of 5%
                ltv_pct = r['ltv'] * 100
                # Round to nearest 5 and compare
                nearest_5 = round(ltv_pct / 5) * 5
                assert r['ltv'] == pytest.approx(nearest_5 / 100, abs=0.01), f"LTV {ltv_pct}% not a 5% increment"

    def test_auto_max_refi_80_ltv(self):
        anchors = discover_anchors(_base_cfg())
        max_refi = next(r for r in anchors['refinance'] if r['id'] == 'ltv_80pct')
        assert max_refi['ltv'] == pytest.approx(0.80, abs=0.01)

    def test_override_replaces_auto(self):
        cfg = _base_cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'custom', 'label': 'Custom Refi', 'cash_out': 100000},
        ]
        anchors = discover_anchors(cfg)
        assert len(anchors['refinance']) == 1
        assert anchors['refinance'][0]['id'] == 'custom'
        assert anchors['refinance'][0]['cash_out'] == 100000

    def test_override_ltv_computed_from_cash_out(self):
        """When override provides cash_out but no ltv, ltv is computed."""
        cfg = _base_cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'partial', 'label': 'Partial', 'cash_out': 100000},
        ]
        anchors = discover_anchors(cfg)
        refi = anchors['refinance'][0]
        # ltv = (mortgage_balance + cash_out) / house_value
        #     = (300000 + 100000) / 800000 = 0.5
        assert refi['ltv'] == pytest.approx(0.5, abs=0.01)

    def test_override_ltv_preserved_if_provided(self):
        cfg = _base_cfg()
        cfg['scenarios']['refinance'] = [
            {'id': 'explicit', 'label': 'Explicit LTV', 'cash_out': 200000, 'ltv': 0.625},
        ]
        anchors = discover_anchors(cfg)
        assert anchors['refinance'][0]['ltv'] == 0.625

    def test_optimal_ltv_between_renewal_and_max(self):
        """Test scenario where optimal LTV is between renewal and 80%.
        
        Simulates a case where:
        - Renewal (low LTV, low rate) is profitable
        - 80% LTV refinance is also profitable
        - The optimizer should find the point that maximizes net benefit
        """
        cfg = _base_cfg()
        # Set up large margin to allow meaningful cash-out
        cfg['property']['margin_available'] = 500000
        cfg['property']['mortgage_balance'] = 300000
        cfg['property']['house_value'] = 800000
        cfg['property']['ltv_max'] = 0.80
        
        # High investment return to make SM profitable
        cfg['assumptions']['investment_return'] = 0.08
        
        anchors = discover_anchors(cfg)
        refi = anchors['refinance']
        
        # Should have multiple LTV levels to explore
        assert len(refi) >= 5, f"Expected >= 5 LTV levels, got {len(refi)}"
        
        # Verify LTV progression: from ~37.5% to 80%
        ltvs = [r['ltv'] for r in refi]
        assert min(ltvs) < 0.45, "Min LTV should be near current mortgage LTV"
        assert max(ltvs) >= 0.80, "Max LTV should be 80%"
        
        # Verify cash_out increases with LTV (with some tolerance for rounding)
        cash_outs = [r['cash_out'] for r in refi]
        for i in range(1, len(cash_outs)):
            assert cash_outs[i] >= cash_outs[i-1], "Cash out should be non-decreasing with LTV"


# ---------------------------------------------------------------------------
# 5. TestStrategyDiscovery
# ---------------------------------------------------------------------------

class TestStrategyDiscovery:
    """DP#6 auto vs scenarios.strategy override, fhsa_pct and child pcts present."""

    def test_auto_includes_readvance_priority_when_conditions_hold(self):
        anchors = discover_anchors(_base_cfg())
        ids = [s['id'] for s in anchors['strategy']]
        assert 'readvance_priority' in ids
        assert 'no_readvance' in ids
        assert 'balanced' in ids

    def test_auto_strategy_has_required_keys(self):
        anchors = discover_anchors(_base_cfg())
        required = ['id', 'label', 'rrsp_pct', 'spousal_rrsp_pct', 'tfsa_pct',
                     'fhsa_pct', 'resp_pct', 'non_reg_pct', 'prioritize_readvanceable',
                     'deduct_later', 'child_fhsa_pct', 'child_tfsa_pct', 'child_rrsp_pct']
        for strat in anchors['strategy']:
            for key in required:
                assert key in strat, f"Strategy {strat['id']} missing key: {key}"

    def test_auto_readvance_has_smith_flag(self):
        anchors = discover_anchors(_base_cfg())
        rp = next(s for s in anchors['strategy'] if s['id'] == 'readvance_priority')
        assert rp['prioritize_readvanceable'] is True

    def test_auto_no_readvance_no_smith_flag(self):
        anchors = discover_anchors(_base_cfg())
        nr = next(s for s in anchors['strategy'] if s['id'] == 'no_readvance')
        assert nr['prioritize_readvanceable'] is False

    def test_override_annotates_auto(self):
        """#890/DP#33: a declared strategy ANNOTATES the DP#6 auto sweep, it does
        not replace it — the declaration is unioned over the discovered
        strategies (appended in situ) and marked."""
        cfg = _base_cfg()
        auto_ids = {s['id'] for s in discover_anchors(cfg)['strategy']}
        cfg['scenarios']['strategy'] = [
            {'id': 'custom', 'label': 'My Strategy', 'rrsp_pct': 0.4, 'tfsa_pct': 0.3,
             'use_smith': True, 'deduct_later': False},
        ]
        anchors = discover_anchors(cfg)
        ids = {s['id'] for s in anchors['strategy']}
        assert auto_ids <= ids  # every discovered strategy survives
        custom = next(s for s in anchors['strategy'] if s['id'] == 'custom')
        assert custom['prioritize_readvanceable'] is True
        assert custom['deduct_later'] is False
        assert custom['declared'] is True

    def test_override_strategy_has_fhsa_and_child_pcts(self):
        cfg = _base_cfg()
        cfg['scenarios']['strategy'] = [
            {'id': 'custom', 'label': 'Custom', 'fhsa_pct': 0.1,
             'child_fhsa_pct': 0.05, 'child_tfsa_pct': 0.02, 'child_rrsp_pct': 0.01},
        ]
        anchors = discover_anchors(cfg)
        strat = next(s for s in anchors['strategy'] if s['id'] == 'custom')
        assert strat['fhsa_pct'] == 0.1
        assert strat['child_fhsa_pct'] == 0.05
        assert strat['child_tfsa_pct'] == 0.02
        assert strat['child_rrsp_pct'] == 0.01


# ---------------------------------------------------------------------------
# 6. TestSMOptions
# ---------------------------------------------------------------------------

class TestSMOptions:
    """[True, False] when heloc_readvance+profitable, [False] when not."""

    def test_both_when_readvance_and_profitable(self):
        anchors = discover_anchors(_base_cfg())
        assert anchors['sm_options'] == [True, False]

    def test_false_only_when_no_readvance(self):
        cfg = _base_cfg()
        cfg['property']['heloc_readvance'] = False
        anchors = discover_anchors(cfg)
        assert anchors['sm_options'] == [False]

    def test_false_only_when_not_profitable(self):
        """Low investment return makes SM unprofitable."""
        cfg = _base_cfg()
        cfg['assumptions']['investment_return'] = 0.03  # well below HELOC cost
        anchors = discover_anchors(cfg)
        assert anchors['sm_options'] == [False]

    def test_false_only_when_readvance_false_regardless_of_return(self):
        """Even with high return, no readvance → [False]."""
        cfg = _base_cfg()
        cfg['property']['heloc_readvance'] = False
        cfg['assumptions']['investment_return'] = 0.15
        anchors = discover_anchors(cfg)
        assert anchors['sm_options'] == [False]


# ---------------------------------------------------------------------------
# 7. TestDeductLaterOptions
# ---------------------------------------------------------------------------

class TestDeductLaterOptions:
    """[True, False] when bracket_gap > 0, [False] when not."""

    def test_both_when_bracket_gap_positive(self):
        """Primary earns much more than spouse → bracket_gap > 0."""
        anchors = discover_anchors(_base_cfg())
        assert anchors['deduct_later_options'] == [True, False]

    def test_false_only_when_bracket_gap_zero(self):
        """Same income for both members → bracket_gap ≈ 0."""
        cfg = _base_cfg()
        cfg['family']['members'][0]['gross_income'] = 70000
        cfg['family']['members'][1]['gross_income'] = 70000
        anchors = discover_anchors(cfg)
        assert anchors['deduct_later_options'] == [False]

    def test_false_only_when_spouse_earns_more(self):
        """Spouse earning more than primary → bracket_gap < 0."""
        cfg = _base_cfg()
        cfg['family']['members'][0]['gross_income'] = 50000
        cfg['family']['members'][1]['gross_income'] = 150000
        anchors = discover_anchors(cfg)
        assert anchors['deduct_later_options'] == [False]


# ---------------------------------------------------------------------------
# 8. TestRESPActions
# ---------------------------------------------------------------------------

class TestRESPActions:
    """[keep, eap, collapse] when balance > 0, [keep] when 0, override."""

    def test_three_actions_when_balance_positive(self):
        cfg = _base_cfg()
        cfg['accounts']['resp_current_balance'] = 5000
        anchors = discover_anchors(cfg)
        assert anchors['resp_action'] == ['keep', 'eap', 'collapse']

    def test_keep_only_when_balance_zero(self):
        anchors = discover_anchors(_base_cfg())
        assert anchors['resp_action'] == ['keep']

    def test_override_annotates_auto(self):
        """#890/DP#33: a declared resp_action ANNOTATES the auto set, it does not
        replace it. _base_cfg has balance 0 (auto ['keep']); the declared novel
        action is unioned over it rather than hiding 'keep'."""
        cfg = _base_cfg()
        cfg['scenarios']['resp_action'] = [
            {'id': 'eap_only'},
        ]
        anchors = discover_anchors(cfg)
        assert anchors['resp_action'] == ['keep', 'eap_only']


# ---------------------------------------------------------------------------
# 9. TestChildAccounts
# ---------------------------------------------------------------------------

class TestChildAccounts:
    """Populated when children have room > 0, empty when not."""

    def test_populated_when_child_has_room(self):
        cfg = _base_cfg()
        cfg['family']['children'] = [
            {
                'name': 'child_a',
                'birth_year': 2015,
                'fhsa_room_accumulated': 8000,
                'tfsa_room_accumulated': 5000,
                'rrsp_room_accumulated': 0,
            },
        ]
        anchors = discover_anchors(cfg)
        ca = anchors['child_accounts']
        assert len(ca) == 1
        assert ca[0]['child_name'] == 'child_a'
        assert ca[0]['fhsa_room'] == 8000
        assert ca[0]['tfsa_room'] == 5000
        assert ca[0]['rrsp_room'] == 0

    def test_empty_when_no_children(self):
        anchors = discover_anchors(_base_cfg())
        assert anchors['child_accounts'] == []

    def test_empty_when_child_has_zero_room(self):
        cfg = _base_cfg()
        cfg['family']['children'] = [
            {
                'name': 'child_b',
                'birth_year': 2018,
                'fhsa_room_accumulated': 0,
                'tfsa_room_accumulated': 0,
                'rrsp_room_accumulated': 0,
            },
        ]
        anchors = discover_anchors(cfg)
        assert anchors['child_accounts'] == []

    def test_mixed_children_only_included_if_room(self):
        """One child with room, one without — only child with room appears."""
        cfg = _base_cfg()
        cfg['family']['children'] = [
            {
                'name': 'child_a',
                'birth_year': 2015,
                'fhsa_room_accumulated': 8000,
                'tfsa_room_accumulated': 0,
                'rrsp_room_accumulated': 0,
            },
            {
                'name': 'child_b',
                'birth_year': 2018,
                'fhsa_room_accumulated': 0,
                'tfsa_room_accumulated': 0,
                'rrsp_room_accumulated': 0,
            },
        ]
        anchors = discover_anchors(cfg)
        ca = anchors['child_accounts']
        assert len(ca) == 1
        assert ca[0]['child_name'] == 'child_a'


class TestChildAllocationSweep:
    """Issue #812 (#701 follow-up): the child-allocation sweep surfaces where a
    child's OWN savings land under each bias, and the strategy scenarios carry
    real child_*_pct (no longer hardcoded 0.0)."""

    def _cfg_with_earning_child(self):
        cfg = _base_cfg()
        cfg['savings'] = {'rate': 0.20}
        cfg['family']['children'] = [
            {
                'name': 'child_a',
                'birth_year': 2010,
                'gross_income': 50000,          # child's OWN income
                'fhsa_room_accumulated': 3000,
                'tfsa_room_accumulated': 6000,
                'rrsp_room_accumulated': 0,
            },
        ]
        return cfg

    def test_child_carries_savings_and_allocation_options(self):
        anchors = discover_anchors(self._cfg_with_earning_child())
        ca = anchors['child_accounts']
        assert len(ca) == 1
        # $50k income * 20% savings = $10k of the child's OWN savings.
        assert ca[0]['savings'] == 10000
        biases = {o['bias'] for o in ca[0]['allocation_options']}
        assert biases == {'room_priority', 'tfsa', 'fhsa', 'rrsp', 'non_reg'}

    def test_room_priority_fills_tfsa_then_fhsa(self):
        ca = discover_anchors(self._cfg_with_earning_child())['child_accounts']
        opt = next(o for o in ca[0]['allocation_options'] if o['bias'] == 'room_priority')
        assert opt['tfsa'] == 6000    # TFSA room filled first (#701)
        assert opt['fhsa'] == 3000    # then FHSA room
        assert opt['non_reg'] == 1000  # $10k - 6k - 3k residual

    def test_tfsa_bias_differs_from_non_reg_bias(self):
        ca = discover_anchors(self._cfg_with_earning_child())['child_accounts']
        opts = {o['bias']: o for o in ca[0]['allocation_options']}
        assert opts['tfsa']['tfsa'] == 6000
        assert opts['non_reg']['tfsa'] == 0
        assert opts['non_reg']['non_reg'] == 10000
        assert opts['tfsa']['non_reg'] != opts['non_reg']['non_reg']

    def test_strategy_scenarios_carry_real_child_pcts(self):
        # The four child dimensions are REAL AllocationStrategy fields now, not
        # the hardcoded 0.0 'Not in AllocationStrategy dataclass' placeholders.
        anchors = discover_anchors(_base_cfg())
        for strat in anchors['strategy']:
            for key in ('child_tfsa_pct', 'child_fhsa_pct',
                        'child_rrsp_pct', 'child_non_reg_pct'):
                assert key in strat


# ---------------------------------------------------------------------------
# 10. TestNoMortgageNoRefi
# ---------------------------------------------------------------------------

class TestNoMortgageNoRefi:
    """No mortgage means no refinance scenarios."""

    def test_no_refi_when_no_mortgage(self):
        cfg = _base_cfg()
        cfg['property']['house_value'] = 0
        cfg['property']['mortgage_balance'] = 0
        cfg['property']['margin_available'] = 0
        anchors = discover_anchors(cfg)
        assert anchors['refinance'] == []

    def test_no_refi_when_zero_margin(self):
        """Even with mortgage, zero margin_available → no refi scenarios."""
        cfg = _base_cfg()
        cfg['property']['margin_available'] = 0
        anchors = discover_anchors(cfg)
        assert anchors['refinance'] == []

    def test_no_refi_when_zero_house_value(self):
        """No house value → no refi."""
        cfg = _base_cfg()
        cfg['property']['house_value'] = 0
        anchors = discover_anchors(cfg)
        assert anchors['refinance'] == []