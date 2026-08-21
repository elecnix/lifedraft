#!/usr/bin/env python3
"""Tests for Issue #327 (DP#7): de-brand 'Smith Manoeuvre' in core modules.

Per DP#7 (model the mechanism, not the branded product), the branded product
name "Smith Manoeuvre" must not appear in library/core modules. The code refers
to the mechanism instead (readvanceable mortgage / readvance / investment-loan
strategy). Brand names belong in input config data, not in library code.

These tests lock:
- The literal brand string "Smith Manoeuvre" no longer appears in the targeted
  core modules (grep guard).
- scenario_discovery exposes the renamed mechanism-based discovery function
  (`_discover_readvanceable_options`) and no longer the branded name.
- Behaviour is unchanged: the renamed function still produces the same
  ``sm_options`` anchor results.
"""

import os
import sys
import unittest

# DP#25 (#998): scenario_discovery's simulation callables are now injected;
# importing simulation_deps configures the injection point at import time.
import simulation_deps  # noqa: F401  (import side-effect: injects SimulationDeps)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Files de-branded by issue #327.
TARGETED_FILES = [
    "simulation_config.py",
    "simulation.py",
    "optimizer.py",
    "output_plugins.py",
    "scenario_discovery.py",
    os.path.join("countries", "canada", "provinces", "quebec", "quebec_deduction.py"),
]


class TestBrandStringRemoved(unittest.TestCase):
    """The branded product name must not appear in core modules (DP#7)."""

    def test_no_smith_manoeuvre_in_targeted_files(self):
        offenders = []
        for rel in TARGETED_FILES:
            path = os.path.join(REPO_ROOT, rel)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if "smith manoeuvre" in line.lower():
                        offenders.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "Branded 'Smith Manoeuvre' string must not appear in core modules "
            "(DP#7); found:\n" + "\n".join(offenders),
        )


class TestDiscoveryFunctionRenamed(unittest.TestCase):
    """The discovery function is mechanism-named, not branded."""

    def test_readvanceable_options_function_exists(self):
        import scenario_discovery

        self.assertTrue(
            hasattr(scenario_discovery, "_discover_readvanceable_options"),
            "scenario_discovery should expose _discover_readvanceable_options",
        )

    def test_branded_function_name_removed(self):
        import scenario_discovery

        self.assertFalse(
            hasattr(scenario_discovery, "_discover_sm_options"),
            "branded _discover_sm_options should have been renamed",
        )
        self.assertFalse(
            hasattr(scenario_discovery, "discover_smith_manoeuvre_options"),
            "branded discover_smith_manoeuvre_options should not exist",
        )

    def test_behaviour_unchanged(self):
        """Renamed function returns identical results for known triggers."""
        from scenario_discovery import _discover_readvanceable_options

        # HELOC readvance disabled -> [False]
        self.assertEqual(
            _discover_readvanceable_options({"property": {"heloc_readvance": False}}),
            [False],
        )

        # Profitable readvance (high return, low heloc cost, modest tax) -> [True, False]
        cfg = {
            "property": {"heloc_readvance": True},
            "assumptions": {
                "heloc_rate": 0.03,
                "investment_return": 0.10,
                "capital_gains_inclusion": 0.5,
            },
        }
        result = _discover_readvanceable_options(cfg)
        self.assertIn(True, result)
        self.assertIn(False, result)


if __name__ == "__main__":
    unittest.main()
