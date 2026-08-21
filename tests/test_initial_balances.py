"""Initial account balances must be loaded from the input, never silently dropped.

Bug: `SimState.initial` hardcoded `tfsa_primary_balance`/`tfsa_spouse_balance` to
0 and only read RRSP from the family member dict, so a config that declares its
balances under `portfolio.accounts.{rrsp,tfsa}.balance` — the shape the schema
documents — started the projection with $0 registered assets. A household with
$700k of registered savings was simulated as if it had none.

Registered accounts are legally *per person* (DP#4: role-based), so the canonical
home is `family.members[].rrsp_balance` / `.tfsa_balance`. Household totals given
under `portfolio.accounts` are allocated rather than dropped.

Fabricated round numbers, role-based names (DP#4, DP#15).
"""

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import (adult_rrsp_slot, adult_rrsp_total,
                             adult_tfsa_slot, adult_tfsa_total)  # #700: per-adult stores


def _base(members, portfolio=None):
    cfg = {
        'family': {'members': members},
        'assumptions': {'start_year': 2026, 'projection_years': 1,
                        'investment_return': 0.0, 'salary_growth': 0.0},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'margin_available': 0, 'heloc_readvance': False},
        'savings': {'rate': 0.0},
        'tax': {'province': 'qc', 'year': 2026},
    }
    if portfolio:
        cfg['portfolio'] = portfolio
    return cfg


def _canada_state(cfg):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim._state.jurisdiction_state['canada']


def test_member_rrsp_balance_seeds_initial_state():
    st = _canada_state(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'rrsp_balance': 500_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
         'rrsp_balance': 100_000},
    ]))
    assert adult_rrsp_slot(st, 0)[0] == 500_000
    assert adult_rrsp_slot(st, 1)[0] == 100_000


def test_member_tfsa_balance_seeds_initial_state():
    """Regression: TFSA opening balances were hardcoded to 0."""
    st = _canada_state(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'tfsa_balance': 40_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
         'tfsa_balance': 60_000},
    ]))
    assert adult_tfsa_slot(st, 0)[0] == 40_000
    assert adult_tfsa_slot(st, 1)[0] == 60_000


def test_portfolio_account_balances_are_not_silently_dropped():
    """Household totals under portfolio.accounts must reach the simulation."""
    st = _canada_state(_base(
        [{'role': 'primary', 'birth_year': 1980, 'gross_income': 0},
         {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0}],
        portfolio={'accounts': {'rrsp': {'balance': 600_000},
                                'tfsa': {'balance': 100_000}}},
    ))
    total_rrsp = adult_rrsp_total(st)
    total_tfsa = adult_tfsa_total(st)
    assert total_rrsp == 600_000
    assert total_tfsa == 100_000


def test_member_balances_win_over_portfolio_totals():
    """Per-member (canonical) balances take precedence over household totals."""
    st = _canada_state(_base(
        [{'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
          'rrsp_balance': 500_000, 'tfsa_balance': 10_000},
         {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
          'rrsp_balance': 0, 'tfsa_balance': 90_000}],
        portfolio={'accounts': {'rrsp': {'balance': 999_999},
                                'tfsa': {'balance': 999_999}}},
    ))
    assert adult_rrsp_slot(st, 0)[0] == 500_000
    assert adult_tfsa_slot(st, 0)[0] == 10_000
    assert adult_tfsa_slot(st, 1)[0] == 90_000


def test_no_balances_starts_at_zero():
    """Absence of data is the only way to start empty (DP#16)."""
    st = _canada_state(_base(
        [{'role': 'primary', 'birth_year': 1980, 'gross_income': 0}]))
    assert adult_rrsp_total(st) == 0
    assert adult_tfsa_total(st) == 0
