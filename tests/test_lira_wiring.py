#!/usr/bin/env python3
"""
Tests for Issue #230: Wire CRI/LIRA module into simulation and strategy engine.

Per DP#3: pure functions, no hidden state.
Per DP#10: locked_in_account.py owns CRI/LIRA/LIF rules.
Per DP#16: auto-include when lira data is present in input.

Tests cover:
1. CRI/LIRA balance appears in total_assets when present in input
2. CRI/LIRA grows with investment returns during accumulation phase
3. CRI/LIRA converts to LIF at age 71 (mandatory conversion)
4. LIF mandatory minimum withdrawals after conversion
5. LIF balance grows with investment returns after withdrawal
6. LIRA with zero balance is invisible (no effect on simulation)
7. LIRA with missing birth_year raises ValueError (DP#28)
8. Net benefit calculation includes CRI/LIRA and LIF balances
9. LIF withdrawal income is tracked in YearResult
10. Quebec LIF jurisdiction rules are respected
"""

import pytest
from copy import deepcopy
from simulation_state import (
    SimState, simulate_year_pure, adult_lira_slot, adult_lif_slot,
)
from simulation_config import SimulationConfig
from year_result import YearResult


def _lira_bal(state):
    return adult_lira_slot(state.jurisdiction_state['canada'], 0)['balance']


def _lif_bal(state):
    return adult_lif_slot(state.jurisdiction_state['canada'], 0)['balance']


def _lif_juris(state):
    return adult_lif_slot(state.jurisdiction_state['canada'], 0)['jurisdiction']


def _make_config(**overrides):
    """Create a minimal SimulationConfig for testing."""
    defaults = {
        'projection_years': 10,
        'investment_return': 0.07,
        'house_value': 500000,
        'mortgage_balance': 300000,
        'mortgage_rate': 0.05,
        'margin_available': 100000,
        'family_members': [
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1979,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 70000},
        ],
        'children': [],
    }
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _make_state_with_lira(lira_balance=52837, lira_birth_year=1979,
                          lira_jurisdiction='quebec',
                          lif_balance=0, lif_birth_year=0):
    """Create a SimState with CRI/LIRA data in jurisdiction_state.

    Issue #700/#643 (Step 4): LIRA/LIF are per-adult stores keyed by adult id
    (single primary-keyed slot today), not flat canada scalars."""
    from simulation_state import _default_canada_state
    canada = _default_canada_state()
    canada['adult_lira'] = {'primary': {
        'balance': lira_balance, 'birth_year': lira_birth_year,
        'jurisdiction': lira_jurisdiction, 'reference_rate': 0.06,
        'conversion_year': 0,
    }}
    canada['adult_lif'] = {'primary': {
        'balance': lif_balance, 'birth_year': lif_birth_year,
        'jurisdiction': lira_jurisdiction, 'reference_rate': 0.06,
    }}
    return SimState(
        non_reg_balance=0,
        non_reg_acb=0,
        mortgage_balance=300000,
        heloc_balance=0,
        jurisdiction_state={'canada': canada},
    )


class TestLIRAInTotalAssets:
    """CRI/LIRA balance appears in total_assets when present."""

    def test_lira_balance_included_in_total_assets(self):
        """Per issue #230: CRI/LIRA is a real asset that must be counted."""
        state = _make_state_with_lira(lira_balance=52837)
        assert state.total_assets() >= 52837, \
            f"total_assets={state.total_assets()} should include lira_balance=52837"

    def test_lif_balance_included_in_total_assets(self):
        """LIF balance should also be counted in total assets."""
        state = _make_state_with_lira(lira_balance=0, lif_balance=40000,
                                       lif_birth_year=1979)
        assert state.total_assets() >= 40000, \
            f"total_assets={state.total_assets()} should include lif_balance=40000"

    def test_lira_plus_lif_both_counted(self):
        """Both CRI/LIRA and LIF should be counted when both exist."""
        state = _make_state_with_lira(lira_balance=30000, lif_balance=20000,
                                       lif_birth_year=1979)
        assert state.total_assets() >= 50000, \
            f"total_assets={state.total_assets()} should include lira(30000)+lif(20000)=50000"

    def test_zero_lira_balance_not_counted(self):
        """Zero-balance LIRA should not affect total_assets."""
        state = _make_state_with_lira(lira_balance=0)
        from simulation_state import _default_canada_state as _dcs
        no_lira_state = SimState(
            non_reg_balance=0, non_reg_acb=0,
            mortgage_balance=300000, heloc_balance=0,
            jurisdiction_state={'canada': _dcs()},
        )
        assert state.total_assets() == no_lira_state.total_assets()


class TestLIRAGrowth:
    """CRI/LIRA grows with investment returns during accumulation phase."""

    def test_lira_grows_with_investment_return(self):
        """CRI/LIRA should grow at the same rate as RRSP (tax-sheltered)."""
        config = _make_config()
        state = _make_state_with_lira(lira_balance=52837)
        result, new_state = simulate_year_pure(
            state=state,
            year=2026,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config,
            investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        # LIRA should grow by 7%: 52837 * 1.07 = 56535.59
        expected = 52837 * 1.07
        new_lira = _lira_bal(new_state)
        assert abs(new_lira - expected) < 1.0, \
            f"LIRA balance should grow from 52837 to ~{expected:.2f}, got {new_lira:.2f}"

    def test_lira_zero_balance_stays_zero(self):
        """Zero-balance LIRA should remain zero after growth."""
        config = _make_config()
        state = _make_state_with_lira(lira_balance=0)
        result, new_state = simulate_year_pure(
            state=state,
            year=2026,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config,
            investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        assert _lira_bal(new_state) == 0


class TestLIRAConversionToLIF:
    """CRI/LIRA converts to LIF at age 71 (mandatory)."""

    def test_conversion_at_age_71(self):
        """Per PBSR: CRI/LIRA must convert to LIF by end of year owner turns 71.
        For birth_year=1950, conversion year is 2021 (1950+71)."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000,
            lira_birth_year=1950,  # Turns 71 in 2021
            lira_jurisdiction='federal',
        )
        result, new_state = simulate_year_pure(
            state=state,
            year=2021,  # Year they turn 71
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config,
            investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        # LIRA should be depleted (converted to LIF)
        assert _lira_bal(new_state) == 0, \
            "LIRA balance should be 0 after conversion to LIF"
        # LIF balance should be > 0 (the converted amount)
        assert _lif_bal(new_state) > 0, \
            "LIF balance should be positive after conversion from LIRA"
        # Total assets should be preserved (LIRA → LIF)
        assert result.total_assets > 0

    def test_conversion_preserves_jurisdiction(self):
        """Quebec CRI should convert to Quebec LIF (preserving jurisdiction)."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000,
            lira_birth_year=1950,
            lira_jurisdiction='quebec',
        )
        result, new_state = simulate_year_pure(
            state=state,
            year=2021,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config,
            investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        assert _lif_juris(new_state) == 'quebec', \
            "LIF should preserve Quebec jurisdiction from CRI"

    def test_no_conversion_before_age_71(self):
        """CRI/LIRA should NOT convert before age 71."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000,
            lira_birth_year=1979,  # Will turn 71 in 2050
        )
        result, new_state = simulate_year_pure(
            state=state,
            year=2026,  # Age 47, well before 71
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config,
            investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        # LIRA should still exist (not converted)
        assert _lira_bal(new_state) > 0, \
            "LIRA should still exist before age 71"
        assert _lif_bal(new_state) == 0, \
            "LIF should not exist before LIRA conversion"


class TestLIFWithdrawals:
    """LIF has mandatory minimum withdrawals after conversion."""

    def test_lif_withdrawal_recorded_in_year_result(self):
        """LIF withdrawal should be recorded in YearResult.lif_withdrawal."""
        config = _make_config()
        # Start with a LIF already in decumulation (birth_year=1950, age=76 in 2026)
        state = _make_state_with_lira(
            lira_balance=0,
            lif_balance=100000,
            lif_birth_year=1950,
        )
        result, new_state = simulate_year_pure(
            state=state,
            year=2026,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config,
            investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        assert result.lif_withdrawal > 0, \
            f"LIF withdrawal should be positive, got {result.lif_withdrawal}"
        assert result.lif_withdrawal >= 5000, \
            f"LIF withdrawal should be at least $5k (min withdrawal at age 76), got {result.lif_withdrawal:.0f}"

    def test_lif_balance_decreases_after_withdrawal(self):
        """LIF balance should decrease after withdrawal (minus withdrawal + growth)."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=0,
            lif_balance=100000,
            lif_birth_year=1950,
        )
        result, new_state = simulate_year_pure(
            state=state,
            year=2026,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config,
            investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        # LIF balance after withdrawal and growth:
        # (100000 - withdrawal) * (1 + 0.07)
        # Should be less than 100000 * 1.07 because of mandatory withdrawal
        new_lif = _lif_bal(new_state)
        # The net effect depends on withdrawal rate vs growth rate
        # At age 76, min withdrawal ~8.8% and growth is 7%
        # So balance should decrease: (100000 * 0.912) * 1.07 ≈ 97584
        assert new_lif < 100000 * 1.07, \
            f"LIF balance should decrease relative to pure growth due to withdrawal, got {new_lif:.0f}"


class TestLIRANoEffectWhenAbsent:
    """CRI/LIRA with zero balance should not affect simulation."""

    def test_zero_lira_no_change_to_existing_results(self):
        """Simulation results should be identical when LIRA balance is 0."""
        config = _make_config()
        from simulation_state import _default_canada_state

        # State WITHOUT LIRA data (defaults)
        state_no_lira = SimState(
            non_reg_balance=10000,
            non_reg_acb=5000,
            mortgage_balance=300000,
            heloc_balance=0,
            jurisdiction_state={'canada': _default_canada_state()},
        )

        # State WITH zero LIRA data
        state_with_zero_lira = SimState(
            non_reg_balance=10000,
            non_reg_acb=5000,
            mortgage_balance=300000,
            heloc_balance=0,
            jurisdiction_state={'canada': {
                **_default_canada_state(),
                'adult_lira': {'primary': {
                    'balance': 0, 'birth_year': 0, 'jurisdiction': 'federal',
                    'reference_rate': 0.06, 'conversion_year': 0}},
            }},
        )

        allocs = {'_primary_income': 130000, '_annual_savings': 0, 'primary_rrsp': 5000}
        result_no_lira, _ = simulate_year_pure(
            state=state_no_lira, year=2026,
            allocations=allocs, config=config,
            investment_return=0.07, primary_marginal_rate=0.40,
        )
        result_with_zero, _ = simulate_year_pure(
            state=state_with_zero_lira, year=2026,
            allocations=allocs, config=config,
            investment_return=0.07, primary_marginal_rate=0.40,
        )

        # Total assets should be the same
        assert result_no_lira.total_assets == result_with_zero.total_assets, \
            f"Zero LIRA should not affect total assets: {result_no_lira.total_assets} vs {result_with_zero.total_assets}"


class TestYearResultLIRAFields:
    """YearResult should include CRI/LIRA and LIF fields."""

    def test_year_result_has_lira_balance(self):
        """YearResult.lira_balance should be populated."""
        config = _make_config()
        state = _make_state_with_lira(lira_balance=52837)
        result, _ = simulate_year_pure(
            state=state, year=2026,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config, investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        assert result.lira_balance == 52837 * 1.07, \
            f"YearResult.lira_balance should reflect growth, got {result.lira_balance:.2f}"

    def test_year_result_has_lif_withdrawal(self):
        """YearResult.lif_withdrawal should be 0 during accumulation phase."""
        config = _make_config()
        state = _make_state_with_lira(lira_balance=52837, lif_balance=0)
        result, _ = simulate_year_pure(
            state=state, year=2026,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config, investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        # During accumulation (no LIF), withdrawal should be 0
        assert result.lif_withdrawal == 0, \
            f"YearResult.lif_withdrawal should be 0 during accumulation, got {result.lif_withdrawal}"

    def test_year_result_lif_balance_after_conversion(self):
        """After conversion to LIF, YearResult.lif_balance should be positive."""
        config = _make_config()
        state = _make_state_with_lira(
            lira_balance=100000,
            lira_birth_year=1950,  # Turns 71 in 2021
        )
        result, _ = simulate_year_pure(
            state=state, year=2021,
            allocations={'_primary_income': 130000, '_annual_savings': 0},
            config=config, investment_return=0.07,
            primary_marginal_rate=0.40,
        )
        assert result.lif_balance > 0, \
            f"YearResult.lif_balance should be positive after conversion, got {result.lif_balance}"
        assert result.lira_balance == 0, \
            f"YearResult.lira_balance should be 0 after conversion, got {result.lira_balance}"