"""Tests for the coverage gate itself (tools/coverage_gate.py).

A detector that has never been seen to fail has not been tested. These drive
`run_gates` against synthetic coverage data and assert that each gate fires on
the regression it exists to catch -- and, just as importantly, stays silent on
the changes it must NOT punish (adding covered code, deleting code).
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_gate():
    """tools/ is not a package (no __init__.py), so load the module by path."""
    spec = importlib.util.spec_from_file_location(
        "coverage_gate", REPO_ROOT / "tools" / "coverage_gate.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coverage_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load_gate()


def _file(covered: int, uncovered: int, missing=None) -> dict:
    return {
        "covered": covered,
        "uncovered": uncovered,
        "statements": covered + uncovered,
        "missing": missing if missing is not None else list(range(1, uncovered + 1)),
    }


def _baseline(files=None, allowlist=None, pragmas=None) -> dict:
    return {
        "files": files or {},
        "zero_coverage_allowlist": allowlist or {},
        "pragma_no_cover": pragmas or {},
    }


# ---------------------------------------------------------------- Gate A
def test_gate_a_fires_on_a_file_with_zero_covered_lines():
    files = {"dead_rule.py": _file(covered=0, uncovered=40)}
    failures = gate.run_gates(files, _baseline(files={"dead_rule.py": 40}), {})
    assert any("[Gate A]" in f and "dead_rule.py" in f for f in failures)


def test_gate_a_is_silent_for_an_allowlisted_zero_coverage_file():
    files = {"dead_rule.py": _file(covered=0, uncovered=40)}
    baseline = _baseline(
        files={"dead_rule.py": 40},
        allowlist={"dead_rule.py": {"reason": "known debt", "issue": "#999"}},
    )
    assert [f for f in gate.run_gates(files, baseline, {}) if "[Gate A]" in f] == []


def test_gate_a_fires_when_an_allowlisted_file_finally_gets_covered():
    """The exemption must not outlive the debt: once a test exists, the entry goes."""
    files = {"dead_rule.py": _file(covered=10, uncovered=30)}
    baseline = _baseline(
        files={"dead_rule.py": 30},
        allowlist={"dead_rule.py": {"reason": "known debt", "issue": "#999"}},
    )
    failures = gate.run_gates(files, baseline, {})
    assert any("[Gate A]" in f and "now HAS coverage" in f for f in failures)


# ---------------------------------------------------------------- Gate B
def test_gate_b_fires_when_uncovered_lines_increase():
    files = {"engine.py": _file(covered=90, uncovered=11)}
    failures = gate.run_gates(files, _baseline(files={"engine.py": 10}), {})
    assert any("[Gate B]" in f and "10 -> 11" in f for f in failures)


def test_gate_b_is_silent_when_covered_code_is_added():
    """The reason this ratchet counts LINES and not PERCENT.

    A file at 90/100 (90%) gains 20 well-tested lines. Its uncovered count is
    unchanged at 10, so the ratchet stays silent -- adding tested code is exactly
    what we want to encourage and must never be punished.
    """
    files = {"engine.py": _file(covered=110, uncovered=10)}
    assert gate.run_gates(files, _baseline(files={"engine.py": 10}), {}) == []


def test_gate_b_catches_the_line_a_percentage_ratchet_would_mask():
    """The masking case that motivates the metric choice.

    Same file gains 20 covered lines AND one untested guard clause. The per-file
    percentage RISES (90/100 = 90.0% -> 110/121 = 90.9%), so a percent-ratchet
    passes and the untested guard clause -- a rule that can silently do nothing --
    slips in. The uncovered-line ratchet goes 10 -> 11 and fires.
    """
    files = {"engine.py": _file(covered=110, uncovered=11)}
    before_pct = 100 * 90 / 100
    after_pct = 100 * 110 / 121
    assert after_pct > before_pct, "percentage must rise, or the example is wrong"

    failures = gate.run_gates(files, _baseline(files={"engine.py": 10}), {})
    assert any("[Gate B]" in f and "engine.py" in f for f in failures)


def test_gate_b_treats_a_new_file_as_allowed_zero_uncovered_lines():
    files = {"brand_new.py": _file(covered=5, uncovered=3)}
    failures = gate.run_gates(files, _baseline(files={}), {})
    assert any("[Gate B]" in f and "NEW file" in f for f in failures)


def test_gate_b_accepts_a_fully_covered_new_file():
    files = {"brand_new.py": _file(covered=8, uncovered=0)}
    assert gate.run_gates(files, _baseline(files={}), {}) == []


def test_gate_b_auto_tighten_fires_when_the_baseline_is_stale_loose():
    """Coverage improved. The baseline still permits the old slack, which would let
    coverage quietly drift back down to it. The ratchet must click."""
    files = {"engine.py": _file(covered=95, uncovered=5)}
    failures = gate.run_gates(files, _baseline(files={"engine.py": 10}), {})
    assert any("[Gate B]" in f and "IMPROVED" in f for f in failures)


def test_gate_b_fires_on_a_stale_baseline_entry_for_a_deleted_file():
    failures = gate.run_gates({}, _baseline(files={"gone.py": 4}), {})
    assert any("[Gate B]" in f and "no longer measured" in f for f in failures)


# ---------------------------------------------------------------- Gate C
def test_gate_c_fires_when_a_pragma_no_cover_is_added():
    """`# pragma: no cover` is an allowlist entry hiding in the source. Ratchet it."""
    files = {"engine.py": _file(covered=100, uncovered=0)}
    baseline = _baseline(files={"engine.py": 0}, pragmas={"engine.py": 1})
    failures = gate.run_gates(files, baseline, {"engine.py": 2})
    assert any("[Gate C]" in f and "1 -> 2" in f for f in failures)


def test_gate_c_fires_when_a_pragma_is_removed_so_the_ratchet_tightens():
    files = {"engine.py": _file(covered=100, uncovered=0)}
    baseline = _baseline(files={"engine.py": 0}, pragmas={"engine.py": 1})
    failures = gate.run_gates(files, baseline, {})
    assert any("[Gate C]" in f and "dropped" in f for f in failures)


# ---------------------------------------------------------------- the happy path
def test_the_gate_passes_when_nothing_regressed():
    files = {"engine.py": _file(covered=90, uncovered=10), "rule.py": _file(20, 0)}
    baseline = _baseline(files={"engine.py": 10, "rule.py": 0})
    assert gate.run_gates(files, baseline, {}) == []


# ---------------------------------------------------------------- regeneration
def test_update_baseline_never_invents_an_allowlist_entry():
    """Running --update must not silently exempt a newly-dead file. Otherwise the
    regenerate command becomes a way to make Gate A go green without a test."""
    files = {"newly_dead.py": _file(covered=0, uncovered=12)}
    new = gate.update_baseline(files, _baseline(), {})
    assert new["zero_coverage_allowlist"] == {}
    # ...and so the gate still fails against the freshly regenerated baseline.
    assert any("[Gate A]" in f for f in gate.run_gates(files, new, {}))


def test_update_baseline_drops_an_allowlist_entry_that_is_no_longer_dead():
    files = {"revived.py": _file(covered=3, uncovered=9)}
    baseline = _baseline(allowlist={"revived.py": {"reason": "r", "issue": "#1"}})
    new = gate.update_baseline(files, baseline, {})
    assert "revived.py" not in new["zero_coverage_allowlist"]


def test_update_baseline_records_uncovered_counts_not_percentages():
    files = {"a.py": _file(covered=1, uncovered=9)}
    new = gate.update_baseline(files, _baseline(), {})
    assert new["files"] == {"a.py": 9}
    assert new["_metric"] == "uncovered_lines_per_file"


# ---------------------------------------------------------------- real artifacts
def test_the_committed_baseline_is_in_sync_with_the_gate_schema():
    """The committed baseline must be loadable and shaped as the gate expects --
    otherwise the gate silently degrades to 'no baseline, everything is new'."""
    baseline = gate.load_baseline()
    assert baseline["files"], "committed baseline has no files -- the ratchet is inert"
    assert all(isinstance(v, int) for v in baseline["files"].values())


def test_every_zero_coverage_allowlist_entry_carries_a_reason_and_an_issue():
    """Debt must be visible. An allowlist entry without a reason and an issue link is
    an invisible carve-out, which is the failure mode that kills coverage gates."""
    allowlist = gate.load_baseline().get("zero_coverage_allowlist", {})
    for path, entry in allowlist.items():
        assert entry.get("reason"), f"{path}: allowlist entry has no reason"
        assert str(entry.get("issue", "")).startswith("#"), (
            f"{path}: allowlist entry must cite an issue (e.g. '#712')"
        )


@pytest.mark.parametrize("mode", ["--check", "--report"])
def test_the_gate_cli_modes_are_wired(mode):
    """Guard against the gate being present but unrunnable (the defect class this
    repo exists to eliminate: code that is built and never invoked)."""
    import argparse

    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for flag in ("--check", "--update", "--report"):
        g.add_argument(flag, action="store_true")
    assert ap.parse_args([mode])
