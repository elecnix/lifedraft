#!/usr/bin/env python3
"""Curated, fast mutation guard that enforces DP#11/DP#18 mechanically.

WHY THIS EXISTS
---------------
DP#11: "Unit tests verify a module's contract; integration tests drive the fold."
DP#18: "Any test claiming an engine behaviour must run the engine and assert its
observable output — not an intermediate the test constructed."

A test that copies production logic, or builds engine state by hand instead of
driving the fold, passes while the engine is broken. The mutation guard catches
this by applying targeted wiring-critical mutations to production code and checking
that at least one test in a targeted subset changes outcome (goes LOUD).

This is NOT full mutation testing (no cosmic-ray/mutmut over the whole repo —
too slow). It is a small set of curated mutations on production code, each marked
as expected_loud (at least one test should catch it) or expected_silent (the
suite is green despite the hole, and the guard documents the gap until a fix
lands).

MUTATIONS ARE APPLIED TRANSIENTLY (in-process monkeypatch), never committed.
Each mutation runs in a FRESH subprocess so there is no state leakage between
mutations.

USAGE
-----
    VIRTUAL_ENV=$PWD/.venv python tools/mutation_guard.py

Exit code:
    0  — every mutation's actual state matches its expected state.
    1  — at least one mutation's actual state mismatches its expected state
         (an expected_loud mutation is silent, or an expected_silent mutation
         is loud).

ADDING A NEW MUTATION
---------------------
See tools/README.md, section "Mutation guard (tools/mutation_guard.py)".

Each mutation is a dict with:
    id          — short slug, used in output and citations
    description — one-liner explaining what the mutation does
    apply_src   — Python source that applies the mutation in-process when
                  executed at module level. Must be valid Python that, when
                  run in a fresh interpreter with the repo on sys.path,
                  monkeypatches the target module(s). The mutation is
                  undone by terminating the subprocess.
    test_files  — list of test file paths (relative to repo root) to run
                  for this mutation. Keep it targeted; the full suite is ~8 min.
    expected    — "loud" or "silent"
    citation    — issue or PR number documenting why the expected state is what it is
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── Targeted test file lists ────────────────────────────────────────────────
# Per-mutation: the RESP + compounding + engine-fold tests that SHOULD catch
# the mutation. NOT the full ~8 min suite — compete with CI for the same 16 cores.

RESP_TEST_FILES = [
    "tests/test_resp_rules_full.py",
    "tests/test_resp_issue_304.py",
    "tests/test_resp_year_versioned.py",
    "tests/test_issue_578_resp_winddown.py",
    "tests/test_issue_88_resp_quebec_computed.py",
    "tests/test_issue_306_tfsa_resp_accuracy.py",
    "tests/test_issue_812_child_saver_fold.py",
    "tests/test_issue_857_child_room_accrual.py",
    "tests/test_issue_914_resp_action_consumed.py",
    "tests/test_issue_1046_resp_allocation_wiring.py",
]

COMPOUNDING_TEST_FILES = [
    "tests/test_golden_trajectory_581.py",
    "tests/test_issue_288_run_is_fold.py",
    "tests/test_issue_583_pure_fold.py",
    "tests/test_issue_97_income_type_growth.py",
    "tests/test_issue_279_investment_return_defaults.py",
    "tests/test_issue_575_576_taxable_investing.py",
    "tests/test_simulation_full.py",
]

RESP_AND_COMPOUNDING = RESP_TEST_FILES + COMPOUNDING_TEST_FILES


# ── Mutation definitions ────────────────────────────────────────────────────
# Each apply_src is a self-contained Python snippet that, when executed at
# module level in a fresh interpreter, monkeypatches the target module.
# The mutation is undone by the subprocess terminating.

COMPOUNDING_APPLY = textwrap.dedent("""\
    import rule_registry as _reg
    import rules_registered_plans as _rrp
    _original = _rrp.apply_resp
    def _patched(ws, ctx):
        original_return = ctx.investment_return
        ctx.investment_return = original_return * 2
        try:
            return _original(ws, ctx)
        finally:
            ctx.investment_return = original_return
    _rrp.apply_resp = _patched
    _reg.RULES['resp'] = _patched
""")

GRANT_WIRING_APPLY = textwrap.dedent("""\
    from countries.canada.resp_rules import RESPCalculator as _RC
    _original = _RC.calculate_cesg
    def _patched(self, contribution, child, year, family_income):
        result = _original(self, contribution, child, year, family_income)
        result = dict(result)
        result['total_cesg'] = 0
        result['basic_cesg'] = 0
        result['additional_cesg'] = 0
        result['remaining_lifetime_cesg'] = max(0, self.CESG_LIFETIME_MAX - child.total_cesg_received)
        return result
    _RC.calculate_cesg = _patched
""")

ALLOC_RESP_APPLY = textwrap.dedent("""\
    import strategy as _st
    _original = _st.StrategyEngine.allocate
    def _patched(self, state, initial_investment=None):
        original_cap = state.resp_annual_match_cap
        state.resp_annual_match_cap = 0  # #1046: recreate the original bug
        try:
            return _original(self, state, initial_investment)
        finally:
            state.resp_annual_match_cap = original_cap
    _st.StrategyEngine.allocate = _patched
""")

STATE_ADVANCE_APPLY = textwrap.dedent("""\
    import rule_registry as _reg
    import rules_registered_plans as _rrp
    _original_apply_resp = _rrp.apply_resp
    def _patched_apply_resp(ws, ctx):
        # Zero the opening CESG accumulation — breaks cross-year CESG carry-forward.
        # This zeroes the cumulative CESG received per child at the start of each
        # year, so the $7,200 lifetime cap can never bind (it never accumulates).
        ws.opening_resp_cesg = [0.0] * len(ws.opening_resp_cesg) if ws.opening_resp_cesg else []
        return _original_apply_resp(ws, ctx)
    _rrp.apply_resp = _patched_apply_resp
    _reg.RULES['resp'] = _patched_apply_resp
""")


MUTATIONS = [
    {
        "id": "COMPOUNDING",
        "description": "Double investment_return in apply_resp compounding",
        "apply_src": COMPOUNDING_APPLY,
        "test_files": RESP_AND_COMPOUNDING,
        "expected": "loud",
        "citation": "#1046 (integration test test_resp_balance_grows_and_cesg_cap_binds catches compounding mutation)",
    },
    {
        "id": "GRANT_WIRING",
        "description": "Force calculate_cesg to return total_cesg=0",
        "apply_src": GRANT_WIRING_APPLY,
        "test_files": RESP_AND_COMPOUNDING,
        "expected": "loud",
        "citation": "#1046 (unit tests catch the calculation; integration test test_cesg_lifetime_cap_7200_per_child also drives CESG through the fold)",
    },
    {
        "id": "ALLOC_RESP",
        "description": "Remove resp_annual_match_cap from allocate min-term",
        "apply_src": ALLOC_RESP_APPLY,
        "test_files": RESP_AND_COMPOUNDING,
        "expected": "loud",
        "citation": "#1046 (test_resp_allocation_nonzero_when_children_present catches cap removal)",
    },
    {
        "id": "STATE_ADVANCE",
        "description": "Zero opening_resp_cesg in apply_resp prologue (breaks CESG lifetime cap)",
        "apply_src": STATE_ADVANCE_APPLY,
        "test_files": RESP_AND_COMPOUNDING,
        "expected": "loud",
        "citation": "#1046 (test_lifetime_state_advances_across_years and test_cesg_lifetime_cap_7200_per_child catch state advancement bypass)",
    },
]


# ── Engine ──────────────────────────────────────────────────────────────────

def _dedup_preserve_order(seq: list[str]) -> list[str]:
    """Remove duplicates while preserving order."""
    seen = set()
    result = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _run_pytest(test_files: list[str],
               mutation_src: str | None = None) -> tuple[int, float, str]:
    """Run a targeted subset of the suite, optionally with a mutation applied.

    If mutation_src is provided, it is prepended to a wrapper script that
    imports the production modules, applies the mutation, then runs pytest
    programmatically. The mutation lives only in the subprocess.

    Returns (exit_code, elapsed_seconds, combined_stdout_stderr).
    """
    test_paths = _dedup_preserve_order(test_files)

    if mutation_src is not None:
        # Build a wrapper script that applies the mutation and then runs pytest
        test_path_strs = [str(REPO_ROOT / t) for t in test_paths]
        wrapper = (
            f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            + mutation_src
            + "\n"
            + "import pytest\n"
            + f"sys.exit(pytest.main({['-q', '--tb=line', '--no-header', '-p', 'no:cacheprovider'] + test_path_strs!r}))\n"
        )
        cmd = [sys.executable, "-c", wrapper]
    else:
        # Baseline run — no mutation
        cmd = [
            sys.executable, "-m", "pytest",
            "-q", "--tb=line", "--no-header",
            "-p", "no:cacheprovider",
        ] + test_paths

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)

    start = time.monotonic()
    result = subprocess.run(
        cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env,
        timeout=300,  # 5 min timeout per mutation run
    )
    elapsed = time.monotonic() - start
    return result.returncode, elapsed, result.stdout + result.stderr


def _parse_pytest_summary(output: str) -> tuple[int, int]:
    """Parse pytest output for passed and failed counts.

    Returns (passed, failed). On parse failure, returns (-1, -1).
    """
    m = re.search(r'(\d+) passed', output)
    passed = int(m.group(1)) if m else -1
    m = re.search(r'(\d+) failed', output)
    failed = int(m.group(1)) if m else 0
    return passed, failed


def run_mutation_guard() -> int:
    """Run all mutations and report. Returns 0 on full match, 1 on mismatch."""
    deduped_files = _dedup_preserve_order(RESP_AND_COMPOUNDING)

    print("=" * 78)
    print("MUTATION GUARD — DP#11/DP#18 enforcement")
    print("=" * 78)
    print()
    print(f"Targeted test files ({len(deduped_files)}):")
    for f in deduped_files:
        print(f"  {f}")
    print()

    # ── Baseline run (no mutation) ────────────────────────────────────────
    print("Running baseline (unmutated) targeted tests...")
    baseline_rc, baseline_elapsed, baseline_output = _run_pytest(deduped_files)

    if baseline_rc not in (0, 1):
        print(f"ERROR: baseline pytest exited with code {baseline_rc}")
        print(baseline_output[-3000:])
        print("Cannot establish baseline. Aborting.")
        return 2

    baseline_passed, baseline_failed = _parse_pytest_summary(baseline_output)
    print(f"  Baseline: {baseline_passed} passed, {baseline_failed} failed "
          f"(exit={baseline_rc}, {baseline_elapsed:.1f}s)")
    print()

    # ── Per-mutation runs ─────────────────────────────────────────────────
    mismatches = []
    results = []

    for mut in MUTATIONS:
        mid = mut["id"]
        expected = mut["expected"]
        print(f"Mutation: {mid} — {mut['description']}")
        print(f"  Expected: {expected}")
        print("  Applying mutation in subprocess...")

        mutated_rc, mutated_elapsed, mutated_output = _run_pytest(
            mut["test_files"], mutation_src=mut["apply_src"]
        )
        mutated_passed, mutated_failed = _parse_pytest_summary(mutated_output)

        # A mutation is LOUD if any test changed outcome:
        # - A previously passing test now fails, OR
        # - The overall exit code changed (from 0 to non-zero)
        # If baseline had failures, mutation is LOUD if it adds MORE failures.
        if baseline_rc == 0:
            # Clean baseline: any failure under mutation = LOUD
            is_loud = mutated_rc != 0
        else:
            # Baseline had some failures; mutation is LOUD if it adds failures
            is_loud = mutated_failed > baseline_failed

        actual = "loud" if is_loud else "silent"
        match = actual == expected

        print(f"  Result: {mutated_passed} passed, {mutated_failed} failed "
              f"(exit={mutated_rc}, {mutated_elapsed:.1f}s)")
        print(f"  Actual: {actual}, Expected: {expected} → "
              f"{'MATCH' if match else 'MISMATCH'}")

        if mutated_rc != 0 and mutated_failed > 0:
            # Show a few failing test names for diagnostic
            fail_lines = [line for line in mutated_output.splitlines()
                          if "FAIL" in line][:5]
            if fail_lines:
                print("  Sample failures:")
                for fl in fail_lines:
                    print(f"    {fl.strip()}")

        if not match:
            mismatches.append(mid)
        results.append({
            "id": mid,
            "expected": expected,
            "actual": actual,
            "match": match,
            "citation": mut["citation"],
            "mutated_passed": mutated_passed,
            "mutated_failed": mutated_failed,
            "mutated_rc": mutated_rc,
            "mutated_elapsed": mutated_elapsed,
        })
        print()

    # ── Summary ──────────────────────────────────────────────────────────
    print("=" * 78)
    print("MUTATION GUARD SUMMARY")
    print("=" * 78)
    for r in results:
        status = "✓" if r["match"] else "✗"
        print(f"  {status} {r['id']:16s}  expected={r['expected']:6s}  "
              f"actual={r['actual']:6s}  ({r['citation']})")
    print()

    if mismatches:
        print(f"MISMATCHES: {', '.join(mismatches)}")
        print("A mutation expected to be LOUD was SILENT (the hole it documents")
        print("is filled), or a mutation expected to be SILENT was LOUD")
        print("(unexpectedly caught). Update the mutation's expected state to")
        print("match reality.")
        return 1
    else:
        print("All mutations match their expected state. Guard passes.")
        return 0


if __name__ == "__main__":
    sys.exit(run_mutation_guard())
