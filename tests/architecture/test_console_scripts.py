"""Every [project.scripts] entry point resolves to a real, importable callable.

Issue #749: ``pyproject.toml`` advertised two console entry points --
``family-optimize = "family_optimize:main"`` and
``compare-scenarios = "compare_scenarios:main"`` -- but the target modules had
been deleted (folded into the single-script ``simulate.py``/``optimize.py``
architecture). ``pip install -e`` still installed both commands on ``PATH``,
so each hard-crashed with ``ModuleNotFoundError`` on the very first invocation.
"Advertised, installed, does nothing" -- precisely the silent-success failure
mode this repo exists to eliminate.

This test is the guard that absence lacked. It parses ``[project.scripts]``
straight out of ``pyproject.toml`` (not via an installed dist, so it bites in
CI before any wheel is built), imports each ``module:attr`` target, and asserts
the named attribute exists and is callable. A future dead entry point fails CI
loudly instead of shipping a command that crashes on first use.

This is an invariant-style guard, not a behavioural test: it does not execute
the CLIs (their ``--help`` is exercised in the install-verification step of the
PR), only that the wiring points somewhere real.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _console_scripts() -> dict[str, str]:
    """Return ``{command_name: "module:attr"}`` for every [project.scripts] entry.

    Read straight from the source ``pyproject.toml`` rather than consulting an
    installed distribution metadata: the whole point of #749 was that the
    installed entry points lied about a module that no longer existed in the
    source tree, so the guard must verify against the tree, not the wheel.
    """
    with _PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    scripts = data.get("project", {}).get("scripts", {})
    if not scripts:
        pytest.fail("pyproject.toml has no [project.scripts]; the guard expects >=1 entry")
    return dict(scripts)


@pytest.mark.parametrize("name, target", sorted(_console_scripts().items()))
def test_console_script_target_is_callable(name: str, target: str) -> None:
    """Each advertised console script resolves to an importable, callable target."""
    # setuptools script spec is ``module:attr`` (attr optional, defaults to
    # ``main``). Reject anything else -- a malformed entry is itself a bug.
    if ":" in target:
        module_name, attr = target.split(":", 1)
    else:
        module_name, attr = target, "main"

    assert module_name and attr, (
        f"[project.scripts] {name} = {target!r}: malformed, expected 'module:attr'"
    )

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"[project.scripts] {name} = {target!r}: target module "
            f"{module_name!r} does not exist (issue #749 regression): {exc}"
        )

    obj = getattr(module, attr, None)
    assert obj is not None, (
        f"[project.scripts] {name} = {target!r}: module {module_name!r} has no "
        f"attribute {attr!r}"
    )
    assert callable(obj), (
        f"[project.scripts] {name} = {target!r}: "
        f"{module_name}.{attr} is not callable (type={type(obj).__name__})"
    )