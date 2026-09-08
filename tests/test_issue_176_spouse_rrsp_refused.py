#!/usr/bin/env python3
"""Tests for issue #176: the spouse's OWN RRSP contribution (``sp_rrsp``) is
clamped to her room by ``apply_contributions``' bare ``min()`` -- and, unlike
the primary's own/spousal contributions (issue #170, which added
``rrsp_refused_own`` / ``rrsp_refused_spousal``), nothing records the refused
slice. The JSON output reports the DECLARED ``contributions.spouse_rrsp``,
not the post-clamp ACTUAL, so a household following the plan literally
over-contributes and risks the CRA 1%/month excess-contribution tax
(T1-OVP) above the $2,000 grace.

This is the same silent-clip failure class as #170, one line over. The fix
mirrors #170 exactly:

  1. ``YearWorkingState.spouse_rrsp_refused`` -- what the fold refused this
     year from the spouse's OWN declaration;
  2. ``YearResult.rrsp_contribution_refused_spouse_own`` -- surfaced on every
     year of output, beside the existing ``_own`` / ``_spousal`` fields;
  3. ``summarize_rrsp_refusal`` / ``worst_rrsp_refusal`` -- the trajectory
     facts now include the spouse-own totals, so the model_fidelity caveat
     (``rrsp_contribution_refused``) names a spouse-own refusal too.

Golden no-op (DP#32): declarations WITHIN the spouse's room refuse $0 and
change no other number -- asserted here at the rule level and (by standing
suite) the golden trajectory.

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


class TestSpouseOwnRefusalSurfacedAtTheRule:
    def test_overroom_spouse_own_is_refused_not_vanished(self):
        """The issue's exact illustration: $20k spouse room, $25k declared to
        the spouse's OWN RRSP. The clamp books only the room; the excess is a
        RECORDED refusal (not silently dropped by min())."""
        ws = _ws(spouse_rrsp_room=20_000.0, sp_rrsp=25_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.sp_rrsp_actual == 20_000.0, "only the room may book"
        assert ws.spouse_rrsp_refused == 5_000.0
        assert ws.rrsp_refused_own == 0.0
        assert ws.rrsp_refused_spousal == 0.0

    def test_partially_refused_spouse_own_records_the_exact_excess(self):
        """The refused slice is the exact excess, not the whole declaration."""
        ws = _ws(spouse_rrsp_room=50_000.0, sp_rrsp=65_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.sp_rrsp_actual == 50_000.0
        assert ws.spouse_rrsp_refused == 15_000.0

    def test_zero_room_refuses_the_whole_contribution(self):
        """No room (opening_spouse_rrsp_room == 0) -> every declared dollar to
        the spouse's own RRSP is refused and the refusal is the full amount,
        not an absent field."""
        ws = _ws(spouse_rrsp_room=0.0, sp_rrsp=10_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.sp_rrsp_actual == 0.0
        assert ws.spouse_rrsp_refused == 10_000.0

    def test_within_room_refuses_zero(self):
        """Golden no-op (DP#32): a declaration within the spouse's room
        records a $0 refusal (present, not absent) and books exactly what was
        declared."""
        ws = _ws(spouse_rrsp_room=100_000.0, sp_rrsp=40_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.sp_rrsp_actual == 40_000.0
        assert ws.spouse_rrsp_refused == 0.0
        assert ws.rrsp_refused_own == 0.0
        assert ws.rrsp_refused_spousal == 0.0

    def test_combined_overroom_every_role_is_surfaced_separately(self):
        """primary own, spousal and spouse-own refusals coexist and each is
        attributed to its own declared line -- no cross-contamination."""
        ws = _ws(rrsp_room=100_000.0, spouse_rrsp_room=10_000.0,
                 p_rrsp=90_000.0, s_rrsp=20_000.0, sp_rrsp=15_000.0)
        apply_contributions(ws, ctx=None)
        assert ws.p_rrsp_actual == 90_000.0
        assert ws.s_rrsp_actual == 10_000.0
        assert ws.sp_rrsp_actual == 10_000.0
        assert ws.rrsp_refused_own == 0.0
        assert ws.rrsp_refused_spousal == 10_000.0
        assert ws.spouse_rrsp_refused == 5_000.0


class TestTrajectorySummary:
    def test_summary_folds_spouse_own_totals_too(self):
        """The trajectory fold must carry the spouse's own refusals in
        addition to the #170 own/spousal ones -- the fidelity caveat reads
        this summary."""
        rows = []
        for i, (own, spousal, spouse_own) in enumerate(
                [(0.0, 0.0, 0.0), (0.0, 0.0, 5_000.0), (2_500.0, 0.0, 0.0)]):
            r = type('R', (), {})()
            r.year = 2026 + i
            r.rrsp_contribution_refused_own = own
            r.rrsp_contribution_refused_spousal = spousal
            r.rrsp_contribution_refused_spouse_own = spouse_own
            rows.append(r)
        s = summarize_rrsp_refusal(rows)
        assert s['engaged'] is True
        assert s['first_refused_year'] == 2027
        assert s['refused_own_total'] == 2_500.0
        assert s['refused_spousal_total'] == 0.0
        assert s['refused_spouse_own_total'] == 5_000.0

    def test_summary_all_clear_when_nothing_refused(self):
        r = type('R', (), {})()
        r.year = 2026
        r.rrsp_contribution_refused_own = 0.0
        r.rrsp_contribution_refused_spousal = 0.0
        r.rrsp_contribution_refused_spouse_own = 0.0
        s = summarize_rrsp_refusal([r])
        assert s['engaged'] is False
        assert s['first_refused_year'] is None
        assert s['refused_own_total'] == 0.0
        assert s['refused_spousal_total'] == 0.0
        assert s['refused_spouse_own_total'] == 0.0

    def test_summary_tolerates_pre_176_rows(self):
        """Rows produced before this change (no spouse-own attribute) fold as
        if the spouse refused $0 -- the getattr default, not a crash."""
        r = type('R', (), {})()
        r.year = 2026
        r.rrsp_contribution_refused_own = 1_000.0
        r.rrsp_contribution_refused_spousal = 0.0
        s = summarize_rrsp_refusal([r])
        assert s['engaged'] is True
        assert s['refused_spouse_own_total'] == 0.0


class TestEngineEndToEnd:
    """Integration (DP#11): drive the REAL fold with a strategy that declares
    an over-room spouse-own RRSP contribution, and assert the refusal reaches
    YearResult. The optimizer's own strategies cap by room, which is why this
    bug was invisible there -- so the trigger uses the sanctioned pluggable-
    strategy seam (DP#8), exactly like #170's engine test."""

    def _config(self):
        return {
            'family': {
                'members': [
                    {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
                     'retirement_age': 65, 'rrsp_room_accumulated': 100_000,
                     'tfsa_room_accumulated': 20_000},
                    {'role': 'spouse', 'birth_year': 1982, 'gross_income': 60_000,
                     'retirement_age': 65, 'rrsp_room_accumulated': 20_000,
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

    def test_overroom_spouse_declaration_surfaces_on_year_result(self):
        """A house declaring an over-room contribution to the SPOUSE'S OWN
        RRSP sees the refused excess ON the YearResult -- and the trajectory
        summary against it is engaged with a non-zero spouse-own total."""
        from unittest import mock
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        from strategy import AllocationResult

        over_room = AllocationResult(spouse_rrsp=35_000.0)
        cfg = SimulationConfig.from_dict(self._config())
        sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))

        with mock.patch('strategy.StrategyEngine.allocate',
                        return_value=over_room):
            results = sim.run()

        first = results[0]
        # Year 1: spouse has $20k accumulated own room; $35k declared ->
        # $20k booked, $15k refused. (#170 semantics preserved: total nothing
        # else of the refusals is on; primary/spousal are zero.)
        assert first.rrsp_contribution_refused_own == 0.0
        assert first.rrsp_contribution_refused_spousal == 0.0
        assert first.rrsp_contribution_refused_spouse_own == 15_000.0
        assert first.spouse_rrsp == 20_000.0
        s = summarize_rrsp_refusal(results)
        assert s['engaged'] is True
        assert s['refused_spouse_own_total'] >= 15_000.0

    def test_within_room_strategy_refuses_zero_end_to_end(self):
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation

        cfg = SimulationConfig.from_dict(self._config())
        results = FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()
        for r in results:
            assert r.rrsp_contribution_refused_spouse_own == 0.0
            assert r.rrsp_contribution_refused_own == 0.0
            assert r.rrsp_contribution_refused_spousal == 0.0


class TestWorstAcrossScenarios:
    """The optimize caller's worst-across-scenarios reduction must count a
    spouse-own refusal among the money refused (ties still break earliest)."""

    def test_worst_includes_spouse_own_total(self):
        from rules_contributions import worst_rrsp_refusal
        rows = [
            {'engaged': True, 'first_refused_year': 2027,
             'refused_own_total': 1_000.0, 'refused_spousal_total': 0.0,
             'refused_spouse_own_total': 0.0},
            {'engaged': True, 'first_refused_year': 2030,
             'refused_own_total': 0.0, 'refused_spousal_total': 0.0,
             'refused_spouse_own_total': 9_000.0},
        ]
        s = worst_rrsp_refusal(rows)
        assert s['first_refused_year'] == 2030
        assert s['refused_spouse_own_total'] == 9_000.0

    def test_all_clear_summary_carries_zero_spouse_own(self):
        from rules_contributions import worst_rrsp_refusal
        s = worst_rrsp_refusal([{'engaged': False}])
        assert s['engaged'] is False
        assert s['refused_spouse_own_total'] == 0.0
        assert worst_rrsp_refusal([None, 'x'])['engaged'] is False


class TestFidelityDisclosure:
    def test_findings_name_a_spouse_own_refusal(self):
        """The caveat's findings must name the spouse-OWN refused total when
        the run recorded one -- a household sees its spouse's own over-room
        declaration, not a silence that reads as 'all booked'."""
        from model_fidelity import (FidelityContext, _describe_rrsp_refusal)
        cfg = {'assumptions': {'rrsp_contribution_refused': {
            'engaged': True, 'first_refused_year': 2027,
            'refused_own_total': 0.0, 'refused_spousal_total': 0.0,
            'refused_spouse_own_total': 12_000.0}}}
        findings = _describe_rrsp_refusal(FidelityContext(cfg=cfg))
        assert findings, "an engaged refusal must name its facts"
        joined = "\n".join(findings)
        assert "12,000" in joined.replace("$", "") or "12,000" in joined