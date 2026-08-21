"""Test issue #40: scenario_discovery builds FamilyState from scratch instead of overlaying base config.

DP#18/5: _build_family_state should overlay on base config values, not hardcode
household-specific financial assumptions like savings rate 20%, FHSA $40k/$8k,
and RESP match cap $750 (the exact Quebec CESG+QESI maximum).

These hardcoded values override what should come from input.json, changing
optimization outcomes.
"""

import unittest
from scenario_discovery import _build_family_state
# DP#25 (#998): scenario_discovery's simulation callables are now injected;
# importing simulation_deps configures the injection point at import time.
import simulation_deps  # noqa: F401  (import side-effect: injects SimulationDeps)


class TestBuildFamilyStateNoHardcodedDefaults(unittest.TestCase):
    """DP#18/5: _build_family_state should use zero/neutral defaults, not household-specific values."""

    def test_savings_rate_defaults_to_zero(self):
        """DP#18/5: Savings rate defaults to 0.0, not hardcoded 0.2 (20%)."""
        cfg = {'family': {'members': [{'role': 'primary', 'gross_income': 100000}]}}
        state = _build_family_state(cfg)
        self.assertEqual(state.annual_savings, 0.0,
                         "Savings should be 0 when no savings rate configured, not 20% of income")

    def test_savings_rate_from_config(self):
        """When savings rate is in config, it's used correctly."""
        cfg = {
            'family': {'members': [{'role': 'primary', 'gross_income': 100000}]},
            'savings': {'rate': 0.15},
        }
        state = _build_family_state(cfg)
        self.assertAlmostEqual(state.annual_savings, 15000.0,
                               msg="Savings should be 15% of income when configured")

    def test_fhsa_lifetime_defaults_to_zero(self):
        """DP#18/5: FHSA lifetime limit defaults to 0, not hardcoded 40000."""
        cfg = {'family': {'members': [{'role': 'primary', 'gross_income': 100000,
                                       'fhsa_first_time_buyer_since': '2024-01-01'}]}}
        state = _build_family_state(cfg)
        self.assertEqual(state.fhsa_lifetime_remaining, 0,
                         "FHSA lifetime should default to 0, not 40000")

    def test_fhsa_lifetime_from_config(self):
        """When FHSA lifetime limit is in config, it's used."""
        cfg = {'family': {'members': [{'role': 'primary', 'gross_income': 100000,
                                       'fhsa_first_time_buyer_since': '2024-01-01',
                                       'fhsa_lifetime_limit': 40000}]}}
        state = _build_family_state(cfg)
        self.assertEqual(state.fhsa_lifetime_remaining, 40000,
                         "FHSA lifetime should use config value")

    def test_resp_match_cap_defaults_to_zero(self):
        """DP#18/5: RESP match cap defaults to 0.0, not hardcoded 750."""
        cfg = {'family': {'members': [{'role': 'primary', 'gross_income': 100000}]}}
        state = _build_family_state(cfg)
        self.assertEqual(state.resp_annual_match_cap, 0.0,
                         "RESP match cap should default to 0, not 750")

    def test_resp_match_cap_from_config(self):
        """When RESP match cap is in config, it's used."""
        cfg = {
            'family': {'members': [{'role': 'primary', 'gross_income': 100000}]},
            'accounts': {'resp_annual_match_cap': 750.0},
        }
        state = _build_family_state(cfg)
        self.assertAlmostEqual(state.resp_annual_match_cap, 750.0,
                               msg="RESP match cap should use config value")

    def test_fhsa_room_defaults_to_zero(self):
        """DP#18/5: FHSA room defaults to 0, not hardcoded 8000."""
        cfg = {'family': {'members': [{'role': 'primary', 'gross_income': 100000}]}}
        state = _build_family_state(cfg)
        self.assertEqual(state.fhsa_room, 0,
                         "FHSA room should default to 0, not 8000")


if __name__ == '__main__':
    unittest.main()