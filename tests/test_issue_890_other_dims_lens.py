"""Issue #890 / DP#33 — a declaration is a LENS, not a BLINDFOLD, for the OTHER
three discovery dimensions (mortgage / strategy / resp_action).

#853 (PR #885) applied DP#33 to the refinance LTV dimension: a declared candidate
ANNOTATES the auto-discovered sweep rather than REPLACING it. #890 extends the
same treatment to the remaining three declarable ``discover_anchors`` dimensions
that still used the #846 replace-and-warn path. For each, a declared candidate
must now appear IN the swept candidate set alongside the auto-discovered ones
(marked as declared), never truncating the search to itself.

The union is done in ``discover_anchors`` (the single place these three
dimensions' replace lived, and the candidate set simulate.py's grid consumes),
through the same ``annotate_declared_over_sweep`` primitive #853 added — now
parametrized to a DISCRETE key (``ndigits=None``) since a strategy / mortgage /
RESP action is categorical, not a point on a continuous curve (DP#9: one
primitive, two shapes).

All data is fabricated with round numbers and role labels — no personal data
(DP#4, DP#15).
"""

import pytest

from scenario_discovery import annotate_declared_over_sweep, discover_anchors
# DP#25 (#998): scenario_discovery's simulation callables are now injected;
# importing simulation_deps configures the injection point at import time.
import simulation_deps  # noqa: F401  (import side-effect: injects SimulationDeps)


def _cfg():
    """A fabricated household with round numbers and NO declared scenarios."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 150000,
                 'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 40000,
                 'fhsa_first_time_buyer_since': None, 'fhsa_room_accumulated': 0},
                {'role': 'spouse', 'gross_income': 70000,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 40000,
                 'fhsa_first_time_buyer_since': None, 'fhsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'property': {
            'house_value': 800000,
            'mortgage_balance': 300000,
            'mortgage_rate': 0.05,
            'margin_available': 50000,
            'ltv_max': 0.80,
            'heloc_readvance': False,
            'refinance_amortization_years': 25,
            'renewal_options': [
                {'name': 'Five-year fixed', 'rate': 0.045, 'type': 'fixed', 'term_years': 5},
                {'name': 'Three-year fixed', 'rate': 0.042, 'type': 'fixed', 'term_years': 3},
            ],
        },
        'accounts': {'resp_current_balance': 60000},  # auto -> keep/eap/collapse
        'assumptions': {'investment_return': 0.07},
        'savings': {'rate': 0.2},
        'scenarios': {},
    }


# ── 1. The primitive's DISCRETE mode (ndigits=None), the #890 extension ──────

class TestAnnotateDiscreteKey:
    """#890 parametrizes ``annotate_declared_over_sweep`` to a categorical key.
    The numeric/continuous mode is pinned by test_issue_853; here the discrete
    mode: exact-value coincidence, no insertion sort, declared-only appended."""

    def _swept(self):
        return [
            {'id': 'balanced', 'label': 'Balanced'},
            {'id': 'rrsp_max', 'label': 'RRSP max'},
        ]

    def test_empty_declaration_returns_the_sweep_unchanged(self):
        out = annotate_declared_over_sweep(self._swept(), [], key='id', ndigits=None)
        assert [r['id'] for r in out] == ['balanced', 'rrsp_max']
        assert all(r['declared'] is False for r in out)

    def test_coincident_declaration_marks_the_candidate_without_growing_it(self):
        declared = [{'id': 'rrsp_max', 'label': 'My RRSP-max'}]
        out = annotate_declared_over_sweep(self._swept(), declared, key='id', ndigits=None)
        assert len(out) == 2  # marked, not added
        marked = [r for r in out if r['declared']]
        assert [r['id'] for r in marked] == ['rrsp_max']
        assert marked[0]['declared_id'] == 'rrsp_max'

    def test_novel_declaration_is_appended_preserving_swept_order(self):
        """A declared candidate not in the auto sweep is added AFTER the swept
        candidates (their order preserved — a categorical dimension has no
        'between' to sort into)."""
        declared = [{'id': 'my_own', 'label': 'My own split'}]
        out = annotate_declared_over_sweep(self._swept(), declared, key='id', ndigits=None)
        assert [r['id'] for r in out] == ['balanced', 'rrsp_max', 'my_own']
        assert out[-1]['declared'] is True
        assert out[-1]['declared_id'] == 'my_own'

    def test_result_is_never_shorter_than_the_sweep(self):
        declared = [{'id': 'balanced', 'label': 'B'}, {'id': 'novel', 'label': 'N'}]
        out = annotate_declared_over_sweep(self._swept(), declared, key='id', ndigits=None)
        assert len(out) >= len(self._swept())
        declared_ids = {r['declared_id'] for r in out if r['declared']}
        assert {'balanced', 'novel'} <= declared_ids


# ── 2. discover_anchors: the declaration annotates the swept candidate set ───

class TestMortgageAnnotatesTheSweep:

    def test_undeclared_household_gets_the_plain_auto_sweep(self):
        """DP#13 regression: no declaration -> exactly the auto sweep, no
        annotation keys, byte-identical to before #890."""
        cands = discover_anchors(_cfg())['mortgage']
        assert {c['id'] for c in cands} == {'renew_five-year_fixed', 'renew_three-year_fixed'}
        assert not any('declared' in c for c in cands)

    def test_declaration_is_unioned_over_the_auto_sweep_never_fewer(self):
        cfg = _cfg()
        cfg['scenarios']['mortgage'] = [
            {'id': 'my_renewal', 'label': 'The renewal I am considering', 'rate': 0.05},
        ]
        cands = discover_anchors(cfg)['mortgage']
        ids = {c['id'] for c in cands}
        # every auto rung survives alongside the declared option (#890)
        assert {'renew_five-year_fixed', 'renew_three-year_fixed', 'my_renewal'} <= ids
        assert len(cands) >= 2
        declared = [c for c in cands if c.get('declared')]
        assert [c['declared_id'] for c in declared] == ['my_renewal']


class TestStrategyAnnotatesTheSweep:

    def test_undeclared_household_gets_the_plain_auto_sweep(self):
        cands = discover_anchors(_cfg())['strategy']
        assert len(cands) > 1  # DP#6 discovery ranks several
        assert not any('declared' in c for c in cands)

    def test_declaration_is_unioned_over_the_auto_sweep_never_fewer(self):
        cfg = _cfg()
        auto_ids = {c['id'] for c in discover_anchors(cfg)['strategy']}
        cfg['scenarios']['strategy'] = [
            {'id': 'my_split', 'label': 'The split I am considering', 'tfsa_pct': 1.0},
        ]
        cands = discover_anchors(cfg)['strategy']
        ids = {c['id'] for c in cands}
        # the declaration ADDS to the sweep, never replaces it
        assert auto_ids <= ids
        assert 'my_split' in ids
        assert len(cands) >= len(auto_ids) + 1
        declared = [c for c in cands if c.get('declared')]
        assert [c['declared_id'] for c in declared] == ['my_split']


class TestRespActionAnnotatesTheSweep:

    def test_undeclared_household_gets_the_plain_auto_sweep(self):
        # balance > 0 -> auto discovers all three actions
        assert discover_anchors(_cfg())['resp_action'] == ['keep', 'eap', 'collapse']

    def test_declaring_one_action_does_not_hide_the_others(self):
        """#890's headline for resp_action: declaring 'keep' must NOT collapse the
        sweep to ['keep'] — the household still explores eap and collapse."""
        cfg = _cfg()
        cfg['scenarios']['resp_action'] = [{'id': 'keep'}]
        actions = discover_anchors(cfg)['resp_action']
        assert set(actions) == {'keep', 'eap', 'collapse'}
        # the auto set is preserved, never fewer than before the declaration
        assert len(actions) == 3

    def test_declaring_a_novel_action_unions_it_over_the_auto_set(self):
        cfg = _cfg()
        cfg['scenarios']['resp_action'] = [{'id': 'eap'}, {'id': 'custom'}]
        actions = discover_anchors(cfg)['resp_action']
        # every auto action survives AND the novel declared token is added
        assert {'keep', 'eap', 'collapse'} <= set(actions)
        assert 'custom' in actions

    def test_output_stays_a_list_of_str_tokens(self):
        """The engine (simulate.build_all_overlays) consumes resp_action as bare
        string tokens; the union must not change that shape."""
        cfg = _cfg()
        cfg['scenarios']['resp_action'] = [{'id': 'keep'}]
        actions = discover_anchors(cfg)['resp_action']
        assert all(isinstance(a, str) for a in actions)
