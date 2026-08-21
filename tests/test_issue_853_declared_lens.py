"""Issue #853 / DP#33 — a declaration is a LENS, not a BLINDFOLD.

#846 made a declared candidate list REPLACE the auto-discovered sweep for its
dimension. That forced a bad trade: to ask "how do my two options compare?", the
household lost the whole LTV curve that gives those two numbers their meaning —
and never learned that a rung they did NOT declare might beat both. DP#33 (#853):
a declaration must ANNOTATE the swept exploration, not truncate it.

These tests pin the two halves:
  1. ``annotate_declared_over_sweep`` — the pure union+annotate primitive.
  2. optimize.py's refinance exploration — declaring options now unions them
     OVER the full ladder and marks them in situ, never fewer than the sweep.

All data is fabricated with round numbers — no personal data (DP#15).
"""

import pytest

import optimize
from scenario_discovery import annotate_declared_over_sweep


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
            'heloc_readvance': True,
            'refinance_amortization_years': 25,
        },
        'accounts': {'resp_current_balance': 0},
        'assumptions': {'investment_return': 0.07},
        'scenarios': {},
    }


def _declared(cfg, *options):
    cfg = dict(cfg)
    cfg['scenarios'] = {'refinance': list(options)}
    return cfg


# ── 1. The pure primitive: union + annotate + insert-in-situ ────────────────

class TestAnnotateDeclaredOverSweep:

    def _swept(self):
        return [
            {'id': 'ltv_0.30', 'label': 'LTV 30%', 'ltv': 0.30},
            {'id': 'ltv_0.40', 'label': 'LTV 40%', 'ltv': 0.40},
            {'id': 'ltv_0.50', 'label': 'LTV 50%', 'ltv': 0.50},
        ]

    def test_empty_declaration_returns_the_sweep_unchanged(self):
        """DP#13/#32: no declaration annotates nothing — the full sweep, each row
        flagged 'not declared'. A declaration cannot make the curve shorter."""
        out = annotate_declared_over_sweep(self._swept(), [], key='ltv')
        assert [r['ltv'] for r in out] == [0.30, 0.40, 0.50]
        assert all(r['declared'] is False for r in out)
        assert all(r['declared_id'] is None for r in out)

    def test_coincident_declaration_marks_the_rung_without_growing_the_sweep(self):
        declared = [{'id': 'mine', 'label': 'My 40% option', 'ltv': 0.40}]
        out = annotate_declared_over_sweep(self._swept(), declared, key='ltv')
        assert len(out) == 3  # marked, not added
        marked = [r for r in out if r['declared']]
        assert [r['ltv'] for r in marked] == [0.40]
        assert marked[0]['declared_id'] == 'mine'
        assert marked[0]['declared_label'] == 'My 40% option'
        # the rung keeps its own identity fields — it is the same point
        assert marked[0]['id'] == 'ltv_0.40'

    def test_between_rung_declaration_is_inserted_in_situ_and_sorted(self):
        """The real value of #853: an odd cash-out that lands between rungs is
        ranked WHERE IT SITS on the curve, not dropped and not appended."""
        declared = [{'id': 'odd', 'label': 'Odd 45%', 'ltv': 0.45, 'source': 'declared'}]
        out = annotate_declared_over_sweep(self._swept(), declared, key='ltv')
        assert [r['ltv'] for r in out] == [0.30, 0.40, 0.45, 0.50]
        inserted = [r for r in out if r['ltv'] == 0.45][0]
        assert inserted['declared'] is True
        assert inserted['declared_id'] == 'odd'
        # the inserted row carries the declared candidate's own fields verbatim
        assert inserted['source'] == 'declared'

    def test_result_is_never_shorter_than_the_sweep(self):
        """The acceptance of #853: declaring never REDUCES the explored set."""
        declared = [{'id': 'a', 'label': 'A', 'ltv': 0.31},
                    {'id': 'b', 'label': 'B', 'ltv': 0.40}]
        out = annotate_declared_over_sweep(self._swept(), declared, key='ltv')
        assert len(out) >= len(self._swept())
        # every declared option is present and flagged
        by_id = {r.get('declared_id') for r in out if r['declared']}
        assert {'a', 'b'} <= by_id

    def test_inputs_are_not_mutated(self):
        swept = self._swept()
        declared = [{'id': 'mine', 'label': 'M', 'ltv': 0.40}]
        annotate_declared_over_sweep(swept, declared, key='ltv')
        assert 'declared' not in swept[1]
        assert 'annotated' not in declared[0]


# ── 2. optimize.py refinance exploration: the declaration annotates the sweep ─

class TestRefinanceCandidatesAnnotateTheSweep:

    def test_undeclared_household_gets_the_plain_ladder(self):
        """DP#13 regression: no declaration → exactly the generic ladder, every
        row flagged not-declared / not-annotated."""
        cands = optimize.refinance_candidates(_cfg())
        assert sorted(c['ltv'] for c in cands) == optimize.DEFAULT_LTV_LADDER
        assert all(c['declared'] is False for c in cands)
        assert all(c['annotated'] is False for c in cands)
        assert {c['source'] for c in cands} == {'ladder'}

    def test_declaration_unions_over_the_full_ladder_never_fewer(self):
        """#853's headline: declaring options must NOT shrink the sweep. The
        whole ladder is still explored; the declaration only adds/marks."""
        cfg = _declared(_cfg(),
                        {'id': 'line', 'label': 'From the line', 'cash_out': 0},
                        {'id': 'advance', 'label': 'Mortgage advance', 'cash_out': 200000})
        cands = optimize.refinance_candidates(cfg)
        ltvs = {round(c['ltv'], 4) for c in cands}
        # every ladder rung survives
        assert set(optimize.DEFAULT_LTV_LADDER) <= ltvs
        assert len(cands) >= len(optimize.DEFAULT_LTV_LADDER)
        assert all(c['annotated'] for c in cands)

    def test_declared_options_are_marked_in_situ(self):
        """Both declared ids are present and flagged, so the household sees its
        own options WITHIN the full curve (advance $200k → ltv 0.625, off-ladder,
        inserted; line $0 → ltv 0.375, off-ladder, inserted)."""
        cfg = _declared(_cfg(),
                        {'id': 'line', 'label': 'From the line', 'cash_out': 0},
                        {'id': 'advance', 'label': 'Mortgage advance', 'cash_out': 200000})
        cands = optimize.refinance_candidates(cfg)
        declared_ids = {c['declared_id'] for c in cands if c['declared']}
        assert {'line', 'advance'} <= declared_ids
        # the $200k advance sits at (300k+200k)/800k = 62.5%, inserted in situ
        advance = [c for c in cands if c['declared_id'] == 'advance'][0]
        assert advance['ltv'] == pytest.approx(0.625)
        assert advance['source'] == 'declared'

    def test_explicit_ltv_steps_still_override_the_declaration(self):
        """A caller that passed an explicit ladder has already decided what to
        sweep; the union does not apply and the rows are plain, un-annotated."""
        cfg = _declared(_cfg(),
                        {'id': 'advance', 'label': 'Mortgage advance', 'cash_out': 200000})
        cands = optimize.refinance_candidates(cfg, ltv_steps=[0.0, 0.5])
        assert sorted(c['ltv'] for c in cands) == [0.0, 0.5]
        assert all(c['annotated'] is False for c in cands)
        assert all(c['declared'] is False for c in cands)


class TestRunLtvExplorationCarriesTheAnnotation:

    def test_rows_carry_the_declared_annotation(self):
        cfg = _declared(_cfg(),
                        {'id': 'advance', 'label': 'Mortgage advance', 'cash_out': 200000})
        results = optimize.run_ltv_exploration(cfg)
        # the full ladder is present (union, not replacement)
        ltvs = {round(r['ltv'], 4) for r in results}
        assert set(optimize.DEFAULT_LTV_LADDER) <= ltvs
        # the declared advance is marked in situ
        declared_rows = [r for r in results if r.get('refinance_declared')]
        assert declared_rows
        assert any(r['refinance_declared_id'] == 'advance' for r in declared_rows)
        assert all(r.get('refinance_annotated') for r in results)


# ── 3. The basis legend: a ★ lens, no longer a "REPLACED" blindfold ─────────

class TestRefinanceBasisIsALens:

    def test_declared_union_prints_the_star_lens_legend(self, capsys):
        cfg = _declared(_cfg(),
                        {'id': 'advance', 'label': 'Mortgage advance', 'cash_out': 200000})
        results = optimize.run_ltv_exploration(cfg)
        optimize._print_refinance_basis(results)
        out = capsys.readouterr().out
        assert '★' in out
        assert 'FULL LTV sweep' in out
        assert '#853' in out
        # the whole curve is ranked, not just the declared options
        assert 'a rung you did not declare' in out.lower()

    def test_explicit_override_still_warns_it_is_not_the_declaration(self, capsys):
        """#845 unchanged: a caller-forced ladder that overrides a declaration
        must still say so loudly."""
        cfg = _declared(_cfg(),
                        {'id': 'advance', 'label': 'Mortgage advance', 'cash_out': 200000})
        results = optimize.run_ltv_exploration(cfg, ltv_steps=[0.0, 0.5])
        optimize._print_refinance_basis(results)
        out = capsys.readouterr().out
        assert 'OVERRIDES' in out
        assert '★' not in out


# ── 4. optimize.py's narrowing notice is gone once every dimension unifies ──

class TestOptimizeHasNoNarrowingNotice:
    """#853 excluded refinance from optimize's narrowing notice (its LTV table
    unions the declared options); #890 made mortgage/strategy/resp_action annotate
    too, so no declared dimension narrows in optimize.py and the notice was
    removed entirely (DP#9 — its output had become unreachable). The refinance
    narrowing that survives lives in simulate.py's grid, notified there."""

    def test_optimize_no_longer_exposes_a_narrowing_notice(self):
        assert not hasattr(optimize, '_print_narrowing_notice')
