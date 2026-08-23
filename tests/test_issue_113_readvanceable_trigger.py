#!/usr/bin/env python3
"""Tests for issue #113: Replace use_readvanceable boolean with data-driven trigger.

DP#16: Modules auto-include when their trigger data is present or inferable.
use_readvanceable should be derived from mortgage config (heloc_readvance=True
AND margin_available > 0), not passed as a standalone boolean flag.

The optimizer can still toggle it explicitly for comparison scenarios.
"""

import pytest
from unittest.mock import MagicMock
from scenario_overlay import ScenarioOverlay
from simulation_config import SimulationConfig


class TestIsReadvanceableProperty:
    """Test SimulationConfig.is_readvanceable auto-detection (DP#16)."""

    def test_not_readvanceable_when_heloc_readvance_false(self):
        """When heloc_readvance=False, is_readvanceable must be False
        regardless of margin_available."""
        config = SimulationConfig(
            heloc_readvance=False,
            margin_available=200000,
        )
        assert config.is_readvanceable is False

    def test_not_readvanceable_when_margin_zero(self):
        """When margin_available=0, is_readvanceable must be False
        even if heloc_readvance=True (no room to readvance into)."""
        config = SimulationConfig(
            heloc_readvance=True,
            margin_available=0,
        )
        assert config.is_readvanceable is False

    def test_readvanceable_when_both_conditions_hold(self):
        """When heloc_readvance=True AND margin_available > 0,
        is_readvanceable must be True."""
        config = SimulationConfig(
            heloc_readvance=True,
            margin_available=200000,
        )
        assert config.is_readvanceable is True

    def test_default_is_not_readvanceable(self):
        """Default config has heloc_readvance=False and margin_available=0,
        so is_readvanceable should be False."""
        config = SimulationConfig()
        assert config.is_readvanceable is False

    def test_readvanceable_with_small_margin(self):
        """Even a small positive margin with heloc_readvance=True
        means readvanceable (DP#16: presence of trigger data activates module)."""
        config = SimulationConfig(
            heloc_readvance=True,
            margin_available=1.0,
        )
        assert config.is_readvanceable is True


class TestIsReadvanceableFromDict:
    """Test that heloc_readvance loads from input.json property section."""

    def test_heloc_readvance_true_from_dict(self):
        """heloc_readvance=True in property section activates readvanceable."""
        cfg = {
            'property': {
                'heloc_readvance': True,
                'margin_available': 200000,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        assert config.heloc_readvance is True
        assert config.is_readvanceable is True

    def test_heloc_readvance_false_from_dict(self):
        """heloc_readvance=False (or absent) deactivates readvanceable."""
        cfg = {
            'property': {
                'margin_available': 200000,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        assert config.heloc_readvance is False
        assert config.is_readvanceable is False

    def test_heloc_readvance_true_but_no_margin(self):
        """heloc_readvance=True with margin_available=0: not readvanceable."""
        cfg = {
            'property': {
                'heloc_readvance': True,
                'margin_available': 0,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        assert config.heloc_readvance is True
        assert config.is_readvanceable is False

    def test_round_trip_preserves_heloc_readvance(self):
        """DP#24: Config round-trips correctly."""
        config = SimulationConfig(
            heloc_readvance=True,
            margin_available=256000,
        )
        d = config.to_dict()
        assert d['property']['heloc_readvance'] is True
        assert d['property']['margin_available'] == 256000
        # Reconstruct
        config2 = SimulationConfig.from_dict(d)
        assert config2.heloc_readvance is True
        assert config2.is_readvanceable is True


class TestFamilySimulationAutoDetect:
    """Test that FamilySimulation auto-detects readvanceable from config (DP#16)."""

    def _make_config(self, heloc_readvance=True, margin_available=200000):
        """Create a minimal config for testing."""
        return SimulationConfig(
            heloc_readvance=heloc_readvance,
            margin_available=margin_available,
            mortgage_balance=100000,
            house_value=500000,
            family_members=[
                {'role': 'primary', 'gross_income': 130000, 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 50000, 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
        )

    def test_auto_detect_readvanceable_true(self):
        """When config.is_readvanceable=True and use_readvanceable=None,
        FamilySimulation should auto-detect True."""
        from simulation import FamilySimulation
        config = self._make_config(heloc_readvance=True, margin_available=200000)
        sim = FamilySimulation(config)
        assert sim.use_readvanceable is True

    def test_auto_detect_readvanceable_false(self):
        """When config.is_readvanceable=False and use_readvanceable=None,
        FamilySimulation should auto-detect False."""
        from simulation import FamilySimulation
        config = self._make_config(heloc_readvance=False, margin_available=200000)
        sim = FamilySimulation(config)
        assert sim.use_readvanceable is False

    def test_explicit_override_true(self):
        """Explicit use_readvanceable=True overrides auto-detection."""
        from simulation import FamilySimulation
        config = self._make_config(heloc_readvance=False, margin_available=200000)
        sim = FamilySimulation(config, use_readvanceable=True)
        assert sim.use_readvanceable is True

    def test_explicit_override_false(self):
        """Explicit use_readvanceable=False overrides auto-detection."""
        from simulation import FamilySimulation
        config = self._make_config(heloc_readvance=True, margin_available=200000)
        sim = FamilySimulation(config, use_readvanceable=False)
        assert sim.use_readvanceable is False

    def test_no_margin_means_not_readvanceable(self):
        """When margin_available=0, auto-detect returns False
        even if heloc_readvance=True (no room to readvance)."""
        from simulation import FamilySimulation
        config = self._make_config(heloc_readvance=True, margin_available=0)
        sim = FamilySimulation(config)
        assert sim.use_readvanceable is False


class TestScenarioOverlayStillBool:
    """ScenarioOverlay.use_readvanceable remains a bool for optimizer comparisons."""

    def test_overlay_use_readvanceable_is_bool(self):
        """ScenarioOverlay.use_readvanceable is bool, not None (DP#18)."""
        overlay = ScenarioOverlay(label="test", use_readvanceable=True)
        assert overlay.use_readvanceable is True

    def test_overlay_default_is_false(self):
        """Default use_readvanceable in overlay is False (no SM)."""
        overlay = ScenarioOverlay(label="test")
        assert overlay.use_readvanceable is False