"""pyproject.toml's [tool.setuptools] py-modules list must match the repo root.

Issue #250, part (b): ``py-modules`` listed 68 modules while 83 root ``*.py``
files existed -- 15 modules shipped in nothing but the source tree (and an
editable install's MAPPING finder resolves root modules from the tree anyway,
so nothing surfaced until someone did a real ``pip install``). Worse than the
gap is that it only grew: ``net_benefit_legs`` appeared on `main` three hours
after #243 created it, with no mechanism to notice. This guard makes the
staleness class unrepresentable, under the same allowlist discipline as the
DP#32 guard (test_dp32_zero_fallback.py):

* **unlisted module fails** -- every root ``*.py`` must be named in
  ``py-modules`` or be in the explicit, cited exclusion set below. The list
  cannot silently grow: a new split that forgets its module (as #236/#237/#243
  did) reddens CI the commit it lands.
* **stale exclusion fails** -- every exclusion entry must still name a real
  root file, and must not also appear in ``py-modules`` (an entry is either
  excluded or shipped, never both -- the disjointness rule of the DP#32
  allowlist). The list cannot silently rot either way.
* **fossil listing fails** -- every ``py-modules`` entry must still name a
  real root file. A module deleted from the tree leaves an entry advertising
  a module the wheel cannot contain.

Stdlib-only, no TOML parser: ``tomllib`` is 3.11+ and these tests must run on
3.10 (house pattern, see test_numpy_runtime_dependency.py). The list is a
quoted, comma-separated row under ``[tool.setuptools]`` -- parse that slice
with a regex, and fail LOUDLY (DP#32) if the table or list shape changes, so
a parse that no longer matches can never vacuously pass.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# Root *.py files deliberately NOT shipped via py-modules. Every entry must
# carry a reason; one whose file disappears (or that moves into py-modules)
# fails test_py_modules_exclusions_are_not_stale, so this set cannot quietly
# rot either.
_EXCLUDED_ROOT_MODULES = {
    "conftest.py": (
        "pytest's root hook/collection entry point, never imported as a "
        "top-level module by the product and not installed by the wheel "
        "(test-only infrastructure, like the other conftest.py files)."
    ),
    "__init__.py": (
        "empty package marker for the repo root, not a module with behaviour "
        "to ship; packages.find() treats directories with __init__.py as "
        "packages, this root marker is not part of the wheel."
    ),
}


def _py_modules() -> list[str]:
    """Parse the [tool.setuptools] py-modules list straight out of the source
    pyproject.toml. Regex slice, not a TOML parser: tomllib is 3.11+ and these
    tests run on 3.10 (house pattern, test_numpy_runtime_dependency.py /
    test_schema_file_split.py). The asserts on table/list presence are
    load-bearing: a parse that matches nothing must fail the build, never
    vacuously pass (DP#32).
    """
    text = _PYPROJECT.read_text()
    table = re.search(r"(?m)^\[tool\.setuptools\]\s*$(.*?)(?=^\[|\Z)", text, re.S)
    assert table, "no [tool.setuptools] table found in pyproject.toml"
    block = re.search(r"(?m)^py-modules\s*=\s*\[(.*?)\]", table.group(1), re.S)
    assert block, (
        "no py-modules list found in [tool.setuptools]; the parity guard "
        "cannot run against a list that is not there"
    )
    modules = re.findall(r'"([^"]+)"', block.group(1))
    assert modules, (
        "py-modules parsed to an empty list; the parity guard cannot " "vacuously pass"
    )
    return modules


def _root_py_files() -> set[str]:
    return {p.name for p in _REPO_ROOT.glob("*.py")}


def test_every_root_module_is_listed_or_excluded():
    """Every root *.py is in py-modules or in the cited exclusion set. A new
    root module that forgets its py-modules entry fails here, naming it --
    issue #250's 15 would each have been caught the commit they appeared."""
    listed = set(_py_modules())
    on_disk = _root_py_files()
    missing = sorted(
        f
        for f in on_disk
        if f[:-3] not in listed and f not in _EXCLUDED_ROOT_MODULES
    )
    assert not missing, (
        "py-modules parity (issue #250): these root *.py files are neither in "
        "[tool.setuptools] py-modules nor in the cited exclusion set of "
        "tests/architecture/test_py_modules_parity.py:\n"
        + "\n".join(f"  {f}" for f in missing)
        + "\nAdd each to py-modules, or to _EXCLUDED_ROOT_MODULES with a reason."
    )


def test_py_modules_entries_all_exist_on_disk():
    """A py-modules entry whose root file is gone advertises a module the
    wheel cannot contain -- the list cannot silently rot towards fiction."""
    listed = set(_py_modules())
    on_disk = _root_py_files()
    fossils = sorted(f[:-3] for f in listed if f + ".py" not in on_disk)
    assert not fossils, (
        "py-modules lists modules with no root file on disk; delete these "
        "stale entries from pyproject.toml or restore the files:\n"
        + "\n".join(f"  {m}" for m in fossils)
    )


def test_py_modules_exclusions_are_not_stale():
    """A stale exclusion entry is a silent lie either way: the file it names
    is gone, or the module has moved into py-modules and the exclusion should
    have left with it. Same staleness discipline as
    test_dp32_allowlist_has_no_stale_entries."""
    on_disk = _root_py_files()
    listed = set(_py_modules())
    for filename, reason in sorted(_EXCLUDED_ROOT_MODULES.items()):
        assert filename in on_disk, (
            "exclusion entry "
            f"{filename!r} names a root file that no longer exists; remove "
            f"the entry (its reason was: {reason})"
        )
        assert filename[:-3] not in listed, (
            f"exclusion entry {filename!r} names a module now listed in "
            "py-modules; an entry is either excluded or shipped, never both "
            "-- remove it from _EXCLUDED_ROOT_MODULES"
        )