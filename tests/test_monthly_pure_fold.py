"""Tests for DP#26 compliance: monthly simulation path uses SimState pure-function fold.

The monthly path (_run_monthly) must thread SimState through each year,
ensuring:
1. ACB tracking works correctly in monthly mode
2. HELOC tracing advances are recorded
3. Quebec deduction carry-forward is propagated between years
4. Monthly and yearly modes produce comparable results
5. Spousal RRSP attribution is tracked in monthly mode
6. RRSP per-contribution ledger (deduct-later) works in monthly mode

These tests verify the fix for issue #20.
"""

import pytest
from copy import deepcopy
from simulation_config import SimulationConfig, YearResult
from simulation_state import SimState, simulate_year_pure


def _make_config(time_step='yearly', projection_years=5, mortgage_balance=300000,
                 margin_available=50000, use_readvanceable=True, deduct_later=False,
                 savings_rate=0.20):
    """Create a SimulationConfig for testing.

    primary_income and spouse_income are derived from family_members.
    use_readvanceable and deduct_later are FamilySimulation constructor params.
    """
    return SimulationConfig(
        projection_years=projection_years,
        start_year=2025,
        savings_rate=savings_rate,
        # issue #681: this fixture used to run a READVANCEABLE mortgage (and a
        # $300k mortgage balance) against no declared property at all --
        # house_value defaulted to 0. A readvanceable line is a claim on the
        # charge registered against a property; with no property there is no
        # charge and no knowable advanceable room, and the rule now refuses
        # rather than silently advancing an unbounded amount (DP#32). A
        # fabricated $800k house makes the household coherent: $800k x 80%
        # charge = $640k, comfortably above the $300k mortgage + $50k margin.
        house_value=800000,
        mortgage_balance=mortgage_balance,
        mortgage_rate=0.05,
        time_step=time_step,
        margin_available=margin_available,
        family_members=[
            {'role': 'primary', 'birth_year': 1979, 'rrsp_balance': 80000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000,
             'fhsa_room_accumulated': 8000, 'fhsa_lifetime_limit': 40000,
             'gross_income': 130000},
            {'role': 'spouse', 'birth_year': 1980, 'rrsp_balance': 30000,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 40000,
             'gross_income': 50000},
        ],
        children=[],
    )


def _make_sim(config, use_readvanceable=True, deduct_later=False):
    """Create a FamilySimulation with the given parameters."""
    from simulation import FamilySimulation
    return FamilySimulation(
        config,
        use_readvanceable=use_readvanceable,
        deduct_later=deduct_later,
    )


class TestMonthlyModeUsesSimState:
    """Verify that monthly mode uses SimState (pure-function fold)."""

    def test_monthly_mode_tracks_acb(self):
        """DP#26: Monthly mode must track non-reg ACB through SimState.

        The monthly path previously mutated NonRegAccount directly, which
        meant ACB was tracked through the mutable object. With SimState,
        ACB must be tracked in SimState.non_reg_acb.
        """
        config = _make_config(time_step='monthly', projection_years=3)
        sim = _make_sim(config, use_readvanceable=True)
        results = sim.run()

        # After contributions and growth, ACB should be positive and ≤ balance
        for r in results:
            assert r.non_reg_acb >= 0, f"Year {r.year}: ACB should be non-negative, got {r.non_reg_acb}"
            assert r.non_reg_acb <= r.non_reg_balance + 1, \
                f"Year {r.year}: ACB ({r.non_reg_acb}) should not exceed balance ({r.non_reg_balance})"

    def test_monthly_mode_tracks_heloc_tracing(self):
        """DP#26: Monthly mode must track HELOC tracing through SimState.

        The HELOC tracing records which advances were for investment purposes
        (deductible) vs. personal (non-deductible). Monthly mode must
        accumulate these advances correctly.
        """
        config = _make_config(time_step='monthly', projection_years=3, use_readvanceable=True)
        sim = _make_sim(config, use_readvanceable=True)
        results = sim.run()

        # If Smith Manoeuvre is active, HELOC advances should be recorded
        assert len(results) > 0
        for r in results:
            if r.readvance_interest > 0:
                assert 0 <= r.sm_deductible_proportion <= 1, \
                    f"Year {r.year}: deductible proportion should be 0-1, got {r.sm_deductible_proportion}"

    def test_monthly_mode_tracks_qc_carry_forward(self):
        """DP#26: Monthly mode must propagate Quebec deduction carry-forward between years.

        The QC carry-forward allows deducting SM interest in future years when
        current-year income doesn't absorb it. Monthly mode must carry this
        forward correctly.
        """
        config = _make_config(time_step='monthly', projection_years=5, use_readvanceable=True)
        sim = _make_sim(config, use_readvanceable=True)
        results = sim.run()

        # QC carry-forward should be non-negative and propagated
        for r in results:
            assert r.sm_qc_carry_forward >= 0, \
                f"Year {r.year}: QC carry-forward should be non-negative, got {r.sm_qc_carry_forward}"

    def test_monthly_mode_tracks_rrsp_ledger(self):
        """DP#26: Monthly mode must track RRSP contributions in the per-contribution ledger.

        The deduct-later feature relies on per-contribution tracking.
        Monthly mode must use SimState.rrsp_ledger for this.
        """
        config = _make_config(time_step='monthly', projection_years=3)
        sim = _make_sim(config, deduct_later=True)
        results = sim.run()

        # With deduct-later, RRSP tax savings should reflect bracket-aware deductions
        assert len(results) > 0
        for r in results:
            # RRSP tax savings should be computed even in deduct-later mode
            assert r.rrsp_tax_savings >= 0, \
                f"Year {r.year}: RRSP tax savings should be non-negative"

    def test_monthly_mode_spousal_rrsp_attribution(self):
        """DP#26: Monthly mode must track spousal RRSP attribution years.

:        Spousal RRSP contributions are tracked in jurisdiction_state['canada']['spousal_contribution_years']
        for the 3-year attribution window (ITA s.146(8.3)).
        """
        config = _make_config(time_step='monthly', projection_years=5)
        sim = _make_sim(config)
        state = sim._state
        assert 'spousal_contribution_years' in state.jurisdiction_state.get('canada', {}), \
            "jurisdiction_state['canada'] must have spousal_contribution_years key"
        results = sim.run()
        assert len(results) > 0


class TestMonthlyYearlyConsistency:
    """Verify monthly and yearly modes produce comparable results."""

    def test_monthly_yearly_similar_total_assets(self):
        """Monthly and yearly modes should produce similar total assets.

        Not identical because monthly compounding differs from annual,
        but within a reasonable tolerance (within 2x magnitude).
        """
        config_yearly = _make_config(time_step='yearly', projection_years=5)
        config_monthly = _make_config(time_step='monthly', projection_years=5)

        sim_yearly = _make_sim(config_yearly, use_readvanceable=True)
        sim_monthly = _make_sim(config_monthly, use_readvanceable=True)

        results_y = sim_yearly.run()
        results_m = sim_monthly.run()

        # Final year total assets should be within 2x of each other
        final_y = results_y[-1].total_assets
        final_m = results_m[-1].total_assets

        assert final_y > 0, "Yearly mode should produce positive total assets"
        assert final_m > 0, "Monthly mode should produce positive total assets"
        assert 0.5 * final_y <= final_m <= 2 * final_y, \
            f"Monthly ({final_m:.0f}) and yearly ({final_y:.0f}) total assets should be within 2x"

    def test_monthly_yearly_similar_debt_trajectory(self):
        """Monthly and yearly modes should produce similar debt trajectories."""
        config_yearly = _make_config(time_step='yearly', projection_years=5)
        config_monthly = _make_config(time_step='monthly', projection_years=5)

        results_y = _make_sim(config_yearly, use_readvanceable=True).run()
        results_m = _make_sim(config_monthly, use_readvanceable=True).run()

        # Total debt should be comparable
        for ry, rm in zip(results_y, results_m):
            assert 0.5 * ry.total_debt <= rm.total_debt <= 2 * ry.total_debt, \
                f"Year {ry.year}: Monthly debt ({rm.total_debt:.0f}) vs Yearly ({ry.total_debt:.0f}) too different"


class TestMonthlyModeSimStatePropagation:
    """Verify that SimState propagates correctly between years in monthly mode."""

    def test_monthly_mode_heloc_tracing_accumulates(self):
        """HELOC tracing should accumulate correctly across years in monthly mode."""
        config = _make_config(time_step='monthly', projection_years=5, use_readvanceable=True)
        sim = _make_sim(config, use_readvanceable=True)
        results = sim.run()

        # HELOC balance should change over time if SM is active
        heloc_balances = [r.heloc_balance for r in results]
        assert any(b > 0 for b in heloc_balances), \
            "HELOC balance should be positive in at least some years with SM active"


class TestMonthlyModeEliminatesMutableAccounts:
    """Verify that the monthly path no longer uses mutable account objects for state tracking.

    After the fix, _run_monthly should delegate to simulate_year_pure
    instead of directly mutating self.rrsp, self.tfsa_primary, etc.
    """

    def test_no_simulate_year_monthly_method(self):
        """The _simulate_year_monthly method should be removed or redirect to pure fold."""
        config = _make_config(time_step='monthly', projection_years=2)
        sim = _make_sim(config)

        # If _simulate_year_monthly still exists, it should not directly mutate accounts
        if hasattr(sim, '_simulate_year_monthly'):
            import inspect
            source = inspect.getsource(sim._simulate_year_monthly)
            # Should not directly mutate mutable account objects
            forbidden_patterns = ['self.rrsp.contribute', 'self.tfsa_primary.contribute',
                                  'self.nonreg.contribute', 'self.rrsp.grow',
                                  'self.tfsa_primary.grow', 'self.nonreg.grow']
            for pattern in forbidden_patterns:
                assert pattern not in source, \
                    f"_simulate_year_monthly should not contain '{pattern}' — use simulate_year_pure instead"