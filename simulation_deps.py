#!/usr/bin/env python3
"""Simulation-layer dependency bundle for the scenario layer (DP#25, #998).

DP#25: the scenario layer (``scenario_discovery``) must not import from the
simulation layer (``tax_calculator`` / ``strategy`` / ``simulation_config``).
The simulation concepts the scenario layer needs at call time -- ``marginal_rate``,
the strategy dataclasses/engine, and the rate resolvers -- are genuine
simulation-layer symbols, so they cannot be relocated to a lower layer. Instead
they are INJECTED: this module (simulation layer) constructs a
``SimulationDeps`` bundle from the simulation layer's own symbols and hands it
to ``scenario_discovery.configure_simulation_deps`` at import time.

The dependency direction stays inward:
  - ``scenario_discovery`` (scenario) declares the ``SimulationDeps`` interface
    and resolves the bundle at call time -- it never imports
    ``tax_calculator`` / ``strategy`` / ``simulation_config`` at runtime.
  - this module (simulation) imports those simulation-layer symbols (sim -> sim,
    fine) and the scenario layer's interface (sim -> scenario, inward for the
    optimization/entry-point layer that wires the pipeline).

The entry points ``simulate.py`` and ``optimize.py`` call
``configure_simulation_deps(build_simulation_deps())`` at module top so every
``discover_anchors(cfg)`` call thereafter resolves the injected bundle. A caller
that imports ``scenario_discovery`` in isolation (e.g. a unit test) must import
this module (or an entry point) first so the bundle is configured -- the
scenario layer fails loudly (DP#32) rather than silently no-op'ing if it is not.
"""

from tax_calculator import marginal_rate
from strategy import FamilyState, ChildState, StrategyEngine, AllocationStrategy
from simulation_config import resolve_return_rate, resolve_heloc_rate

from scenario_discovery import SimulationDeps


def build_simulation_deps() -> SimulationDeps:
    """Construct the ``SimulationDeps`` bundle from the simulation layer's own
    symbols (DP#25/#998 injection point).

    Byte-exact with the pre-#998 direct imports: the very same callables/classes
    ``scenario_discovery`` used to import at module top are handed through the
    bundle, so every computed value is identical by construction.
    """
    return SimulationDeps(
        marginal_rate=marginal_rate,
        FamilyState=FamilyState,
        ChildState=ChildState,
        StrategyEngine=StrategyEngine,
        AllocationStrategy=AllocationStrategy,
        resolve_return_rate=resolve_return_rate,
        resolve_heloc_rate=resolve_heloc_rate,
    )


# Configure the scenario layer's injection point at import time so any caller
# that imports this module (directly or via an entry point) has the bundle
# available for every subsequent ``discover_anchors`` call.
from scenario_discovery import configure_simulation_deps as _configure

_configure(build_simulation_deps())