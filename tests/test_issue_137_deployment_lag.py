"""Issue #137: the engine prices deployment lag (cash drag).

Before #137 a borrowed lump sum deployed at year 0 with no idle-cash carry:
the refinance's lump (a HELOC margin draw plus a mortgage cash-out) was
invested in the same simulated year, so a household that took 3 months to
move the money got a projection **byte-identical** to one that moved it the
same day. The delay's cost was silently modelled as $0 (DP#32).

This pins the fix: a declared `deployment_lag_months` prices the spread
between the (blended) financing rate and the idle parking rate over the lag,
reduces the deployed lump accordingly, and surfaces the dollar cost on
`YearResult.deployment_lag_cost`. Absent or zero lag stays byte-identical
(DP#32).
"""

import pytest

from deployment_lag import (
    deployment_lag_years,
    deployment_carry_cost,
    lump_financing_rate,
)
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter

HOUSE_VALUE = 800_000
CHARGE = int(0.80 * HOUSE_VALUE)  # OSFI B-20 charge
SURPLUS = 480_000
ADVANCE_RATE = 0.0370  # amortizing, cheaper
LINE_RATE = 0.0470  # revolving, interest-only, dearer


def _household(revolving_share: float) -> dict:
    """Config identical in shape to the #850 fixture: no registered room, so
    the whole borrowed surplus lands in non-reg (income-producing, hence
    s.20(1)(c)-qualifying)."""
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
            'investment_return': 0.06, 'salary_growth': 0.0,
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


def _run(lag_months: int, idle_rate: float = 0.0, revolving_share: float = 0.5,
         monthly: bool = False):
    cfg_dict = _household(revolving_share)
    if monthly:
        cfg_dict['assumptions']['time_step'] = 'monthly'
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(
        cfg, adapter=CanadaAdapter(cfg), use_readvanceable=False,
        deduct_later=False, lump_sum=SURPLUS,
        deployment_lag_months=lag_months, deployment_idle_rate=idle_rate,
    )
    return sim.run()


# ============================================================================
# 1. The optimizer exposes the lag as a sweepable search dimension
# ============================================================================

class TestOptimizerSweepsTheLag:
    """Issue #137 / DP#22/#31: the optimizer RANKS 0/3/6/12 months of delay,
    it does not silently hardcode one. A caller that asks for the lag sweep
    gets one ranked scenario per candidate, tagged with its month count."""

    def test_lag_sweep_produces_tagged_scenarios(self):
        from optimizer import GridOptimizer
        from objective import MAX_NET_BENEFIT
        from countries.canada.strategies import STRATEGY_BALANCED

        cfg = SimulationConfig.from_dict(_household(0.5))
        ranked = GridOptimizer(cfg).optimize(
            strategies=[STRATEGY_BALANCED],
            objective=MAX_NET_BENEFIT,
            income_overrides=[None],
            ltv_levels=[0.0],
            use_readvanceable_options=[False],
            deduct_later_options=[False],
            draw_fraction_options=[1.0],
            deployment_lag_options=[0, 3, 6],
        )
        lags = sorted(r.config_overrides['deployment_lag_months'] for r in ranked)
        assert lags == [0, 3, 6], f"expected one scenario per lag candidate, got {lags}"
        by_lag = {r.config_overrides['deployment_lag_months']: r for r in ranked}
        assert by_lag[0].results[0].deployment_lag_cost == 0.0
        assert by_lag[3].results[0].deployment_lag_cost > 0.0
        assert by_lag[6].results[0].deployment_lag_cost > by_lag[3].results[0].deployment_lag_cost
        assert ranked[0].results[0].deployment_lag_cost == min(
            r.results[0].deployment_lag_cost for r in ranked), (
            "a household that moves the money immediately cannot be ranked worse "
            "than the same household that dawdles — the lag is a pure cost")


# ============================================================================
# 2. The pure cost function
# ============================================================================


class TestCarryCostIsPure:
    """DP#3/#32: the carry is a pure function of the lump, the lag, and the
    two rates; absent or zero lag is a hard zero, never a fabricated cost."""

    def test_absent_lag_is_zero(self):
        assert deployment_carry_cost(100_000, 0, 0.05, 0.0) == 0.0
        assert deployment_carry_cost(100_000, None, 0.05, 0.0) == 0.0
        assert deployment_carry_cost(0, 3, 0.05, 0.0) == 0.0

    def test_zero_idle_rate_matches_issue_headline(self):
        # $100k at 5% over 3 months, parked at 0% -> ~$410/mo, $1,250 for the
        # quarter (the issue's exact round-number grab).
        assert deployment_carry_cost(100_000, 3, 0.05, 0.0) == pytest.approx(1_250.0)
        assert deployment_carry_cost(100_000, 6, 0.05, 0.0) == pytest.approx(2_500.0)

    def test_idle_rate_offsets_the_spread(self):
        # Park the proceeds in a 3% HISA while they wait: the spread drops to
        # 2%, so the quarter's carry is $500, not $1,250.
        assert deployment_carry_cost(100_000, 3, 0.05, 0.03) == pytest.approx(500.0)

    def test_lag_months_to_years(self):
        assert deployment_lag_years(0) == 0.0
        assert deployment_lag_years(None) == 0.0
        assert deployment_lag_years(3) == pytest.approx(0.25)
        assert deployment_lag_years(12) == pytest.approx(1.0)

    def test_blended_financing_rate(self):
        # Pure revolving draw: the line's own rate.
        assert lump_financing_rate(100_000, 100_000, LINE_RATE, ADVANCE_RATE) == pytest.approx(LINE_RATE)
        # Pure cash-out: the mortgage's own rate.
        assert lump_financing_rate(100_000, 0, LINE_RATE, ADVANCE_RATE) == pytest.approx(ADVANCE_RATE)
        # Half/half: balance-weighted blend.
        blended = lump_financing_rate(100_000, 50_000, LINE_RATE, ADVANCE_RATE)
        assert blended == pytest.approx(0.5 * LINE_RATE + 0.5 * ADVANCE_RATE)

    def test_nonpositive_lump_has_no_financing_to_price(self):
        # DP#32: a nonpositive lump has no financing to price and returns 0.0
        # — a hard zero, never a fabricated rate from a zero-division.
        assert lump_financing_rate(0, 100_000, LINE_RATE, ADVANCE_RATE) == 0.0
        assert lump_financing_rate(-1, 100_000, LINE_RATE, ADVANCE_RATE) == 0.0


# ============================================================================
# 3. The engine prices it
# ============================================================================

class TestEnginePricesDeploymentLag:
    """The projection must move when a lag is declared, and the reported cost
    must match the pure function."""

    def test_zero_lag_carries_no_cost(self):
        results = _run(0)
        assert results[0].deployment_lag_cost == 0.0

    def test_declared_lag_reduces_terminal_assets(self):
        base = _run(0)
        lag3 = _run(3)
        lag6 = _run(6)
        assert lag3[-1].total_assets < base[-1].total_assets, (
            "a 3-month deployment lag must price idle-cash drag")
        assert lag6[-1].total_assets < lag3[-1].total_assets, (
            "a longer lag must cost more")

    def test_year0_cost_surfaces_the_computed_carry(self):
        results = _run(3, revolving_share=0.5)
        cfg = SimulationConfig.from_dict(_household(0.5))
        rate = lump_financing_rate(
            SURPLUS, cfg.margin_available, LINE_RATE, ADVANCE_RATE)
        expected = deployment_carry_cost(SURPLUS, 3, rate, 0.0)
        assert results[0].deployment_lag_cost == pytest.approx(expected)

    def test_deployed_principal_is_reduced_by_the_carry(self):
        base = _run(0)
        lag3 = _run(3)
        cfg = SimulationConfig.from_dict(_household(0.5))
        rate = lump_financing_rate(SURPLUS, cfg.margin_available, LINE_RATE, ADVANCE_RATE)
        expected = deployment_carry_cost(SURPLUS, 3, rate, 0.0)
        # The deployed principal is reduced by the carry, AND the forgone
        # principal also stops compounding for part of the year — so the
        # end-of-year non-reg gap is the carry plus a fraction of the year's
        # growth on it. Assert the reduction is at least the carry and not
        # wildly more (growth-of-a-carry, not a doubled cost).
        gap = base[0].non_reg_balance - lag3[0].non_reg_balance
        assert gap >= expected, f"carry {expected:,.2f} must appear as a reduction"
        assert gap <= expected * 1.2, f"reduction {gap:,.2f} far exceeds the carry"