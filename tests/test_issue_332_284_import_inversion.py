#!/usr/bin/env python3
"""Tests for issues #332 and #284 (DP#25): canonical imports & registry, plus #385 optimizer imports.

DP#25: the four layers are data -> scenario -> simulation -> optimization, and
dependencies point inward.

#332: objective.py (optimization layer) must import YearResult / SimulationConfig
      from the canonical data/config module (simulation_config), not from the
      simulation engine module (simulation) that merely re-exports them.

#284: strategy.py (core, layer 2-3) must not import the country strategy module
      (countries.canada.strategies). Instead the Canada package registers its
      strategies into strategy._STRATEGY_REGISTRY at import time (DP#16
      package-presence trigger), and strategy.list_strategies() discovers them
      through the jurisdiction-agnostic ``countries`` registry.

#385: optimizer.py, monte_carlo_optimizer.py must not import directly from
      countries.canada. They must use the jurisdiction_providers registry.

Run with: python3 -m pytest tests/test_issue_332_284_import_inversion.py -v
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import objective
import strategy


def _imported_modules(source_path):
    """Return every module name referenced by an import in a Python source file."""
    with open(source_path) as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestObjectiveImportsCanonicalSource(unittest.TestCase):
    """#332: objective.py imports YearResult/SimulationConfig from the data layer."""

    def test_imports_from_simulation_config_not_simulation(self):
        # AST over the whole source catches both top-level and function-local
        # imports, so a re-introduced ``from simulation import ...`` fails here.
        modules = _imported_modules(objective.__file__)
        self.assertIn("simulation_config", modules)
        self.assertNotIn(
            "simulation", modules,
            "objective.py must import from simulation_config (the canonical "
            "data/config layer), not from the simulation engine module.",
        )

    def test_year_result_is_the_canonical_class(self):
        # Behaviour-preserving: the symbols objective uses are the same objects
        # defined in simulation_config, not look-alikes.
        import simulation_config
        self.assertIs(objective.YearResult, simulation_config.YearResult)
        self.assertIs(objective.SimulationConfig, simulation_config.SimulationConfig)


class TestStrategyDoesNotImportCountryStrategies(unittest.TestCase):
    """#284: strategy.py must not import the country strategy module."""

    def test_no_country_strategies_import_anywhere_in_source(self):
        # The DP#25 inversion this issue targets: strategy.py (core) must not
        # reach outward to countries.canada.strategies. A re-introduced
        # ``from countries.canada.strategies import ...`` fails this test.
        bad = {
            m for m in _imported_modules(strategy.__file__)
            if m == "countries.canada.strategies"
        }
        self.assertEqual(bad, set())

    def test_no_country_package_import_in_list_strategies(self):
        # The discovery path must go through the jurisdiction-agnostic
        # ``countries`` registry, never a specific country package.
        modules = _imported_modules(strategy.__file__)
        self.assertNotIn("countries.canada", modules)


class TestStrategiesStillResolveViaRegistry(unittest.TestCase):
    """#284: behaviour preserved — strategies resolve through the registry."""

    def test_list_strategies_populates_from_registry(self):
        # Importing the Canada package registers its strategies inward, and
        # list_strategies() (which triggers country discovery when empty)
        # surfaces them without strategy.py importing any country module.
        strategies = strategy.list_strategies()
        self.assertGreater(len(strategies), 0)
        self.assertIn("readvance_priority", strategies)

    def test_canada_registers_strategies_at_import_time(self):
        import countries.canada  # noqa: F401  (import-time registration)
        # The registry the core module reads from is populated by the country
        # package, not by strategy.py importing Canada.
        self.assertIn("readvance_priority", strategy._STRATEGY_REGISTRY)


class TestOptimizerImportInversion(unittest.TestCase):
    """#385: optimizer.py must not import directly from countries.canada.

    DP#25: The four layers are data -> scenario -> simulation -> optimization,
    and dependencies point inward. optimizer.py (optimization layer) must use
    the jurisdiction_providers registry instead of direct imports.
    """

    def test_no_canada_rate_model_import_in_optimizer(self):
        import optimizer
        modules = _imported_modules(optimizer.__file__)
        bad = {m for m in modules if m == "countries.canada.rate_model"}
        self.assertEqual(bad, set(),
            "optimizer.py must use get_provider('rate_model') instead of "
            "direct import from countries.canada.rate_model (DP#25)")

    def test_no_canada_strategies_import_in_optimizer(self):
        import optimizer
        modules = _imported_modules(optimizer.__file__)
        bad = {m for m in modules if m == "countries.canada.strategies"}
        self.assertEqual(bad, set(),
            "optimizer.py must use list_strategies() instead of direct import "
            "from countries.canada.strategies (DP#25)")

    def test_no_canada_rate_model_import_in_monte_carlo(self):
        import monte_carlo_optimizer
        modules = _imported_modules(monte_carlo_optimizer.__file__)
        bad = {m for m in modules if m == "countries.canada.rate_model"}
        self.assertEqual(bad, set(),
            "monte_carlo_optimizer.py must use get_provider('rate_model') instead of "
            "direct import from countries.canada.rate_model (DP#25)")

    def test_no_canada_strategies_import_in_monte_carlo(self):
        import monte_carlo_optimizer
        modules = _imported_modules(monte_carlo_optimizer.__file__)
        bad = {m for m in modules if m == "countries.canada.strategies"}
        self.assertEqual(bad, set(),
            "monte_carlo_optimizer.py must use list_strategies() instead of direct import "
            "from countries.canada.strategies (DP#25)")

    def test_no_canada_strategies_import_in_scipy(self):
        import scipy_optimizer
        modules = _imported_modules(scipy_optimizer.__file__)
        bad = {m for m in modules if m == "countries.canada.strategies"}
        self.assertEqual(bad, set(),
            "scipy_optimizer.py must use list_strategies() instead of direct import "
            "from countries.canada.strategies (DP#25)")

    def test_no_canada_rate_model_import_in_dp(self):
        import dp_optimizer
        modules = _imported_modules(dp_optimizer.__file__)
        bad = {m for m in modules if m == "countries.canada.rate_model"}
        self.assertEqual(bad, set(),
            "dp_optimizer.py must use get_provider('rate_model') instead of "
            "direct import from countries.canada.rate_model (DP#25)")

    def test_no_canada_strategies_import_in_dp(self):
        import dp_optimizer
        modules = _imported_modules(dp_optimizer.__file__)
        bad = {m for m in modules if m == "countries.canada.strategies"}
        self.assertEqual(bad, set(),
            "dp_optimizer.py must use list_strategies() instead of direct import "
            "from countries.canada.strategies (DP#25)")
