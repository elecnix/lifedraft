#!/usr/bin/env python3
"""Countries package — auto-discovers country modules.

Per DP#16: Package presence is a trigger. Adding a new country = adding a
directory under countries/ with an __init__.py that provides a `register()`
function. The discover_countries() function scans for such packages at runtime.

Per DP#3: The country registry is an explicit CountryRegistry instance,
not a module-level mutable dict. This replaces the old COUNTRY_MODULES
global with a class that can be explicitly passed to callers.
"""

from module_registry import CountryRegistry


def _build_default_registry() -> CountryRegistry:
    """Build the default countries registry with auto-discovery.

    Scans the countries/ directory for packages with register() functions.
    """
    registry = CountryRegistry()
    registry.discover()
    return registry


default_registry = _build_default_registry()


def register_all(tax_provider):
    """Register all known country modules with a TaxDataProvider."""
    default_registry.register_all(tax_provider)


def discover_countries():
    """Auto-discover country packages by scanning the countries/ directory.

    Per DP#16: Package presence is a trigger. If countries/<country>/ exists
    and is importable with a register() function, it auto-registers.
"""
    registry = CountryRegistry()
    registry.discover()
    return registry



