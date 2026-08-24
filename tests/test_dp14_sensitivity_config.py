#!/usr/bin/env python3
"""
Tests for DP#14: Scripts read a common config schema; each script uses the parts it needs.

Verifies that sensitivity.py uses SimulationConfig.from_json + ScenarioOverlay
instead of constructing config dicts manually or passing individual float parameters.

Issue #222: sensitivity.py constructs config dicts manually instead of using
SimulationConfig.from_json + ScenarioOverlay
"""

import unittest
import inspect
from copy import deepcopy


class TestSensitivityDP14Compliance(unittest.TestCase):
    """Verify sensitivity.py follows DP#14: uses the standard config pipeline."""

    def test_run_scenario_uses_simulation_config_and_overlay(self):
        """run_scenario must accept SimulationConfig + ScenarioOverlay, not raw floats.

        DP#14: All scenario scripts read the common config schema. Per #222,
        run_scenario should not take individual float parameters like
        investment_return, heloc_rate, inflation — it should use ScenarioOverlay.
        """
        from sensitivity import run_scenario
        from scenario_overlay import ScenarioOverlay
        from simulation_config import SimulationConfig

        sig = inspect.signature(run_scenario)
        params = list(sig.parameters.keys())

        # Must have 'config' and 'overlay' parameters (or similar)
        # Must NOT have 'investment_return', 'heloc_rate', 'inflation' as separate float params
        self.assertIn('config', params,
                      "run_scenario must accept 'config' parameter (SimulationConfig)")
        self.assertIn('overlay', params,
                      "run_scenario must accept 'overlay' parameter (ScenarioOverlay)")
        self.assertNotIn('investment_return', params,
                         "run_scenario must not take individual float 'investment_return' per DP#14")
        self.assertNotIn('heloc_rate', params,
                         "run_scenario must not take individual float 'heloc_rate' per DP#14")
        self.assertNotIn('inflation', params,
                         "run_scenario must not take individual float 'inflation' per DP#14")

    def test_run_scenario_with_simulation_config(self):
        """run_scenario must work with SimulationConfig + ScenarioOverlay."""
        from sensitivity import run_scenario
        from scenario_overlay import ScenarioOverlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            projection_years=10,
            investment_return=0.07,
            house_value=500000,
            mortgage_balance=200000,
            mortgage_rate=0.05,
            margin_available=50000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000, 'birth_year': 1980},
                {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1982},
            ],
        )
        overlay = ScenarioOverlay(
            label="test-dp14",
            investment_return=0.07,
            mortgage_rate=0.05,
            inflation=0.025,
        )
        result = run_scenario(config, overlay)
        self.assertIn('net_benefit', result)
        self.assertIn('strategy', result)
        self.assertAlmostEqual(result['investment_return'], 0.07, places=4)
        self.assertAlmostEqual(result['heloc_rate'], 0.05, places=4)
        self.assertAlmostEqual(result['inflation'], 0.025, places=4)

    def test_run_scenario_with_dict_backward_compat(self):
        """run_scenario must also accept a dict for backward compatibility."""
        from sensitivity import run_scenario
        from scenario_overlay import ScenarioOverlay

        cfg = {
            'assumptions': {'investment_return': 0.07, 'projection_years': 10},
            'property': {'house_value': 500000, 'mortgage_balance': 200000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80, 'margin_available': 50000},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 100000, 'birth_year': 1980},
                {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1982},
            ]},
        }
        overlay = ScenarioOverlay(
            label="test-dp14-dict",
            investment_return=0.07,
            mortgage_rate=0.05,
            inflation=0.025,
        )
        # Should not raise TypeError when passed a dict
        result = run_scenario(cfg, overlay)
        self.assertIn('net_benefit', result)

    def test_no_load_inputs_function(self):
        """load_inputs should not exist per DP#9 (no backward compat)."""
        import sensitivity
        self.assertFalse(hasattr(sensitivity, 'load_inputs'),
                        "load_inputs should not exist per DP#9; use SimulationConfig.from_json instead")

    def test_no_run_scenario_raw_function(self):
        """run_scenario_raw should not exist per DP#9 (no backward compat)."""
        import sensitivity
        self.assertFalse(hasattr(sensitivity, 'run_scenario_raw'),
                        "run_scenario_raw should not exist per DP#9; use run_scenario(config, overlay) instead")

    def test_monte_carlo_uses_simulation_config(self):
        """monte_carlo must accept SimulationConfig (DP#14)."""
        from sensitivity import monte_carlo
        from simulation_config import SimulationConfig

        sig = inspect.signature(monte_carlo)
        params = list(sig.parameters.keys())
        self.assertIn('config', params,
                      "monte_carlo must accept 'config' parameter (SimulationConfig)")

    def test_two_way_sensitivity_uses_simulation_config(self):
        """two_way_sensitivity must accept SimulationConfig (DP#14)."""
        from sensitivity import two_way_sensitivity
        sig = inspect.signature(two_way_sensitivity)
        params = list(sig.parameters.keys())
        self.assertIn('config', params)

    def test_tornado_data_uses_simulation_config(self):
        """tornado_data must accept SimulationConfig (DP#14)."""
        from sensitivity import tornado_data
        sig = inspect.signature(tornado_data)
        params = list(sig.parameters.keys())
        self.assertIn('config', params)

    def test_break_even_analysis_uses_simulation_config(self):
        """break_even_analysis must accept SimulationConfig (DP#14)."""
        from sensitivity import break_even_analysis
        sig = inspect.signature(break_even_analysis)
        params = list(sig.parameters.keys())
        self.assertIn('config', params)

    def test_scenario_overlay_has_inflation(self):
        """ScenarioOverlay must support inflation for sensitivity analysis (DP#14)."""
        from scenario_overlay import ScenarioOverlay

        overlay = ScenarioOverlay(
            label="test-inflation",
            investment_return=0.07,
            mortgage_rate=0.05,
            inflation=0.03,
        )
        self.assertEqual(overlay.inflation, 0.03)
        self.assertEqual(overlay.investment_return, 0.07)
        self.assertEqual(overlay.mortgage_rate, 0.05)

    def test_scenario_overlay_inflation_round_trip(self):
        """ScenarioOverlay inflation must round-trip via to_dict/from_dict (DP#24)."""
        from scenario_overlay import ScenarioOverlay

        overlay = ScenarioOverlay(
            label="test-round-trip",
            investment_return=0.07,
            mortgage_rate=0.05,
            inflation=0.03,
        )
        data = overlay.to_dict()
        restored = ScenarioOverlay.from_dict(data)
        self.assertAlmostEqual(restored.inflation, 0.03, places=4)
        self.assertAlmostEqual(restored.investment_return, 0.07, places=4)
        self.assertAlmostEqual(restored.mortgage_rate, 0.05, places=4)

    def test_apply_overlay_handles_inflation(self):
        """apply_overlay must propagate inflation to the derived config (DP#14)."""
        from scenario_overlay import ScenarioOverlay, apply_overlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            investment_return=0.07,
            mortgage_rate=0.05,
            family_members=[
                {'role': 'primary', 'gross_income': 100000, 'birth_year': 1980},
            ],
        )
        overlay = ScenarioOverlay(
            label="test-overlay-inflation",
            investment_return=0.10,
            mortgage_rate=0.06,
            inflation=0.04,
        )
        derived = apply_overlay(config.to_dict(), overlay)
        # #260: overlay writes the swept return into return_model (single source of
        # truth), not the deprecated assumptions.investment_return scalar.
        self.assertAlmostEqual(derived['return_model']['rate'], 0.10, places=4)
        # Overlay should set mortgage_rate to 0.06
        self.assertAlmostEqual(derived['property']['mortgage_rate'], 0.06, places=4)
        # Overlay should set inflation
        self.assertAlmostEqual(derived['assumptions']['inflation'], 0.04, places=4)

    def test_no_hardcoded_magic_numbers_in_run_scenario(self):
        """run_scenario should not contain hardcoded 0.07, 0.0495, 0.025 as defaults.

        DP#13: defaults are fallbacks, not opinions. DP#14: values come from
        the config pipeline, not from hardcoded parameters in function calls.
        """
        import sensitivity
        source = inspect.getsource(sensitivity.run_scenario)
        # The function body should not contain the old magic numbers as parameters
        # (0.07 was old default investment return, 0.0495 was old default HELOC rate, 0.025 was old default inflation)
        # These values should come from config, not be embedded in function calls
        lines_with_magic = []
        for i, line in enumerate(source.split('\n'), 1):
            # Look for the old pattern: run_scenario(cfg, 0.07, 0.0495, 0.025)
            if '0.0495' in line:
                lines_with_magic.append((i, line.strip()))
        self.assertEqual(len(lines_with_magic), 0,
                         f"Found hardcoded 0.0495 in run_scenario (lines: {lines_with_magic}). "
                         "Values should come from config per DP#14.")

    def test_print_sensitivity_report_uses_simulation_config(self):
        """print_sensitivity_report must accept SimulationConfig (DP#14)."""
        from sensitivity import print_sensitivity_report
        sig = inspect.signature(print_sensitivity_report)
        params = list(sig.parameters.keys())
        self.assertIn('config', params)

    def test_cli_uses_simulation_config_from_json(self):
        """The __main__ block must use SimulationConfig.from_json (DP#14)."""
        import sensitivity
        source = inspect.getsource(sensitivity)
        # Check that the main block uses SimulationConfig.from_json
        self.assertIn('SimulationConfig.from_json', source,
                      "__main__ must use SimulationConfig.from_json per DP#14")
        # Check that raw json.load is NOT used in the main block
        # (load_inputs is a backward-compat wrapper, but the CLI should not use it)
        main_block = source[source.rfind('if __name__'):]
        self.assertNotIn('load_inputs', main_block,
                         "__main__ should not call load_inputs() per DP#14; "
                         "use SimulationConfig.from_json instead")


class TestScenarioOverlaySalaryGrowth(unittest.TestCase):
    """Test that ScenarioOverlay correctly handles salary_growth for sensitivity."""

    def test_overlay_salary_growth_default(self):
        """Overlay salary_growth defaults to None (no override)."""
        from scenario_overlay import ScenarioOverlay

        overlay = ScenarioOverlay(label="test-default")
        self.assertIsNone(overlay.salary_growth)

    def test_overlay_salary_growth_round_trip(self):
        """salary_growth must round-trip via to_dict/from_dict."""
        from scenario_overlay import ScenarioOverlay

        overlay = ScenarioOverlay(
            label="test-salary-growth",
            investment_return=0.07,
            salary_growth=0.04,
        )
        data = overlay.to_dict()
        restored = ScenarioOverlay.from_dict(data)
        self.assertAlmostEqual(restored.salary_growth, 0.04, places=4)

    def test_apply_overlay_handles_salary_growth(self):
        """apply_overlay must propagate salary_growth to the derived config."""
        from scenario_overlay import ScenarioOverlay, apply_overlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            family_members=[{'role': 'primary', 'gross_income': 100000}],
            salary_growth=0.02,
        )
        overlay = ScenarioOverlay(label="salary-growth-test", salary_growth=0.05)
        derived = apply_overlay(config.to_dict(), overlay)
        self.assertAlmostEqual(derived['assumptions']['salary_growth'], 0.05, places=4)

    def test_overlay_salary_growth_none_means_no_override(self):
        """When salary_growth is None, the base config value is preserved."""
        from scenario_overlay import ScenarioOverlay, apply_overlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            family_members=[{'role': 'primary', 'gross_income': 100000}],
            salary_growth=0.03,
        )
        overlay = ScenarioOverlay(label="no-salary-growth-override")
        derived = apply_overlay(config.to_dict(), overlay)
        self.assertAlmostEqual(derived['assumptions']['salary_growth'], 0.03, places=4)


class TestScenarioOverlayFromOverlayDiff(unittest.TestCase):
    """Test from_overlay_diff handles inflation and salary_growth."""

    def test_from_overlay_diff_inflation(self):
        """from_overlay_diff must map inflation changes to overlay."""
        from scenario_overlay import ScenarioOverlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(inflation=0.025, investment_return=0.07)
        modified = SimulationConfig(inflation=0.04, investment_return=0.07)
        diff = SimulationConfig.overlay_diff(config, modified)
        overlay = ScenarioOverlay.from_overlay_diff(diff, config)
        self.assertAlmostEqual(overlay.inflation, 0.04, places=4)

    def test_from_overlay_diff_salary_growth(self):
        """from_overlay_diff must map salary_growth changes to overlay."""
        from scenario_overlay import ScenarioOverlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(salary_growth=0.02, investment_return=0.07)
        modified = SimulationConfig(salary_growth=0.05, investment_return=0.07)
        diff = SimulationConfig.overlay_diff(config, modified)
        overlay = ScenarioOverlay.from_overlay_diff(diff, config)
        self.assertAlmostEqual(overlay.salary_growth, 0.05, places=4)


class TestScenarioOverlayExtract(unittest.TestCase):
    """Test extract() handles inflation and salary_growth."""

    def test_extract_inflation(self):
        """extract must detect inflation changes."""
        from scenario_overlay import ScenarioOverlay

        base_cfg = {'assumptions': {'inflation': 0.025, 'investment_return': 0.07},
                    'property': {'mortgage_rate': 0.05},
                    'family': {'members': []},
                    'accounts': {}}
        derived_cfg = {'assumptions': {'inflation': 0.04, 'investment_return': 0.07},
                       'property': {'mortgage_rate': 0.05},
                       'family': {'members': []},
                       'accounts': {}}
        overlay = ScenarioOverlay.extract(base_cfg, derived_cfg)
        self.assertAlmostEqual(overlay.inflation, 0.04, places=4)

    def test_extract_salary_growth(self):
        """extract must detect salary_growth changes."""
        from scenario_overlay import ScenarioOverlay

        base_cfg = {'assumptions': {'salary_growth': 0.02, 'investment_return': 0.07},
                    'property': {'mortgage_rate': 0.05},
                    'family': {'members': []},
                    'accounts': {}}
        derived_cfg = {'assumptions': {'salary_growth': 0.05, 'investment_return': 0.07},
                       'property': {'mortgage_rate': 0.05},
                       'family': {'members': []},
                       'accounts': {}}
        overlay = ScenarioOverlay.extract(base_cfg, derived_cfg)
        self.assertAlmostEqual(overlay.salary_growth, 0.05, places=4)


class TestScenarioOverlayInflation(unittest.TestCase):
    """Test that ScenarioOverlay correctly handles inflation for sensitivity."""

    def test_overlay_inflation_zero_by_default(self):
        """Overlay inflation defaults to None (no override)."""
        from scenario_overlay import ScenarioOverlay

        overlay = ScenarioOverlay(label="test-default")
        self.assertIsNone(overlay.inflation)

    def test_overlay_inflation_applied_to_config(self):
        """Inflation overlay propagates to the derived config dict."""
        from scenario_overlay import ScenarioOverlay, apply_overlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            family_members=[{'role': 'primary', 'gross_income': 100000}],
        )
        overlay = ScenarioOverlay(label="inflation-test", inflation=0.05)
        derived = apply_overlay(config.to_dict(), overlay)
        self.assertAlmostEqual(derived['assumptions']['inflation'], 0.05, places=4)

    def test_overlay_inflation_none_means_no_override(self):
        """When inflation is None, the base config value is preserved."""
        from scenario_overlay import ScenarioOverlay, apply_overlay
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            family_members=[{'role': 'primary', 'gross_income': 100000}],
            inflation=0.03,
        )
        overlay = ScenarioOverlay(label="no-inflation-override")
        derived = apply_overlay(config.to_dict(), overlay)
        # Base config's inflation should be preserved since overlay.inflation is None
        self.assertAlmostEqual(derived['assumptions']['inflation'], 0.03, places=4)


class TestSimulationConfigInflation(unittest.TestCase):
    """Test that SimulationConfig supports inflation as a top-level field."""

    def test_inflation_default(self):
        """SimulationConfig inflation defaults to 0.025 (DP#13: round number)."""
        from simulation_config import SimulationConfig
        config = SimulationConfig()
        self.assertAlmostEqual(config.inflation, 0.025, places=4)

    def test_inflation_from_dict(self):
        """SimulationConfig reads inflation from config dict."""
        from simulation_config import SimulationConfig
        cfg = {
            'assumptions': {'inflation': 0.04},
            'family': {'members': [{'role': 'primary', 'gross_income': 100000}]},
        }
        config = SimulationConfig.from_dict(cfg)
        self.assertAlmostEqual(config.inflation, 0.04, places=4)

    def test_inflation_round_trip(self):
        """SimulationConfig inflation round-trips via to_dict/from_dict."""
        from simulation_config import SimulationConfig
        config = SimulationConfig(inflation=0.035)
        cfg = config.to_dict()
        restored = SimulationConfig.from_dict(cfg)
        self.assertAlmostEqual(restored.inflation, 0.035, places=4)


if __name__ == '__main__':
    unittest.main()