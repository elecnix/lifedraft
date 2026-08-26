#!/usr/bin/env python3
"""Issue #74: a DECLARABLE staggered deployment schedule on the refinance
cash-out advance.

The engine's default (and the ``deployment_lag`` layer's short-months-window
carry) assumes the cash-out advance deploys as a year-0 lump.  A household
that wants to DCA / stagger the advance over several years -- parking the
undeployed portion and dripping it in equal annual tranches -- could not ask
whether that changes the outcome: there was no time-spread decision variable.

Declaring ``deployment_schedule_years`` opts into pricing the opportunity cost
of staggering.  During the stagger window the undeployed tranches sit at
``parking_rate`` instead of being invested at the portfolio's year-0 return;
the foregone return net of parking earnings (summed linearly over the window)
is applied as a year-0-equivalent reduction of the deployable principal (the
SAME seam ``deployment_lag_cost`` and ``transaction_cost_year0`` use).

This module tests:
  * the pure arithmetic seam (``schedule_cost``, DP#11);
  * the end-to-end engine behaviour (driving ``FamilySimulation.run`` and
    asserting the engine's observable output, DP#11/DP#18);
  * the AMBIGUITY GUARD (DP#32/DP#5): a LAG (months) and a SCHEDULE (years)
    are rival timings of the same money -- declaring both on the same option
    raises loudly; neither/one passes;
  * tranche conservation (money conservation is exact: the full borrowed lump
    stays on the debt side, the deployed principal is <= the lump);
  * optimizer visibility (a staggered config produces different terminal-asset
    ordering than lump);
  * contract-mapping / round-trip (DP#24);
  * schema-coverage (the leaf is consumed, not merely parsed).

Fabricated round numbers, role-based names (DP#4/DP#15).
"""
import unittest


import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deployment_schedule import schedule_cost


# ============================================================================
# Unit tests: the pure arithmetic seam (DP#11 -- call the function directly).
# ============================================================================

class ScheduleCostPureTest(unittest.TestCase):
    """``schedule_cost`` is a pure function (DP#3): same inputs -> same
    output, no hidden state. These tests verify its contract directly."""

    def test_no_schedule_is_hard_zero(self):
        """years == 0 (no schedule declared) is a hard zero -- the cost IS
        zero, never a default that masks a missing input (DP#32). Byte-for-byte
        the pre-feature behaviour."""
        self.assertEqual(schedule_cost(100_000, 0, 0.0, 0.05), 0.0)

    def test_one_year_is_hard_zero(self):
        """years == 1 (a single tranche = immediate deployment) is a hard
        zero -- one tranche deploys at year 0, same as today's lump."""
        self.assertEqual(schedule_cost(100_000, 1, 0.0, 0.05), 0.0)

    def test_no_lump_is_hard_zero(self):
        """lump == 0 (no cash-out to schedule, e.g. a no-refinance scenario)
        is a hard zero -- there is nothing to carry a cost on."""
        self.assertEqual(schedule_cost(0, 4, 0.0, 0.05), 0.0)

    def test_zero_spread_is_zero(self):
        """When the investment return equals the parking rate the net cost is
        exactly zero -- the idle money earns what investing would have."""
        self.assertEqual(schedule_cost(100_000, 4, 0.05, 0.05), 0.0)

    def test_positive_cost_basic(self):
        """lump * (return_rate - parking) * (years - 1) / 2. A $100k lump,
        6% return, 0% parking, 4 years -> 100_000 * 0.06 * 3 / 2 = 9_000.
        Verified against explicit per-year summation:
          year 0: park 75k, cost = 75k * 0.06 = 4500
          year 1: park 50k, cost = 50k * 0.06 = 3000
          year 2: park 25k, cost = 25k * 0.06 = 1500
          year 3: park 0,    cost = 0
          total = 9000."""
        self.assertAlmostEqual(
            schedule_cost(100_000, 4, 0.0, 0.06), 9_000.0, places=6)

    def test_two_year_schedule(self):
        """A 2-year schedule: one tranche at year 0, one at year 1. The
        undeployed half sits for 1 year. cost = lump * (r-p) * 1/2.
        100k * 0.06 * 0.5 = 3000."""
        self.assertAlmostEqual(
            schedule_cost(100_000, 2, 0.0, 0.06), 3_000.0, places=6)

    def test_three_year_schedule(self):
        """A 3-year schedule: cost = 100k * 0.06 * 2/2 = 6000.
        Per-year: y0 park 66.67k cost 4000, y1 park 33.33k cost 2000, y2 0.
        Total 6000."""
        self.assertAlmostEqual(
            schedule_cost(100_000, 3, 0.0, 0.06), 6_000.0, places=6)

    def test_parking_rate_reduces_cost(self):
        """Both sides of the parking/return threshold (DP#17): a positive
        parking rate (idle money earning something) reduces the cost vs 0%
        parking. 100k * (0.06 - 0.02) * 3/2 = 6000 vs 9000 at 0% parking."""
        self.assertAlmostEqual(
            schedule_cost(100_000, 4, 0.02, 0.06), 6_000.0, places=6)

    def test_parking_above_return_is_negative_cost(self):
        """parking_rate > return_rate is a real, representable scenario (idle
        money earning more than the portfolio) and returns a NEGATIVE cost --
        a gain, not floored at zero. 100k * (0.02 - 0.06) * 3/2 = -6000."""
        self.assertAlmostEqual(
            schedule_cost(100_000, 4, 0.06, 0.02), -6_000.0, places=6)

    def test_negative_cost_not_floored_at_zero(self):
        """The negative-cost case is honoured as-is, never silently floored
        at zero (DP#32: a plausible number from a real declared scenario is
        not a fabricated zero)."""
        self.assertLess(schedule_cost(50_000, 3, 0.06, 0.03), 0.0)

    def test_cost_scales_linearly_with_years(self):
        """The cost is linear in (years-1): a 5-year schedule costs twice a
        3-year schedule ( (5-1)/2 = 2 vs (3-1)/2 = 1 )."""
        three = schedule_cost(200_000, 3, 0.01, 0.06)
        five = schedule_cost(200_000, 5, 0.01, 0.06)
        self.assertAlmostEqual(five, 2 * three, places=6)

    def test_cost_scales_linearly_with_lump(self):
        """The cost is linear in the lump -- a $200k lump costs twice a $100k
        lump at the same schedule."""
        small = schedule_cost(100_000, 4, 0.0, 0.06)
        large = schedule_cost(200_000, 4, 0.0, 0.06)
        self.assertAlmostEqual(large, 2 * small, places=6)

    def test_negative_years_raises(self):
        """years < 0 is a bad input, not a 'schedule in the other direction'
        -- a plausible sign-flipped cost would be a confident wrong number
        (DP#32)."""
        with self.assertRaises(ValueError):
            schedule_cost(100_000, -1, 0.0, 0.05)

    def test_negative_lump_raises(self):
        """lump < 0 is a bad input, not a cost to price."""
        with self.assertRaises(ValueError):
            schedule_cost(-1, 4, 0.0, 0.05)

    def test_absurd_negative_spread_raises(self):
        """A net rate (return_rate - parking_rate) at or below -100% raises
        -- parking earning 100%+ more than the investment return is an absurd
        scenario whose 'cost' would be a large fabricated gain; refusing
        rather than silently inventing money (DP#32). This is the SAME
        threshold the sibling deployment_lag_cost uses (Finding 2)."""
        with self.assertRaises(ValueError):
            schedule_cost(100_000, 4, 0.0, -1.0)   # spread = -1.0
        with self.assertRaises(ValueError):
            schedule_cost(100_000, 4, 1.5, 0.0)   # spread = -1.5

    def test_just_above_negative_one_spread_is_ok(self):
        """A net spread just above -100% (e.g. -0.995) is allowed -- it
        produces a large negative cost (a gain), which is a real,
        representable scenario (parking earning slightly less than 100%
        more than the portfolio). The -1.0 threshold is the absurd extreme,
        not the normal negative-cost case (same as deployment_lag_cost)."""
        cost = schedule_cost(100_000, 4, 1.0, 0.005)  # spread = -0.995
        self.assertLess(cost, 0.0)

    def test_zero_return_rate_is_ok(self):
        """return_rate == 0 (a flat-zero portfolio) with zero parking is a
        valid edge -- the spread is 0.0, the cost is zero (there is no
        foregone return), not a raise. 0 is a value, not an absence (DP#32)."""
        self.assertEqual(schedule_cost(100_000, 4, 0.0, 0.0), 0.0)

    def test_negative_return_rate_with_low_parking_is_ok(self):
        """A negative return_rate with a parking_rate close to it (spread
        above -1.0) is allowed -- the cost is a small negative number (a
        small gain from parking outperforming a losing portfolio), not a
        raise. The sibling's -1.0 threshold semantics allow this; the old
        `return_rate < 0` raise would have refused it (Finding 2)."""
        cost = schedule_cost(100_000, 4, 0.0, -0.02)  # spread = -0.02
        self.assertLess(cost, 0.0)  # small gain

    def test_is_pure(self):
        """DP#3: same inputs always yield the same output -- no hidden state."""
        args = (123_456, 5, 0.015, 0.045)
        self.assertEqual(schedule_cost(*args), schedule_cost(*args))


# ============================================================================
# Integration tests: drive FamilySimulation.run() and assert the engine's
# observable output (DP#11/DP#18 -- never hand-build engine internals).
# ============================================================================

from scenario_overlay import ScenarioOverlay, apply_overlay
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation

# A fabricated household (DP#4/DP#15) with a mortgage and undrawn HELOC room,
# mirroring test_issue_137_deployment_lag's cash-out fixture. Round numbers,
# role-based names. RRSP room is 0 for both members so the year-0 draw is not
# confounded by an RRSP-refund HELOC paydown.
SCH_START_YEAR = 2026
SCH_PRIMARY_BIRTH = 1980
SCH_SPOUSE_BIRTH = 1982
SCH_GROSS_RETURN = 0.06
SCH_OPENING_MORTGAGE = 200_000
SCH_OPENING_MARGIN = 150_000
SCH_HOUSE_VALUE = 600_000
SCH_MORTGAGE_RATE = 0.05
SCH_CASH_OUT = 100_000
SCH_AMORTIZATION = 25


def _sch_base_config() -> dict:
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': SCH_PRIMARY_BIRTH, 'gross_income': 150_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
            {'role': 'spouse', 'birth_year': SCH_SPOUSE_BIRTH, 'gross_income': 60_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
        ]},
        'accounts': {'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': SCH_START_YEAR, 'projection_years': 10,
            'investment_return': SCH_GROSS_RETURN, 'salary_growth': 0.0,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': SCH_HOUSE_VALUE, 'mortgage_balance': SCH_OPENING_MORTGAGE,
            'mortgage_rate': SCH_MORTGAGE_RATE, 'amortization_years': 20,
            'margin_available': SCH_OPENING_MARGIN, 'ltv_max': 0.80,
            'heloc_readvance': False,
        },
        'savings': {'rate': 0.10},
        'tax': {'province': 'qc'},
    }


def _run_cashout(deployment_schedule_years=0, parking_rate=0.0,
                 time_step='yearly'):
    """Drive the engine end-to-end: apply_overlay books the cash-out refinance,
    then FamilySimulation.run() folds the projection. The deployment schedule
    is set on the property dict (the from_dict path the adapter writes),
    exercising the full config -> engine seam (DP#18: the leaf reaches the
    engine, not just the merged config). Returns the list of YearResult.
    ``time_step`` selects the yearly or monthly fold."""
    base_cfg = _sch_base_config()
    overlay = ScenarioOverlay(label='cashout_schedule_test', cash_out=SCH_CASH_OUT,
                              mortgage_rate=base_cfg['property']['mortgage_rate'],
                              refinance_amortization_years=SCH_AMORTIZATION)
    overlaid_cfg = apply_overlay(base_cfg, overlay)
    overlaid_cfg['property']['deployment_schedule_years'] = deployment_schedule_years
    overlaid_cfg['property']['deployment_schedule_parking_rate'] = parking_rate
    overlaid_cfg['assumptions']['time_step'] = time_step
    sim_cfg = SimulationConfig.from_dict(overlaid_cfg)
    lump_sum = sim_cfg.margin_available + sim_cfg.cash_out
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                          use_readvanceable=False, deduct_later=False,
                          lump_sum=lump_sum)
    return sim.run()


class ScheduleIntegrationTest(unittest.TestCase):
    """Drive FamilySimulation.run() and assert the engine's observable output
    (DP#11/DP#18): the schedule's cost flows into the year-0 result and the
    terminal trajectory."""

    def test_no_declared_schedule_surfaces_zero_cost(self):
        """A run with no declared schedule (years 0) surfaces
        deployment_schedule_cost = 0.0 on the year-0 result -- the no-op
        path, byte-identical to the pre-feature behaviour (DP#32)."""
        results = _run_cashout(deployment_schedule_years=0)
        self.assertEqual(results[0].deployment_schedule_cost, 0.0)

    def test_one_year_schedule_surfaces_zero_cost(self):
        """A declared schedule of 1 year (single tranche = immediate
        deployment) surfaces zero cost -- 1 is the canonical no-schedule
        state, byte-identical to absent (DP#32)."""
        results = _run_cashout(deployment_schedule_years=1)
        self.assertEqual(results[0].deployment_schedule_cost, 0.0)

    def test_declared_schedule_surfaces_positive_cost_on_year_0(self):
        """A declared schedule surfaces the computed cost on the year-0
        result. cost = cash_out * (return - parking) * (years - 1) / 2 =
        100_000 * (0.06 - 0.0) * (4 - 1) / 2 = 9_000. The return_rate is the
        portfolio's year-0 return (SCH_GROSS_RETURN), NOT the mortgage
        rate -- the borrowing rate double-counts the debt cost the mortgage
        already pays."""
        results = _run_cashout(deployment_schedule_years=4, parking_rate=0.0)
        self.assertAlmostEqual(
            results[0].deployment_schedule_cost, 9_000.0, places=6)

    def test_declared_schedule_cost_only_on_year_0(self):
        """The cost is a YEAR-0-equivalent cost -- it is 0.0 on every year
        after year 0 (the schedule cost is a one-time deployment-timing cost,
        not a recurring charge)."""
        results = _run_cashout(deployment_schedule_years=4, parking_rate=0.0)
        self.assertGreater(results[0].deployment_schedule_cost, 0.0)
        for r in results[1:]:
            self.assertEqual(r.deployment_schedule_cost, 0.0)

    def test_declared_schedule_reduces_terminal_assets(self):
        """The cost reduces the deployable principal at year 0, so less
        compounds -- the schedule's cost flows into every objective via
        terminal total_assets. A run WITH a schedule ends with LESS than the
        same run WITHOUT one (the cost is real, not silently $0)."""
        no_sch = _run_cashout(deployment_schedule_years=0)
        with_sch = _run_cashout(deployment_schedule_years=4, parking_rate=0.0)
        self.assertGreater(no_sch[-1].total_assets, with_sch[-1].total_assets)

    def test_schedule_one_is_byte_identical_to_no_schedule(self):
        """A declared schedule of 1 year is byte-identical to no declared
        schedule -- both surface deployment_schedule_cost = 0.0 and the same
        terminal assets (DP#32: 1 is a value, not a fallback; the no-schedule
        path is unchanged)."""
        no_sch = _run_cashout(deployment_schedule_years=0)
        one_sch = _run_cashout(deployment_schedule_years=1, parking_rate=0.0)
        self.assertEqual(no_sch[-1].total_assets, one_sch[-1].total_assets)
        self.assertEqual(
            no_sch[0].deployment_schedule_cost,
            one_sch[0].deployment_schedule_cost)

    def test_longer_schedule_costs_more_monotonically(self):
        """DP#17: under a flat positive return, a longer schedule costs more
        monotonically -- terminal assets decrease as the schedule lengthens
        (more years of foregone return on the undeployed tranches)."""
        results = {}
        for years in (2, 4, 6, 8):
            results[years] = _run_cashout(
                deployment_schedule_years=years, parking_rate=0.0)
        # Monotonic: more years -> less terminal assets
        for i in range(2, 7, 2):
            self.assertGreater(
                results[i][-1].total_assets,
                results[i + 2][-1].total_assets,
                f"terminal assets should decrease as schedule lengthens "
                f"from {i} to {i + 2} years")
        # Monotonic: more years -> higher year-0 cost
        for i in range(2, 7, 2):
            self.assertGreater(
                results[i + 2][0].deployment_schedule_cost,
                results[i][0].deployment_schedule_cost,
                f"year-0 cost should increase as schedule lengthens "
                f"from {i} to {i + 2} years")

    def test_parking_rate_below_return_costs_more_than_zero_parking(self):
        """Both sides of the parking/return threshold: a positive parking
        rate (idle money earning something) reduces the cost vs 0% parking,
        so terminal assets are higher than the 0%-parking schedule run."""
        zero_parking = _run_cashout(
            deployment_schedule_years=4, parking_rate=0.0)
        two_pct_parking = _run_cashout(
            deployment_schedule_years=4, parking_rate=0.02)
        # cost with 2% parking = 100k * (0.06 - 0.02) * 3/2 = 6000 < 9000
        self.assertLess(
            two_pct_parking[0].deployment_schedule_cost,
            zero_parking[0].deployment_schedule_cost)
        self.assertGreater(
            two_pct_parking[-1].total_assets,
            zero_parking[-1].total_assets)

    def test_negative_cost_caps_borrowed_investment_at_lump(self):
        """When the parking rate EXCEEDS the return rate the cost is negative
        (a gain). The deployable principal is CAPPED at the borrowed lump --
        the stagger cannot inflate invested principal above what was actually
        borrowed. The raw negative cost is still surfaced for observability,
        but terminal assets do NOT exceed the no-schedule baseline."""
        no_sch = _run_cashout(deployment_schedule_years=0)
        # return 6%, parking 8% -> negative cost (gain)
        gain_sch = _run_cashout(
            deployment_schedule_years=4, parking_rate=0.08)
        self.assertLess(gain_sch[0].deployment_schedule_cost, 0.0)
        # Capped: the year-0 invested principal does not exceed the no-schedule
        # run (no solvency inflation).
        self.assertLessEqual(
            gain_sch[0].contributions_total,
            no_sch[0].contributions_total + 1e-6)
        # Terminal assets do not exceed no-schedule -- the gain is not invented
        # as borrowed money.
        self.assertLessEqual(
            gain_sch[-1].total_assets, no_sch[-1].total_assets + 1e-6)

    def test_debt_side_unchanged_by_schedule(self):
        """The full borrowed lump stays on the debt side (the year-0 purpose
        tracing is untouched): the year-0 mortgage balance (which carries the
        full cash_out advance) is the same with and without a declared
        schedule. The cost reduces only the DEPLOYED principal, not the debt
        booked -- money conservation is exact (the full lump is borrowed, the
        deployed principal is <= the lump)."""
        no_sch = _run_cashout(deployment_schedule_years=0)
        with_sch = _run_cashout(deployment_schedule_years=4, parking_rate=0.0)
        self.assertAlmostEqual(
            no_sch[0].mortgage_balance, with_sch[0].mortgage_balance,
            places=2)
        # Conservation: the deployed principal (contributions from borrowing)
        # is <= the borrowed lump in both runs.
        lump = SCH_CASH_OUT + SCH_OPENING_MARGIN
        self.assertLessEqual(no_sch[0].contributions_total, lump + 1e-6)
        self.assertLessEqual(with_sch[0].contributions_total, lump + 1e-6)

    def test_monthly_path_surfaces_year_0_cost(self):
        """The monthly fold's per-year loop must surface the year-0 schedule
        cost on results[0], mirroring the yearly path. The monthly path's
        pre-projection step computes a year-0 result0 that is NOT appended
        (it only deploys the lump), so without threading the cost into the
        per-year loop's simulate_year_pure call the first appended result
        would carry deployment_schedule_cost=0.0 even when a cost was
        applied -- the cost would be paid but invisible. cost =
        100_000 * (0.06 - 0.0) * 3/2 = 9_000."""
        results = _run_cashout(
            deployment_schedule_years=4, parking_rate=0.0,
            time_step='monthly')
        self.assertAlmostEqual(
            results[0].deployment_schedule_cost, 9_000.0, places=6)
        for r in results[1:]:
            self.assertEqual(r.deployment_schedule_cost, 0.0)

    def test_monthly_and_yearly_surface_the_same_cost(self):
        """Both folds surface the same year-0 cost (DP#9: one spelling of the
        year-0 fact)."""
        yearly = _run_cashout(
            deployment_schedule_years=4, parking_rate=0.0,
            time_step='yearly')
        monthly = _run_cashout(
            deployment_schedule_years=4, parking_rate=0.0,
            time_step='monthly')
        self.assertAlmostEqual(
            yearly[0].deployment_schedule_cost,
            monthly[0].deployment_schedule_cost, places=6)

    def test_tranche_conservation_exact(self):
        """Tranche conservation: the full borrowed lump is on the debt side
        (mortgage_balance carries the full cash_out), and the deployed
        principal (contributions from borrowing) never exceeds the lump.
        The schedule cost is an opportunity-cost reduction, not money that
        appears or disappears -- the difference between the borrowed lump
        and the deployed principal IS the cost, and it is a real economic
        loss (foregone return), not an accounting gap. Under a staggered
        schedule the deployed principal is strictly less than the lump
        (the cost is positive), and the debt is the full lump."""
        results = _run_cashout(deployment_schedule_years=5, parking_rate=0.0)
        lump = SCH_CASH_OUT + SCH_OPENING_MARGIN
        # Debt side: the full lump is borrowed (mortgage carries cash_out,
        # HELOC carries the margin draw). The year-0 mortgage balance
        # includes the cash_out advance.
        self.assertGreater(results[0].mortgage_balance, SCH_OPENING_MORTGAGE)
        # Asset side: deployed principal <= borrowed lump (conservation).
        self.assertLessEqual(results[0].contributions_total, lump + 1e-6)
        # The schedule cost is the gap: it is positive (a real cost).
        self.assertGreater(results[0].deployment_schedule_cost, 0.0)

    def test_extreme_schedule_floors_deployable_at_zero(self):
        """Finding 7: an extreme config where the schedule cost EXCEEDS the
        lump sum must floor the deployable principal at 0.0, never go
        negative. A negative deployable would mean the engine invests a
        NEGATIVE amount (a short position) -- silently inventing money.
        With a very long schedule (90 years) and the fixture's 6% return,
        the cost = 100_000 * 0.06 * 89 / 2 = 267_000 -- exceeding the
        lump (cash_out 100k + margin 150k = 250k). The deployable must be
        max(0, ...) = 0, and money conservation must hold (the deployed
        principal is <= the lump, the debt carries the full lump)."""
        results = _run_cashout(deployment_schedule_years=90, parking_rate=0.0)
        lump = SCH_CASH_OUT + SCH_OPENING_MARGIN
        # The schedule cost exceeds the total lump sum.
        self.assertGreater(results[0].deployment_schedule_cost, lump)
        # Deployable is floored at 0: contributions from borrowing are 0
        # (no negative investment). The floor prevents solvency inflation.
        self.assertGreaterEqual(results[0].contributions_total, 0.0)
        self.assertLessEqual(results[0].contributions_total, lump + 1e-6)
        # Conservation: the debt still carries the full borrowed lump.
        self.assertGreater(results[0].mortgage_balance, SCH_OPENING_MORTGAGE)

    def test_extreme_schedule_floors_deployable_monthly(self):
        """Finding 7 (monthly fold): the same extreme config through the
        monthly fold must also floor the deployable at 0.0 -- both folds
        apply the same max(0, ...) floor."""
        results = _run_cashout(
            deployment_schedule_years=90, parking_rate=0.0,
            time_step='monthly')
        lump = SCH_CASH_OUT + SCH_OPENING_MARGIN
        self.assertGreater(results[0].deployment_schedule_cost, lump)
        self.assertGreaterEqual(results[0].contributions_total, 0.0)
        self.assertLessEqual(results[0].contributions_total, lump + 1e-6)
        self.assertGreater(results[0].mortgage_balance, SCH_OPENING_MORTGAGE)


# ============================================================================
# Optimizer visibility: a staggered config must produce different terminal-
# asset ordering than lump (the ranked scenarios reflect the declared
# schedule).
# ============================================================================

class ScheduleOptimizerVisibilityTest(unittest.TestCase):
    """Direct-simulation ordering: a staggered config must produce different
    terminal-asset ordering than lump. These tests drive FamilySimulation.run()
    directly (not the optimizer pipeline) and assert terminal-asset ordering
    across schedule values -- the ordering the optimizer would see. The FULL
    optimizer pipeline (candidate discovery -> overlay -> run_optimization ->
    net_benefit ranking) is covered by ScheduleOptimizerPathTest below
    (Finding 3)."""

    def test_lump_beats_staggered_under_positive_return(self):
        """Under a flat positive return, immediate (lump) deployment produces
        higher terminal assets than any staggered schedule -- the opportunity
        cost of staggering is real and flows into the objective. This is the
        ordering the optimizer would see: a household weighing lump vs
        staggered deployment gets a different answer for each."""
        lump = _run_cashout(deployment_schedule_years=0)
        staggered = _run_cashout(deployment_schedule_years=4, parking_rate=0.0)
        self.assertGreater(lump[-1].total_assets, staggered[-1].total_assets)

    def test_different_schedules_produce_different_rankings(self):
        """Two different schedule lengths produce different terminal assets --
        the ranked scenarios distinguish them (a 2-year schedule beats a
        6-year schedule under a positive return)."""
        two_year = _run_cashout(deployment_schedule_years=2, parking_rate=0.0)
        six_year = _run_cashout(deployment_schedule_years=6, parking_rate=0.0)
        self.assertGreater(
            two_year[-1].total_assets, six_year[-1].total_assets)


# ============================================================================
# REAL OPTIMIZER PATH (Finding 3): drive the actual LTV/refinance
# exploration entry point the optimizer uses (optimize.run_ltv_exploration)
# with a schedule-declared config and assert the ranked results reflect the
# schedule cost. This is NOT a direct-simulation ordering test -- it goes
# through the optimizer's candidate discovery, overlay construction, and
# run_optimization fold (the full pipeline the household's ranked table
# comes from).
# ============================================================================

import optimize  # noqa: E402


def _opt_cfg():
    """A fabricated household in the INTERNAL config format
    optimize.run_ltv_exploration expects, with a mortgage and undrawn margin
    so the LTV ladder produces cash-out candidates. Round numbers, role-based
    names (DP#4/DP#15)."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 150_000,
                 'rrsp_room_accumulated': 30_000, 'tfsa_room_accumulated': 40_000,
                 'fhsa_first_time_buyer_since': None, 'fhsa_room_accumulated': 0},
                {'role': 'spouse', 'gross_income': 70_000,
                 'rrsp_room_accumulated': 20_000, 'tfsa_room_accumulated': 40_000,
                 'fhsa_first_time_buyer_since': None, 'fhsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'property': {
            'house_value': 800_000,
            'mortgage_balance': 300_000,
            'mortgage_rate': 0.05,
            'margin_available': 50_000,
            'ltv_max': 0.80,
            'heloc_readvance': True,
            'refinance_amortization_years': 25,
        },
        'accounts': {'resp_current_balance': 0},
        'assumptions': {'investment_return': 0.07, 'start_year': 2026,
                        'projection_years': 10, 'salary_growth': 0.0,
                        'frozen_brackets': True},
        'savings': {'rate': 0.10},
        'tax': {'province': 'qc'},
        'scenarios': {},
    }


class ScheduleOptimizerPathTest(unittest.TestCase):
    """Finding 3: drive the REAL optimizer entry point
    (optimize.run_ltv_exploration) with a schedule-declared config and assert
    the ranked results reflect the schedule cost. The schedule reduces the
    deployable principal at year 0, so a cash-out candidate's net_benefit is
    LOWER with a schedule than without one."""

    def test_schedule_reduces_optimizer_net_benefit(self):
        """The optimizer's LTV exploration produces ranked candidates with a
        net_benefit score. A config WITH a deployment schedule (years=6,
        7% return, 0% parking) prices a real opportunity cost that reduces
        the deployable principal, so the best cash-out candidate's
        net_benefit is LOWER than the same config WITHOUT a schedule.
        This drives the full optimizer pipeline: candidate discovery ->
        overlay construction -> run_optimization -> FamilySimulation.run ->
        compute_net_benefit."""
        cfg_no_sched = _opt_cfg()
        cfg_with_sched = _opt_cfg()
        cfg_with_sched['property']['deployment_schedule_years'] = 6
        cfg_with_sched['property']['deployment_schedule_parking_rate'] = 0.0

        results_no = optimize.run_ltv_exploration(cfg_no_sched)
        results_with = optimize.run_ltv_exploration(cfg_with_sched)

        scored_no = [r for r in results_no if 'net_benefit' in r]
        scored_with = [r for r in results_with if 'net_benefit' in r]
        self.assertGreater(len(scored_no), 0)
        self.assertGreater(len(scored_with), 0)

        best_no = max(r['net_benefit'] for r in scored_no)
        best_with = max(r['net_benefit'] for r in scored_with)
        self.assertGreater(best_no, best_with)

    def test_schedule_cost_visible_in_optimizer_year_by_year(self):
        """The schedule cost surfaces on the year-0 YearResult inside the
        optimizer's result dicts (year_by_year[0].deployment_schedule_cost).
        With year_by_year included, the first year of a cash-out candidate
        carries the schedule cost; without a schedule it is 0.0."""
        cfg = _opt_cfg()
        cfg['property']['deployment_schedule_years'] = 4
        cfg['property']['deployment_schedule_parking_rate'] = 0.0
        results = optimize.run_ltv_exploration(cfg)
        scored = [r for r in results if 'net_benefit' in r and r.get('year_by_year')]
        self.assertGreater(len(scored), 0)
        has_cost = any(
            r['year_by_year'][0].get('deployment_schedule_cost', 0.0) > 0.0
            for r in scored
            if r.get('cashout', 0) > 0)
        self.assertTrue(has_cost)


# ============================================================================
# AMBIGUITY GUARD tests (DP#32/DP#5): a deployment LAG (months) and a
# deployment SCHEDULE (years) are rival timings of the same money.
# Declaring both on the same option must raise loudly. Absence of either
# never conflicts.
# ============================================================================

import copy
from test_input_contract import _load_example, _two_generation_subset  # noqa: E402
import input_contract as ic  # noqa: E402
from contract_schema import validate_contract  # noqa: E402
from contract_errors import ContractAdaptationError  # noqa: E402


def _example_doc() -> dict:
    """The shipped example, trimmed to the two-generation sub-family the
    adapter maps, with every optional illustrative block stripped (the
    shared minimal_example helper pattern)."""
    from _example_doc import minimal_example
    return minimal_example()


def _example_with_schedule(years: int, parking_rate: float = 0.0) -> dict:
    """Declare a deployment schedule on a cash-out refinance option."""
    doc = _example_doc()
    refi = copy.deepcopy(doc["decisions"]["mortgage"]["refinance_options"])
    for opt in refi:
        if opt.get("cash_out", 0) > 0:
            opt["deployment_schedule_years"] = years
            opt["parking_rate"] = parking_rate
            break
    doc["decisions"]["mortgage"]["refinance_options"] = refi
    return doc


def _example_with_lag(months: int, parking_rate: float = 0.0) -> dict:
    """Declare a deployment lag on a cash-out refinance option."""
    doc = _example_doc()
    refi = copy.deepcopy(doc["decisions"]["mortgage"]["refinance_options"])
    for opt in refi:
        if opt.get("cash_out", 0) > 0:
            opt["deployment_lag_months"] = months
            opt["parking_rate"] = parking_rate
            break
    doc["decisions"]["mortgage"]["refinance_options"] = refi
    return doc


def _example_with_both_on_same_option(
        months: int, years: int) -> dict:
    """Declare BOTH a deployment lag AND a deployment schedule on the SAME
    cash-out refinance option -- the ambiguity the guard must catch."""
    doc = _example_doc()
    refi = copy.deepcopy(doc["decisions"]["mortgage"]["refinance_options"])
    for opt in refi:
        if opt.get("cash_out", 0) > 0:
            opt["deployment_lag_months"] = months
            opt["deployment_schedule_years"] = years
            opt["parking_rate"] = 0.0
            break
    doc["decisions"]["mortgage"]["refinance_options"] = refi
    return doc


class AmbiguityGuardTest(unittest.TestCase):
    """DP#32/DP#5: a LAG (months) and a SCHEDULE (years) are rival timings
    of the same money. Declaring both on the same option raises loudly.
    Absence of either never conflicts. Dispatch routes each declared
    dimension to its own pricing."""

    def test_both_on_same_option_raises(self):
        """Declaring both deployment_lag_months AND deployment_schedule_years
        on the SAME refinance option raises loudly at the contract mapping
        boundary -- the advance cannot both sit idle for N months AND drip
        in over N years."""
        doc = _example_with_both_on_same_option(months=3, years=4)
        with self.assertRaises(ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_both_on_same_option_zero_values_raises(self):
        """Even with zero values, declaring both keys on the same option
        raises -- the keys' PRESENCE is the declaration, not the value
        (DP#32: 0 is a value, but two rival timing declarations on the same
        option is a contradiction regardless of the values)."""
        doc = _example_with_both_on_same_option(months=0, years=0)
        with self.assertRaises(ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_neither_declared_passes(self):
        """A contract that declares neither a lag nor a schedule maps without
        error -- the no-declaration path is byte-identical (golden)."""
        doc = _example_doc()
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertNotIn("deployment_lag_months", legacy["property"])
        self.assertNotIn("deployment_schedule_years", legacy["property"])

    def test_only_lag_declared_passes(self):
        """A contract that declares only a lag maps without error -- the lag
        routes to its own pricing (deployment_lag_cost), no schedule is
        carried."""
        doc = _example_with_lag(months=4, parking_rate=0.015)
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy["property"]["deployment_lag_months"], 4)
        self.assertNotIn("deployment_schedule_years", legacy["property"])

    def test_only_schedule_declared_passes(self):
        """A contract that declares only a schedule maps without error -- the
        schedule routes to its own pricing (deployment_schedule_cost), no
        lag is carried."""
        doc = _example_with_schedule(years=4, parking_rate=0.015)
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(
            legacy["property"]["deployment_schedule_years"], 4)
        self.assertNotIn("deployment_lag_months", legacy["property"])

    def test_schedule_with_parking_rate_maps_parking(self):
        """A schedule-declaring option's parking_rate maps to the
        schedule-specific parking key, not the lag parking key."""
        doc = _example_with_schedule(years=4, parking_rate=0.02)
        legacy = ic.to_internal_config(doc)
        self.assertAlmostEqual(
            legacy["property"]["deployment_schedule_parking_rate"], 0.02)
        self.assertNotIn("deployment_lag_parking_rate", legacy["property"])

    # -- CROSS-OPTION GUARD (Finding 1): the single-scalar design carries
    # lag and schedule independently from DIFFERENT options. When lag is on
    # option A and schedule on option B, both costs fire on every sweep
    # candidate -- double-counting the same opportunity cost. The contract-
    # level guard refuses both carried from any options.
    # --

    def test_cross_option_both_dimensions_raises(self):
        """Declaring deployment_lag_months on one option AND
        deployment_schedule_years on a DIFFERENT option raises loudly at
        the contract mapping boundary -- both scalars would land on prop_cfg
        and both costs would fire on every sweep candidate, double-counting
        the same opportunity cost of the same borrowed advance."""
        doc = _example_doc()
        refi = copy.deepcopy(doc["decisions"]["mortgage"]["refinance_options"])
        # refi has at least two cash_out>0 options after the minimal_example
        # strip (the shipped example's refi_50k and refi_100k). Put lag on the
        # first cash_out>0 option, schedule on the second.
        cashout_opts = [o for o in refi if o.get("cash_out", 0) > 0]
        self.assertGreaterEqual(
            len(cashout_opts), 2,
            "test needs two cash_out>0 refinance options")
        cashout_opts[0]["deployment_lag_months"] = 3
        cashout_opts[0]["parking_rate"] = 0.0
        cashout_opts[1]["deployment_schedule_years"] = 4
        cashout_opts[1]["parking_rate"] = 0.0
        doc["decisions"]["mortgage"]["refinance_options"] = refi
        with self.assertRaises(ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_cross_option_single_dimension_passes(self):
        """A contract that declares only ONE dimension across all options
        (lag on one, no schedule anywhere) maps without error -- the
        cross-option guard only fires when BOTH are carried."""
        doc = _example_with_lag(months=3, parking_rate=0.0)
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy["property"]["deployment_lag_months"], 3)
        self.assertNotIn("deployment_schedule_years", legacy["property"])

    def test_shipped_example_maps_cleanly(self):
        """The shipped example.json (after the Finding-1 rework) carries ONLY
        a deployment schedule on a cash_out>0 option (refi_100k) and NO lag --
        so the cross-option guard does NOT fire and the example maps cleanly.
        This is the regression pin for the shipped example's document state."""
        doc = _two_generation_subset(_load_example())
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        # The lag is carried as 0 (inert -- the example illustrates the lag
        # leaf with a no-op value so the schema-coverage guard sees it, but
        # the cross-option guard does not fire because 0 is not active).
        self.assertEqual(legacy["property"].get("deployment_lag_months", 0), 0)
        self.assertEqual(
            legacy["property"].get("deployment_schedule_years"), 4)
        # The schedule is on a cash_out>0 option (not inert, Finding 6).
        refi = doc["decisions"]["mortgage"]["refinance_options"]
        schedule_opt = next(
            (o for o in refi if "deployment_schedule_years" in o), None)
        self.assertIsNotNone(schedule_opt)
        self.assertGreater(schedule_opt["cash_out"], 0)


# ============================================================================
# Contract-mapping / round-trip tests (DP#24): the leaf reaches the engine,
# not just the merged config; a declared schedule survives a load -> modify
# -> save cycle.
# ============================================================================

class ScheduleContractMappingTest(unittest.TestCase):
    """The contract leaf -> internal config -> SimulationConfig seam (DP#18:
    the leaf reaches the engine, not just the merged config)."""

    def test_contract_with_schedule_maps_to_internal_keys(self):
        """A contract declaring deployment_schedule_years + parking_rate on a
        refinance option is schema-valid and maps to the internal property
        keys SimulationConfig.from_dict reads."""
        doc = _example_with_schedule(years=5, parking_rate=0.015)
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy["property"]["deployment_schedule_years"], 5)
        self.assertAlmostEqual(
            legacy["property"]["deployment_schedule_parking_rate"], 0.015)

    def test_contract_schedule_loads_onto_simulation_config(self):
        """The mapped internal keys load onto the SimulationConfig fields the
        year-0 deployment reads."""
        doc = _example_with_schedule(years=5, parking_rate=0.015)
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(sim_cfg.deployment_schedule_years, 5)
        self.assertAlmostEqual(
            sim_cfg.deployment_schedule_parking_rate, 0.015)

    def test_contract_with_no_schedule_maps_no_keys(self):
        """A contract whose refinance options declare no deployment schedule
        maps NEITHER internal key -- the no-schedule path carries no keys,
        byte-identical to the pre-feature shape (DP#24/DP#32: absence is
        absence)."""
        doc = _example_doc()
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertNotIn("deployment_schedule_years", legacy["property"])
        self.assertNotIn(
            "deployment_schedule_parking_rate", legacy["property"])

    def test_round_trip_preserves_a_declared_schedule(self):
        """DP#24: a declared schedule survives a load -> modify -> save cycle.
        to_dict re-emits the schedule (only when declared > 1); from_dict
        reads it back. A no-schedule config round-trips to absence."""
        doc = _example_with_schedule(years=5, parking_rate=0.015)
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        round_tripped = sim_cfg.to_dict()
        self.assertEqual(
            round_tripped["property"]["deployment_schedule_years"], 5)
        self.assertAlmostEqual(
            round_tripped["property"]["deployment_schedule_parking_rate"],
            0.015)

    def test_no_schedule_round_trips_to_absent(self):
        """A no-schedule config round-trips to absence -- neither key is
        re-emitted (DP#24/DP#32)."""
        doc = _example_doc()
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        round_tripped = sim_cfg.to_dict()
        self.assertNotIn(
            "deployment_schedule_years", round_tripped["property"])
        self.assertNotIn(
            "deployment_schedule_parking_rate", round_tripped["property"])

    def test_schedule_one_round_trips_to_absent(self):
        """A declared schedule of 1 (single tranche = immediate deployment)
        round-trips to absent -- 1 is the canonical no-schedule state
        (DP#24/DP#32)."""
        doc = _example_with_schedule(years=1, parking_rate=0.0)
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        round_tripped = sim_cfg.to_dict()
        # years=1 is falsy, so it round-trips to absent
        self.assertNotIn(
            "deployment_schedule_years", round_tripped["property"])


if __name__ == "__main__":
    unittest.main()