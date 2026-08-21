#!/usr/bin/env python3
"""
Tests for Return Model Extensions (DP#21)
Tests for Module Registry Auto-Include Updates (DP#16)

Covers:
- StressedReturn: crash+recovery modeling
- build_return_model with stressed and mean_reverting types
- build_return_model_from_config
- Module registry auto-include for the sections that are still real
- SCENARIO_SEED §3.2, §8.1 integration tests

epic #603 Track C Phase 2 (DP#9): RateScenarioPath/RateScenarioConfig (§8.2)
are deleted along with rate_scenarios.scenarios[] -- zero production callers
(#593's DEAD_ALLOWLIST). The heloc/rental_properties/employer_benefits/
life_events auto-include assertions below are deleted for the same reason:
check_auto_includes itself has zero production callers (a separate,
already-documented finding -- see test_schema_coverage.py's module
docstring), and the *_data fields it used to gate no longer exist on
SimulationConfig at all.
"""

import pytest
from return_model import (
    ReturnModel, ReturnEngine, FixedReturn, VariableReturn, StochasticReturn, MeanRevertingReturn,
    StressedReturn,
    build_return_model, build_return_model_from_config,
)


# =============================================================================
# StressedReturn Tests
# =============================================================================

class TestStressedReturn:
    """Test stressed return model (SCENARIO_SEED §8.1)."""

    def test_crash_year_returns_crash_pct(self):
        """Crash year should return the crash percentage."""
        model = StressedReturn(baseline_rate=0.07, crash_year=2, crash_pct=-0.40)
        assert model.return_for_year(2) == -0.40

    def test_pre_crash_returns_baseline(self):
        """Years before crash should return baseline."""
        model = StressedReturn(baseline_rate=0.07, crash_year=2, crash_pct=-0.40)
        assert model.return_for_year(0) == 0.07
        assert model.return_for_year(1) == 0.07

    def test_recovery_rate_greater_than_crash(self):
        """Recovery years should have positive returns."""
        model = StressedReturn(baseline_rate=0.07, crash_year=2, crash_pct=-0.40,
                               recovery_years=5)
        for yr in range(3, 7):
            rate = model.return_for_year(yr)
            assert rate > 0, f"Year {yr} should have positive return, got {rate}"

    def test_post_recovery_returns_baseline(self):
        """After recovery period, returns should normalize."""
        model = StressedReturn(baseline_rate=0.07, crash_year=2, crash_pct=-0.40,
                               recovery_years=5)
        # Year 10 should be back to baseline
        rate = model.return_for_year(10)
        assert rate == pytest.approx(0.07, abs=0.01)

    def test_2008_style_crash(self):
        """SCENARIO_SEED §8.1: -40% crash in year 1, 5-year recovery."""
        model = StressedReturn(crash_year=1, crash_pct=-0.40, recovery_years=5)
        assert model.return_for_year(0) == 0.07  # Pre-crash
        assert model.return_for_year(1) == -0.40  # Crash
        # Recovery years should be positive
        for yr in range(2, 7):
            assert model.return_for_year(yr) > 0

    def test_default_values(self):
        """Default stressed model should have reasonable values."""
        model = StressedReturn()
        assert model.rate == 0.07
        assert model.crash_year == 2
        assert model.crash_pct == -0.40
        assert model.recovery_years == 5


# =============================================================================
# build_return_model Tests
# =============================================================================

class TestBuildReturnModel:
    """Test return model factory functions."""

    def test_build_fixed(self):
        """Factory should create fixed return model."""
        model = build_return_model('fixed', rate=0.07)
        assert isinstance(model, ReturnModel)
        assert model.type == 'fixed'
        assert model.return_for_year(0) == 0.07

    def test_build_stressed(self):
        """Factory should create stressed return model."""
        model = build_return_model('stressed', stressed={
            'crash_year': 1, 'crash_pct': -0.40, 'recovery_years': 5,
        })
        assert isinstance(model, ReturnModel)
        assert model.type == 'stressed'
        assert model.return_for_year(1) == -0.40

    def test_build_mean_reverting(self):
        """Factory should create mean_reverting return model."""
        model = build_return_model('mean_reverting', mean=0.07, mean_reverting={
            'reversion_speed': 0.3, 'volatility': 0.02,
        })
        assert isinstance(model, ReturnModel)
        assert model.type == 'mean_reverting'
        # First year should be close to initial rate
        rate0 = model.return_for_year(0)
        assert rate0 > -0.5  # Reasonable range

    def test_build_from_config_fixed(self):
        """build_return_model_from_config with fixed type."""
        model = build_return_model_from_config({'type': 'fixed', 'rate': 0.06})
        assert isinstance(model, ReturnModel)
        assert model.type == 'fixed'
        assert model.rate == 0.06

    def test_build_from_config_stressed(self):
        """build_return_model_from_config with stressed type."""
        model = build_return_model_from_config({
            'type': 'stressed',
            'stressed': {
                'crash_year': 2,
                'crash_pct': -0.30,
                'recovery_years': 3,
            }
        })
        assert isinstance(model, ReturnModel)
        assert model.type == 'stressed'
        assert model.crash_year == 2

    def test_build_from_config_mean_reverting(self):
        """build_return_model_from_config with mean_reverting type."""
        model = build_return_model_from_config({
            'type': 'mean_reverting',
            'mean_reverting': {
                'long_term_mean': 0.07,
                'reversion_speed': 0.25,
                'volatility': 0.03,
            }
        })
        assert isinstance(model, ReturnModel)
        assert model.type == 'mean_reverting'

    def test_build_from_config_variable(self):
        """build_return_model_from_config with variable type."""
        model = build_return_model_from_config({
            'type': 'variable',
            'rates': [0.06, 0.07, 0.08],
            'fallback': 0.07,
        })
        assert isinstance(model, ReturnModel)
        assert model.type == 'variable'
        assert model.return_for_year(0) == 0.06


# =============================================================================
# Module Registry Auto-Include Tests -- DELETED (epic #603 Track C Phase 2b)
# =============================================================================
#
# This class tested module_registry.check_auto_includes, deleted along with
# that function (zero production callers, operated on the legacy input
# shape's auto-include triggers -- portfolio/rental_properties/heloc/
# life_events blocks that no longer exist). See module_registry.py's module
# docstring.

# =============================================================================
# SCENARIO_SEED §8 Integration Tests
# =============================================================================

class TestScenario81StressReturn:
    """SCENARIO_SEED §8.1: Market Crash While Leveraged."""

    def test_crash_then_recovery(self):
        """-40% crash in year 1, recovery over 5 years."""
        model = StressedReturn(crash_year=1, crash_pct=-0.40, recovery_years=5)
        # Pre-crash
        assert model.return_for_year(0) > 0
        # Crash
        assert model.return_for_year(1) == -0.40
        # Recovery (positive)
        for yr in range(2, 7):
            rate = model.return_for_year(yr)
            assert rate > 0, f"Year {yr} should be positive during recovery"
        # Post-recovery (back to baseline)
        assert model.return_for_year(10) == pytest.approx(0.07, abs=0.01)

    # §8.2 (RateScenarioPath-based HELOC rate spike) used to live here.
    # Deleted in epic #603 Track C Phase 2 (DP#9) along with
    # RateScenarioPath/RateScenarioConfig -- see the module docstring.

    def test_rate_sensitivity_breakeven(self):
        """At what rate does SM break even with 7% expected return?"""
        # SM after-tax cost = heloc_rate × (1 - MTR)
        # Break-even: after-tax cost = expected return
        mtr = 0.4571
        investment_return = 0.07
        
        # After-tax HELOC cost increases with rate
        for rate in [0.03, 0.04, 0.05, 0.06, 0.07, 0.08]:
            after_tax_cost = rate * (1 - mtr)
            if after_tax_cost > investment_return:
                breakeven = rate
                break
        
        # Break-even should be around 7% / (1 - 0.4571) ≈ 12.9%
        # But with deduction, even at 7% HELOC, after-tax is only ~3.8%
        after_tax_at_7 = 0.07 * (1 - mtr)
        assert after_tax_at_7 < investment_return  # SM still profitable at 7% HELOC