#!/usr/bin/env python3
"""Issue #754: model realized capital gains in the fold.

The non-registered pot grows and is drawn, but until now a DISPOSITION never
surfaced a realized capital gain (proceeds - ACB) that downstream tax -- the
50% inclusion, and later AMT (#710, ITA s.127.52(1)(d): 100% inclusion) -- can
read. The gain WAS already taxed (bundled into the drawdown's taxable income at
the 50% inclusion); what was missing is the raw realized-gain figure, surfaced
per year.

This models it: a non-reg draw realizes `drawn * (FMV - ACB)/FMV`, the ACB is
tracked down across the horizon, and the year's realized capital gain is
surfaced on YearResult -- taxable at the capital-gains inclusion rate.

Lean, single-responsibility, invariant-based (DP#3). Round numbers, no PII.

Run: uv run pytest tests/test_issue_754_realized_capital_gains.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.retirement_transition import plan_drawdown_net
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation, SimulationConfig


# ---------------------------------------------------------------------------
# 1. Realization math: a non-reg disposition realizes proceeds - ACB, and the
#    realized gain is taxed at the capital-gains inclusion rate.
# ---------------------------------------------------------------------------
class TestDrawdownRealizesCapitalGain:
    def test_realized_gain_is_proceeds_minus_acb(self):
        # $200k FMV over $100k ACB => 50% of every dollar drawn is gain.
        # cg_inclusion 0.5, marginal 40% => each gross $1 delivers
        # 1 - (0.5 gain frac)*(0.5 incl)*(0.40 rate) = 1 - 0.10 = $0.90 net.
        # $45k net therefore draws $50k gross; the realized gain is 50% of that.
        plan = plan_drawdown_net(
            45_000, ['non_reg'], {}, non_reg_balance=200_000,
            non_reg_acb=100_000, marginal_rate=0.40, cg_inclusion=0.5)
        assert plan.total_withdrawn == pytest.approx(50_000)
        # Realized capital gain = proceeds - ACB portion = drawn * gain_frac.
        assert plan.realized_capital_gain == pytest.approx(25_000)
        # And it is taxed at the 50% inclusion: the taxable slice booked into
        # ordinary income is exactly cg_inclusion x the realized gain.
        assert plan.taxable_withdrawn == pytest.approx(12_500)
        assert plan.taxable_withdrawn == pytest.approx(
            plan.realized_capital_gain * 0.5)

    def test_no_gain_when_acb_equals_fmv(self):
        # ACB == FMV: a draw is a pure return of capital, zero realized gain.
        plan = plan_drawdown_net(
            30_000, ['non_reg'], {}, non_reg_balance=100_000,
            non_reg_acb=100_000, marginal_rate=0.40, cg_inclusion=0.5)
        assert plan.total_withdrawn == pytest.approx(30_000)
        assert plan.realized_capital_gain == pytest.approx(0.0)
        assert plan.taxable_withdrawn == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 2. Fold: the year's realized capital gain is surfaced on YearResult and the
#    ACB is tracked DOWN across a disposition year.
# ---------------------------------------------------------------------------
def _config(projection_years, non_reg_balance, non_reg_cost_basis,
            spending_target=90_000, primary_birth=1959, spouse_birth=1961):
    """A household that reaches retirement with an embedded non-reg gain and
    a spending target its government income cannot cover -- so the drawdown
    disposes of non-reg (drawdown_order default: tfsa -> non_reg -> rrsp)."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': primary_birth, 'gross_income': 120_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 1_000},
                {'role': 'spouse', 'birth_year': spouse_birth, 'gross_income': 80_000,
                 'retirement_age': 65, 'cpp_monthly_estimated': 900},
            ],
        },
        'assumptions': {
            'start_year': 2026,
            'projection_years': projection_years,
            'investment_return': 0.05,
            'salary_growth': 0.02,
            'inflation': 0.02,
            'frozen_brackets': True,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': non_reg_balance, 'cost_basis': non_reg_cost_basis,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.015, 'interest': 0.01},
                },
            },
        },
        'savings': {'rate': 0.10},
        'retirement': {'spending_target': spending_target, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
    }


def _run(cfg):
    sim_cfg = SimulationConfig.from_dict(cfg)
    return FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg)).run()


class TestFoldSurfacesRealizedGain:
    def test_disposition_surfaces_gain_and_tracks_acb_down(self):
        # Big non-reg pot ($600k) with $200k ACB -> a real embedded gain drawn
        # down once the household retires into a spending target CPP can't meet.
        results = _run(_config(15, non_reg_balance=600_000,
                               non_reg_cost_basis=200_000))

        # The year's realized capital gain is a surfaced fold figure.
        gain_years = [i for i, r in enumerate(results)
                      if r.realized_capital_gains > 0]
        assert gain_years, "no realized capital gain ever surfaced in the fold"

        # ACB is tracked DOWN across a disposition: in a year that realizes a
        # gain by drawing non-reg, the end-of-year ACB is below the prior year's
        # (cost base leaves the account with the dollars withdrawn).
        assert any(results[i].non_reg_acb < results[i - 1].non_reg_acb
                   for i in gain_years if i > 0), \
            "ACB never fell across a realizing disposition"

        # It is never negative, and the balance/ACB identity holds every year.
        for r in results:
            assert r.realized_capital_gains >= 0.0
            assert r.non_reg_unrealized_gains == pytest.approx(
                r.non_reg_balance - r.non_reg_acb)


# ---------------------------------------------------------------------------
# 3. Absence is a no-op: a pure pre-retirement horizon (the non-reg pot only
#    grows, never disposed) surfaces zero realized capital gain every year.
# ---------------------------------------------------------------------------
class TestAbsenceIsNoOp:
    def test_pre_retirement_horizon_realizes_nothing(self):
        # Members retire at 65 but the horizon stops well before -- the non-reg
        # account only accumulates; no disposition, no realized gain.
        results = _run(_config(8, non_reg_balance=300_000,
                               non_reg_cost_basis=150_000,
                               primary_birth=1985, spouse_birth=1987))
        assert all(not r.any_retired for r in results)
        assert all(r.realized_capital_gains == 0.0 for r in results)
