#!/usr/bin/env python3
"""Tests for issue #170: RRSP contributions that exceed the contributor's
room are silently clipped to zero by ``apply_contributions``' bare ``min()`` --
no warning, no model_fidelity disclosure, no redirect. The declared
contribution is budgeted and "paid", but the engine books $0 and nothing
records the refusal.

The fix surfaces the clip (DP#32: a declared contribution that is refused is
a FACT the engine must disclose, not an absence to absorb):

  1. ``YearWorkingState.rrsp_refused_own`` / ``rrsp_refused_spousal`` -- what
     the fold refused this year;
  2. ``YearResult.rrsp_contribution_refused_own`` / ``_spousal`` -- surfaced on
     every year of output;
  3. ``summarize_rrsp_refusal`` -- the trajectory-level facts, recorded onto
     ``assumptions.rrsp_contribution_refused`` by the optimize caller (the same
     bridge ``decumulation_shortfall`` uses, #707);
  4. a registered model_fidelity Approximation that fires only when a run
     actually refused money.

Golden no-op (DP#32): contributions WITHIN room refuse $0 and change no other
number -- asserted here at the rule level and by the standing golden suite.

Fabricated round numbers, role-based names only (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rule_registry import YearWorkingState
from rules_contributions import apply_contributions, summarize_rrsp_refusal


def _ws(*, rrsp_room=100_000.0, spouse_rrsp_room=0.0,
        p_rrsp=0.0, s_rrsp=0.0, sp_rrsp=0.0,
        tfsa_p_room=0.0, tfsa_sp_room=0.0,
        p_tfsa=0.0, sp_tfsa=0.0, non_reg_alloc=0.0):
    """A minimal working state: the only fields ``apply_contributions`` reads."""
    ws = YearWorkingState()
    ws.opening_rrsp_room = rrsp_room
    ws.opening_spouse_rrsp_room = spouse_rrsp_room
    ws.opening_tfsa_primary_room = tfsa_p_room
    ws.opening_tfsa_spouse_room = tfsa_sp_room
    ws.opening_rrsp_balance = 0.0
    ws.opening_spousal_rrsp_balance = 0.0
    ws.opening_spouse_rrsp_balance = 0.0
    ws.opening_tfsa_primary_balance = 0.0
    ws.opening_tfsa_spouse_balance = 0.0
    ws.opening_non_reg_balance = 0.0
    ws.opening_non_reg_acb = 0.0
    ws.p_rrsp = p_rrsp
    ws.s_rrsp = s_rrsp
    ws.sp_rrsp = sp_rrsp
    ws.p_tfsa = p_tfsa
    ws.sp_tfsa = sp_tfsa
    ws.non_reg_alloc = non_reg_alloc
    return ws


class TestRefusalSurfacedAtTheRule:
    def test_overroom_spousal_is_refused_not_vanished(self):
        """The issue's exact illustration: $100k room, $100k own + $10k
        spousal declared. The own contribution books in full; the spousal
        contribution is refused IN FULL -- and the refusal is now RECORDED
        ($10k), not silently dropped by min()."""
        ws = _ws(rrsp_room=100_000.0, p_rrsp=100_000.0, s_rrsp=10_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.s_rrsp_actual == 0.0, "over-room spousal contribution must not book"
        assert ws.rrsp_refused_spousal == 10_000.0
        assert ws.rrsp_refused_own == 0.0

    def test_partially_refused_spousal_records_the_exact_excess(self):
        """Own takes part of the pool; spousal gets only the remainder -- the
        refused slice is the exact excess, not the whole declaration."""
        ws = _ws(rrsp_room=100_000.0, p_rrsp=60_000.0, s_rrsp=50_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.s_rrsp_actual == 40_000.0      # the remaining room
        assert ws.p_rrsp_actual == 60_000.0
        assert ws.rrsp_refused_spousal == 10_000.0
        assert ws.rrsp_refused_own == 0.0

    def test_overroom_own_is_refused_too(self):
        """An own-RRSP declaration above the contributor's total room is also
        surfaced, not absorbed."""
        ws = _ws(rrsp_room=80_000.0, p_rrsp=100_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.p_rrsp_actual == 80_000.0
        assert ws.rrsp_refused_own == 20_000.0

    def test_within_room_refuses_zero(self):
        """Golden no-op (DP#32): contributions within room record a $0
        refusal and book exactly what was declared."""
        ws = _ws(rrsp_room=100_000.0, p_rrsp=60_000.0, s_rrsp=40_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.p_rrsp_actual == 60_000.0
        assert ws.s_rrsp_actual == 40_000.0
        assert ws.rrsp_refused_own == 0.0
        assert ws.rrsp_refused_spousal == 0.0


class TestTrajectorySummary:
    def test_summary_folds_the_first_refused_year_and_totals(self):
        rows = []
        for i, (own, spousal) in enumerate([(0.0, 0.0), (5_000.0, 0.0),
                                            (0.0, 2_500.0)]):
            r = type('R', (), {})()
            r.year = 2026 + i
            r.rrsp_contribution_refused_own = own
            r.rrsp_contribution_refused_spousal = spousal
            rows.append(r)
        s = summarize_rrsp_refusal(rows)
        assert s['engaged'] is True
        assert s['first_refused_year'] == 2027
        assert s['refused_own_total'] == 5_000.0
        assert s['refused_spousal_total'] == 2_500.0

    def test_summary_all_clear_when_nothing_refused(self):
        r = type('R', (), {})()
        r.year = 2026
        r.rrsp_contribution_refused_own = 0.0
        r.rrsp_contribution_refused_spousal = 0.0
        s = summarize_rrsp_refusal([r])
        assert s['engaged'] is False
        assert s['first_refused_year'] is None
        assert s['refused_own_total'] == 0.0
        assert s['refused_spousal_total'] == 0.0


class TestEngineEndToEnd:
    """Integration (DP#11): drive the REAL fold with a strategy that declares
    over-room RRSP contributions, and assert the refusal reaches YearResult.
    The optimizer's own strategies cap by room (strategy.py clamps spousal to
    primary_rrsp_room and own to the remainder), which is exactly why this bug
    was invisible there -- so the engine-level trigger uses the sanctioned
    pluggable-strategy seam (DP#8) to declare what a manual household would."""

    def _config(self):
        return {
            'family': {
                'members': [
                    {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
                     'retirement_age': 65, 'rrsp_room_accumulated': 100_000,
                     'tfsa_room_accumulated': 20_000},
                    {'role': 'spouse', 'birth_year': 1982, 'gross_income': 60_000,
                     'retirement_age': 65, 'rrsp_room_accumulated': 0,
                     'tfsa_room_accumulated': 20_000},
                ],
                'children': [],
            },
            'accounts': {'rrsp_annual_max': 31_000},
            'assumptions': {
                'start_year': 2026, 'projection_years': 3,
                'investment_return': 0.05, 'salary_growth': 0.0,
                'frozen_brackets': True,
            },
            'savings': {'rate': 0.10},
            'property': {
                'house_value': 600_000, 'mortgage_balance': 200_000,
                'mortgage_rate': 0.045, 'ltv_max': 0.80,
                'current_payment_monthly': 1_200, 'amortization_years': 25,
                'margin_available': 0,
            },
            'tax': {'province': 'qc'},
        }

    def test_overroom_declaration_surfaces_on_year_result(self):
        """A declaration path that hands OVER-ROOM RRSP contributions to the
        fold (the manual-declaration case from the issue -- the optimizer's
        own StrategyEngine caps by room and never produces one) must see the
        refusal ON the YearResult. Only the allocation boundary is stubbed;
        the fold clamp, the working state, the epilogue and YearResult are the
        real engine."""
        from unittest import mock
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        from strategy import AllocationResult

        over_room = AllocationResult(primary_rrsp=100_000.0,
                                     spousal_rrsp=10_000.0)
        cfg = SimulationConfig.from_dict(self._config())
        sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))

        with mock.patch('strategy.StrategyEngine.allocate',
                        return_value=over_room):
            results = sim.run()

        first = results[0]
        # The own contribution booked in full; the spousal contribution was
        # refused in full -- and the refusal is ON THE OUTPUT, not vanished.
        assert first.spousal_rrsp == 0.0
        assert first.rrsp_contribution_refused_own == 0.0
        assert first.rrsp_contribution_refused_spousal == 10_000.0
        # And the trajectory-level summary folds it: the mocked declaration
        # recurs EVERY year, so 3 projection years refuse 3 x $10k.
        s = summarize_rrsp_refusal(results)
        assert s['engaged'] is True
        assert s['first_refused_year'] == first.year
        assert s['refused_spousal_total'] == 3 * 10_000.0

    def test_within_room_strategy_refuses_zero(self):
        """Golden no-op end-to-end (DP#32): a normal within-room run records
        $0 refusal on every year and changes no balance the golden suite
        doesn't already pin."""
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation

        cfg = SimulationConfig.from_dict(self._config())
        results = FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()
        for r in results:
            assert r.rrsp_contribution_refused_own == 0.0
            assert r.rrsp_contribution_refused_spousal == 0.0


class TestWorstAcrossScenarios:
    """The optimize caller reduces per-scenario summaries to the one the
    run-wide caveat names (worst = most money refused; ties to earliest)."""

    def test_worst_is_the_largest_total_refusal(self):
        from rules_contributions import worst_rrsp_refusal
        rows = [
            {'engaged': True, 'first_refused_year': 2027,
             'refused_own_total': 1_000.0, 'refused_spousal_total': 0.0},
            {'engaged': True, 'first_refused_year': 2030,
             'refused_own_total': 0.0, 'refused_spousal_total': 9_000.0},
        ]
        s = worst_rrsp_refusal(rows)
        assert s['first_refused_year'] == 2030
        assert s['refused_spousal_total'] == 9_000.0

    def test_tie_breaks_to_earliest_year(self):
        from rules_contributions import worst_rrsp_refusal
        rows = [
            {'engaged': True, 'first_refused_year': 2031,
             'refused_own_total': 5_000.0, 'refused_spousal_total': 0.0},
            {'engaged': True, 'first_refused_year': 2028,
             'refused_own_total': 5_000.0, 'refused_spousal_total': 0.0},
        ]
        assert worst_rrsp_refusal(rows)['first_refused_year'] == 2028

    def test_all_clear_rows_give_the_all_clear_summary(self):
        from rules_contributions import worst_rrsp_refusal
        s = worst_rrsp_refusal([{'engaged': False}])
        assert s['engaged'] is False
        assert s['refused_own_total'] == 0.0
        # Non-dict rows (older/synthetic) are skipped, not crashed on.
        assert worst_rrsp_refusal([None, 'x'])['engaged'] is False


class TestFidelityDisclosure:
    def test_approximation_is_registered(self):
        from model_fidelity import all_approximations
        ids = {a.id for a in all_approximations()}
        assert 'rrsp_contribution_refused' in ids

    def test_fires_only_when_the_run_recorded_a_refusal(self):
        """The caveat reads assumptions.rrsp_contribution_refused (the same
        run-recorded bridge as decumulation_shortfall): absent -> inactive;
        present-and-engaged -> active, naming the year and the amounts."""
        from model_fidelity import (FidelityContext, active_approximations,
                                    rrsp_refusal_summary)
        # Absent key AND non-dict cfg both resolve to the all-clear summary.
        assert rrsp_refusal_summary({})['engaged'] is False
        assert rrsp_refusal_summary(None)['engaged'] is False
        cfg = {'assumptions': {}}
        active = {a.id for a in active_approximations(cfg)}
        assert 'rrsp_contribution_refused' not in active

        cfg = {'assumptions': {'rrsp_contribution_refused': {
            'engaged': True, 'first_refused_year': 2027,
            'refused_own_total': 5_000.0, 'refused_spousal_total': 10_000.0}}}
        by_id = {a.id: a for a in active_approximations(cfg)}
        assert 'rrsp_contribution_refused' in by_id
        findings = by_id['rrsp_contribution_refused'].findings(
            FidelityContext(cfg=cfg))
        assert findings, "an engaged refusal must name its facts"
        joined = "\n".join(findings)
        assert "2027" in joined
        assert "5,000" in joined.replace("$", "") or "5,000" in joined

    def test_findings_empty_when_not_engaged(self):
        """A disengaged summary produces no finding lines (the early return
        the registry's not-engaged path exists for)."""
        from model_fidelity import FidelityContext, _describe_rrsp_refusal
        cfg = {'assumptions': {'rrsp_contribution_refused': {
            'engaged': False, 'first_refused_year': None,
            'refused_own_total': 0.0, 'refused_spousal_total': 0.0}}}
        assert _describe_rrsp_refusal(FidelityContext(cfg=cfg)) == []
