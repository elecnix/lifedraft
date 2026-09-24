"""CI guard for the runnable, research-backed examples (issue #300).

One pair of tests per ``examples/<source>/<slug>/`` directory (xdist spreads
them):

* ``test_example_static_contract``: the five files are present and nothing else,
  each under its size cap and none gitignored; ``input.json`` passes
  ``input_contract.load_and_map``; ``meta.json`` has exactly its keys and types;
  README.md has every required section, cites the source URL, names every
  top-level input key under Encoding, and states the same verdict as
  meta.json; no home path, e-mail address or SIN-shaped number.
* ``test_example_regenerates_byte_identical``: re-runs the REAL optimize.py
  through ``tools/examples.py``'s ``regenerate`` (the same function the
  ``regen`` CLI uses; DP#11) and compares both reports byte for byte with what
  is committed (under the canonical Python; see CANONICAL_PYTHON for the
  nightly legs). It only compares: it never writes into examples/.

Discovery happens at import, so a missing or empty examples/ tree raises
``ExamplesError`` during collection and the whole run errors (DP#32). pytest
would otherwise report an empty parameter set as a green "skipped".
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_tool():
    """tools/ is not a package, so load the module by path."""
    spec = importlib.util.spec_from_file_location(
        "lifedraft_examples_tool", REPO_ROOT / "tools" / "examples.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lifedraft_examples_tool"] = mod
    spec.loader.exec_module(mod)
    return mod


ex = _load_tool()

EXAMPLES = ex.discover_examples()


def test_examples_tree_is_not_empty():
    """Backstop for the import-time raise above: if a refactor ever made
    discovery lenient, zero examples must still fail here, not skip."""
    assert len(ex.discover_examples()) >= 1


@pytest.mark.parametrize("example_dir", EXAMPLES, ids=ex.example_id)
def test_example_static_contract(example_dir):
    problems = ex.static_problems(example_dir)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("example_dir", EXAMPLES, ids=ex.example_id)
def test_example_regenerates_byte_identical(example_dir):
    # workers=1: serial inside each xdist worker, so no nested process pool.
    json_text, md_text = ex.regenerate(example_dir, workers=1)
    if ex.is_canonical_python():
        # CI's PR leg: exact bytes, which also pins the full report's sha256.
        mismatches = ex.compare_reports(example_dir, json_text, md_text)
    else:
        # Nightly 3.10/3.11 legs: floats drift by one ulp across minor
        # versions (measured, see tools/examples.py CANONICAL_PYTHON), so
        # report.md is still compared byte for byte and report.json value by
        # value with a 1e-9 relative float tolerance.
        mismatches = ex.compare_reports_near(example_dir, json_text, md_text)
    assert not mismatches, "\n\n".join(mismatches)
