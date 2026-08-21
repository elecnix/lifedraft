"""Tests for the perf gate itself (tools/perf_gate.py).

A detector that has never been seen to fail has not been tested. These drive
``is_regression`` and ``run_gate`` against synthetic timing data and assert
that the gate fires on the regression it exists to catch -- and stays silent
on the changes it must NOT punish (improvements, new tests, noise).
"""

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_gate():
    """tools/ is not a package (no __init__.py), so load the module by path."""
    spec = importlib.util.spec_from_file_location(
        "perf_gate", REPO_ROOT / "tools" / "perf_gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["perf_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()

# Use values above MIN_BASELINE (10.0s) for all regression checks.
BASE_FAST = 20.0    # a test that takes 20s baseline
BASE_SLOW = 100.0   # a test that takes 100s baseline


def _timings(tests: dict, total: float = 100.0) -> dict:
    """Build a timings payload like the conftest.py plugin produces."""
    return {
        "total_duration": total,
        "test_count": len(tests),
        "tests": {k: v if isinstance(v, dict) else {"duration": v, "outcome": "passed"}
                  for k, v in tests.items()},
    }


def _baseline(tests: dict, total: float = 100.0) -> dict:
    return {"tests": tests, "total_duration": total}


# -------------------------------------------------------------- is_regression

def test_regression_fires_on_1_5x_and_2s_increase():
    """A test that goes 20s -> 35s (1.75x, +15s) is a regression."""
    assert gate.is_regression(BASE_FAST, 35.0)


def test_regression_does_not_fire_on_noise_within_1_5x():
    """A test that goes 100s -> 140s (1.4x) is within noise."""
    assert not gate.is_regression(BASE_SLOW, 140.0)


def test_regression_does_not_fire_on_tests_below_min_baseline():
    """A test under 10s is too noisy to gate on, even if it 10x's."""
    assert not gate.is_regression(5.0, 50.0)


def test_regression_fires_on_a_large_test_at_1_5x():
    """A 100s test going to 160s (1.6x, +60s) is a regression."""
    assert gate.is_regression(BASE_SLOW, 160.0)


def test_regression_boundary_at_exactly_1_5x():
    """At the exact boundary (20s -> 30s = 1.5x) it should NOT fire
    (the condition is strict >)."""
    assert not gate.is_regression(BASE_FAST, 30.0)


# -------------------------------------------------------------- run_gate

def test_run_gate_flags_a_regression():
    timings = _timings({"tests/test_slow.py::test_x": 45.0})
    baseline = _baseline({"tests/test_slow.py::test_x": 20.0})
    failures = gate.run_gate(timings, baseline)
    assert len(failures) == 1
    assert "test_x" in failures[0]
    assert "2.2x" in failures[0]


def test_run_gate_is_silent_on_improvement():
    """Improvements do NOT auto-tighten -- timing noise must not make a fast
    run into a too-tight baseline."""
    timings = _timings({"tests/test_slow.py::test_x": 10.0})
    baseline = _baseline({"tests/test_slow.py::test_x": 50.0})
    assert gate.run_gate(timings, baseline) == []


def test_run_gate_allows_new_tests():
    """A test absent from the baseline has nothing to regress against."""
    timings = _timings({"tests/test_new.py::test_new": 120.0})
    baseline = _baseline({})
    assert gate.run_gate(timings, baseline) == []


def test_run_gate_ignores_failed_tests():
    """A test that failed/errored is already caught by the test suite; its
    timing is not meaningful (it may have crashed early)."""
    timings = _timings({
        "tests/test_x.py::test_x": {"duration": 999.0, "outcome": "failed"},
    })
    baseline = _baseline({"tests/test_x.py::test_x": 20.0})
    assert gate.run_gate(timings, baseline) == []


def test_run_gate_ignores_removed_tests():
    """A test in the baseline but not in current is silently dropped (not a
    failure -- the test was removed, which is fine)."""
    timings = _timings({})
    baseline = _baseline({"tests/test_gone.py::test_gone": 50.0})
    assert gate.run_gate(timings, baseline) == []


def test_run_gate_multiple_regressions_all_reported():
    timings = _timings({
        "tests/test_a.py::test_a": 45.0,
        "tests/test_b.py::test_b": 80.0,
        "tests/test_c.py::test_c": 25.0,  # not a regression (20->25, 1.25x)
    })
    baseline = _baseline({
        "tests/test_a.py::test_a": 20.0,
        "tests/test_b.py::test_b": 30.0,
        "tests/test_c.py::test_c": 20.0,
    })
    failures = gate.run_gate(timings, baseline)
    assert len(failures) == 2
    assert any("test_a" in f for f in failures)
    assert any("test_b" in f for f in failures)


# -------------------------------------------------------------- update_baseline

def test_update_baseline_writes_current_durations():
    timings = _timings({
        "tests/test_a.py::test_a": 50.0,
        "tests/test_b.py::test_b": {"duration": 30.0, "outcome": "skipped"},
    })
    new = gate.update_baseline(timings, _baseline({}))
    assert new["tests"]["tests/test_a.py::test_a"] == 50.0
    assert new["tests"]["tests/test_b.py::test_b"] == 30.0
    assert new["total_duration"] == 100.0
    assert new["test_count"] == 2


def test_update_baseline_replaces_old_durations_wholesale():
    """Unlike coverage (which auto-tightens), the perf baseline is replaced
    wholesale on --update. A test that got faster sets the new floor."""
    timings = _timings({"tests/test_x.py::test_x": 15.0})
    old = _baseline({"tests/test_x.py::test_x": 50.0, "tests/test_gone.py::test_gone": 20.0})
    new = gate.update_baseline(timings, old)
    assert new["tests"] == {"tests/test_x.py::test_x": 15.0}
    # removed test is dropped
    assert "test_gone" not in new["tests"]


def test_update_baseline_excludes_failed_tests():
    """A test that failed should not anchor the baseline."""
    timings = _timings({
        "tests/test_ok.py::test_ok": {"duration": 50.0, "outcome": "passed"},
        "tests/test_bad.py::test_bad": {"duration": 999.0, "outcome": "failed"},
    })
    new = gate.update_baseline(timings, _baseline({}))
    assert "test_bad" not in new["tests"]
    assert new["tests"]["tests/test_ok.py::test_ok"] == 50.0


def test_update_baseline_records_thresholds():
    """The baseline records what 'regression' meant so a future reader knows
    the gate's calibration."""
    timings = _timings({"tests/test_x.py::test_x": 15.0})
    new = gate.update_baseline(timings, _baseline({}))
    assert new["_thresholds"]["regression_factor"] == gate.REGRESSION_FACTOR
    assert new["_thresholds"]["regression_min_absolute"] == gate.REGRESSION_MIN_ABSOLUTE
    assert new["_thresholds"]["min_baseline"] == gate.MIN_BASELINE
