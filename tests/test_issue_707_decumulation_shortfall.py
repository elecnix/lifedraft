"""Issue #707: a decumulation plan that runs out of money before the
projection horizon must surface the shortfall, not print a confident terminal
number.

This is the repo's founding defect class -- the engine silently substitutes
zero. A missing input becomes 0; an unimplemented rule becomes a no-op; and
here, a bankrupt year becomes just another number. Before the fix, the golden
household with ``retirement.spending_target`` raised to 250,000 ran out of
money in year 26 and still printed ``retirement_income = 44,216`` against a
``205,784`` net target with ``drawdown_net_delivered = 0`` and every account
at 0, and no field signalled the gap.

These tests prove the fix and guard the property:

1. **Reproduction is real** -- the trajectory really does exhaust (fabricated
   round numbers, role-based names -- DP#4/DP#15).
2. **The shortfall is detected** -- ``YearResult.drawdown_shortfall`` is
   non-zero in the bankrupt years; ``decumulation.summarize_drawdown_shortfall``
   folds the trajectory into {exhausted, first_shortfall_year, gap, ...}.
3. **The invariant catches the silent zero** -- ``drawdown_shortfall_surfaced``
   fails on a year that exhausted with delivered < target and the field at 0
   (the pre-fix state), and passes once the field is recorded.
4. **The objective does not crown a bankrupt plan** -- ``ranking_key`` sorts
   an exhausted trajectory below a non-exhausted one even when its headline
   net benefit is higher.
5. **The shortfall reaches every output surface** -- the model_fidelity
   caveat (console/TXT/JSON/HTML) names the first year and the gap.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from year_result import YearResult
import decumulation
import model_fidelity
from trajectory_invariants import run_invariant
from output_plugins import TextReport, JsonReport, HtmlReport

from test_golden_trajectory_581 import golden_household_config, _run


# ── 1. Reproduction: the trajectory really exhausts before the horizon ─────

def _bankrupt_results():
    """The golden household with spending_target raised to 250,000 -- fabricated
    round numbers, role-based names (DP#4/DP#15). Runs out of money partway
    through retirement."""
    cfg = golden_household_config()
    cfg['retirement']['spending_target'] = 250_000
    return _run(cfg)


class TestReproduction(unittest.TestCase):
    """The bug is real: a well-funded-looking household exhausts its assets
    years before the horizon, and before the fix the run said nothing."""

    def test_the_trajectory_actually_runs_out_of_money(self):
        rs = _bankrupt_results()
        # There is at least one retirement year where the drawdown could not
        # deliver the net target AND every drawable account is exhausted.
        bankrupt = [
            r for r in rs
            if r.drawdown_net_target > 0
            and r.drawdown_net_delivered < r.drawdown_net_target - 1.0
            and (r.total_rrsp + r.total_tfsa + r.non_reg_balance
                 + r.lif_balance + r.lira_balance) <= 1.0
        ]
        self.assertGreater(len(bankrupt), 0,
                           "expected the 250k spending target to bankrupt the "
                           "golden household before the horizon; the reproduction "
                           "no longer reproduces -- re-examine the fixture")
        # And it runs out MANY years before the end (the issue's "19 years
        # before death" symptom), not just the final year.
        self.assertGreater(len(bankrupt), 5)
        # #1046 RESP-allocation fix changed the drawdown float path so
        # terminal total_assets is float dust (~1.57e-11) rather than exactly
        # 0.0; the trajectory still runs out (assertGreater(len(bankrupt), 5)
        # still holds).
        self.assertTrue(abs(rs[-1].total_assets) < 1e-6,
                        f"terminal total_assets should be effectively zero, got "
                        f"{rs[-1].total_assets}")
        self.assertLess(rs[-1].retirement_income, rs[-1].drawdown_net_target)


# ── 2. Detection: the shortfall is recorded on the year and in the fold ────

class TestDetection(unittest.TestCase):
    def test_drawdown_shortfall_field_is_populated_in_bankrupt_years(self):
        rs = _bankrupt_results()
        shortfall_years = [r for r in rs if r.drawdown_shortfall > 0]
        self.assertGreater(len(shortfall_years), 0)
        for r in shortfall_years:
            self.assertAlmostEqual(
                r.drawdown_shortfall,
                r.drawdown_net_target - r.drawdown_net_delivered,
                places=2,
                msg="drawdown_shortfall must equal target - delivered when set")

    def test_no_shortfall_in_the_well_funded_golden_run(self):
        # The unmodified golden household (70k target) does NOT exhaust -- the
        # field is 0 everywhere, proving the detection does not false-fire on
        # a solvent trajectory.
        rs = _run(golden_household_config())
        self.assertTrue(all(r.drawdown_shortfall == 0.0 for r in rs))

    def test_summarize_drawdown_shortfall_names_first_year_and_gap(self):
        rs = _bankrupt_results()
        summary = decumulation.summarize_drawdown_shortfall(rs)
        self.assertTrue(summary['engaged'])
        self.assertTrue(summary['exhausted'])
        self.assertIsNotNone(summary['first_shortfall_year'])
        self.assertGreater(summary['first_shortfall_gap'], 0.0)
        self.assertGreater(summary['shortfall_years'], 0)
        self.assertGreater(summary['total_unmet'], 0.0)
        # The first shortfall year's gap matches the field on that year.
        first = next(r for r in rs if r.drawdown_shortfall > 0)
        self.assertEqual(summary['first_shortfall_year'], first.year)
        self.assertAlmostEqual(summary['first_shortfall_gap'],
                               first.drawdown_shortfall, places=2)

    def test_summary_is_all_false_for_a_solvent_trajectory(self):
        rs = _run(golden_household_config())
        summary = decumulation.summarize_drawdown_shortfall(rs)
        self.assertFalse(summary['exhausted'])
        self.assertIsNone(summary['first_shortfall_year'])


# ── 3. The invariant catches the silent zero (the guard that would have
#       caught the bug, wired into the default pytest suite) ─────────────────

class TestInvariant(unittest.TestCase):
    def test_invariant_passes_on_the_fixed_bankrupt_trajectory(self):
        rs = _bankrupt_results()
        violations = run_invariant('drawdown_shortfall_surfaced', rs, {})
        self.assertEqual(violations, [],
                         f"the fixed trajectory must not violate the surfaced "
                         f"invariant: {[str(v) for v in violations]}")

    def test_invariant_fails_when_a_shortfall_is_silently_swallowed(self):
        # The pre-fix state: a year that exhausted, delivered < target, and
        # the field at 0. The invariant MUST flag it -- this is the exact
        # shape #707 is about.
        bad = YearResult(
            year=27,
            drawdown_net_target=205_784.0,
            drawdown_net_delivered=0.0,
            drawdown_shortfall=0.0,  # silently swallowed -- the bug
        )
        violations = run_invariant('drawdown_shortfall_surfaced', [bad], {})
        self.assertEqual(len(violations), 1)
        self.assertIn('silently swallowed', str(violations[0]))

    def test_invariant_passes_once_the_field_is_recorded(self):
        good = YearResult(
            year=27,
            drawdown_net_target=205_784.0,
            drawdown_net_delivered=0.0,
            drawdown_shortfall=205_784.0,  # surfaced
        )
        violations = run_invariant('drawdown_shortfall_surfaced', [good], {})
        self.assertEqual(violations, [])

    def test_invariant_does_not_fire_when_a_balance_remained_undrawn(self):
        # delivered < target BUT a balance remained -> that is a drawdown-sizing
        # bug (the sibling invariant's job), NOT a silent shortfall. The
        # surfaced invariant must not double-report it.
        sizing_bug = YearResult(
            year=27,
            drawdown_net_target=205_784.0,
            drawdown_net_delivered=100_000.0,
            drawdown_shortfall=0.0,
            total_rrsp=50_000.0,
        )
        violations = run_invariant('drawdown_shortfall_surfaced', [sizing_bug], {})
        self.assertEqual(violations, [])

    def test_invariant_skips_a_year_the_solvency_waterfall_drew_after(self):
        # drawdown did not exhaust (field 0) but the cash-flow identity then
        # drew the rest -- final balances cannot tell us whether the DRAWDOWN
        # exhausted, so the invariant must skip rather than guess (DP#32).
        solvency_drew = YearResult(
            year=27,
            drawdown_net_target=205_784.0,
            drawdown_net_delivered=100_000.0,
            drawdown_shortfall=0.0,
            solvency_shortfall=50_000.0,
        )
        violations = run_invariant('drawdown_shortfall_surfaced', [solvency_drew], {})
        self.assertEqual(violations, [])


# ── 4. The objective does not crown a bankrupt plan ────────────────────────

class TestObjectiveRanking(unittest.TestCase):
    def test_an_exhausted_plan_ranks_below_a_solvent_one_with_higher_score(self):
        # A bankrupt plan that prints a HIGHER headline net benefit than a
        # solvent one -- exactly the case the issue warns "is how a bankrupt
        # plan wins" on a single-score ranking.
        short = decumulation.summarize_drawdown_shortfall([YearResult(
            year=27, drawdown_net_target=205_784.0,
            drawdown_net_delivered=0.0, drawdown_shortfall=205_784.0)])
        ok = decumulation.summarize_drawdown_shortfall([YearResult(year=1)])
        results = [
            {'label': 'Bankrupt', 'net_benefit': 1_200_000,
             'drawdown_shortfall': short, 'exhausted': True},
            {'label': 'Solvent', 'net_benefit': 500_000,
             'drawdown_shortfall': ok, 'exhausted': False},
        ]
        ranked = sorted(
            results,
            key=lambda r: decumulation.ranking_key(r, r.get('net_benefit', 0)),
            reverse=True)
        self.assertEqual(ranked[0]['label'], 'Solvent')
        self.assertEqual(ranked[-1]['label'], 'Bankrupt')

    def test_within_exhausted_higher_score_still_ranks_first(self):
        short = {'engaged': True, 'exhausted': True, 'first_shortfall_year': 27,
                 'first_shortfall_gap': 1.0, 'shortfall_years': 1, 'total_unmet': 1.0}
        results = [
            {'label': 'B1', 'net_benefit': 100, 'drawdown_shortfall': short, 'exhausted': True},
            {'label': 'B2', 'net_benefit': 500, 'drawdown_shortfall': short, 'exhausted': True},
        ]
        ranked = sorted(
            results,
            key=lambda r: decumulation.ranking_key(r, r.get('net_benefit', 0)),
            reverse=True)
        self.assertEqual([r['label'] for r in ranked], ['B2', 'B1'])

    def test_synthetic_rows_without_exhausted_flag_are_treated_as_solvent(self):
        # A safe drop-in for the bare net_benefit sort key: older/synthetic
        # rows that carry no 'exhausted' do not crash and sort by score.
        results = [
            {'label': 'old', 'net_benefit': 10},
            {'label': 'old2', 'net_benefit': 99},
        ]
        ranked = sorted(
            results,
            key=lambda r: decumulation.ranking_key(r, r.get('net_benefit', 0)),
            reverse=True)
        self.assertEqual([r['label'] for r in ranked], ['old2', 'old'])


# ── 5. The shortfall reaches every output surface ──────────────────────────

def _report_result_dicts():
    """Two scenario result dicts for the report tests: one solvent, one that
    exhausted in year 27 with a $205,784 gap. Fabricated round numbers."""
    short = {'engaged': True, 'exhausted': True, 'first_shortfall_year': 27,
             'first_shortfall_gap': 205_784.0, 'shortfall_years': 19,
             'total_unmet': 3_000_000.0}
    ok = {'engaged': True, 'exhausted': False, 'first_shortfall_year': None,
          'first_shortfall_gap': 0.0, 'shortfall_years': 0, 'total_unmet': 0.0}
    return [
        {'label': 'Solvent plan', 'net_benefit': 500_000, 'future_value': 500_000,
         'total_debt': 0, 'ltv': 0.0, 'objective_name': 'max_net_benefit',
         'drawdown_shortfall': ok, 'exhausted': False, 'deduct_later': False,
         'strategy': 'a', 'cash_out': 0, 'primary_income': None,
         'spouse_income': None, 'mortgage_rate': 0.05, 'use_readvanceable': False},
        {'label': 'Bankrupt plan', 'net_benefit': 1_200_000, 'future_value': 1_200_000,
         'total_debt': 0, 'ltv': 0.0, 'objective_name': 'max_net_benefit',
         'drawdown_shortfall': short, 'exhausted': True, 'deduct_later': False,
         'strategy': 'b', 'cash_out': 0, 'primary_income': None,
         'spouse_income': None, 'mortgage_rate': 0.05, 'use_readvanceable': False},
    ]


def _cfg_with_recorded_shortfall():
    cfg = golden_household_config()
    results = _report_result_dicts()
    cfg.setdefault('assumptions', {})['decumulation_shortfall'] = \
        decumulation.worst_drawdown_shortfall(results)
    return cfg, results


class TestModelFidelityCaveat(unittest.TestCase):
    def test_caveat_is_inactive_when_no_shortfall_recorded(self):
        ids = {a.id for a in
               model_fidelity.active_approximations({}, 'max_net_benefit')}
        self.assertNotIn('decumulation_shortfall', ids)

    def test_caveat_fires_and_names_the_year_and_gap(self):
        cfg, _ = _cfg_with_recorded_shortfall()
        active = {a.id: a for a in
                  model_fidelity.active_approximations(cfg, 'max_net_benefit')}
        self.assertIn('decumulation_shortfall', active)
        ctx = model_fidelity.FidelityContext(cfg=cfg, objective_name='max_net_benefit')
        findings = active['decumulation_shortfall'].findings_for(ctx)
        joined = " ".join(findings)
        self.assertIn("year 27", joined)
        self.assertIn("205,784", joined)


class TestReportsSurfaceShortfall(unittest.TestCase):
    def test_text_report_marks_exhausted_scenario_inline(self):
        cfg, results = _cfg_with_recorded_shortfall()
        txt = TextReport(results, cfg, title="T").render()
        self.assertIn("EXHAUSTED yr 27", txt)
        # The model_fidelity section names the year and the gap.
        self.assertIn("year 27", txt)
        self.assertIn("205,784", txt)

    def test_json_report_carries_caveat_and_per_scenario_flag(self):
        cfg, results = _cfg_with_recorded_shortfall()
        out = json.loads(JsonReport(results, cfg, title="T").render())
        ids = [a['id'] for a in out['model_fidelity']['approximations']]
        self.assertIn('decumulation_shortfall', ids)
        entry = next(a for a in out['model_fidelity']['approximations']
                     if a['id'] == 'decumulation_shortfall')
        joined = " ".join(entry.get('findings', []))
        self.assertIn("year 27", joined)
        self.assertIn("205,784", joined)
        # Per-scenario data is threaded through.
        flags = {s.get('label'): s.get('exhausted') for s in out['scenarios']}
        self.assertTrue(flags['Bankrupt plan'])
        self.assertFalse(flags['Solvent plan'])

    def test_html_report_marks_exhausted_scenario(self):
        cfg, results = _cfg_with_recorded_shortfall()
        html = HtmlReport(results, cfg, title="T").render()
        self.assertIn("EXHAUSTED yr 27", html)
        # The model-fidelity card surfaces the caveat text.
        self.assertIn("ran out of money", html)


if __name__ == '__main__':
    unittest.main()