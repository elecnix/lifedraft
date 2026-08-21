"""RRIF minimum-withdrawal enforcement in the year-by-year engine.

Regression for the pre-fix behaviour where the RRSP was drawn only for the
spending need and grew unbounded through the 70s-90s (a large RRSP compounded to
several times its starting value by 95), ignoring the mandatory RRIF minimum
required from age 71 (CRA T4040).

Fabricated round numbers and role-based names only (DP#4, DP#15).
"""

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig

START_YEAR = 2026
PRIMARY_BIRTH = 1960          # age 66 at start — retired, RRIF age reached in-horizon
SPOUSE_BIRTH = 1960


def _config(**overrides):
    """A retired couple with a large RRSP, drawing a modest net spend."""
    cfg = {
        'family': {'members': [
            {'role': 'primary', 'birth_year': PRIMARY_BIRTH, 'gross_income': 0,
             'retirement_age': 65, 'cpp_monthly_estimated': 1000,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            {'role': 'spouse', 'birth_year': SPOUSE_BIRTH, 'gross_income': 0,
             'retirement_age': 65, 'cpp_monthly_estimated': 1000,
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
        ]},
        'assumptions': {
            'start_year': START_YEAR,
            'horizon_age': 95,
            'investment_return': 0.05,
            'salary_growth': 0.0,
            'frozen_brackets': True,
        },
        'portfolio': {'accounts': {
            'rrsp': {'balance': 1_000_000},
            'tfsa': {'balance': 0},
            'non_reg': {'balance': 0, 'cost_basis': 0},
        }},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'margin_available': 0, 'heloc_readvance': False},
        'retirement': {
            'spending_target': 40_000,
            'drawdown_order': ['rrsp'],
            'rrif_conversion_age': 71,
        },
        'tax': {'province': 'qc', 'year': 2026},
    }
    cfg['assumptions'].update(overrides.pop('assumptions', {}))
    cfg.update(overrides)
    return cfg


def _run(cfg):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


def _age(index):
    return (START_YEAR + index) - PRIMARY_BIRTH


def test_rrsp_decumulates_instead_of_ballooning():
    """With RRIF minimums enforced, a modest spend cannot leave the RRSP growing.

    Pre-fix, a $1M RRSP drawn only for a $40k need grew every year (5% > draw).
    The mandatory minimum forces it down.
    """
    res = _run(_config())
    final = res[-1].total_rrsp
    peak = max(r.total_rrsp for r in res)
    assert final < peak            # forced decumulation, not monotonic growth
    assert final < 1_000_000       # below the starting balance by the horizon


def test_minimum_is_withdrawn_even_when_spending_is_covered():
    """RRIF minimum is mandatory: it is drawn even with a $0 spending need."""
    res = _run(_config(retirement={
        'spending_target': 0,
        'drawdown_order': ['rrsp'],
        'rrif_conversion_age': 71,
    }))
    post_71 = [r for i, r in enumerate(res) if _age(i) >= 72]
    assert all(r.drawdown_taxable > 0 for r in post_71)


def test_no_forced_withdrawal_before_conversion_age():
    """Threshold rule path (DP#17): below the RRIF conversion age nothing is forced."""
    res = _run(_config(retirement={
        'spending_target': 0,
        'drawdown_order': ['rrsp'],
        'rrif_conversion_age': 71,
    }))
    pre_71 = [r for i, r in enumerate(res) if _age(i) < 71]
    assert all(r.drawdown_taxable == 0 for r in pre_71)


def test_forced_excess_is_reinvested_after_tax_in_non_reg():
    """Unspent forced minimum lands in the taxable account, net of tax (not lost)."""
    res = _run(_config(retirement={
        'spending_target': 0,
        'drawdown_order': ['rrsp'],
        'rrif_conversion_age': 71,
    }))
    assert res[-1].non_reg_balance > 0
    # ACB tracks the reinvested after-tax capital (DP#19).
    assert res[-1].non_reg_acb > 0
