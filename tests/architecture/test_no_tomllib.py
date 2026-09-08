"""No ``import tomllib`` anywhere in the tree: ``pyproject.toml`` declares
``requires-python = \">=3.10"`` and ships a 3.10 classifier, so every import
that must work on 3.10.

``tomllib`` only entered the stdlib in Python 3.11 (the ``tomli`` backport
is NOT a declared dependency and must not become one -- the repo prefers to
parse the small ``pyproject.toml`` slices a test needs with a regex, see
``test_numpy_runtime_dependency.py``). The nightly full-matrix leg
``test (3.10)`` silently turned RED the day three test modules started
importing it:

- ``tests/architecture/test_console_scripts.py`` (module scope; a
  collection ERROR that killed the whole module)
- ``tests/test_schema_file_split.py`` (inside
  ``test_every_declared_part_ships_in_the_installed_package``)
- (repaired by the issue that also added this guard)

This test makes the failure class unrepresentable: a future
``import tomllib`` anywhere in first-party source or tests fails CI loudly
on every leg instead of waiting for the next nightly 3.10 run.

Syntactic AST scan (same shape as ``repo_scan``): walks every ``*.py`` under
the repo root excluding virtualenvs/build artifacts, and flags any
``import tomllib`` or ``from tomllib import ...``.
"""
from __future__ import annotations

import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# mirror repo_scan.EXCLUDE_DIR_NAMES: never first-party source/tests.
_EXCLUDE_DIR_NAMES = {
    ".venv", "venv", "__pycache__", ".git", ".pytest_cache",
    "canadian_financial_optimizer.egg-info", "node_modules", "build", "dist",
}


def _tomllib_import_files() -> list[str]:
    findings = []
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIR_NAMES]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), REPO_ROOT)
            try:
                tree = ast.parse(open(os.path.join(dirpath, fn), encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    a.name == "tomllib" or a.name.startswith("tomllib.")
                    for a in node.names
                ):
                    findings.append(f"{rel}:{node.lineno}: import tomllib")
                    break
                if isinstance(node, ast.ImportFrom) and node.module == "tomllib":
                    findings.append(f"{rel}:{node.lineno}: from tomllib import ...")
                    break
    return findings


def test_no_tomllib_import_anywhere():
    """tomllib is 3.11+; this repo supports 3.10 (requires-python, 3.10
    classifier, nightly CI leg). A tomllib import anywhere -- production or
    tests -- reddens the 3.10 leg; parse pyproject.toml slices with regex
    instead (house pattern, test_numpy_runtime_dependency.py)."""
    hits = _tomllib_import_files()
    assert not hits, (
        f"tomllib is stdlib 3.11+, but the repo supports 3.10: {len(hits)} "
        f"module(s) import it and will break the nightly test (3.10) leg: "
        + "; ".join(hits)
    )