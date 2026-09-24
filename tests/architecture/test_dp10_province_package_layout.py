"""DP#10: every province is a package, ``countries/<country>/provinces/<province>/``.

Issue #87. DESIGN_PRINCIPLES.md DP#10 says the directory structure mirrors the
political hierarchy, with provincial modules under
``countries/<country>/provinces/<province>/``. Quebec followed it; Ontario did
not: ``provinces/ontario.py`` and ``provinces/ontario_credits.py`` sat loose at
the ``provinces/`` level, one directory too high.

Why a layout test and not just "the imports work": import success cannot see
this defect, in either direction.

* A loose ``provinces/ontario.py`` left beside a ``provinces/ontario/`` package
  is SHADOWED by the package at import time. Every import keeps working; the
  stale file just sits there, a second spelling of the province (DP#9).
* A province directory without ``__init__.py`` still imports, as an implicit
  namespace package, in an editable install. But ``setuptools.packages.find``
  (the non-editable install path) leaves it out of the wheel, and the coverage
  gate never measures an untracked file. Green locally, broken when shipped.

So these tests read the file tree itself, and cross-check it against the
province registry (``PROVINCE_MODULES``) so that the scan cannot pass by
finding nothing.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from repo_scan import ROOT  # noqa: E402

COUNTRIES_DIR = os.path.join(ROOT, "countries")


def _is_ignored_dir(name: str) -> bool:
    return name == "__pycache__" or name.startswith(".")


def _provinces_dirs() -> Dict[str, str]:
    """country code -> absolute path of ``countries/<country>/provinces/``."""
    found: Dict[str, str] = {}
    for country in sorted(os.listdir(COUNTRIES_DIR)):
        if _is_ignored_dir(country):
            continue
        candidate = os.path.join(COUNTRIES_DIR, country, "provinces")
        if os.path.isdir(candidate):
            found[country] = candidate
    return found


def _province_dirs(provinces_dir: str) -> List[str]:
    """Names of the subdirectories directly under a ``provinces/`` directory."""
    return sorted(
        name
        for name in os.listdir(provinces_dir)
        if os.path.isdir(os.path.join(provinces_dir, name)) and not _is_ignored_dir(name)
    )


def _scanned_provinces_dirs() -> Dict[str, str]:
    """``_provinces_dirs()``, refusing an empty result so no test below can pass
    on a scan that looked in the wrong place."""
    found = _provinces_dirs()
    assert found, (
        f"scan found nothing: no countries/<country>/provinces/ directory under "
        f"{os.path.relpath(COUNTRIES_DIR, ROOT)!r}. Path moved?"
    )
    return found


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def test_provinces_scan_is_not_vacuous():
    """A scan rooted at the wrong path finds nothing and passes everything below.
    Refuse that: there must be at least one ``provinces/`` directory holding at
    least one province package."""
    provinces = _provinces_dirs()
    assert provinces, (
        f"scan found nothing: no countries/<country>/provinces/ directory under "
        f"{_rel(COUNTRIES_DIR)!r}. Path moved?"
    )
    for country, path in provinces.items():
        assert _province_dirs(path), (
            f"scan found nothing: {_rel(path)} contains no province package. Path moved?"
        )


def test_no_loose_modules_under_provinces():
    """Directly under ``provinces/``, the only Python file allowed is ``__init__.py``.
    Any other module belongs in its province's package (DP#10)."""
    loose = []
    for country, path in _scanned_provinces_dirs().items():
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isfile(full) and name.endswith(".py") and name != "__init__.py":
                loose.append(_rel(full))
    assert not loose, (
        "DP#10: provincial modules must live in countries/<country>/provinces/<province>/, "
        "not loose in provinces/. Move each of these into its province package "
        "(e.g. countries/canada/provinces/<province>/<file>.py): " + ", ".join(loose)
    )


def test_every_province_directory_is_a_package():
    """Every province directory needs ``__init__.py``. Without it Python still
    imports it as a namespace package locally, but the wheel build omits it."""
    missing = []
    checked = 0
    for country, path in _scanned_provinces_dirs().items():
        for name in _province_dirs(path):
            checked += 1
            init = os.path.join(path, name, "__init__.py")
            if not os.path.isfile(init):
                missing.append(_rel(init))
    assert checked, "scan found nothing: no province directory to check. Path moved?"
    assert not missing, f"province directories without __init__.py: {missing}"


def test_province_modules_resolve_to_their_own_package():
    """Each registered province class is defined inside its own province package,
    and the set of province packages on disk is exactly the set of registered
    provinces: no unregistered package, no registered province without one."""
    provinces = _scanned_provinces_dirs()
    for country, path in provinces.items():
        pkg = f"countries.{country}.provinces"
        registry = importlib.import_module(pkg).PROVINCE_MODULES
        assert registry, f"{pkg}.PROVINCE_MODULES is empty"

        on_disk = set(_province_dirs(path))
        registered = {cls.PROVINCE for cls in registry.values()}
        assert on_disk == registered, (
            f"province packages on disk under {_rel(path)} {sorted(on_disk)} differ from "
            f"the provinces registered in {pkg}.PROVINCE_MODULES {sorted(registered)}"
        )

        wrong = []
        for code, cls in sorted(registry.items()):
            parts = cls.__module__.split(".")
            expected_prefix = pkg.split(".") + [cls.PROVINCE]
            if parts[: len(expected_prefix)] != expected_prefix or len(parts) != len(expected_prefix) + 1:
                wrong.append(
                    f"{code!r} -> {cls.__qualname__} is defined in {cls.__module__}, "
                    f"expected {pkg}.{cls.PROVINCE}.<module>"
                )
        assert not wrong, "province classes outside their own package: " + "; ".join(wrong)

    canada = importlib.import_module("countries.canada.provinces").PROVINCE_MODULES
    assert canada["on"] is canada["ontario"], "'on' and 'ontario' resolve to different classes"
    assert canada["qc"] is canada["quebec"], "'qc' and 'quebec' resolve to different classes"


def test_no_shim_at_old_ontario_paths():
    """DP#9: no compatibility shim at the pre-#87 locations. The old module path
    must not resolve, and ``provinces.ontario`` must be the package, not a module."""
    assert importlib.util.find_spec("countries.canada.provinces.ontario_credits") is None, (
        "countries.canada.provinces.ontario_credits still resolves: a shim or stale "
        "file remains at countries/canada/provinces/ontario_credits.py"
    )
    ontario = importlib.import_module("countries.canada.provinces.ontario")
    assert hasattr(ontario, "__path__"), (
        f"countries.canada.provinces.ontario is a plain module ({ontario.__file__}), "
        "not the province package"
    )
