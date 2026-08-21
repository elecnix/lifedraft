"""Regression guard for issue #333 fallout: simulate.py imports numpy at module
load (DP#23 RNG), so numpy must be a *runtime* dependency, not dev-only —
otherwise simulate.py crashes on a normal install with ModuleNotFoundError.

Stdlib-only / no tomllib (which is 3.11+; CI runs 3.10): match the core
``dependencies = [...]`` line in [project] with a regex.
"""
import re
from pathlib import Path


def test_numpy_is_a_core_runtime_dependency():
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    # Core (non-optional) deps live at column 0 in [project]; the dev list lives
    # under [project.optional-dependencies] as ``dev = [...]``. Match a top-level
    # ``dependencies = [...]`` containing numpy.
    m = re.search(r"(?m)^dependencies\s*=\s*\[(.*?)\]", text, re.S)
    assert m, "no core [project].dependencies list found in pyproject.toml"
    assert "numpy" in m.group(1), (
        "numpy must be in core [project].dependencies (simulate.py imports it "
        "unconditionally); got: " + m.group(1).strip()
    )
