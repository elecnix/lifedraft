"""Every ``call_graph.REGISTRY_DISPATCH`` row is TRUE of the live registry.

``REGISTRY_DISPATCH`` tells the static reach-detector that reaching a provider
GETTER reaches the provider class a jurisdiction package registered behind it
(a dispatch through an unresolvable receiver the AST cannot follow). A row that
is not backed by the actual registration would certify dead code as live -- the
exact failure the unreached-module guard exists to catch -- so each row is
checked here at runtime: calling the getter after importing the jurisdiction
package returns an instance of exactly the class the row names, and the getter
itself is reached from production (otherwise the row confers nothing).
"""
from __future__ import annotations

import importlib

import pytest

from call_graph import REGISTRY_DISPATCH, CallGraph


@pytest.mark.parametrize('getter,provider', sorted(REGISTRY_DISPATCH.items()))
def test_registry_dispatch_row_matches_the_live_registration(getter, provider):
    importlib.import_module('countries.canada')  # import-time registration
    getter_mod, getter_name = getter
    provider_mod, provider_cls = provider
    registered = getattr(importlib.import_module(getter_mod), getter_name)()
    cls = getattr(importlib.import_module(provider_mod), provider_cls)
    assert type(registered) is cls, (
        f"{getter_mod}.{getter_name}() returns {type(registered)!r}, not "
        f"{provider_mod}.{provider_cls} -- the REGISTRY_DISPATCH row is false")


def test_every_registry_getter_is_reached_from_production():
    graph = CallGraph()
    for getter in REGISTRY_DISPATCH:
        assert graph.is_reached(*getter), (
            f"{getter} is not reached from production, so its "
            "REGISTRY_DISPATCH row confers reachability on nothing real")
