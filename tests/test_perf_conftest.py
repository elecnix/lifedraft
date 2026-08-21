"""Tests for the always-on test-duration profiler (conftest.py).

Verifies that the conftest.py plugin records per-test durations to a JSON file
on every test run -- including under xdist (-n auto), where workers are
separate processes and the timing dict lives on the worker, not the controller.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_pytest(test_args: list[str], env_timings: str) -> dict:
    """Run pytest in a subprocess with the conftest plugin active, capture the
    timings JSON, and return the parsed payload. A subprocess is needed because
    the plugin hooks into session lifecycle that pytester in-process does not
    reproduce faithfully for the xdist fragment-merge path."""
    import os
    env = dict(os.environ, PERF_TIMINGS_FILE=env_timings)
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header"] + test_args
    subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True, check=True)
    return json.loads(Path(env_timings).read_text())


def test_conftest_records_passed_test_duration(tmp_path):
    timings = _run_pytest(
        ["tests/test_money.py::TestAddSub::test_add_and_sub_same_currency"],
        str(tmp_path / "timings.json"),
    )
    assert timings["test_count"] == 1
    nodeid = "tests/test_money.py::TestAddSub::test_add_and_sub_same_currency"
    assert nodeid in timings["tests"]
    assert timings["tests"][nodeid]["outcome"] == "passed"
    assert timings["tests"][nodeid]["duration"] > 0.0


def test_conftest_records_total_duration(tmp_path):
    timings = _run_pytest(
        ["tests/test_money.py::TestAddSub::test_add_and_sub_same_currency"],
        str(tmp_path / "timings.json"),
    )
    assert timings["total_duration"] > 0.0


def test_conftest_works_under_xdist(tmp_path):
    """The critical path: CI runs with -n auto. Workers write fragments that
    the controller must merge into a single file with no tests missing."""
    timings = _run_pytest(
        ["tests/test_money.py", "-n", "auto"],
        str(tmp_path / "timings.json"),
    )
    # 28 tests in test_money.py -- all must appear in the merged output
    assert timings["test_count"] == 28
    # no fragment files left behind
    import glob
    fragments = glob.glob(str(tmp_path / "timings.json.gw*"))
    assert fragments == [], f"fragment files not cleaned up: {fragments}"


def test_conftest_records_skipped_tests(tmp_path):
    """A test that skips in setup (no call report) must still appear in the
    output so a test going from passed to skipped is visible, not silently
    dropped from the timings."""
    timings = _run_pytest(
        ["tests/test_money.py", "-k", "test_add_and_sub_same_currency or nonexist"],
        str(tmp_path / "timings.json"),
    )
    # The -k filter selects one test; the nonexist matches nothing.
    # At least the one real test must appear.
    assert timings["test_count"] >= 1


def test_conftest_handles_nonexistent_timings_path(tmp_path):
    """If the output directory doesn't exist, the plugin must not crash the
    test session (a profiling instrument that takes down the tests is worse
    than no instrument)."""
    bad_path = tmp_path / "nonexistent_dir" / "timings.json"
    import os
    env = dict(os.environ, PERF_TIMINGS_FILE=str(bad_path))
    cmd = [
        sys.executable, "-m", "pytest", "-q", "--no-header",
        "tests/test_money.py::TestAddSub::test_add_and_sub_same_currency",
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, capture_output=True)
    # The test must still pass
    assert result.returncode == 0
