#!/usr/bin/env python3
"""
Module Registry — country package discovery (DP#16).

DP#16 extension: Package presence is a trigger. If countries/canada/
exists and is importable, Canadian modules auto-register regardless of
whether Canadian-specific fields are in the config.

Epic #603 Track C Phase 2b: this module used to ALSO carry a config-shape
merge/auto-include layer (``merge_config_with_country``/``_deep_merge``/
``check_auto_includes``/``MODULE_TRIGGERS``) built for the LEGACY input
shape (deep-merging a country overlay of default VALUES into whatever
top-level ``family``/``property``/``heloc``/... dict a caller handed in).
That whole layer is deleted here (DP#9), not kept as an unwired
alternative, because:

- ``check_auto_includes``/``MODULE_TRIGGERS``/``_has_nested_field`` had
  ZERO production callers even before this PR (confirmed by
  ``tests/test_schema_coverage.py``'s own audit) -- only tests and this
  module's own docstring ever invoked it.
- ``merge_config_with_country``/``load_country_schema``/``_deep_merge``/
  ``_apply_item_template`` had exactly ONE production caller
  (``SimulationConfig.from_json``), which no longer needs them: the input
  contract (``schema/input_schema.json`` + the Canada overlay, composed and
  validated by ``input_contract.py``) requires every field explicit --
  ``required``/``additionalProperties: false`` in the JSON Schema, not a
  runtime deep-merge of defaults into an unvalidated dict (DP#32: a
  document that omits a required fact is a validation error, not a
  silently-filled-in default). The legacy example-instance files these
  functions read defaults FROM (root ``input_schema.json`` /
  ``countries/canada/input_schema.json``) are deleted in the same PR.

What remains is genuinely jurisdiction-agnostic and still live: country
PACKAGE discovery/registration for the tax-data provider layer
(``discover_country_packages``/``CountryRegistry``/``register_all_countries``),
unrelated to input-document shape.

Usage:
    from module_registry import discover_country_packages, CountryRegistry

    registry = CountryRegistry().discover()
    registry.register_all(tax_provider)
"""

import importlib
from pathlib import Path
from typing import Callable, Dict, Optional


# ── Country package discovery (DP#16) ──────────────────────────────────────

def discover_country_packages() -> Dict[str, object]:
    """Auto-discover countries/<country>/ packages with register() functions.
    
    Per DP#16: Package presence is a trigger. If countries/canada/ is on
    disk and importable, its register() function auto-includes Canadian
    modules with the core providers.
    
    Returns:
        Dict mapping country code to register function.
    """
    countries_dir = Path(__file__).parent / "countries"
    discovered = {}
    
    if not countries_dir.is_dir():
        return discovered
    
    for country_path in sorted(countries_dir.iterdir()):
        if not country_path.is_dir():
            continue
        if country_path.name.startswith("_") or country_path.name.startswith("."):
            continue
        init_file = country_path / "__init__.py"
        if not init_file.exists():
            continue
        
        country_code = country_path.name
        try:
            module = importlib.import_module(f"countries.{country_code}")
            if hasattr(module, "register"):
                discovered[country_code] = module.register
        except ImportError:
            continue
    
    return discovered


# ── Country package registry (DP#3: explicit instance, no hidden global) ────

class CountryRegistry:
    """Registry of country packages, replacing the module-level COUNTRY_MODULES dict.
    
    DP#3: Pure functions, no hidden state. The registry is an explicit
       instance that is passed to callers, not accessed via a module-level
       global. Two independent instances share no state.
    """
    
    def __init__(self, modules: Optional[Dict[str, Callable]] = None):
        self._modules: Dict[str, Callable] = dict(modules) if modules else {}
        self._generation: int = 0
    
    def register(self, country_code: str, register_fn: Callable) -> None:
        """Register a country's register function."""
        self._modules[country_code] = register_fn
        self._generation += 1
    
    def get(self, country_code: str, default=None) -> Optional[Callable]:
        """Get a country's register function, or default if not found."""
        return self._modules.get(country_code, default)
    
    def discover(self) -> 'CountryRegistry':
        """Auto-discover country packages from the countries/ directory.
        
        Per DP#16: Package presence is a trigger. If countries/<country>/
        exists and is importable with a register() function, it is added.
        
        Returns self for chaining.
        """
        countries_dir = Path(__file__).parent / "countries"
        if not countries_dir.is_dir():
            return self
        
        added_any = False
        for country_path in sorted(countries_dir.iterdir()):
            if not country_path.is_dir():
                continue
            if country_path.name.startswith("_") or country_path.name.startswith("."):
                continue
            init_file = country_path / "__init__.py"
            if not init_file.exists():
                continue
            
            country_code = country_path.name
            if country_code in self._modules:
                continue
            
            try:
                module = importlib.import_module(f"countries.{country_code}")
                if hasattr(module, "register"):
                    self._modules[country_code] = module.register
                    added_any = True
            except ImportError:
                continue

        if added_any:
            self._generation += 1
        return self
    
    def register_all(self, tax_provider) -> None:
        """Register all discovered country packages with a TaxDataProvider.
        
        Per DP#16: Only registers packages that are importable. If a
        country package can't be imported, it's silently skipped.
        """
        for register_fn in self._modules.values():
            register_fn(tax_provider)
    
    def __len__(self) -> int:
        return len(self._modules)
    
    def __iter__(self):
        return iter(self._modules)
    
    def __contains__(self, country_code: str) -> bool:
        return country_code in self._modules
    
    @property
    def generation(self) -> int:
        """Monotonic counter bumped when _modules changes."""
        return self._generation

    @property
    def modules(self) -> Dict[str, Callable]:
        """Read-only view of registered modules."""
        return dict(self._modules)


# Module-level default registry.
# New code should accept a `registry` parameter explicitly.
default_registry = CountryRegistry()
default_registry.discover()


def register_all_countries(tax_provider) -> None:
    """Register all discovered country packages with a TaxDataProvider.
    
    Delegates to default_registry.register_all().
    Prefer passing an explicit registry instance instead.
    """
    default_registry.register_all(tax_provider)
