"""The package re-export shims must actually re-export.

These two `__init__.py` files were BOTH at zero coverage: nothing in the test suite
imported them, so nothing checked that the ~58 names they advertise resolve. A
re-export block is exactly the shape that rots silently -- a module gets renamed or
deleted, the `__all__` entry stays, and the failure only surfaces for whoever first
does `from lifedraft import ThatName`.

That is not hypothetical here. The same rot in `pyproject.toml`'s `[project.scripts]`
shipped two console entry points (`family-optimize`, `compare-scenarios`) that are
installed onto PATH and die with ModuleNotFoundError, because the modules behind them
were deleted and the metadata was not (issue #749).

Written to take these files off the zero-coverage list by TESTING them, which is the
only legitimate way off that list.
"""

import importlib

import pytest

from root_package_loader import load_root_package


def test_root_package_imports():
    """DP#25: the root package must import with no jurisdiction-specific dependency."""
    root = load_root_package()
    assert root.__all__, "root package declares no __all__"


def test_every_name_in_root_dunder_all_actually_resolves():
    """A name in `__all__` that does not resolve is an advertised export that does not
    exist -- `from lifedraft import X` raises ImportError for anyone who believes it."""
    root = load_root_package()
    missing = [name for name in root.__all__ if not hasattr(root, name)]
    assert not missing, f"__all__ advertises names the package does not define: {missing}"


def test_provinces_package_reexports_resolve():
    """The provinces package maps province code -> implementation. A broken re-export
    here silently removes a province."""
    prov = importlib.import_module("countries.canada.provinces")
    for name in (
        "QuebecTaxData",
        "QuebecDeductionTracker",
        "compute_sm_qc_benefit",
        "quebec_interest_deduction",
        "quebec_sm_portfolio_optimization",
    ):
        assert hasattr(prov, name), f"provinces package does not re-export {name}"


@pytest.mark.parametrize("dotted", ["countries", "countries.canada", "countries.canada.provinces"])
def test_country_packages_import_cleanly(dotted):
    assert importlib.import_module(dotted) is not None
