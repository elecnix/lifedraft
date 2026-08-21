"""Always-on test-duration profiler.

This conftest is auto-loaded by pytest for every test session (root-level, so it
covers all ``testpaths``). It records per-test wall-clock durations to a JSON
file at session end, so CI can detect runtime regressions the same way the
coverage gate detects coverage regressions: compare against a committed
baseline, fail if something got slower.

WHY A conftest.py, NOT A --flag
------------------------------
The brief is "the profiler should always run." A flag is opt-in; an auto-loaded
conftest is not. There is no way to run the suite without producing the
timings file -- which is the point: a regression that slips through because
someone forgot ``--profile`` is exactly the failure mode this exists to
prevent.

OVERHEAD
--------
The per-test hook does nothing but stash a float in a dict. The JSON is written
ONCE, at session end. Measured overhead is below noise (the dict insert is
cheaper than pytest's own report collection, which already happens).

xdist
-----
With ``-n auto`` each worker is a separate process with its own config, so the
timing dict lives on the worker, not the controller. Each worker writes a
per-worker fragment file; the controller merges them in its own
``pytest_sessionfinish``. With no xdist (``-n 0`` or unset) the single process
writes directly.

OUTPUT
------
``test_timings.json`` in the CWD (override with ``PERF_TIMINGS_FILE``). The
file is gitignored (the blanket ``*.json`` rule covers it); it is a CI artifact,
uploaded as a workflow artifact, never committed.
"""

from __future__ import annotations

import json
import os
import time

import pytest


def _timings_path() -> str:
    return os.environ.get("PERF_TIMINGS_FILE", "test_timings.json")


def pytest_configure(config):
    """Register the per-session accumulator. Stashed on config so the hook
    below can reach it without a module-global (which would leak across
    pytester's in-process re-invocations in the test suite)."""
    config._perf_timings = {}
    config._perf_session_start = time.monotonic()


def _is_worker(config) -> bool:
    return hasattr(config, "workerinput")


def _worker_id(config) -> str:
    return getattr(config, "workerinput", {}).get("workerid", "main")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Record setup + call duration for each test. The call report carries
    the actual test-body wall time, but a module-scoped fixture (e.g. the VOI
    sweep) runs during the FIRST test's setup phase -- that 150s is setup time,
    not call time. Gating only on call time would miss exactly the regressions
    this exists to catch, so both phases are accumulated."""
    outcome = yield
    report = outcome.get_result()
    config = item.config
    nodeid = item.nodeid
    if nodeid not in config._perf_timings:
        config._perf_timings[nodeid] = {"duration": 0.0, "outcome": "passed"}
    # Accumulate setup + call (teardown is usually negligible and not what a
    # regression is about). Store raw, round at write time to avoid accumulating
    # rounding error across phases.
    if report.when in ("setup", "call"):
        config._perf_timings[nodeid]["duration"] += report.duration
    # Outcome from the call phase is authoritative; a skip in setup means the
    # call phase never runs, so capture it there.
    if report.when == "call":
        config._perf_timings[nodeid]["outcome"] = report.outcome
    elif report.when == "setup" and report.skipped:
        config._perf_timings[nodeid]["outcome"] = "skipped"


def pytest_sessionfinish(session, exitstatus):
    """Write timings once at session end. Under xdist, each worker writes a
    fragment; the controller (no ``workerinput``) merges them. Never let a
    write failure crash the test session itself."""
    config = session.config
    timings = getattr(config, "_perf_timings", None)
    if timings is None:
        return
    path = _timings_path()

    if _is_worker(config):
        # Worker: write a fragment the controller will merge.
        wid = _worker_id(config)
        fragment = path + f".{wid}"
        try:
            with open(fragment, "w") as f:
                json.dump(timings, f)
        except OSError:
            pass
        return

    # Controller (or non-xdist single process): merge worker fragments if any.
    merged = dict(timings)
    import glob

    for fragment in glob.glob(path + ".gw*"):
        try:
            with open(fragment) as f:
                merged.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    # Clean up fragments so they don't accumulate across runs.
    for fragment in glob.glob(path + ".gw*"):
        try:
            os.remove(fragment)
        except OSError:
            pass

    # Round accumulated durations (stored raw to avoid phase-by-phase rounding error).
    for _nodeid, data in merged.items():
        data["duration"] = round(data["duration"], 6)

    session_start = getattr(config, "_perf_session_start", None)
    total = time.monotonic() - session_start if session_start else 0.0
    payload = {
        "total_duration": round(total, 3),
        "test_count": len(merged),
        "tests": dict(sorted(merged.items())),
    }
    try:
        with open(path, "w") as f:
            json.dump(payload, f, indent=2)
    except OSError:
        pass
