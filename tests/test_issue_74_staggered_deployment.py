"""Issue #74: staggered deployment as a SEARCH DIMENSION in the main ranking.

Before #74 the year-0 borrowed lump deployed all-at-once: #137 priced the cost
of ONE fixed delay, but the household could not ask "what if I drip the
$482,000 cash-out into the market over 2/3/4 years instead?" — a SPREAD over
N annual tranches, not a single lag. The optimizer had no dimension to rank
such schedules on.

This pins the fix: ``deployment_schedule_years`` threads from GridOptimizer /
FamilySimulation into the same year-0 seam #137 prices its lag at, where
``deployment_lag.deployment_schedule_cost`` nets a year-0-equivalent reduction
off the deployed principal — exact under the flat return the deterministic
ranker scores with (forgone per-tranche compounding + #137's parking carry,
discounted to year 0). Absent or 1 stays byte-identical (DP#32).

Deliberately OUT of scope here (documented follow-up, not a silent half): the
stochastic half of #74 part (b) — co-ranking on a return DISTRIBUTION so DCA
gets credited for variance/sequence-risk mitigation. Under the deterministic
ranker a spread can only ever cost; these tests pin exactly that honest
pricing.
"""

from typing import Optional

import pytest

from deployment_lag import (
    deployment_carry_cost,
    deployment_schedule_cost,
    validate_deployment_dimensions,
    year0_deployment_cost,
)
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter

HOUSE_VALUE = 800_000
CHARGE = int(0.80 * HOUSE_VALUE)  # OSFI B-20 charge
SURPLUS = 482_000  # fabricated round number (issue #74's own example shape)
ADVANCE_RATE = 0.0370  # amortizing, cheaper
LINE_RATE = 0.0470  # revolving, interest-only, dearer
RETURN = 0.06


def _household(revolving_share: float = 0.5) -> dict:
    """Same shape as the #137 fixture: no registered room, so the whole
    borrowed surplus lands in non-reg."""
    line_draw = min(SURPLUS, CHARGE * revolving_share)
    mortgage_balance = CHARGE - line_draw
    margin_available = CHARGE * revolving_share
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
                 'retirement_age': 65,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'accounts': {'rrsp_annual_max': 0},
        'assumptions': {
            'start_year': 2026, 'horizon_age': 60,
            'investment_return': RETURN, 'salary_growth': 0.0,
            'inflation': 0.0, 'frozen_brackets': True,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': 0, 'cost_basis': 0,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.02, 'interest': 0.0},
                },
            },
        },
        'property': {
            'house_value': HOUSE_VALUE,
            'mortgage_balance': mortgage_balance,
            'mortgage_rate': ADVANCE_RATE,
            'heloc_rate': LINE_RATE,
            'margin_available': margin_available,
            'heloc_readvance': False,
            'amortization_years': 25,
        },
        'household_budget': {'annual_living_costs': 60_000},
    }


def _run(schedule_years: Optional[int], monthly: bool = False,
         revolving_share: float = 0.5):
    cfg_dict = _household(revolving_share)
    if monthly:
        cfg_dict['assumptions']['time_step'] = 'monthly'
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(
        cfg, adapter=CanadaAdapter(cfg), use_readvanceable=False,
        deduct_later=False, lump_sum=SURPLUS,
        deployment_schedule_years=schedule_years,
    )
    return sim.run()


def _blended_financing_rate() -> float:
    """The lump's blended rate at this fixture's 50/50 revolving split."""
    from deployment_lag import lump_financing_rate
    cfg = SimulationConfig.from_dict(_household())
    return lump_financing_rate(
        SURPLUS, cfg.margin_available, LINE_RATE, ADVANCE_RATE)


# ============================================================================
# 1. The pure schedule-cost function
# ============================================================================

class TestScheduleCostIsPure:
    """DP#3/#32: the schedule cost is a pure function of the lump, the spread,
    and the flat return; absent or 1-year schedules are hard zeros."""

    def test_no_schedule_is_zero(self):
        assert deployment_schedule_cost(482_000, None, 0.047) == 0.0
        assert deployment_schedule_cost(482_000, 1, 0.047) == 0.0
        assert deployment_schedule_cost(0, 4, 0.047) == 0.0
        assert deployment_schedule_cost(-5, 3, 0.047) == 0.0

    def test_hand_computed_two_year_spread(self):
        # $100k over 2 years at a 4.7% financing rate parked at 0%, market at
        # 6%: the second $50k tranche gives up one year of compounding AND
        # pays one year of full carry, both discounted to year 0:
        #   forgone growth: 50k*(1 - 1/1.06)   = 2830.19
        #   parking carry:  50k*0.047 / 1.06   = 2216.98
        #   total                              = 5047.17
        assert deployment_schedule_cost(
            100_000, 2, 0.047, 0.0, RETURN) == pytest.approx(5_047.17)

    def test_zero_return_reduces_to_average_parking_years(self):
        # With no growth to forgo, only the carry remains: each tranche parks
        # on average (N-1)/2 years -> L*(f-i)*(N-1)/2.
        assert deployment_schedule_cost(
            100_000, 2, 0.05, 0.03, 0.0) == pytest.approx(100_000 * 0.02 * 0.5)
        assert deployment_schedule_cost(
            100_000, 3, 0.05, 0.0, 0.0) == pytest.approx(100_000 * 0.05 * 1.0)

    def test_idle_parking_offsets_the_carry(self):
        # A promo HISA parking sleeve narrows the spread (#74: "a promo HISA
        # is the natural vehicle for a staggered cash-park") but cannot touch
        # the forgone-compounding half.
        with_hisa = deployment_schedule_cost(
            482_000, 4, LINE_RATE, 0.03, RETURN)
        without = deployment_schedule_cost(482_000, 4, LINE_RATE, 0.0, RETURN)
        assert 0.0 < with_hisa < without

    def test_cost_strictly_increases_with_the_spread(self):
        costs = [deployment_schedule_cost(482_000, n, LINE_RATE, 0.0, RETURN)
                 for n in (1, 2, 3, 4)]
        assert costs[0] == 0.0
        assert all(a < b for a, b in zip(costs, costs[1:])), (
            "spreading over more years must cost strictly more")

    def test_return_at_or_below_minus_100pct_refuses(self):
        with pytest.raises(ValueError, match="-100%"):
            deployment_schedule_cost(100_000, 2, 0.05, 0.0, -1.0)


class TestDeploymentDimensionGuard:
    """A fixed lag (#137) and a spread (#74) are rival timings of the same
    money — declaring both is ambiguous and must fail loudly (DP#32)."""

    def test_both_declared_raises(self):
        with pytest.raises(ValueError, match="Ambiguous deployment timing"):
            validate_deployment_dimensions(lag_months=6, schedule_years=2)

    def test_absent_values_never_conflict(self):
        validate_deployment_dimensions(None, 1)
        validate_deployment_dimensions(0, 4)
        validate_deployment_dimensions(12, None)
        validate_deployment_dimensions(None, None)

    def test_seam_dispatches_to_the_right_pricing(self):
        # The one seam (DP#9): lag -> #137's carry; schedule -> #74's cost;
        # neither -> 0.0.
        assert year0_deployment_cost(
            100_000, 12, 1, 0.05) == deployment_carry_cost(100_000, 12, 0.05)
        assert year0_deployment_cost(
            100_000, 0, 2, 0.05, 0.0, RETURN
        ) == deployment_schedule_cost(100_000, 2, 0.05, 0.0, RETURN)
        assert year0_deployment_cost(100_000, 0, 1, 0.05) == 0.0


# ============================================================================
# 2. The engine prices it (both folds)
# ============================================================================

class TestEnginePricesStaggeredDeployment:
    """The projection must move when a schedule is declared, and the reported
    cost must match the pure function."""

    def test_one_year_schedule_is_byte_identical_to_no_dimension(self):
        # A 1-year schedule deploys the whole lump at year 0, so it must be
        # byte-identical to a run that declares NO schedule dimension at all
        # (deployment_schedule_years=None -> the seam no-ops, DP#32). The
        # previous form compared _run(1) against _run(1) -- identical args --
        # which could never detect a regression in the no-dimension path.
        base = _run(1)
        assert base[0].deployment_lag_cost == 0.0
        assert repr(base[-1].total_assets) == repr(_run(None)[-1].total_assets)

    def test_longer_schedules_cost_more_terminal_wealth(self):
        terminals = {n: _run(n)[-1].total_assets for n in (1, 2, 3, 4)}
        assert all(terminals[n] > terminals[n + 1]
                   for n in (1, 2, 3)), terminals

    def test_reported_cost_matches_the_pure_function(self):
        rate = _blended_financing_rate()
        for n in (2, 3, 4):
            expected = deployment_schedule_cost(SURPLUS, n, rate, 0.0, RETURN)
            assert _run(n)[0].deployment_lag_cost == pytest.approx(expected), (
                f"year-0 reported cost must be the schedule's year-0-equivalent "
                f"reduction for N={n}")

    def test_deployed_principal_is_reduced_by_the_cost(self):
        base = _run(1)
        sched = _run(3)
        expected = deployment_schedule_cost(
            SURPLUS, 3, _blended_financing_rate(), 0.0, RETURN)
        gap = base[0].non_reg_balance - sched[0].non_reg_balance
        assert gap >= expected, f"cost {expected:,.2f} must appear as a reduction"
        assert gap <= expected * 1.2, (
            f"reduction {gap:,.2f} far exceeds the priced cost")

    def test_monthly_fold_prices_the_same_schedule(self):
        # Cross-engine agreement (#258): the monthly pre-step prices through
        # the SAME seam, so staggering costs there too (directionally; the
        # monthly compounding makes it not byte-equal to the yearly fold).
        # A mostly-advance split: the pure-revolver split trips a PRE-EXISTING
        # monthly-path gap (heloc_interest_servicing needs ctx.year_brackets;
        # reproduced on the base commit, unrelated to this issue).
        monthly_1 = _run(1, monthly=True, revolving_share=0.25)
        monthly_2 = _run(2, monthly=True, revolving_share=0.25)
        assert monthly_2[-1].total_assets < monthly_1[-1].total_assets, (
            "the monthly fold must price the stagger too")


class TestFamilySimulationRefusesAmbiguity:
    def test_lag_and_schedule_together_raise_eagerly(self):
        cfg = SimulationConfig.from_dict(_household())
        with pytest.raises(ValueError, match="Ambiguous deployment timing"):
            FamilySimulation(
                cfg, adapter=CanadaAdapter(cfg), use_readvanceable=False,
                deduct_later=False, lump_sum=SURPLUS,
                deployment_lag_months=6, deployment_schedule_years=2,
            )


# ============================================================================
# 3. The optimizer ranks it as a search dimension
# ============================================================================

class TestOptimizerSweepsTheSchedule:
    """Issue #74 / DP#22/#31: the optimizer RANKS deploy-over-1/2/3-year
    schedules when asked, and never sweeps them unless asked."""

    def test_schedule_sweep_produces_tagged_ranked_scenarios(self):
        from optimizer import GridOptimizer
        from objective import MAX_NET_BENEFIT
        from countries.canada.strategies import STRATEGY_BALANCED

        cfg = SimulationConfig.from_dict(_household())
        ranked = GridOptimizer(cfg).optimize(
            strategies=[STRATEGY_BALANCED],
            objective=MAX_NET_BENEFIT,
            income_overrides=[None],
            ltv_levels=[0.0],
            use_readvanceable_options=[False],
            deduct_later_options=[False],
            draw_fraction_options=[1.0],
            deployment_schedule_options=[1, 2, 3],
        )
        scheds = sorted(r.config_overrides['deployment_schedule_years']
                        for r in ranked)
        assert scheds == [1, 2, 3], (
            f"expected one scenario per schedule candidate, got {scheds}")
        by_sched = {r.config_overrides['deployment_schedule_years']: r
                    for r in ranked}
        assert by_sched[1].results[0].deployment_lag_cost == 0.0
        assert by_sched[2].results[0].deployment_lag_cost > 0.0
        assert by_sched[3].results[0].deployment_lag_cost > by_sched[2].results[0].deployment_lag_cost
        # Under the DETERMINISTIC ranker a stagger is priced as a pure cost
        # (its benefit is variance reduction — part (b), out of scope), so
        # deploying at year 0 must weakly dominate every schedule:
        best = ranked[0]
        assert best.config_overrides['deployment_schedule_years'] == 1, (
            "with no return variance to hedge, the lump must rank first")

    def test_default_sweep_is_single_scenario(self):
        from optimizer import GridOptimizer
        from objective import MAX_NET_BENEFIT
        from countries.canada.strategies import STRATEGY_BALANCED

        cfg = SimulationConfig.from_dict(_household())
        ranked = GridOptimizer(cfg).optimize(
            strategies=[STRATEGY_BALANCED],
            objective=MAX_NET_BENEFIT,
            income_overrides=[None],
            ltv_levels=[0.0],
            use_readvanceable_options=[False],
            deduct_later_options=[False],
            draw_fraction_options=[1.0],
        )
        assert len(ranked) == 1
        assert ranked[0].config_overrides['deployment_schedule_years'] == 1
        assert ranked[0].results[0].deployment_lag_cost == 0.0

    def test_grid_sweeping_both_timings_raises_loudly(self):
        from optimizer import GridOptimizer
        from objective import MAX_NET_BENEFIT
        from countries.canada.strategies import STRATEGY_BALANCED

        cfg = SimulationConfig.from_dict(_household())
        with pytest.raises(ValueError, match="Ambiguous deployment timing"):
            GridOptimizer(cfg).optimize(
                strategies=[STRATEGY_BALANCED],
                objective=MAX_NET_BENEFIT,
                income_overrides=[None],
                ltv_levels=[0.0],
                use_readvanceable_options=[False],
                deduct_later_options=[False],
                draw_fraction_options=[1.0],
                deployment_lag_options=[6],
                deployment_schedule_options=[2],
            )
