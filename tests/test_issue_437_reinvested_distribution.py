"""Issue #437: a reinvested portfolio distribution is taxed twice.

The declared distribution yield of a non-reg portfolio is taxed once, as
income, in the year it is earned (the after-tax distribution is what grows
the balance). Statutorily, interest or dividends received form part of the
cost of the securities purchased with them, so a later disposition realizes
the gain over the new cost -- not over the original outlay. ACB must therefore
accrete by the after-tax distribution reinvested each year; today it does not
(DP#19: cost basis grows only with contributions), so the second taxation
lands at withdrawal, at 50% inclusion.

Engine-driven: the fold is run for one year with a spending target below
government income, so the non-reg pot is grown but never disposed -- the
year's ACB move is then purely growth-side, with no drawdown in the way.
Fabricated round numbers, role-based names (DP#4/DP#15).
"""

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation, SimulationConfig

OPENING_BALANCE = 600_000
OPENING_ACB = 200_000


def _config(yield_pct):
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1959, 'gross_income': 0,
             'retirement_age': 65, 'cpp_monthly_estimated': 1_000,
             'oas_monthly_estimated': 716},
            {'role': 'spouse', 'birth_year': 1961, 'gross_income': 0,
             'retirement_age': 65, 'cpp_monthly_estimated': 900,
             'oas_monthly_estimated': 716},
        ]},
        'assumptions': {
            'start_year': 2026, 'projection_years': 1,
            'investment_return': 0.06, 'salary_growth': 0.0,
            'inflation': 0.02, 'frozen_brackets': True,
        },
        'portfolio': {'accounts': {'non_reg': {
            'balance': OPENING_BALANCE, 'cost_basis': OPENING_ACB,
            'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
            'yield': {'interest': yield_pct},
        }}},
        'savings': {'rate': 0.0},
        # Below government income: the pot is grown, never drawn.
        'retirement': {'spending_target': 10_000, 'rrif_conversion_age': 71},
        'tax': {'province': 'qc'},
    }


def _run(yield_pct):
    sim_cfg = SimulationConfig.from_dict(_config(yield_pct))
    return FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg)).run()


def test_no_distribution_leaves_acb_untouched():
    # 0% declared yield: no income distributed, nothing to accrete.
    results = _run(0.0)
    assert results[0].non_reg_acb == pytest.approx(OPENING_ACB)


def test_reinvested_after_tax_distribution_accretes_to_acb():
    # 4% declared interest on a 6% gross return: the pot receives the
    # after-tax interest in cash and reinvests it. That reinvested amount
    # is new money in, i.e. cost -- the year-end ACB must exceed the
    # opening ACB by (at least) the after-tax distribution retained.
    results = _run(0.04)
    assert results[0].non_reg_acb > OPENING_ACB
