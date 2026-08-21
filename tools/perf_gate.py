#!/usr/bin/env python3
"""Per-test runtime regression gate.

WHY THIS EXISTS
---------------
The test suite's wall-clock time is dominated by a handful of tests (the VOI
sweep, the objective-selection CLI tests). A change that makes one of them
2x slower can add minutes to CI without any test failing -- it just takes
longer, and nobody notices until the suite is unbearable.

This gate catches that the same way the coverage gate catches coverage
regressions: compare per-test durations against a committed baseline, fail
if something got materially slower.

WHY NOT A FLAT "SUITE MUST FINISH IN N MINUTES" THRESHOLD
---------------------------------------------------------
A single wall-clock target is the timing equivalent of `fail_under = 93` on
coverage: a single number that moves for reasons unrelated to the question.
CI load on the self-hosted runner swings the total by 20% between runs (327s
vs 412s for the same commit). A flat threshold tight enough to catch a real
regression would fire on noise; one loose enough to survive noise lets real
regressions through.

Per-test durations are more stable than the total (a 150s VOI sweep does not
get 150s of scheduling jitter -- its subprocesses are CPU-bound), and the gate
only flags a test that is BOTH 1.5x slower AND at least 1s slower in absolute
terms, which is beyond the noise band on this runner.

WHY NO AUTO-TIGHTENING (unlike coverage)
-----------------------------------------
The coverage ratchet clicks one way: if a file's uncovered count drops, the
gate FAILS until the baseline is tightened, so the slack cannot be left behind.
Timing is different: a fast CI run is noise, not improvement. Auto-tightening
on a lucky-fast run would set a baseline the next run could not meet, turning
the gate into a source of false positives. So improvements are SILENTLY FINE
(not a failure); the baseline is only updated by an explicit `--update`.
The report still names tests that got faster, so a real optimization can be
locked in deliberately.

THE GATE
--------
  A test is a regression when ALL of:
    - its baseline duration is >= MIN_BASELINE (10s) -- below that, CI load
      is too noisy to gate on;
    - its current duration exceeds baseline * REGRESSION_FACTOR (1.5x);
    - the absolute increase exceeds REGRESSION_MIN_ABSOLUTE (2.0s).

  New tests (absent from the baseline) are ALLOWED -- they have nothing to
  regress against. Removed tests are silently dropped on --update.

USAGE
-----
    # measure (the conftest.py plugin writes test_timings.json on every run)
    pytest -q

    # enforce (CI)
    python tools/perf_gate.py --check

    # regenerate the baseline after a legitimate change, then commit it
    python tools/perf_gate.py --update

    # findings: slowest tests, regressions, improvements
    python tools/perf_gate.py --report
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TIMINGS_PATH = REPO_ROOT / "test_timings.json"
BASELINE_PATH = REPO_ROOT / "tools" / "perf_baseline.json"

# A test must clear ALL THREE of these to be flagged as a regression.
# The thresholds are calibrated for a self-hosted runner where load swings
# per-test timings by 2-3x for tests under ~10s (OS scheduling jitter on a
# shared 16-core box). Only tests that already take >=10s are gated on --
# those are the expensive ones (VOI sweep, pandas export, objective-selection
# CLI) whose regression actually matters, and at that scale a 1.5x slowdown
# is signal, not noise.
REGRESSION_FACTOR = 1.5        # current > baseline * 1.5
REGRESSION_MIN_ABSOLUTE = 2.0  # current - baseline > 2.0s
MIN_BASELINE = 10.0           # only gate on tests that take >= 10s

REGEN_HINT = "python tools/perf_gate.py --update   # then commit tools/perf_baseline.json"


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------
def load_timings(path: Path) -> dict:
    """Read test_timings.json -> {tests: {nodeid: {duration, outcome}},
    total_duration, test_count}."""
    if not path.exists():
        sys.exit(
            f"error: {path} not found. Run the test suite first (the conftest.py "
            f"plugin writes it automatically):\n  pytest -q"
        )
    raw = json.loads(path.read_text())
    return raw


def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {"tests": {}, "total_duration": 0.0}
    return json.loads(BASELINE_PATH.read_text())


def _load_baseline(path: Path) -> dict:
    if not path.exists():
        return {"tests": {}, "total_duration": 0.0}
    return json.loads(path.read_text())


# --------------------------------------------------------------------------
# gate
# --------------------------------------------------------------------------
def is_regression(baseline_dur: float, current_dur: float) -> bool:
    """A regression is material: beyond noise on BOTH a relative and an
    absolute scale. A single fast run on a quiet box is not a regression;
    a 150s test going to 230s is."""
    if baseline_dur < MIN_BASELINE:
        return False
    if current_dur <= baseline_dur * REGRESSION_FACTOR:
        return False
    if current_dur - baseline_dur <= REGRESSION_MIN_ABSOLUTE:
        return False
    return True


def run_gate(timings: dict, baseline: dict) -> list[str]:
    """Return failure messages. Empty list == the gate passes."""
    failures: list[str] = []
    base_tests: dict[str, float] = baseline.get("tests", {})
    current: dict[str, dict] = timings.get("tests", {})

    for nodeid in sorted(current):
        cur = current[nodeid]
        if cur["outcome"] not in ("passed", "skipped"):
            # A test that now fails/errored is already caught by the test suite
            # itself; its timing is not meaningful (it may have crashed early).
            continue
        dur = cur["duration"]
        base = base_tests.get(nodeid)
        if base is None:
            continue  # new test -- allowed
        if is_regression(base, dur):
            ratio = dur / base if base > 0 else float("inf")
            failures.append(
                f"[perf] {nodeid}: {base:.3f}s -> {dur:.3f}s "
                f"(+{dur - base:.3f}s, {ratio:.1f}x). This is beyond the noise "
                f"threshold (>{REGRESSION_FACTOR}x AND >+{REGRESSION_MIN_ABSOLUTE}s). "
                f"If the slowdown is deliberate, regenerate the baseline so it is "
                f"visible in review:\n          {REGEN_HINT}"
            )

    return failures


# --------------------------------------------------------------------------
# baseline regeneration
# --------------------------------------------------------------------------
def update_baseline(timings: dict, baseline: dict) -> dict:
    """Rewrite the baseline from the current measurement. Carry over nothing --
    durations are replaced wholesale. The thresholds are recorded in the file
    so a future reader knows what 'regression' meant when it was written."""
    tests = {}
    for nodeid, data in sorted(timings.get("tests", {}).items()):
        if data["outcome"] in ("passed", "skipped"):
            tests[nodeid] = round(data["duration"], 6)
    return {
        "_comment": (
            "Per-test runtime ratchet. `tests` maps a test nodeid to its "
            "duration in seconds. A test is a regression when it exceeds "
            f"baseline * {REGRESSION_FACTOR} AND the increase exceeds {REGRESSION_MIN_ABSOLUTE}s, and its "
            f"baseline is >= {MIN_BASELINE}s. Unlike coverage, improvements do NOT "
            "auto-tighten -- timing is noisy, so the baseline is only updated "
            "by an explicit --update. Regenerate with: "
            "python tools/perf_gate.py --update"
        ),
        "_metric": "test_duration_seconds",
        "_thresholds": {
            "regression_factor": REGRESSION_FACTOR,
            "regression_min_absolute": REGRESSION_MIN_ABSOLUTE,
            "min_baseline": MIN_BASELINE,
        },
        "total_duration": timings.get("total_duration", 0.0),
        "test_count": len(tests),
        "tests": tests,
    }


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def print_report(timings: dict, baseline: dict) -> None:
    base_tests: dict[str, float] = baseline.get("tests", {})
    current: dict[str, dict] = timings.get("tests", {})

    print("=" * 78)
    print("RUNTIME FINDINGS")
    print("=" * 78)
    print(f"total suite duration : {timings.get('total_duration', 0):.1f}s")
    print(f"tests recorded       : {len(current)}")
    base_total = baseline.get("total_duration", 0.0)
    if base_total:
        delta = timings.get("total_duration", 0) - base_total
        sign = "+" if delta >= 0 else ""
        print(f"baseline total       : {base_total:.1f}s  ({sign}{delta:.1f}s)")
    print()

    # Regressions
    regressions = []
    for nodeid, cur in current.items():
        if cur["outcome"] not in ("passed", "skipped"):
            continue
        base = base_tests.get(nodeid)
        if base is None:
            continue
        if is_regression(base, cur["duration"]):
            regressions.append((nodeid, base, cur["duration"]))
    print(f"-- REGRESSIONS ({len(regressions)}) " + "-" * 40)
    if not regressions:
        print("   none")
    for nodeid, base, dur in sorted(regressions, key=lambda x: x[2] - x[1], reverse=True):
        print(f"   {base:8.3f}s -> {dur:8.3f}s  ({dur / base:.1f}x)  {nodeid}")
    print()

    # Improvements (informational -- not a failure, but worth surfacing so a
    # real optimization can be locked in via --update)
    improvements = []
    for nodeid, cur in current.items():
        if cur["outcome"] not in ("passed", "skipped"):
            continue
        base = base_tests.get(nodeid)
        if base is None or base < MIN_BASELINE:
            continue
        if cur["duration"] < base * 0.7 and base - cur["duration"] > REGRESSION_MIN_ABSOLUTE:
            improvements.append((nodeid, base, cur["duration"]))
    print(f"-- IMPROVEMENTS ({len(improvements)}) " + "-" * 38)
    if not improvements:
        print("   none")
    for nodeid, base, dur in sorted(improvements, key=lambda x: x[1] - x[2], reverse=True):
        print(f"   {base:8.3f}s -> {dur:8.3f}s  ({dur / base:.1f}x)  {nodeid}")
    print()

    # Slowest tests
    slowest = sorted(
        current.items(), key=lambda kv: kv[1]["duration"]
    )[-20:]
    print("-- SLOWEST 20 TESTS " + "-" * 40)
    for nodeid, data in slowest:
        dur = data["duration"]
        base = base_tests.get(nodeid, 0.0)
        base_str = f"  (baseline: {base:.3f}s)" if base else ""
        print(f"   {dur:8.3f}s  {nodeid}{base_str}")
    print()


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="enforce the gate (CI); exit 1 on failure")
    g.add_argument("--update", action="store_true", help="regenerate the committed baseline")
    g.add_argument("--report", action="store_true", help="print the human-readable findings report")
    ap.add_argument("--timings", type=Path, default=TIMINGS_PATH)
    ap.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    args = ap.parse_args()

    timings = load_timings(args.timings)
    baseline = _load_baseline(args.baseline)

    if args.report:
        print_report(timings, baseline)
        return 0

    if args.update:
        new = update_baseline(timings, baseline)
        args.baseline.write_text(json.dumps(new, indent=2, sort_keys=False) + "\n")
        print(f"wrote {args.baseline.resolve().relative_to(REPO_ROOT)}")
        print(f"  {new['test_count']} tests, {new['total_duration']:.1f}s total duration")
        return 0

    # --check
    failures = run_gate(timings, baseline)
    if not failures:
        current = timings.get("tests", {})
        total = timings.get("total_duration", 0.0)
        print(
            f"perf gate PASSED: {len(current)} tests, {total:.1f}s total, "
            f"no runtime regressions."
        )
        return 0

    print("PERF GATE FAILED", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    for f in failures:
        print(f"  {f}", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    print(f"{len(failures)} regression(s).", file=sys.stderr)
    print(
        "\nIf the slowdown is deliberate (e.g. more thorough tests), regenerate the "
        "baseline in the SAME PR so the new floor is visible in review.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
