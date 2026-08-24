#!/usr/bin/env python3
"""Tests for issue #116: Replace deduct_later boolean flag with data-driven
trigger from bracket target config (DP#16).

DP#16: Modules auto-include when their trigger data is present or inferable.
deduct_later should be derived from bracket target config
(deduct_later_bracket_target > 0), not passed as a standalone boolean flag.

The optimizer can still toggle it explicitly for comparison scenarios.
"""

import pytest
from scenario_overlay import ScenarioOverlay
from simulation_config import SimulationConfig


class TestShouldDeductLaterProperty:
    """Test SimulationConfig.should_deduct_later auto-detection (DP#16)."""

    def test_not_deduct_later_when_bracket_target_zero(self):
        """When deduct_later_bracket_target=0, should_deduct_later must be False."""
        config = SimulationConfig(deduct_later_bracket_target=0)
        assert config.should_deduct_later is False

    def test_deduct_later_when_bracket_target_positive(self):
        """When deduct_later_bracket_target > 0, should_deduct_later must be True."""
        config = SimulationConfig(deduct_later_bracket_target=117045)
        assert config.should_deduct_later is True

    def test_default_is_not_deduct_later(self):
        """Default config has deduct_later_bracket_target=0,
        so should_deduct_later should be False."""
        config = SimulationConfig()
        assert config.should_deduct_later is False

    def test_deduct_later_with_small_bracket_target(self):
        """Even a small positive bracket target activates deduct_later
        (DP#16: presence of trigger data activates module)."""
        config = SimulationConfig(deduct_later_bracket_target=1.0)
        assert config.should_deduct_later is True


class TestShouldDeductLaterFromDict:
    """Test that deduct_later_bracket_target loads from input.json."""

    def test_bracket_target_from_accounts_section(self):
        """deduct_later_bracket_target in accounts section activates deduct_later."""
        cfg = {
            'accounts': {
                'deduct_later_bracket_target': 117045,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        assert config.deduct_later_bracket_target == 117045
        assert config.should_deduct_later is True

    def test_bracket_target_from_assumptions_section(self):
        """deduct_later_bracket_target in assumptions section activates deduct_later."""
        cfg = {
            'assumptions': {
                'deduct_later_bracket_target': 100000,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        assert config.deduct_later_bracket_target == 100000
        assert config.should_deduct_later is True

    def test_no_bracket_target_means_not_deduct_later(self):
        """Absent deduct_later_bracket_target means not deduct_later."""
        cfg = {}
        config = SimulationConfig.from_dict(cfg)
        assert config.deduct_later_bracket_target == 0
        assert config.should_deduct_later is False

    def test_round_trip_preserves_bracket_target(self):
        """DP#24: Config round-trips correctly."""
        config = SimulationConfig(deduct_later_bracket_target=117045)
        d = config.to_dict()
        # Check bracket target is in the accounts output section
        assert d['accounts']['deduct_later_bracket_target'] == 117045
        # Reconstruct
        config2 = SimulationConfig.from_dict(d)
        assert config2.deduct_later_bracket_target == 117045
        assert config2.should_deduct_later is True


class TestFamilySimulationAutoDetect:
    """Test that FamilySimulation auto-detects deduct_later from config (DP#16)."""

    def _make_config(self, bracket_target=0, **kwargs):
        """Create a minimal config for testing."""
        defaults = dict(
            mortgage_balance=100000,
            house_value=500000,
            family_members=[
                {'role': 'primary', 'gross_income': 130000, 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 50000, 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
            deduct_later_bracket_target=bracket_target,
        )
        defaults.update(kwargs)
        return SimulationConfig(**defaults)

    def test_auto_detect_deduct_later_true(self):
        """When config.should_deduct_later=True and deduct_later=None,
        FamilySimulation should auto-detect True."""
        from simulation import FamilySimulation
        config = self._make_config(bracket_target=117045)
        sim = FamilySimulation(config)
        assert sim.deduct_later is True

    def test_auto_detect_deduct_later_false(self):
        """When config.should_deduct_later=False and deduct_later=None,
        FamilySimulation should auto-detect False."""
        from simulation import FamilySimulation
        config = self._make_config(bracket_target=0)
        sim = FamilySimulation(config)
        assert sim.deduct_later is False

    def test_explicit_override_true(self):
        """Explicit deduct_later=True overrides auto-detection."""
        from simulation import FamilySimulation
        config = self._make_config(bracket_target=0)
        sim = FamilySimulation(config, deduct_later=True)
        assert sim.deduct_later is True

    def test_explicit_override_false(self):
        """Explicit deduct_later=False overrides auto-detection."""
        from simulation import FamilySimulation
        config = self._make_config(bracket_target=117045)
        sim = FamilySimulation(config, deduct_later=False)
        assert sim.deduct_later is False

    def test_no_bracket_target_means_not_deduct_later(self):
        """When bracket_target=0, auto-detect returns False
        (no trigger data present)."""
        from simulation import FamilySimulation
        config = self._make_config(bracket_target=0)
        sim = FamilySimulation(config)
        assert sim.deduct_later is False


class TestScenarioOverlayStillBool:
    """ScenarioOverlay.deduct_later remains a bool for optimizer comparisons."""

    def test_overlay_deduct_later_is_bool(self):
        """ScenarioOverlay.deduct_later is bool, not None (DP#18)."""
        overlay = ScenarioOverlay(label="test", deduct_later=True)
        assert overlay.deduct_later is True

    def test_overlay_default_is_false(self):
        """Default deduct_later in overlay is False (no deduction deferral)."""
        overlay = ScenarioOverlay(label="test")
        assert overlay.deduct_later is False