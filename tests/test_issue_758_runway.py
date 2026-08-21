#!/usr/bin/env python3
"""Runway — months to insolvency after a dated income shock (issue #758).

## What this tests (DP#4/DP#15: fabricated round numbers, role-based names)

1. ``runway.compute_runway`` in isolation (DP#11: unit tests verify each
   module) — the pure fold over a ``YearResult`` trajectory, composing
   ``liquidation_waterfall.summarize_solvency`` (#679) rather than
   re-inventing the cash-flow identity (DP#9).
2. The months question (issue #758, hard part #1): the headline is a
   LABELLED interpolation inside an honest bracket — never an annual figure
   presented as monthly (the defect class this repo exists to kill).
3. The two endpoints are distinct: months-to-RUIN (headline) vs
   months-to-first-shortfall (stress begins). Conflating them is the bug.
4. DP#32 (hard part #2): an un-engaged run (no ``annual_living_costs``) is
   reported as un-engaged and names the missing input — never "0 months".
5. The shock-date sweep (hard part #5): ``shift_income_scenario_dates`` /
   ``runway_curve`` re-use one authored shock shape across shifted dates
   (DP#9: one shock shape, swept by data).
"""

import unittest
from datetime import date

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

from runway import (
    RunwayResult, compute_runway, runway_curve, shift_income_scenario_dates,
)
from simulation_state import (
    SimState, simulate_year_pure, adult_lira_slot, adult_fhsa_slot,
)  # #700/#643/#704: per-adult LIRA/FHSA stores
# Import the #679 fixture helpers WITHOUT the `tests.` prefix (the repo's
# convention for sibling test imports under pytest's rootdir insertion). A
# `from tests.test_issue_679_solvency` form would resolve `tests` to a GLOBAL
# `tests` package some self-hosted CI runners carry (~/.local/.../tests ->
# ultralytics -> yaml), breaking collection -- the no-prefix form is the one
# every other sibling-importing test in this repo uses.
from test_issue_679_solvency import (
    _reserve_config, _run_income_collapse_trajectory,
)


# ── A trajectory whose income collapses in year 2 (0-based), reusing the
#    #679 golden fixture's fabricated round numbers (DP#9: one fixture).
def _collapse_trajectory(collapse_year: int = 2, n_years: int = 5):
    return _run_income_collapse_trajectory(
        collapse_year=collapse_year, n_years=n_years)


class TestComputeRunwayEngagedAndLoudAbsence(unittest.TestCase):
    """DP#32 (hard part #2): absence of ``annual_living_costs`` must fail
    loudly, naming the missing input — never an optimistic number from a
    partial outflow list."""

    def test_unengaged_trajectory_reports_absence_not_zero(self):
        results = _collapse_trajectory(collapse_year=99, n_years=3)
        # The #679 fixture sets living_costs=0 only in the un-engaged test
        # helper; here we strip it to simulate a contract with NO budget.
        for r in results:
            r.living_costs = 0.0
        runway = compute_runway(results, shock_date=date(2026, 1, 1),
                                start_year=2026)
        self.assertFalse(runway.engaged)
        self.assertIsNone(runway.runway_months)
        self.assertIsNotNone(runway.unengaged_reason)
        self.assertIn("annual_living_costs", runway.unengaged_reason)

    def test_empty_trajectory_is_unengaged(self):
        runway = compute_runway([], shock_date=date(2026, 1, 1))
        self.assertFalse(runway.engaged)


class TestRunwayMonthsIsALabelledInterpolationInAnHonestBracket(unittest.TestCase):
    """Hard part #1: the engine steps in years; runway is a months question.
    The headline must be a labelled interpolation inside an honest bracket —
    never an annual figure presented as monthly."""

    def test_ruined_trajectory_yields_a_point_inside_its_bracket(self):
        # Income collapses in year 2 (0-based): solvent years 0-1, ruined 2+.
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        runway = compute_runway(results, shock_date=date(2026, 1, 1),
                                start_year=2026)
        self.assertTrue(runway.engaged)
        self.assertIsNotNone(runway.runway_months)
        self.assertTrue(runway.interpolated,
                        "a months figure from a year-granular engine MUST be "
                        "labelled as an interpolation, never presented as exact")
        self.assertIn("interpolation", runway.method.lower())

        lower, upper = runway.runway_months_bracket
        self.assertIsNotNone(lower)
        self.assertIsNotNone(upper)
        self.assertGreaterEqual(runway.runway_months, lower - 1e-6)
        self.assertLessEqual(runway.runway_months, upper + 1e-6)

    def test_bracket_spans_the_ruin_year_not_annual(self):
        # Shock Jan 1 2026, start_year 2026. Ruin year (1-based) 3 -> calendar
        # 2028. The household is solvent through end of 2027 (year index 2)
        # and ruined during 2028. The bracket must therefore run from the
        # start of 2028 (24 months in) to the end of 2028 (36 months in) —
        # NOT 0..12 (annual-as-monthly) and NOT a bare "2 years".
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        runway = compute_runway(results, shock_date=date(2026, 1, 1),
                                start_year=2026)
        lower, upper = runway.runway_months_bracket
        # ~24 and ~36 months (day-count / 30.4375, so not exactly integral).
        self.assertAlmostEqual(lower, 24.0, places=0,
                               msg="bracket lower = start of ruin year ≈ 24 months")
        self.assertAlmostEqual(upper, 36.0, places=0,
                               msg="bracket upper = end of ruin year ≈ 36 months")
        # And critically: the bracket is a YEAR wide, not collapsed to a point
        # or stretched to the whole horizon.
        self.assertLess(upper - lower, 13.0)
        self.assertGreater(upper - lower, 11.0)

    def test_interpolation_fraction_uses_the_waterfalls_own_measurement(self):
        # The interpolation fraction is covered/shortfall in the ruin year —
        # the waterfall's own bookkeeping of how far the liquid stock
        # stretched, not a guessed half-year. Construct a ruin year whose
        # cushion covers exactly half the annual gap and assert the point
        # lands at the bracket midpoint.
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        # Force the ruin year's covered/shortfall to 0.5 by direct stamping
        # (DP#3: the fold reads the YearResult fields the 'solvency' rule
        # would have written; this test isolates the interpolation math).
        ruin_row = results[2]
        ruin_row.solvency_shortfall = 10_000.0
        ruin_row.solvency_covered = 5_000.0   # exactly half
        ruin_row.ruined = True
        runway = compute_runway(results, shock_date=date(2026, 1, 1),
                                start_year=2026)
        lower, upper = runway.runway_months_bracket
        midpoint = (lower + upper) / 2.0
        self.assertAlmostEqual(runway.runway_months, midpoint, places=1)


class TestRuinAndStressBeginsAreDistinctEndpoints(unittest.TestCase):
    """Hard part: the issue's parenthetical ("first solvency_shortfall year")
    names STRESS BEGINS; the precise definition ("first cannot fund from
    income plus liquid resources") names RUIN. They are not the same number
    and must not be conflated."""

    def test_stress_begins_precedes_or_equals_ruin(self):
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        runway = compute_runway(results, shock_date=date(2026, 1, 1),
                                start_year=2026)
        # The first shortfall year is the collapse year (year 2); ruin is the
        # same year here (the cushion is small). Stress begins at the START
        # of the shortfall year; ruin is interpolated within it. Stress must
        # not be later than ruin.
        self.assertIsNotNone(runway.stress_begins_months)
        self.assertIsNotNone(runway.runway_months)
        self.assertLessEqual(runway.stress_begins_months + 1e-6,
                             runway.runway_months)

    def test_a_trajectory_that_shortfalls_but_never_ruins_survives(self):
        # A cushion large enough to cover every shortfall year: the waterfall
        # engages (stress begins) but never exhausts (no ruin) -> runway is
        # None (survives), with a survives-horizon floor.
        cfg = _reserve_config(emergency_reserve_rate=0.0)
        state = SimState(
            emergency_reserve_balance=2_000_000,   # huge cushion
            mortgage_balance=300_000,
            non_reg_balance=0.0, non_reg_acb=0.0,
            jurisdiction_state={'canada': {}},
        )
        results = []
        from test_issue_679_solvency import (
            RUIN_AFTER_TAX_EI, RUIN_AFTER_TAX_WORKING, RUIN_LIVING_COSTS,
            RUIN_MORTGAGE_PAYMENT, RUIN_RRSP_CONTRIBUTION, _mort_data,
        )
        for year in range(4):
            working = year < 2
            after_tax = RUIN_AFTER_TAX_WORKING if working else RUIN_AFTER_TAX_EI
            allocations = {'_primary_income': 95_000,
                           '_annual_savings': RUIN_RRSP_CONTRIBUTION,
                           'primary_rrsp': RUIN_RRSP_CONTRIBUTION}
            mort = _mort_data(state.mortgage_balance,
                             payment=RUIN_MORTGAGE_PAYMENT)
            result, state = simulate_year_pure(
                state=state, year=year, allocations=allocations, config=cfg,
                investment_return=0.0, primary_marginal_rate=0.30,
                mortgage_data=mort, living_costs=RUIN_LIVING_COSTS,
                after_tax_income=after_tax,
            )
            results.append(result)
        runway = compute_runway(results, shock_date=date(2026, 1, 1),
                                start_year=2026)
        # The cushion covers the gap -> no ruin.
        self.assertIsNone(runway.runway_months,
                          "a household whose cushion covers every shortfall "
                          "year survives -> runway is None, not a number")
        self.assertIsNotNone(runway.survives_horizon_months)


class TestRunwayMeasuresFromShockDateNotProjectionStart(unittest.TestCase):
    """Hard part #5: the same household has different runway for a shock
    today vs in 12 months. Runway is measured FROM the shock date, so a
    mid-horizon shock subtracts the pre-shock months."""

    def test_later_shock_date_compresses_the_runway_axis(self):
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        # Same trajectory, shock dated one year later. The bracket must shift
        # by ~12 months (the pre-shock year no longer counts toward runway).
        early = compute_runway(results, shock_date=date(2026, 1, 1),
                               start_year=2026)
        late = compute_runway(results, shock_date=date(2027, 1, 1),
                              start_year=2026)
        e_lo, _ = early.runway_months_bracket
        l_lo, _ = late.runway_months_bracket
        self.assertGreater(e_lo, l_lo,
                           "a later shock date must report fewer months of "
                           "runway (the pre-shock year is not runway)")
        self.assertAlmostEqual(e_lo - l_lo, 12.0, places=1)


class TestShiftIncomeScenarioDates(unittest.TestCase):
    """The shock-date sweep re-uses ONE authored shock shape across shifted
    dates (DP#9: one shock shape, swept by data), preserving every segment's
    relative length and the gaps between them."""

    def test_shifts_from_and_to_by_the_same_delta(self):
        original = date(2026, 3, 1)
        new = date(2027, 3, 1)   # +365 days
        segments = [
            {'kind': 'ei', 'amount': 28_000, 'from': '2026-03-01',
             'to': '2027-01-08'},   # ~45 weeks of EI
            {'kind': 'employment', 'amount': 90_000, 'from': '2027-01-08',
             'to': None},            # re-employment, permanent
        ]
        shifted = shift_income_scenario_dates(segments, original, new)
        self.assertEqual(shifted[0]['from'], '2027-03-01')
        self.assertEqual(shifted[0]['to'], '2028-01-08')
        self.assertEqual(shifted[1]['from'], '2028-01-08')
        self.assertIsNone(shifted[1]['to'], "permanent (to=None) is preserved")

    def test_does_not_mutate_input(self):
        segments = [{'kind': 'ei', 'amount': 28_000, 'from': '2026-03-01',
                     'to': None}]
        before = dict(segments[0])
        shift_income_scenario_dates(segments, date(2026, 3, 1), date(2027, 3, 1))
        self.assertEqual(segments[0], before)


class TestRunwayCurve(unittest.TestCase):
    """The runway-vs-shock-date curve is a directly testable fold, not
    something only observable by parsing a printed chart (mirrors
    summarize_solvency's split of pure fold from printing)."""

    def test_curve_has_one_point_per_shock_date(self):
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        pairs = [
            (date(2026, 1, 1), results),
            (date(2027, 1, 1), results),
        ]
        curve = runway_curve(pairs, start_year=2026)
        self.assertEqual(len(curve), 2)
        self.assertEqual(curve[0].shock_date, date(2026, 1, 1))
        self.assertEqual(curve[1].shock_date, date(2027, 1, 1))
        for p in curve:
            self.assertTrue(p.engaged)


class TestRunwaySweepDriver(unittest.TestCase):
    """The shock-date SWEEP driver (issue #758 hard part #5): one authored
    shock shape, re-run at shifted dates, producing a curve. End-to-end
    through the optimizer (DP#11: integration test of composition)."""

    def _cfg_with_shock(self):
        return {
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'capital_gains_inclusion': 0.5, 'inflation': 0.02},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 200000, 'birth_year': 1985,
                 'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000,
                 'rrsp_balance': 80000, 'tfsa_balance': 40000}],
            },
            'property': {'house_value': 900000, 'mortgage_balance': 300000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'household_budget': {'living_costs': 78000},
            'portfolio': {'accounts': {'non_reg': {'balance': 60000,
                                                    'cost_basis': 60000}}},
            'scenarios': {'income': [
                {'id': 'jobloss', 'label': 'Job loss (EI, permanent)',
                 'members': [{'role': 'primary', 'kind': 'ei',
                              'gross_income': 28000,
                              'from': '2027-01-01', 'to': None}]},
            ]},
        }

    def test_sweep_returns_one_point_per_shock_date(self):
        from optimize import run_runway_sweep
        cfg = self._cfg_with_shock()
        # Simulate what input_contract.load_and_map does: attach the shock's
        # dated segments to the primary member so shock_date_from_members can
        # read the shock date, and expose the scenario via scenarios.income
        # (the internal path scenario_discovery reads).
        cfg['family']['members'][0]['income_segments'] = [
            {'kind': 'ei', 'amount': 28000, 'from': '2027-01-01', 'to': None}]
        curve = run_runway_sweep(
            cfg, [date(2026, 7, 1), date(2027, 1, 1), date(2028, 1, 1)],
            input_path='inline')
        self.assertEqual(len(curve), 3)
        self.assertEqual([p.shock_date for p in curve],
                         [date(2026, 7, 1), date(2027, 1, 1), date(2028, 1, 1)])
        for p in curve:
            self.assertTrue(p.engaged,
                            "a shock the household cannot survive must engage "
                            "the cash-flow identity, not be silently UNCHECKED")
            self.assertIsNotNone(p.runway_months)

    def test_sweep_returns_empty_when_no_shock_authored(self):
        from optimize import run_runway_sweep
        cfg = {
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'capital_gains_inclusion': 0.5, 'inflation': 0.02},
            'family': {'members': [{'role': 'primary', 'gross_income': 200000,
                                     'birth_year': 1985}]},
            'property': {'house_value': 900000, 'mortgage_balance': 300000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'household_budget': {'living_costs': 78000},
        }
        curve = run_runway_sweep(cfg, [date(2027, 1, 1)], input_path='inline')
        self.assertEqual(curve, [],
                         "no authored shock -> nothing to sweep, not an "
                         "invented shock")

    def test_sweep_survives_point_has_none_bracket(self):
        # A FINITE shock (45 wks EI then recovery to full salary) that the
        # household weathers -> the sweep point survives (runway_months=None),
        # covering run_runway_sweep's survives path.
        from optimize import run_runway_sweep
        cfg = {
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'capital_gains_inclusion': 0.5, 'inflation': 0.02},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 200000, 'birth_year': 1985,
                 'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 20000,
                 'rrsp_balance': 80000, 'tfsa_balance': 40000,
                 'income_segments': [
                     {'kind': 'ei', 'amount': 28000, 'from': '2027-01-01',
                      'to': '2027-11-08'},
                     {'kind': 'employment', 'amount': 200000,
                      'from': '2027-11-08', 'to': None}]}]},
            'property': {'house_value': 900000, 'mortgage_balance': 300000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'household_budget': {'living_costs': 78000},
            'portfolio': {'accounts': {'non_reg': {'balance': 60000,
                                                    'cost_basis': 60000}}},
            'scenarios': {'income': [
                {'id': 'jobloss', 'label': 'Job loss (45wks EI then recovery)',
                 'members': [{'role': 'primary', 'kind': 'ei',
                              'gross_income': 28000, 'from': '2027-01-01',
                              'to': '2027-11-08'},
                             {'role': 'primary', 'kind': 'employment',
                              'gross_income': 200000, 'from': '2027-11-08',
                              'to': None}]}]},
        }
        curve = run_runway_sweep(cfg, [date(2027, 1, 1)], input_path='inline')
        self.assertEqual(len(curve), 1)
        # The household survives the finite shock -> runway_months is None.
        self.assertIsNone(curve[0].runway_months)

    def test_sweep_unengaged_point_covers_none_bracket_branch(self):
        # A shock cfg with NO household_budget.living_costs -> the solvency
        # rule never engages, so the winner's runway is un-engaged and its
        # serialized bracket is None. Covers run_runway_sweep's
        # `if bracket is None: bracket = (None, None)` defensive branch.
        from optimize import run_runway_sweep
        cfg = {
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'capital_gains_inclusion': 0.5, 'inflation': 0.02},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 200000, 'birth_year': 1985,
                 'income_segments': [{'kind': 'ei', 'amount': 28000,
                                      'from': '2027-01-01', 'to': None}]}]},
            'property': {'house_value': 900000, 'mortgage_balance': 300000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'scenarios': {'income': [
                {'id': 'jobloss', 'label': 'Job loss',
                 'members': [{'role': 'primary', 'kind': 'ei',
                              'gross_income': 28000, 'from': '2027-01-01',
                              'to': None}]}]},
        }
        curve = run_runway_sweep(cfg, [date(2027, 1, 1)], input_path='inline')
        self.assertEqual(len(curve), 1)
        self.assertFalse(curve[0].engaged)
        self.assertEqual(curve[0].runway_months_bracket, (None, None))


class TestRetirementDoubleCountAndWorkingLifeScoping(unittest.TestCase):
    """The #679 solvency rule used to charge the WORKING-phase
    ``annual_living_costs`` in RETIREMENT years on top of the drawdown (which
    already funds retirement spending), AND did not count CPP/OAS/pension in
    `available` (while the drawdown is sized net of them). That double-count
    force-sold assets every retirement year for spending that does not happen
    -- corrupting the solvency table, #707's decumulation output, and #758's
    runway. These tests pin the fix at the source AND the runway scoping.

    End-to-end through the optimizer (DP#11: integration), fabricated round
    numbers, role-based names (DP#4/DP#15).
    """

    def _stay_cfg(self):
        # High working-life cash flow, NO income shock, retirement ~year 9
        # (primary born 1970 -> age 65 in 2035 = projection year 9).
        return {
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'capital_gains_inclusion': 0.5, 'inflation': 0.02},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 250_000, 'birth_year': 1970,
                 'rrsp_room_accumulated': 100_000, 'tfsa_room_accumulated': 80_000,
                 'rrsp_balance': 600_000, 'tfsa_balance': 120_000}]},
            'property': {'house_value': 1_200_000, 'mortgage_balance': 500_000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'household_budget': {'living_costs': 80_000},
            'retirement': {'retirement_age': 65, 'spending_target': 90_000},
            'portfolio': {'accounts': {'non_reg': {'balance': 1_100_000,
                                                    'cost_basis': 900_000}}},
        }

    def test_stay_scenario_has_no_working_life_forced_sale(self):
        """The proof criterion (issue #758 review): a $250k earner with a
        sound retirement plan must NOT force-sell during working life, and
        must not force-sell in retirement merely because of the double-
        charge. Before the source fix this force-sold ~$54k/yr in retirement
        for spending that does not happen."""
        from optimize import run_optimization
        res = run_optimization(self._stay_cfg(), 'inline')
        yby = res[0]['year_by_year']
        working_shortfalls = [i for i, y in enumerate(yby)
                              if y.get('solvency_shortfall', 0) > 0
                              and not y.get('any_retired', False)]
        self.assertEqual(working_shortfalls, [],
                         "a $250k earner must have NO working-life shortfall")
        # The only solvency shortfall is in retirement, and it equals the
        # REAL mortgage gap (debt_service), not a double-count of spending.
        ret_short = [y for y in yby
                     if y.get('solvency_shortfall', 0) > 0
                     and y.get('any_retired', False)]
        if ret_short:
            y = ret_short[0]
            self.assertAlmostEqual(y['solvency_shortfall'],
                                   y['debt_service'], delta=1.0,
                                   msg="retirement shortfall must be the real "
                                       "mortgage gap (debt_service), not a "
                                       "double-count of living_costs on top of "
                                       "the drawdown")

    def test_stay_scenario_runway_is_na_no_shock(self):
        """Runway measures time FROM a shock. The STAY scenario has no
        income shock -> runway is n/a, not 0 and not a false 'survives'."""
        from optimize import run_optimization
        res = run_optimization(self._stay_cfg(), 'inline')
        rw = res[0]['runway']
        self.assertTrue(rw['engaged'],
                        "living_costs is declared -> solvency engaged")
        self.assertIsNone(rw['runway_months'],
                          "no shock -> no runway number (n/a), never 0")
        self.assertIn("no income shock", rw['method'])

    def test_runway_ignores_retirement_shortfall_as_stress(self):
        """A retirement-year solvency shortfall (the real mortgage gap) is
        NOT a shock-induced runway event. stress_begins must point to a
        WORKING-LIFE shortfall (or be absent), never to the retirement year."""
        import copy
        from optimize import run_optimization
        # Permanent job loss NOW: a working-life shortfall starts in year 2
        # (the shock year). The retirement mortgage gap (year 9+) must NOT
        # be reported as stress_begins.
        cfg = copy.deepcopy(self._stay_cfg())
        cfg['family']['members'][0]['income_segments'] = [
            {'kind': 'ei', 'amount': 0, 'from': '2027-01-01', 'to': None}]
        res = run_optimization(cfg, 'inline')
        rw = res[0]['runway']
        # stress_begins ~0 mo (year 2, the shock) -- NOT ~96 mo (retirement).
        self.assertIsNotNone(rw['stress_begins_months'])
        self.assertLess(rw['stress_begins_months'], 12.0,
                        "stress must begin at the shock (year 2 ~0 mo), not at "
                        "retirement (~96 mo) -- the retirement mortgage gap is "
                        "#707's domain, not a runway event")
        # first_shortfall_year is the working-life shock year, not retirement.
        self.assertEqual(rw['first_shortfall_year'], 2)


class TestRunwayHelpersAndRenderers(unittest.TestCase):
    """Directly execute the pure helpers and the console/TXT/JSON/HTML
    renderers so the new code paths are covered (the coverage gate requires
    new production code to be exercised, not just asserted on output
    strings). DP#11: unit tests for each helper."""

    # ── format_runway: every branch ────────────────────────────────────
    def test_format_runway_unengaged(self):
        from runway import format_runway
        self.assertEqual(format_runway({'engaged': False}), "UNCHECKED")

    def test_format_runway_no_shock(self):
        from runway import format_runway
        rw = {'engaged': True, 'runway_months': None,
              'method': "n/a — no income shock declared (runway measures time "
                        "from a shock; this scenario has none)"}
        self.assertEqual(format_runway(rw), "n/a (no shock)")

    def test_format_runway_survives_with_floor(self):
        from runway import format_runway
        rw = {'engaged': True, 'runway_months': None,
              'survives_horizon_months': 96.0, 'method': 'survives working life'}
        self.assertEqual(format_runway(rw), ">=96 mo (survives working life)")

    def test_format_runway_survives_no_floor(self):
        from runway import format_runway
        rw = {'engaged': True, 'runway_months': None,
              'survives_horizon_months': None, 'method': 'survives'}
        self.assertEqual(format_runway(rw), "never (survives)")

    def test_format_runway_ruined_with_bracket(self):
        from runway import format_runway
        rw = {'engaged': True, 'runway_months': 24.3,
              'runway_months_bracket': [24.0, 36.0],
              'method': 'linear interpolation within the ruin year'}
        self.assertEqual(format_runway(rw), "~24 mo [24-36] (interp)")

    def test_format_runway_ruined_without_bracket(self):
        from runway import format_runway
        rw = {'engaged': True, 'runway_months': 5.0,
              'runway_months_bracket': None, 'method': 'linear interpolation'}
        self.assertEqual(format_runway(rw), "~5 mo (interp)")

    def test_format_runway_method_absent(self):
        # Covers the `if method is None: method = ''` defensive branch.
        from runway import format_runway
        rw = {'engaged': True, 'runway_months': 5.0,
              'runway_months_bracket': [4.0, 6.0]}  # no 'method' key
        self.assertEqual(format_runway(rw), "~5 mo [4-6] (interp)")

    def test_compute_runway_shock_date_set_start_year_none(self):
        # Covers `start_year = shock_date.year` (start_year inferred from the
        # shock date when not explicitly supplied).
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        runway = compute_runway(results, shock_date=date(2026, 1, 1))
        self.assertTrue(runway.engaged)
        self.assertIsNotNone(runway.runway_months)

    def test_compute_runway_ruin_with_zero_shortfall_is_lower_bound(self):
        # Covers the defensive `frac = 0.0` branch: a ruined row whose
        # shortfall is 0 (should not happen, but must not divide by zero).
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        results[2].solvency_shortfall = 0.0
        results[2].solvency_covered = 0.0
        results[2].ruined = True
        runway = compute_runway(results, shock_date=date(2026, 1, 1),
                                start_year=2026)
        self.assertTrue(runway.interpolated)
        lower, _ = runway.runway_months_bracket
        self.assertAlmostEqual(runway.runway_months, lower, places=1)

    def test_runway_curve_point_to_dict(self):
        from runway import RunwayCurvePoint
        d = RunwayCurvePoint(
            shock_date=date(2027, 1, 1), runway_months=24.3,
            runway_months_bracket=(24.0, 36.0), stress_begins_months=0.0,
            survives_horizon_months=107.0, engaged=True).to_dict()
        self.assertEqual(d['shock_date'], '2027-01-01')
        self.assertEqual(d['runway_months_bracket'], [24.0, 36.0])
        self.assertTrue(d['engaged'])

    def test_runway_curve_point_to_dict_unengaged(self):
        from runway import RunwayCurvePoint
        d = RunwayCurvePoint(
            shock_date=date(2027, 1, 1), runway_months=None,
            runway_months_bracket=(None, None), stress_begins_months=None,
            survives_horizon_months=None, engaged=False,
            unengaged_reason='absent').to_dict()
        self.assertIsNone(d['runway_months_bracket'])
        self.assertFalse(d['engaged'])

    def test_worst_runway_skips_survives_and_picks_later_smaller(self):
        # Covers both `continue` branches (survives skipped; an unengaged row
        # skipped) AND the `months < worst_months` comparison (a later row
        # with a smaller finite runway wins over an earlier larger one).
        from runway import worst_runway_summary
        results = [
            {'income_scenario_label': 'unengaged',
             'runway': {'engaged': False, 'runway_months': None}},  # skip
            self._rw_row('survives', True, None),     # months is None -> skip
            self._rw_row('long', True, 36.0),          # first finite -> worst
            self._rw_row('shorter', True, 12.0),       # 12 < 36 -> replaces
        ]
        s = worst_runway_summary(results)
        self.assertEqual(s['runway_months'], 12.0)
        self.assertEqual(s['scenario_label'], 'shorter')

    # ── RunwayResult.to_dict ───────────────────────────────────────────
    def test_to_dict_serializes_bracket_as_list(self):
        from runway import RunwayResult
        d = RunwayResult(engaged=True, runway_months=24.3,
                         runway_months_bracket=(24.0, 36.0),
                         method='interp', interpolated=True).to_dict()
        self.assertTrue(d['engaged'])
        self.assertEqual(d['runway_months_bracket'], [24.0, 36.0])
        self.assertTrue(d['interpolated'])

    def test_to_dict_unengaged_bracket_is_none(self):
        from runway import RunwayResult
        d = RunwayResult(engaged=False,
                         unengaged_reason="absent").to_dict()
        self.assertIsNone(d['runway_months_bracket'])
        self.assertEqual(d['unengaged_reason'], "absent")

    # ── worst_runway_summary: the model_fidelity bridge ────────────────
    def _rw_row(self, label, engaged, runway_months):
        return {'income_scenario_label': label,
                'runway': {'engaged': engaged, 'runway_months': runway_months,
                           'relies_on_credit_facility': False,
                           'drew_registered': False}}

    def test_worst_runway_picks_the_shortest_finite_runway(self):
        from runway import worst_runway_summary
        results = [self._rw_row('survives', True, None),
                   self._rw_row('short', True, 12.0),
                   self._rw_row('long', True, 36.0)]
        s = worst_runway_summary(results)
        self.assertTrue(s['engaged'])
        self.assertEqual(s['runway_months'], 12.0)
        self.assertEqual(s['scenario_label'], 'short')

    def test_worst_runway_all_survive_is_unengaged(self):
        from runway import worst_runway_summary
        results = [self._rw_row('a', True, None),
                   self._rw_row('b', True, None)]
        s = worst_runway_summary(results)
        self.assertFalse(s['engaged'])
        self.assertIsNone(s['runway_months'])

    def test_worst_runway_empty(self):
        from runway import worst_runway_summary
        s = worst_runway_summary([])
        self.assertFalse(s['engaged'])

    # ── shock_date_from_members ────────────────────────────────────────
    def test_shock_date_from_members_none_when_no_segments(self):
        from runway import shock_date_from_members
        self.assertIsNone(shock_date_from_members(
            [{'role': 'primary'}, {'role': 'spouse'}]))

    def test_shock_date_from_members_earliest_from(self):
        from runway import shock_date_from_members
        members = [{'role': 'primary', 'income_segments': [
            {'kind': 'ei', 'amount': 28000, 'from': '2027-06-01', 'to': None},
            {'kind': 'employment', 'amount': 200000, 'from': '2028-01-08',
             'to': None}]}]
        self.assertEqual(shock_date_from_members(members), date(2027, 6, 1))

    def test_shock_date_from_members_skips_missing_from(self):
        from runway import shock_date_from_members
        # A segment with 'from' absent is skipped (not crashed on) -- the
        # earliest present 'from' wins.
        members = [{'role': 'primary', 'income_segments': [
            {'kind': 'ei', 'amount': 28000, 'to': None},  # no 'from'
            {'kind': 'employment', 'amount': 200000, 'from': '2028-01-08',
             'to': None}]}]
        self.assertEqual(shock_date_from_members(members), date(2028, 1, 8))

    # ── compute_runway: the no-shock-no-start_year anchor branch ───────
    def test_compute_runway_with_no_shock_date_and_no_start_year(self):
        # Covers the day-one-shock fallback that anchors the frame on the
        # trajectory's own first index (start_year=1) when neither shock_date
        # nor start_year is supplied. With shock_date=None the no-shock n/a
        # path returns runway_months=None, but the frame-resolution fallback
        # (start_year=1) executes first -- covering those lines.
        results = _collapse_trajectory(collapse_year=2, n_years=5)
        runway = compute_runway(results, shock_date=None, start_year=None)
        self.assertTrue(runway.engaged)
        # No shock date -> n/a (no runway number), never 0.
        self.assertIsNone(runway.runway_months)
        self.assertIn("no income shock", runway.method)

    # ── output_plugins: TXT/JSON/HTML runway rendering ─────────────────
    def _runway_results(self):
        return [
            {'label': 'Stay', 'net_benefit': 1_000_000, 'ltv': 0.0,
             'future_value': 2_000_000, 'total_debt': 500_000,
             'income_scenario_id': 'stay', 'income_scenario_label': 'Stay',
             'runway': {'engaged': True, 'runway_months': None,
                        'runway_months_bracket': [None, None],
                        'stress_begins_months': None,
                        'survives_horizon_months': None,
                        'method': "n/a — no income shock declared",
                        'interpolated': False, 'first_ruin_year': None,
                        'first_shortfall_year': None,
                        'relies_on_credit_facility': False,
                        'drew_registered': False, 'unengaged_reason': None}},
            {'label': 'Job loss', 'net_benefit': 800_000, 'ltv': 0.0,
             'future_value': 1_800_000, 'total_debt': 500_000,
             'income_scenario_id': 'jobloss', 'income_scenario_label': 'Job loss',
             'runway': {'engaged': True, 'runway_months': 24.3,
                        'runway_months_bracket': [24.0, 36.0],
                        'stress_begins_months': 0.0,
                        'survives_horizon_months': 107.0,
                        'method': "linear interpolation within the ruin year",
                        'interpolated': True, 'first_ruin_year': 4,
                        'first_shortfall_year': 2,
                        'relies_on_credit_facility': False,
                        'drew_registered': True, 'unengaged_reason': None}},
        ]

    def test_text_report_renders_runway_section(self):
        from output_plugins import TextReport
        cfg = {'assumptions': {}}
        txt = TextReport(self._runway_results(), cfg, 't').render()
        self.assertIn('Runway — months to insolvency', txt)
        self.assertIn('n/a (no shock)', txt)
        self.assertIn('~24 mo [24-36] (interp)', txt)

    def test_json_report_emits_runway_and_sweep(self):
        import json
        from output_plugins import JsonReport
        cfg = {'assumptions': {'runway_sweep': [
            {'shock_date': '2027-01-01', 'runway_months': 76.0,
             'runway_months_bracket': [72.0, 84.0], 'stress_begins_months': 0.0,
             'survives_horizon_months': None, 'engaged': True,
             'unengaged_reason': None}]}}
        out = json.loads(JsonReport(self._runway_results(), cfg, 't').render())
        self.assertIn('runway', out)
        self.assertEqual(len(out['runway']), 2)
        self.assertEqual(out['runway_sweep'][0]['shock_date'], '2027-01-01')
        self.assertIn('runway', out['scenarios'][0])

    def test_html_report_renders_runway_card(self):
        from output_plugins import HtmlReport
        cfg = {'assumptions': {},
               'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
               'property': {'house_value': 1, 'mortgage_balance': 0}}
        html = HtmlReport(self._runway_results(), cfg, 't').render()
        self.assertIn('Runway — months to insolvency', html)
        self.assertIn('drew RRSP', html)  # the caveat column for the job-loss row

    def test_html_report_runway_not_checked(self):
        # Results carry the `runway` key but with None values -> the NOT
        # CHECKED card fires.
        from output_plugins import HtmlReport
        cfg = {'assumptions': {},
               'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
               'property': {'house_value': 1, 'mortgage_balance': 0}}
        results = [{'label': 'x', 'net_benefit': 1, 'ltv': 0.0,
                    'future_value': 1, 'total_debt': 0, 'runway': None}]
        html = HtmlReport(results, cfg, 't').render()
        self.assertIn('NOT CHECKED', html)

    def test_html_report_no_runway_card_when_key_absent(self):
        # No result carries a `runway` key at all -> no runway card rendered.
        from output_plugins import HtmlReport
        cfg = {'assumptions': {},
               'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
               'property': {'house_value': 1, 'mortgage_balance': 0}}
        results = [{'label': 'x', 'net_benefit': 1, 'ltv': 0.0,
                    'future_value': 1, 'total_debt': 0}]
        html = HtmlReport(results, cfg, 't').render()
        self.assertNotIn('Runway', html)

    def test_html_report_credit_facility_caveat(self):
        # A runway row that drew a revolving credit facility surfaces the
        # "leans on an unsecured credit line" caveat in the HTML card.
        from output_plugins import HtmlReport
        cfg = {'assumptions': {},
               'family': {'members': [{'role': 'primary', 'gross_income': 1}]},
               'property': {'house_value': 1, 'mortgage_balance': 0}}
        results = [{'label': 'shock', 'net_benefit': 1, 'ltv': 0.0,
                    'future_value': 1, 'total_debt': 0,
                    'income_scenario_id': 's', 'income_scenario_label': 'shock',
                    'runway': {'engaged': True, 'runway_months': 10.0,
                               'runway_months_bracket': [9.0, 12.0],
                               'stress_begins_months': 0.0,
                               'survives_horizon_months': None,
                               'method': 'linear interpolation',
                               'interpolated': True, 'first_ruin_year': 2,
                               'first_shortfall_year': 1,
                               'relies_on_credit_facility': True,
                               'drew_registered': False, 'unengaged_reason': None}}]
        html = HtmlReport(results, cfg, 't').render()
        self.assertIn('leans on an unsecured credit line', html)

    def test_text_report_runway_not_checked_when_only_unengaged(self):
        from output_plugins import TextReport
        # Results carry the `runway` key but with a None value (an
        # older/synthetic row) -> _runway_by_scenario returns [] and the
        # NOT-CHECKED notice fires (the defensive branch).
        results = [{'label': 'x', 'net_benefit': 1, 'ltv': 0.0,
                    'future_value': 1, 'total_debt': 0, 'runway': None}]
        txt = TextReport(results, {'assumptions': {}}, 't').render()
        self.assertIn('NOT CHECKED', txt)


class TestOptimizeRunwayConsoleAndSweep(unittest.TestCase):
    """Execute optimize.py's runway console renderers and the sweep driver's
    print wrapper so the new presentation code is covered (the coverage gate
    requires new code to be exercised, not just present)."""

    def _cfg_with_shock(self):
        return {
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'capital_gains_inclusion': 0.5, 'inflation': 0.02},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 200_000, 'birth_year': 1985,
                 'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000,
                 'rrsp_balance': 80_000, 'tfsa_balance': 40_000,
                 'income_segments': [{'kind': 'ei', 'amount': 28_000,
                                      'from': '2027-01-01', 'to': None}]}]},
            'property': {'house_value': 900_000, 'mortgage_balance': 300_000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'household_budget': {'living_costs': 78_000},
            'portfolio': {'accounts': {'non_reg': {'balance': 60_000,
                                                    'cost_basis': 60_000}}},
            'scenarios': {'income': [
                {'id': 'jobloss', 'label': 'Job loss (EI, permanent)',
                 'members': [{'role': 'primary', 'kind': 'ei',
                              'gross_income': 28_000,
                              'from': '2027-01-01', 'to': None}]}]},
        }

    def test_absent_runway_is_unengaged(self):
        from optimize import _absent_runway
        rw = _absent_runway()
        self.assertFalse(rw['engaged'])
        self.assertIsNotNone(rw['unengaged_reason'])

    def test_print_runway_report_engaged(self):
        import io, contextlib
        from optimize import (run_optimization, winners_by_income_scenario,
                              _print_runway_report)
        res = run_optimization(self._cfg_with_shock(), 'inline')
        for r in res:
            r['income_scenario_id'] = 'jobloss'
            r['income_scenario_label'] = 'Job loss (EI, permanent)'
        winners = winners_by_income_scenario(res)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_runway_report(winners)
        out = buf.getvalue()
        self.assertIn('RUNWAY', out)
        # Either a runway number or a survives/n/a line appears for the scenario.
        self.assertIn('Job loss', out)

    def test_print_runway_report_not_checked(self):
        import io, contextlib
        from optimize import _print_runway_report
        # No winner carries an engaged runway -> the NOT-CHECKED notice fires.
        winners = [{'label': 'x', 'solvency': {'engaged': False},
                    'runway': {'engaged': False, 'unengaged_reason': 'absent',
                               'runway_months': None}}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_runway_report(winners)
        self.assertIn('NOT CHECKED', buf.getvalue())

    def test_run_and_print_runway_sweep_renders_curve(self):
        import io, contextlib
        from optimize import _run_and_print_runway_sweep
        cfg = self._cfg_with_shock()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            curve = _run_and_print_runway_sweep(cfg, 'inline', 0.80)
        out = buf.getvalue()
        self.assertEqual(len(curve), 3)
        self.assertIn('RUNWAY vs SHOCK DATE', out)
        self.assertIn('Shock date', out)

    def test_run_and_print_runway_sweep_no_shock_returns_empty(self):
        # A cfg with no dated income shock -> nothing to sweep; returns []
        # (covers the `if shock is None: return []` branch).
        import io, contextlib
        from optimize import _run_and_print_runway_sweep
        cfg = {
            'assumptions': {'start_year': 2026, 'investment_return': 0.05,
                            'capital_gains_inclusion': 0.5, 'inflation': 0.02},
            'family': {'members': [{'role': 'primary', 'gross_income': 200_000,
                                     'birth_year': 1985}]},
            'property': {'house_value': 900_000, 'mortgage_balance': 300_000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80},
            'household_budget': {'living_costs': 78_000},
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            curve = _run_and_print_runway_sweep(cfg, 'inline', 0.80)
        self.assertEqual(curve, [])

    def test_print_runway_report_credit_facility_caveat(self):
        # A winner whose runway drew a revolving credit facility surfaces the
        # "leans on credit line" caveat in the console report.
        import io, contextlib
        from optimize import _print_runway_report
        winners = [{'label': 'shock', 'solvency': {'engaged': True},
                    'runway': {'engaged': True, 'runway_months': 10.0,
                               'runway_months_bracket': [9.0, 12.0],
                               'stress_begins_months': 0.0,
                               'survives_horizon_months': None,
                               'method': 'linear interpolation',
                               'interpolated': True, 'first_ruin_year': 2,
                               'first_shortfall_year': 1,
                               'relies_on_credit_facility': True,
                               'drew_registered': False, 'unengaged_reason': None}}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _print_runway_report(winners)
        self.assertIn('leans on credit line', buf.getvalue())

    def test_record_runway_sweep_off_returns_empty(self):
        from optimize import _record_runway_sweep
        from argparse import Namespace
        cfg = self._cfg_with_shock()
        out = _record_runway_sweep(Namespace(runway_sweep=False), cfg, 'inline', 0.80)
        self.assertEqual(out, [])
        self.assertNotIn('runway_sweep', cfg.get('assumptions', {}))

    def test_record_runway_sweep_on_records_curve(self):
        import io, contextlib
        from optimize import _record_runway_sweep
        from argparse import Namespace
        cfg = self._cfg_with_shock()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = _record_runway_sweep(Namespace(runway_sweep=True), cfg, 'inline', 0.80)
        self.assertEqual(len(out), 3)
        # The curve is recorded onto cfg for the JSON report.
        self.assertEqual(len(cfg['assumptions']['runway_sweep']), 3)
        self.assertIn('RUNWAY vs SHOCK DATE', buf.getvalue())


class TestFormatCurvePoint(unittest.TestCase):
    """Unit-test every branch of runway.format_curve_point (the pure
    per-point sweep renderer) -- covers the unengaged / survives / ruined
    branches without driving a full optimizer sweep per branch."""

    def _p(self, **kw):
        from runway import RunwayCurvePoint
        base = dict(shock_date=date(2027, 1, 1), runway_months=None,
                    runway_months_bracket=(None, None),
                    stress_begins_months=None,
                    survives_horizon_months=None, engaged=True)
        base.update(kw)
        return RunwayCurvePoint(**base)

    def test_unengaged(self):
        from runway import format_curve_point
        self.assertEqual(format_curve_point(self._p(engaged=False)),
                         ("UNCHECKED", "-"))

    def test_survives_with_floor(self):
        from runway import format_curve_point
        self.assertEqual(
            format_curve_point(self._p(runway_months=None,
                                       survives_horizon_months=96.0)),
            (">=96 mo (survives)", "-"))

    def test_survives_no_floor(self):
        from runway import format_curve_point
        self.assertEqual(format_curve_point(self._p(runway_months=None)),
                         ("never (survives)", "-"))

    def test_ruined_with_bracket(self):
        from runway import format_curve_point
        self.assertEqual(
            format_curve_point(self._p(runway_months=24.3,
                                       runway_months_bracket=(24.0, 36.0))),
            ("~24 mo (interp)", "[24-36]"))

    def test_ruined_without_bracket(self):
        from runway import format_curve_point
        self.assertEqual(
            format_curve_point(self._p(runway_months=5.0,
                                       runway_months_bracket=(None, None))),
            ("~5 mo (interp)", "-"))


class TestSolvencyRetirementBranch(unittest.TestCase):
    """Directly exercise apply_solvency's RETIREMENT branch (issue #758 root
    fix): in retirement the identity charges the retirement spending target
    (not the working living_costs) and counts CPP/OAS/pension in available,
    so the shortfall is the real debt_service gap, not a double-count."""

    def test_retirement_shortfall_equals_debt_service_not_living_costs(self):
        from simulation_config import SimulationConfig
        from simulation_state import SimState, simulate_year_pure
        from test_issue_679_solvency import (
            _reserve_config, _mort_data, RUIN_MORTGAGE_PAYMENT,
        )
        cfg = _reserve_config(emergency_reserve_rate=0.0)
        state = SimState(
            emergency_reserve_balance=0,
            mortgage_balance=200_000,
            non_reg_balance=50_000, non_reg_acb=50_000,
            jurisdiction_state={'canada': {  # #700: per-adult stores
                'adult_tfsa': {'primary': {'balance': 30_000, 'room': 0.0}},
                'adult_rrsp': {'primary': {'own': 40_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}}},
        )
        mort = _mort_data(state.mortgage_balance, payment=RUIN_MORTGAGE_PAYMENT)
        # A retirement year: any_retired, drawdown delivers 60k net,
        # CPP+OAS+pension = 30k, spending_target = 90k, working living_costs
        # = 80k (HIGHER than the 60k drawdown would imply is covered). The
        # shortfall must be debt_service (the mortgage), NOT
        # debt_service + (living_costs - spending_target) -- i.e. no double-
        # count of spending the drawdown already funds.
        result, _ = simulate_year_pure(
            state=state, year=5, allocations={'_primary_income': 0,
                                               '_annual_savings': 0},
            config=cfg, investment_return=0.0,
            primary_marginal_rate=0.30, mortgage_data=mort,
            living_costs=80_000, after_tax_income=0.0,
            cpp_income=15_000, oas_income=10_000, pension_income=5_000,
            drawdown_net_target=60_000, retiree_marginal_rate=0.20,
            any_retired=True, retirement_spending_target=90_000,
        )
        self.assertTrue(result.any_retired)
        # available = after_tax(0) + drawdown_net(60k) + cpp+oas+pension(30k)
        #           = 90k = spending_target. required = spending_target(90k)
        #           + debt_service. shortfall = debt_service.
        self.assertAlmostEqual(result.solvency_shortfall,
                               result.debt_service, delta=1.0,
                               msg="retirement shortfall must be the real "
                                   "debt_service gap, not a double-count of "
                                   "living_costs on top of the drawdown")

    def test_working_life_branch_unchanged_charges_living_costs(self):
        # The pre-retirement branch still charges the working living_costs
        # (a shock-induced shortfall) -- the fix only changes retirement.
        from simulation_state import SimState, simulate_year_pure
        from test_issue_679_solvency import (
            _reserve_config, _mort_data, RUIN_MORTGAGE_PAYMENT,
            RUIN_LIVING_COSTS, RUIN_AFTER_TAX_EI,
        )
        cfg = _reserve_config(emergency_reserve_rate=0.0)
        state = SimState(
            emergency_reserve_balance=0, mortgage_balance=200_000,
            non_reg_balance=0, non_reg_acb=0,
            jurisdiction_state={'canada': {}})
        mort = _mort_data(state.mortgage_balance, payment=RUIN_MORTGAGE_PAYMENT)
        result, _ = simulate_year_pure(
            state=state, year=1, allocations={'_primary_income': 0,
                                               '_annual_savings': 0},
            config=cfg, investment_return=0.0,
            primary_marginal_rate=0.30, mortgage_data=mort,
            living_costs=RUIN_LIVING_COSTS, after_tax_income=RUIN_AFTER_TAX_EI,
            # working life: any_retired=False (default), no drawdown.
        )
        self.assertFalse(result.any_retired)
        # required = debt_service + living_costs; the working-life identity
        # is the pre-fix behaviour, unchanged.
        self.assertGreater(result.solvency_shortfall, 0)
        self.assertAlmostEqual(result.solvency_shortfall,
                               result.debt_service + RUIN_LIVING_COSTS
                               - RUIN_AFTER_TAX_EI, delta=1.0)


class TestRetirementDrawdownFromLiraAndFhsa(unittest.TestCase):
    """The net-target drawdown (``@rule('retirement_drawdown')``) must apply
    its per-account balance deltas for a LIRA and an FHSA, not just the
    RRSP/TFSA/non-reg accounts every other decumulation test exercises.

    The apply loop in ``simulation_rules.apply_retirement_drawdown`` has a
    branch per balance key; the ``lira_balance`` and ``fhsa_balance`` branches
    only fire when the plan actually draws from those two accounts, which
    needs both to be in the ``drawdown_order``, both to carry a balance, and
    a net target large enough to reach them. This drives exactly that,
    through the pure engine seam (``simulate_year_pure`` -- the Scenario C /
    ``test_lira_wiring`` pattern) so the draw is fully observable in the
    returned next-state's ``jurisdiction_state['canada']`` balances, not
    inferred (DP#4/DP#15: fabricated round numbers, role-based primary).
    """

    def _draw(self, net_target):
        from simulation_state import _default_canada_state
        from simulation_config import SimulationConfig
        # #700/#643/#704: LIRA/FHSA are per-adult stores (single primary slot).
        canada = _default_canada_state()
        canada['adult_lira'] = {'primary': {
            'balance': 100_000.0, 'birth_year': 1960, 'jurisdiction': 'federal',
            'reference_rate': 0.06, 'conversion_year': 0}}
        canada['adult_fhsa'] = {'primary': {
            'balance': 40_000.0, 'room': 0.0, 'lifetime_used': 0.0,
            'lifetime_limit': 40000}}
        state = SimState(jurisdiction_state={'canada': canada})
        cfg = SimulationConfig(
            projection_years=1, investment_return=0.06, mortgage_balance=0,
            mortgage_rate=0.05, margin_available=0,
            family_members=[{'role': 'primary', 'gross_income': 0,
                             'birth_year': 1960, 'rrsp_room_accumulated': 0,
                             'tfsa_room_accumulated': 0}],
            children=[])
        result, nxt = simulate_year_pure(
            state=state, year=0, calendar_year=2025,
            allocations={'_primary_income': 0, '_annual_savings': 0},
            config=cfg, investment_return=0.06,
            primary_marginal_rate=0.30, retiree_marginal_rate=0.30,
            # LIRA first (fully taxable, exhausts), then FHSA (tax-free) --
            # so a target above the LIRA's after-tax capacity spills into the
            # FHSA branch.
            drawdown_order=['lira', 'fhsa'],
            drawdown_net_target=net_target)
        return result, nxt.jurisdiction_state['canada']

    def test_lira_branch_applies_its_delta(self):
        # A target the LIRA alone cannot fund (its after-tax capacity is only
        # ~$74k on a $106k grown balance at a 30% rate) empties the LIRA.
        result, canada = self._draw(net_target=100_000.0)
        self.assertEqual(adult_lira_slot(canada, 0)['balance'], 0.0,
                         "the LIRA branch of the drawdown apply loop must "
                         "reduce lira_balance -- it drew nothing")
        # The engine surfaces the drawn-to-zero LIRA on the YearResult too.
        self.assertEqual(result.lira_balance, 0.0)

    def test_fhsa_branch_applies_its_delta(self):
        # Same target: after the LIRA is exhausted, the remaining net need
        # spills into the FHSA -- drawn, but not to zero (a strictly partial
        # draw is unambiguous proof the branch fired, not a coincidental 0).
        _result, canada = self._draw(net_target=100_000.0)
        opening_grown_fhsa = 40_000.0 * 1.06
        self.assertGreater(adult_fhsa_slot(canada, 0)['balance'], 0.0)
        self.assertLess(adult_fhsa_slot(canada, 0)['balance'], opening_grown_fhsa,
                        "the FHSA branch of the drawdown apply loop must "
                        "reduce fhsa_balance below its grown opening -- it "
                        "drew nothing")

    def test_full_target_delivered_from_the_two_accounts(self):
        # Money conservation at the account level: the two branches together
        # deliver the whole net need when the accounts can fund it.
        result, _canada = self._draw(net_target=100_000.0)
        self.assertAlmostEqual(result.drawdown_net_delivered, 100_000.0,
                               delta=1.0)


if __name__ == "__main__":
    unittest.main()