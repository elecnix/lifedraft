#!/usr/bin/env python3
"""Jurisdiction Provider Registry — DP#25 adapter pattern.

Per DP#25: core modules must not import from jurisdiction packages.
Instead, jurisdiction packages register their providers here, and
core modules access them through the registry.

This module provides the registry and convenience accessors.
Jurisdiction packages (e.g., countries/canada/__init__.py) call
register_provider() to make their functions available.

Usage (in countries/canada/__init__.py):
    from jurisdiction_providers import register_provider
    register_provider('strategies', STRATEGIES)
    register_provider('rate_model', {
        'RatePath': RatePath,
        'build_rate_path': build_rate_path,
        'amortization_schedule': amortization_schedule,
        'annual_summary': annual_summary,
    })

Usage (in core modules):
    from jurisdiction_providers import get_provider
    STRATEGIES = get_provider('strategies')
    RatePath = get_provider('rate_model')['RatePath']

When no provider is registered, get_provider() raises KeyError with a
helpful message explaining that a jurisdiction package needs to be imported.

Backward compatibility:
    If no provider is registered, the registry falls back to importing from
    countries.canada (auto-imported as fallback). This ensures existing
    code continues to work while migration is in progress.
"""

import warnings
from typing import Any, Dict

_PROVIDERS: Dict[str, Any] = {}


def register_provider(name: str, provider: Any) -> None:
    """Register a jurisdiction provider.

    Args:
        name: Provider name (e.g., 'strategies', 'rate_model', 'tax_calc')
        provider: The provider object (module, dict, class, etc.)
    """
    _PROVIDERS[name] = provider


def get_provider(name: str) -> Any:
    """Get a registered jurisdiction provider.

    Falls back to importing from countries.canada if no provider is
    warning if no provider is registered. This ensures backward
    compatibility during migration.

    Args:
        name: Provider name

    Returns:
        The registered provider object

    Raises:
        KeyError: If no provider is registered and fallback import fails
    """
    if name in _PROVIDERS:
        return _PROVIDERS[name]

    # Fallback: import from countries.canada if no provider is registered
    try:
        if name == 'strategies':
            from countries.canada.strategies import STRATEGIES
            register_provider('strategies', STRATEGIES)
            return STRATEGIES
        elif name == 'rate_model':
            from countries.canada.rate_model import (
                RatePath, HELOCPath, build_rate_path,
                amortization_schedule, annual_summary,
            )
            provider = {
                'RatePath': RatePath,
                'HELOCPath': HELOCPath,
                'build_rate_path': build_rate_path,
                'amortization_schedule': amortization_schedule,
                'annual_summary': annual_summary,
            }
            register_provider('rate_model', provider)
            return provider
        elif name == 'tax_calc':
            import countries.canada.tax_calc as tax_calc
            register_provider('tax_calc', tax_calc)
            return tax_calc
        elif name == 'estate':
            from countries.canada.estate import (
                compute_estate, couple_terminal_returns, EstatePlan, EstateResult,
                after_tax_networth_of_own_accounts,
            )
            from countries.canada.cca import recapture_on_disposition
            provider = {
                'compute_estate': compute_estate,
                # #705: the couple -> death-ordered terminal-return list mapping
                # (reads the ITA couple plan fields), resolved through the seam
                # so objective.py keeps zero countries imports (DP#25).
                'couple_terminal_returns': couple_terminal_returns,
                'EstatePlan': EstatePlan,
                'EstateResult': EstateResult,
                # epic #841 bite 4: per-member after-tax net worth, resolved
                # through the same seam so objective.py keeps zero countries
                # imports (DP#25).
                'after_tax_networth_of_own_accounts': after_tax_networth_of_own_accounts,
                # #694: CCA recapture on the rental's deemed disposition, resolved
                # through the same seam so objective.py stays countries-free (DP#25).
                'recapture_on_disposition': recapture_on_disposition,
            }
            register_provider('estate', provider)
            return provider
        else:
            raise KeyError(
                f"No provider registered for '{name}' and no fallback available. "
                f"Register a provider using jurisdiction_providers.register_provider()."
            )
    except ImportError as e:
        raise KeyError(
            f"No provider registered for '{name}' and fallback import failed: {e}. "
            f"Ensure the jurisdiction package is installed or register a provider."
        )


def clear_providers() -> None:
    """Clear all registered providers (for testing)."""
    _PROVIDERS.clear()

# ── Auto-registration: discover and register available jurisdiction packages ──
# This ensures providers are available at import time, even when tests
# import optimizer before countries.canada is explicitly imported.
def _auto_register():
    """Auto-discover and register jurisdiction providers.

    Per DP#16: package presence is a trigger. If countries/canada/ exists
    on disk, we eagerly register its providers.
    """
    from pathlib import Path
    countries_dir = Path(__file__).parent / "countries"
    if not countries_dir.is_dir():
        return

    # Try importing and registering
    try:
        from countries.canada.strategies import STRATEGIES
        register_provider('strategies', STRATEGIES)
    except ImportError:
        pass

    try:
        from countries.canada.rate_model import (
            RatePath, HELOCPath, build_rate_path,
            amortization_schedule, annual_summary,
        )
        register_provider('rate_model', {
            'RatePath': RatePath,
            'HELOCPath': HELOCPath,
            'build_rate_path': build_rate_path,
            'amortization_schedule': amortization_schedule,
            'annual_summary': annual_summary,
        })
    except ImportError:
        pass

    # Issue #732 (DP#25): the estate tax math lives in the jurisdiction
    # package; the optimization layer (objective.py) resolves it through this
    # registry seam instead of importing countries.canada.estate directly.
    try:
        from countries.canada.estate import (
            compute_estate, couple_terminal_returns, EstatePlan, EstateResult,
            after_tax_networth_of_own_accounts,
        )
        from countries.canada.cca import recapture_on_disposition
        register_provider('estate', {
            'compute_estate': compute_estate,
            # #705: couple -> death-ordered terminal-return list (DP#25 seam).
            'couple_terminal_returns': couple_terminal_returns,
            'EstatePlan': EstatePlan,
            'EstateResult': EstateResult,
            # epic #841 bite 4: per-member after-tax net worth (DP#25 seam).
            'after_tax_networth_of_own_accounts': after_tax_networth_of_own_accounts,
            # #694: CCA recapture on the rental's deemed disposition (DP#25 seam).
            'recapture_on_disposition': recapture_on_disposition,
        })
    except ImportError:
        pass

_auto_register()
