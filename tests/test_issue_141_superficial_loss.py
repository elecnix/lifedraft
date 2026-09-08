"""Issue #141: the superficial-loss rule (ITA s.53(1)(c)/(f)), slice A.

Two layers, per DP#11:

- UNIT: ``superficial_loss.classify_window`` -- the pure annualized-window
  arithmetic (same-step deny, cross-year pend → deny / pend → release,
  declared-substitute allow, the DP#32 loud refusals).
- ENGINE: ``simulate_year_pure`` drives the registered ``superficial_loss``
  rule through the real fold -- a household forced to liquidate securities
  below ACB (a solvency shortfall) while re-contributing to the non-reg pot
  in the SAME step must see the loss denied and the denied dollars added to
  the repurchased pot's ACB (s.53(1)(f)), and the NEXT year's settlement
  must see a held-over loss released when no repurchase lands. The tests
  assert the engine's output (YearResult fields), never state a unit test
  constructed by hand (DP#11/DP#18).

The affiliated-person (spouse) limb of s.251.1 is SUBSUMED by the
household-level repurchase check at the engine's current granularity (the
non-reg pot is household-wide; a spouse's contribution is a repurchase) --
asserted below via the same engine call the primary drives.
"""

import pytest

from superficial_loss import (
    classify_window,
    summarize_superficial_loss,
    worst_superficial_loss,
)

from simulation_config import SimulationConfig
from simulation_state import SimState, _default_canada_state, simulate_year_pure

from contract_decisions import map_superficial_loss


# ── Unit: classify_window ──────────────────────────────────────────────────


class TestSameStepDenial:
    def test_loss_with_same_step_repurchase_is_denied(self):
        denied, pended, released, new_pending = classify_window(
            realized_loss_raw=10_000.0, repurchase_this_step=5_000.0,
            substitutes_declared=False, opening_pending=[], year=2026)
        assert (denied, pended, released, new_pending) == \
            (10_000.0, 0.0, 0.0, [])

    def test_denial_requires_a_repurchase(self):
        # No repurchase in the step: the Y+1 limb is still open, the loss
        # is pended -- NOT denied in its own step.
        denied, pended, released, new_pending = classify_window(
            realized_loss_raw=10_000.0, repurchase_this_step=0.0,
            substitutes_declared=False, opening_pending=[], year=2026)
        assert (denied, pended, released) == (0.0, 10_000.0, 0.0)
        assert new_pending == [{'year': 2026, 'amount': 10_000.0}]

    def test_declared_substitute_allows_the_loss_in_its_own_step(self):
        # The declaration asserts the repurchase is NON-identical, so the
        # s.53(1)(c) limb is not met: allowed now, nothing pended.
        denied, pended, released, new_pending = classify_window(
            realized_loss_raw=10_000.0, repurchase_this_step=5_000.0,
            substitutes_declared=True, opening_pending=[], year=2026)
        assert (denied, pended, released, new_pending) == \
            (0.0, 0.0, 0.0, [])


class TestCrossYearResolution:
    def test_pended_loss_denied_by_next_step_repurchase(self):
        opening = [{'year': 2026, 'amount': 10_000.0}]
        denied, pended, released, new_pending = classify_window(
            realized_loss_raw=0.0, repurchase_this_step=3_000.0,
            substitutes_declared=False, opening_pending=opening, year=2027)
        assert (denied, pended, released, new_pending) == \
            (10_000.0, 0.0, 0.0, [])

    def test_pended_loss_released_when_next_step_does_not_repurchase(self):
        opening = [{'year': 2026, 'amount': 10_000.0}]
        denied, pended, released, new_pending = classify_window(
            realized_loss_raw=0.0, repurchase_this_step=0.0,
            substitutes_declared=False, opening_pending=opening, year=2027)
        assert (denied, pended, released, new_pending) == \
            (0.0, 0.0, 10_000.0, [])
        # Released means it enters THIS step's settlement -- one year late,
        # the disclosed cost of the annual step (model_fidelity).

    def test_declared_substitute_releases_the_pended_loss(self):
        # A declaration made in the FOLLOWING step resolves the prior
        # pended loss as non-superficial: coherent resolution when the
        # declaration changes across years.
        opening = [{'year': 2026, 'amount': 10_000.0}]
        denied, pended, released, _ = classify_window(
            realized_loss_raw=0.0, repurchase_this_step=3_000.0,
            substitutes_declared=True, opening_pending=opening, year=2027)
        assert (denied, pended, released) == (0.0, 0.0, 10_000.0)

    def test_new_loss_pends_while_pended_entry_releases(self):
        # Year Y: loss L1, no repurchase -> pended. Year Y+1: loss L2,
        # still no repurchase -> L2 pends AND L1 releases (no repurchase
        # in Y+1 means L1's window closed non-superficially).
        opening = [{'year': 2026, 'amount': 10_000.0}]
        denied, pended, released, new_pending = classify_window(
            realized_loss_raw=4_000.0, repurchase_this_step=0.0,
            substitutes_declared=False, opening_pending=opening, year=2027)
        assert (denied, pended, released) == (0.0, 4_000.0, 10_000.0)
        assert new_pending == [{'year': 2027, 'amount': 4_000.0}]


class TestLoudRefusals:
    """DP#32: the caller's disagreement with the window contract must
    raise, never coerce to zero."""

    @pytest.mark.parametrize('loss,repurchase', [
        (-1.0, 0.0),   # negative loss magnitude
        (0.0, -1.0),   # negative repurchase
    ])
    def test_negative_inputs_raise(self, loss, repurchase):
        with pytest.raises(ValueError, match='superficial_loss'):
            classify_window(loss, repurchase, False, [], 2026)

    @pytest.mark.parametrize('entry', [
        {'year': 2026},                        # missing amount
        {'amount': 100.0},                     # missing year
        {'year': 2026, 'amount': 0.0},         # zero amount
        {'year': 2026, 'amount': -5.0},        # negative amount
        'not a dict',                          # not an entry at all
    ])
    def test_malformed_pending_entry_raises(self, entry):
        with pytest.raises(ValueError, match='superficial_loss'):
            classify_window(0.0, 0.0, False, [entry], 2026)


# ── Unit: summarizers ──────────────────────────────────────────────────────


class TestSummarize:
    def test_all_clear_when_nothing_engaged(self):
        assert summarize_superficial_loss([]) == {
            'engaged': False, 'first_denied_year': None,
            'denied_total': 0.0, 'acb_added_total': 0.0,
            'pending_years': 0}

    def test_folds_dict_rows_and_object_rows_identically(self):
        from types import SimpleNamespace
        row = {'year': 3, 'superficial_loss_denied': 100.0,
               'superficial_loss_acb_added': 100.0,
               'superficial_loss_pended': 0.0}
        obj = SimpleNamespace(**row)
        assert summarize_superficial_loss([row]) == \
            summarize_superficial_loss([obj])
        s = summarize_superficial_loss([row])
        assert s['engaged'] and s['first_denied_year'] == 3

    def test_worst_picks_largest_denial_tie_breaks_earliest(self):
        a = {'engaged': True, 'first_denied_year': 5,
             'denied_total': 100.0, 'acb_added_total': 100.0,
             'pending_years': 0}
        b = {'engaged': True, 'first_denied_year': 2,
             'denied_total': 100.0, 'acb_added_total': 100.0,
             'pending_years': 1}
        assert worst_superficial_loss([a, b]) == b
        # The all-clear is a CHECKED result (DP#32), not an absence.
        assert worst_superficial_loss([])['engaged'] is False


# ── Engine: the registered rule through the real fold ─────────────────────


def _make_config(**overrides):
    defaults = dict(
        projection_years=5,
        investment_return=0.06,
        mortgage_balance=0,
        mortgage_rate=0.05,
        margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1990,
             'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1992,
             'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


class TestEngineDrivenDenial:
    """Drive simulate_year_pure -- never construct rule state by hand
    (DP#11/DP#18). A household holding a non-reg position BELOW its cost
    base, forced by a solvency shortfall to liquidate, while contributing
    to the non-reg pot the SAME step."""

    def _run_year(self, config, opening_pending=None):
        """A forced below-ACB liquidation + same-step non-reg contribution.

        The state holds $400k of non-reg at a $500k ACB ($100k embedded
        unrealized loss, the proportional-ACB shape the engine tracks).
        living_costs ($60k) exceed after-tax income ($45k) by $15k, so the
        `solvency` rule's forced-liquidation waterfall sells securities
        below ACB and realizes a genuine loss; the same step books a $10k
        explicit `non_reg` contribution into the same pot -- the in-window
        repurchase. Assert YearResult output only.
        """
        state = SimState(
            non_reg_balance=400_000.0,
            non_reg_acb=500_000.0,
            jurisdiction_state={'canada': _default_canada_state()},
        )
        if opening_pending:
            state.jurisdiction_state['canada']['superficial_loss_pending'] \
                = list(opening_pending)
        yr, _state = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 130_000, 'non_reg': 10_000,
                         '_annual_savings': 10_000},
            config=config, investment_return=0.06,
            primary_marginal_rate=0.30,
            living_costs=60_000, after_tax_income=45_000,
        )
        return yr

    def test_same_step_loss_and_contribution_denies_and_defers_acb(self):
        yr = self._run_year(_make_config())
        # The waterfall realized a genuine loss (sold below ACB), and the
        # household re-contributed in the same step -> denied.
        assert yr.superficial_loss_denied > 0.0, (
            'expected the superficial-loss rule to fire on a below-ACB '
            f'forced liquidation beside a same-step contribution; got '
            f'denied={yr.superficial_loss_denied!r}, '
            f'forced_liquidation_realized_loss='
            f'{yr.forced_liquidation_realized_loss!r}')
        # s.53(1)(f): the FULL denial is deferred into the repurchased
        # property's ACB -- never destroyed.
        assert yr.superficial_loss_acb_added == yr.superficial_loss_denied

    def test_denial_adds_to_the_repurchased_pots_acb(self):
        # Run the SAME scenario with the declaration (loss allowed) and
        # without (loss denied). The denied run's closing non-reg ACB must
        # exceed the allowed run's by exactly the denied dollars: the
        # s.53(1)(f) deferral, visible as an ACB delta, not a hand-built
        # intermediate.
        allowed = self._run_year(_make_config(
            superficial_loss_substitute_pairs=[['XEQT', 'VEQT']]))
        denied = self._run_year(_make_config())
        delta = denied.non_reg_acb - allowed.non_reg_acb
        assert delta == pytest.approx(denied.superficial_loss_denied), (
            f'ACB delta {delta!r} != denied {denied.superficial_loss_denied!r}')

    def test_declared_substitutes_allow_the_loss(self):
        yr = self._run_year(_make_config(
            superficial_loss_substitute_pairs=[['XEQT', 'VEQT']]))
        assert yr.superficial_loss_denied == 0.0
        assert yr.superficial_loss_acb_added == 0.0
        assert yr.superficial_loss_pended == 0.0

    def test_year_with_no_loss_and_no_pending_is_a_strict_no_op(self):
        # No shortfall, no contribution beyond the loss: the rule must not
        # fire (DP#32: a no-op is the golden-preserving default).
        state = SimState(
            non_reg_balance=400_000.0, non_reg_acb=300_000.0,
            jurisdiction_state={'canada': _default_canada_state()},
        )
        yr, _ = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 130_000, '_annual_savings': 0},
            config=_make_config(), investment_return=0.06,
            primary_marginal_rate=0.30,
            living_costs=0.0, after_tax_income=100_000,
        )
        assert yr.superficial_loss_denied == 0.0
        assert yr.superficial_loss_pended == 0.0
        assert yr.superficial_loss_released == 0.0
        assert yr.superficial_loss_acb_added == 0.0


# ── Contract mapping: decisions.superficial_loss.substitute_pairs ─────────


def _two_generation_doc():
    """A validated two-generation contract document (the adapter's Phase 1
    sub-family), via test_input_contract's fixture builders."""
    import contract_schema
    from test_input_contract import _load_example, _two_generation_subset
    doc = _two_generation_subset(_load_example())
    contract_schema.validate_contract(doc)
    return doc


class TestSubstitutePairDeclarationMapping:
    """DP#32 on the INPUT side: a malformed `decisions.superficial_loss`
    declaration must FAIL LOUDLY at load time, never coerce to the
    conservative default -- a household that declares substitutes and is
    silently ignored loses money it was told it could keep."""

    @staticmethod
    def _doc(pairs):
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = {'substitute_pairs': pairs}
        return doc

    def test_absent_block_returns_empty(self):
        doc = _two_generation_doc()
        assert 'superficial_loss' not in doc['decisions']
        assert map_superficial_loss(doc) == []

    def test_empty_block_returns_empty(self):
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = {}
        assert map_superficial_loss(doc) == []

    def test_declared_pairs_round_trip(self):
        doc = self._doc([['XEQT', 'VEQT'], ['XBB', 'VAB']])
        assert map_superficial_loss(doc) == [['XEQT', 'VEQT'], ['XBB', 'VAB']]

    def test_adapter_emits_pairs_only_when_declared(self):
        import input_contract as ic
        # Absent block: the key stays OUT of the internal shape entirely,
        # so a no-declaration household round-trips byte-identically.
        legacy_without = ic.to_internal_config(_two_generation_doc())
        assert 'superficial_loss_substitute_pairs' not in legacy_without
        # Declared block: the pairs reach the legacy config the engine reads.
        legacy = ic.to_internal_config(self._doc([['XEQT', 'VEQT']]))
        assert legacy['superficial_loss_substitute_pairs'] == [['XEQT', 'VEQT']]

    def test_non_object_block_raises(self):
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = ['XEQT', 'VEQT']
        with pytest.raises(ValueError, match='must be an object'):
            map_superficial_loss(doc)

    def test_block_without_substitute_pairs_raises(self):
        # A partial declaration is refused: `substitute_pairs` absent means
        # the household STARTED a declaration and stopped -- silence here
        # would apply the conservative default to a household that meant
        # to declare (DP#32).
        doc = _two_generation_doc()
        doc['decisions']['superficial_loss'] = {'declared_elsewhere': True}
        with pytest.raises(ValueError, match='substitute_pairs is'):
            map_superficial_loss(doc)

    def test_non_list_substitute_pairs_raises(self):
        doc = self._doc('XEQT')
        with pytest.raises(ValueError, match='must be a list'):
            map_superficial_loss(doc)

    @pytest.mark.parametrize('bad_pair', [
        'XEQT',            # not a list
        ['XEQT'],          # one element
        ['XEQT', 'VEQT', 'XBB'],  # three elements
        ['XEQT', ''],      # empty string
        ['XEQT', 42],      # non-string
    ])
    def test_malformed_pair_raises(self, bad_pair):
        doc = self._doc([bad_pair])
        with pytest.raises(ValueError, match='exactly two non-empty strings'):
            map_superficial_loss(doc)

    def test_self_pair_raises(self):
        # A security is trivially identical to itself: declaring it a
        # substitute of itself asserts a falsehood to dodge the window.
        doc = self._doc([['XEQT', 'XEQT']])
        with pytest.raises(ValueError, match='same security twice'):
            map_superficial_loss(doc)


class TestFidelityDisclosure:
    """The run-recorded bridge (#685/#707): the optimize caller writes the
    worst-across-scenarios summary onto assumptions.superficial_loss and the
    registered approximation reads it. Drive the REGISTRY entry, never the
    private describe function, so the registration itself is what the test
    proves."""

    @staticmethod
    def _summary(**overrides):
        s = {'engaged': True, 'first_denied_year': 3,
             'denied_total': 12_000.0, 'acb_added_total': 12_000.0,
             'pending_years': 1}
        s.update(overrides)
        return s

    def _approximation(self):
        import model_fidelity
        active = [a for a in model_fidelity.all_approximations()
                  if a.id == 'superficial_loss_annual_window']
        assert active, 'superficial_loss_annual_window must be registered'
        return active[0]

    def test_engaged_summary_fires_every_finding_branch(self):
        import model_fidelity
        approx = self._approximation()
        cfg = {'assumptions': {'superficial_loss': self._summary()}}
        ctx = model_fidelity.FidelityContext(cfg=cfg)
        assert approx.is_active(ctx)
        findings = approx.findings_for(ctx)
        text = '\n'.join(findings)
        assert 'year 3' in text                        # first_denied_year named
        assert '12,000' in text                        # denied dollars named
        assert '53(1)(f)' in text                      # ACB deferral named
        assert 'held' in text                          # pending carry named
        assert 'ANNUAL' in text                        # the abstraction itself

    def test_not_engaged_is_inactive_and_silent(self):
        import model_fidelity
        approx = self._approximation()
        ctx = model_fidelity.FidelityContext(cfg={'assumptions': {}})
        assert not approx.is_active(ctx)
        assert approx.findings_for(ctx) == []

    def test_zero_denial_engaged_summary_is_not_a_finding_source(self):
        # Engaged=False even with figures present: the caveat must not
        # fire on an all-clear recorded summary.
        import model_fidelity
        approx = self._approximation()
        cfg = {'assumptions': {'superficial_loss': self._summary(engaged=False)}}
        ctx = model_fidelity.FidelityContext(cfg=cfg)
        assert not approx.is_active(ctx)
