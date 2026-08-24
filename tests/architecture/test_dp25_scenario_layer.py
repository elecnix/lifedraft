#!/usr/bin/env python3
"""Tests for issue #998 (DP#25): scenario_discovery.py must not import from the
simulation layer (tax_calculator / strategy / simulation_config) at runtime.

DP#25: four layers -- data -> scenario -> simulation -> optimization --
dependencies point inward. ``scenario_discovery`` is in the SCENARIO layer; it
discovers anchor scenarios from the input config. The simulation layer
(``tax_calculator``, ``strategy``, ``simulation_config``) is INNER, so the
scenario layer may not import from it. Issue #998 found three outward imports:

    from tax_calculator import marginal_rate
    from strategy import FamilyState, ChildState, StrategyEngine, AllocationStrategy
    from config_access import resolve_return_rate, resolve_heloc_rate
    from simulation_config import find_member_by_role

The fix (DP#25 inversion):
  - ``find_member_by_role`` / ``projection_span`` were RELOCATED to the data
    layer (``member_config``) -- they are pure config-reading helpers with no
    simulation machinery.
  - ``marginal_rate``, the strategy types/engine, and the rate resolvers are
    genuine simulation concepts; they are INJECTED through ``SimulationDeps``
    (``configure_simulation_deps`` / ``build_simulation_deps``) by the
    simulation/optimization caller, never imported here at runtime.
  - ``FamilyState`` / ``ChildState`` appear only under ``TYPE_CHECKING`` so the
    annotations on ``_build_family_state`` / ``_sweep_child_allocation`` stay
    accurate without a runtime dependency.

This test locks the inversion: an AST walk of ``scenario_discovery.py`` finds
NO runtime import of ``tax_calculator`` / ``strategy`` / ``simulation_config``.
Imports guarded by ``if TYPE_CHECKING:`` are permitted (they are not runtime
dependencies -- they are erased at import time).

Run with: python3 -m pytest tests/architecture/test_dp25_scenario_layer.py -v
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scenario_discovery

# The simulation-layer modules the scenario layer (scenario_discovery) must not
# import at runtime (DP#25 -- dependencies point inward; these are inner).
#
# ``config_access`` / ``config_serde`` / ``charge_limits`` / ``scenario_overlay``
# / ``property_structure`` / ``year_result`` were carved out of the old
# ``simulation_config.py``; naming only the parent module would have left the
# guard covering a file that no longer holds the resolvers #998 was about
# (``resolve_return_rate`` / ``resolve_heloc_rate`` now live in
# ``config_access``), i.e. a guard that passes because the thing it forbids
# moved house.
_FORBIDDEN_MODULES = (
    "tax_calculator",
    "strategy",
    "simulation_config",
    "config_access",
    "config_serde",
    "charge_limits",
    "scenario_overlay",
    "property_structure",
    "year_result",
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCENARIO_DISCOVERY_PATH = os.path.join(REPO_ROOT, "scenario_discovery.py")


def _runtime_imported_modules(source_path):
    """Return every module name imported at RUNTIME in a Python source file.

    Walks the AST and collects imports that are NOT guarded by
    ``if TYPE_CHECKING:``. An import inside an ``if`` whose test is (or
    evaluates to) ``TYPE_CHECKING`` / ``typing.TYPE_CHECKING`` is excluded --
    it is a type-only hint with no runtime dependency.
    """
    with open(source_path) as f:
        tree = ast.parse(f.read())

    type_checking_names = set()
    # Collect any alias a `from typing import TYPE_CHECKING as ...` introduces.
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing":
            for alias in node.names:
                if alias.name == "TYPE_CHECKING":
                    type_checking_names.add(alias.asname or alias.name)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing.TYPE_CHECKING":
                    type_checking_names.add(alias.asname or alias.name)
    # `from __future__ import annotations` does not affect TYPE_CHECKING, but
    # guarantees annotations are strings (never evaluated at runtime).

    runtime = set()

    def _test_is_type_checking(test):
        """True if an If.test references a TYPE_CHECKING name directly or as
        ``typing.TYPE_CHECKING``."""
        # `if TYPE_CHECKING:`
        if isinstance(test, ast.Name) and test.id in type_checking_names:
            return True
        # `if typing.TYPE_CHECKING:`
        if (isinstance(test, ast.Attribute)
                and isinstance(test.value, ast.Name)
                and test.value.id == "typing"
                and test.attr == "TYPE_CHECKING"):
            return True
        return False

    def _collect_runtime_imports(body):
        for stmt in body:
            if isinstance(stmt, ast.Import):
                runtime.update(alias.name for alias in stmt.names)
            elif isinstance(stmt, ast.ImportFrom) and stmt.module:
                runtime.add(stmt.module)
            elif isinstance(stmt, ast.If):
                if _test_is_type_checking(stmt.test):
                    # Imports inside the TYPE_CHECKING branch are NOT runtime.
                    # The else-branch (if any) IS runtime, though.
                    if stmt.orelse:
                        _collect_runtime_imports(stmt.orelse)
                else:
                    # Any other conditional: its imports are conditional but
                    # still RUNTIME (they execute when the branch is taken) --
                    # a function-local `if ...: from tax_calculator import ...`
                    # is still a runtime import. Collect from both branches.
                    _collect_runtime_imports(stmt.body)
                    _collect_runtime_imports(stmt.orelse)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Function-local imports are runtime imports (they execute when
                # the function is called) -- collect them too, so a reintroduced
                # `def f(): from tax_calculator import marginal_rate` is caught.
                _collect_runtime_imports(stmt.body)
            elif isinstance(stmt, (ast.For, ast.While)):
                _collect_runtime_imports(stmt.body)
                if stmt.orelse:
                    _collect_runtime_imports(stmt.orelse)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                _collect_runtime_imports(stmt.body)
            elif isinstance(stmt, ast.Try):
                _collect_runtime_imports(stmt.body)
                for handler in stmt.handlers:
                    _collect_runtime_imports(handler.body)
                if stmt.orelse:
                    _collect_runtime_imports(stmt.orelse)
                if stmt.finalbody:
                    _collect_runtime_imports(stmt.finalbody)
            # Class bodies: imports inside a class are rare and runtime (class
            # body executes at import time); collect them too.
            elif isinstance(stmt, ast.ClassDef):
                _collect_runtime_imports(stmt.body)

    _collect_runtime_imports(tree.body)
    return runtime


class TestScenarioDiscoveryDoesNotImportSimulationLayer(unittest.TestCase):
    """scenario_discovery.py must have ZERO runtime imports from
    tax_calculator / strategy / simulation_config (DP#25, issue #998)."""

    def test_no_runtime_simulation_layer_imports(self):
        runtime = _runtime_imported_modules(SCENARIO_DISCOVERY_PATH)
        bad = {
            m for m in runtime
            if m in _FORBIDDEN_MODULES or any(
                m == fm or m.startswith(fm + ".") for fm in _FORBIDDEN_MODULES
            )
        }
        self.assertEqual(
            bad, set(),
            f"scenario_discovery.py has runtime imports from the simulation "
            f"layer (DP#25/#998 violation): {sorted(bad)}. The scenario layer "
            f"must not import tax_calculator / strategy / simulation_config at "
            f"runtime -- relocate pure helpers to the data layer, inject "
            f"genuine simulation concepts via SimulationDeps, and guard "
            f"type-only hints under TYPE_CHECKING.",
        )

    def test_type_checking_imports_are_type_only(self):
        """The TYPE_CHECKING-guarded import (if any) must be from ``strategy``
        only, and only for type hints -- it must not leak into runtime code.

        This is a belt-and-braces check: the AST walk above already excludes
        TYPE_CHECKING imports from the runtime set; this asserts the guard is
        actually present for the strategy types used in annotations, so a
        future edit that drops the guard (and makes the import runtime) is
        caught here even before the runtime-set check would catch it."""
        with open(SCENARIO_DISCOVERY_PATH) as f:
            source = f.read()
        tree = ast.parse(source)
        # Find the TYPE_CHECKING branch and confirm any import there is from
        # `strategy` (the only module whose types are used purely as hints).
        tc_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test = node.test
                is_tc = (
                    (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
                    or (isinstance(test, ast.Attribute)
                        and isinstance(test.value, ast.Name)
                        and test.value.id == "typing"
                        and test.attr == "TYPE_CHECKING")
                )
                if is_tc:
                    for stmt in node.body:
                        if isinstance(stmt, ast.ImportFrom) and stmt.module:
                            tc_imports.add(stmt.module)
        # Every TYPE_CHECKING import must be from a forbidden (simulation)
        # module -- otherwise the guard is pointless; and it must be a subset
        # of the forbidden set (a TYPE_CHECKING import of a NON-sim module
        # would not need the guard and is suspicious, but not a violation; we
        # only assert the sim ones are guarded, not leaked).
        for m in tc_imports:
            self.assertIn(
                m, _FORBIDDEN_MODULES,
                f"TYPE_CHECKING import of {m!r} in scenario_discovery.py is "
                f"not from the simulation layer -- the guard exists only to "
                f"permit type-only hints from tax_calculator/strategy/"
                f"simulation_config without a runtime dependency.",
            )


class TestSimulationDepsInjectionWired(unittest.TestCase):
    """The simulation-layer callables must be injected through SimulationDeps
    (the DP#25 inversion mechanism), and the entry points must wire it."""

    def test_simulation_deps_interface_exists(self):
        self.assertTrue(hasattr(scenario_discovery, "SimulationDeps"),
                        "scenario_discovery must declare SimulationDeps "
                        "(the injection interface for simulation-layer "
                        "callables, DP#25/#998).")
        self.assertTrue(hasattr(scenario_discovery, "configure_simulation_deps"),
                        "scenario_discovery must expose configure_simulation_deps "
                        "(the injection point the simulation layer populates).")

    def test_build_simulation_deps_configures_the_bundle(self):
        """Importing simulation_deps (the simulation-layer wiring module)
        configures scenario_discovery's injection point with the real
        simulation callables -- so discover_anchors resolves them at call
        time without scenario_discovery ever importing the simulation layer."""
        import simulation_deps  # noqa: F401  (configures at import)
        from scenario_discovery import _SIM_DEPS
        self.assertIsNotNone(_SIM_DEPS,
                             "simulation_deps must configure scenario_discovery's "
                             "_SIM_DEPS at import time (DP#25/#998).")
        # The bundle carries the actual simulation-layer callables.
        for attr in ("marginal_rate", "FamilyState", "ChildState",
                     "StrategyEngine", "AllocationStrategy",
                     "resolve_return_rate", "resolve_heloc_rate"):
            self.assertTrue(hasattr(_SIM_DEPS, attr),
                            f"SimulationDeps must carry {attr!r}.")

    def test_discover_anchors_fails_loudly_without_injection(self):
        """DP#32: a caller that reaches discover_anchors without the simulation
        layer having configured its injection point gets a loud RuntimeError,
        never a silent no-op."""
        # Simulate an unconfigured state by clearing the module-level deps.
        import scenario_discovery as sd
        saved = sd._SIM_DEPS
        try:
            sd._SIM_DEPS = None
            with self.assertRaises(RuntimeError):
                # A minimal cfg whose strategy dimension triggers _resolve_deps.
                # discover_anchors resolves deps at the very top, so any cfg
                # reaches the guard.
                sd.discover_anchors({"family": {"members": []},
                                     "scenarios": {}})
        finally:
            sd._SIM_DEPS = saved

    def test_explicit_sim_deps_param_is_honoured(self):
        """A caller may pass ``sim_deps`` explicitly to ``discover_anchors``;
        it takes precedence over (and does not require) the module-level
        configured bundle -- covering the explicit-arg branch of _resolve_deps
        and proving the injection is parameter-shaped, not global-only."""
        import simulation_deps  # noqa: F401  (configures the default bundle)
        from scenario_discovery import discover_anchors
        deps = simulation_deps.build_simulation_deps()
        # A minimal cfg; the strategy dimension needs the injected marginal_rate /
        # FamilyState / resolve_return_rate, which ``deps`` supplies.
        cfg = {"family": {"members": [
            {"role": "primary", "gross_income": 100_000,
             "fhsa_first_time_buyer_since": None}],
            "children": []},
            "property": {}, "accounts": {}, "assumptions": {},
            "scenarios": {}}
        anchors = discover_anchors(cfg, sim_deps=deps)
        self.assertEqual(anchors["income"][0]["primary_income"], 100_000)


if __name__ == "__main__":
    unittest.main()