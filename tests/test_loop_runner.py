"""Unit tests for `loop.py`'s pure helpers.

`loop.py` (the concurrent pi-chain runner) was the last file at ZERO coverage. Most of
it is asyncio + subprocess orchestration that is not worth driving from a unit test --
but `build_cmd` and `Stats.record` are pure, and they are where the bugs of this shape
live: a flag silently dropped from a command line, or an error counted as a success.

So it comes off the zero-coverage list by being tested. The orchestration lines that
remain untested stay recorded in tools/coverage_baseline.json as uncovered -- visible,
ratcheted debt rather than an invisible allowlist entry.
"""

import importlib

loop = importlib.import_module("loop")


def test_build_cmd_without_provider_or_model():
    assert loop.build_cmd("/run-chain dp -- task", None, None) == [
        "pi",
        "-p",
        "/run-chain dp -- task",
    ]


def test_build_cmd_includes_provider_when_given():
    cmd = loop.build_cmd("/run-chain dp", "openrouter", None)
    assert "--provider" in cmd and "openrouter" in cmd
    assert cmd[-1] == "/run-chain dp", "the prompt must remain the final argument"


def test_build_cmd_includes_model_when_given():
    cmd = loop.build_cmd("/run-chain dp", None, "some-model")
    assert "--model" in cmd and "some-model" in cmd
    assert cmd[-1] == "/run-chain dp"


def test_build_cmd_keeps_the_prompt_last_with_both_flags():
    """The prompt is positional. If a flag is ever appended after it, pi silently
    reinterprets the prompt as a flag value -- the chain then runs the wrong thing."""
    cmd = loop.build_cmd("/run-chain issue", "prov", "mod")
    assert cmd[:2] == ["pi", "-p"]
    assert cmd[-1] == "/run-chain issue"
    assert cmd.count("/run-chain issue") == 1


def test_stats_record_counts_a_success():
    s = loop.Stats()
    s.record("dp", 0)
    assert s.counts["dp"] == 1
    assert "dp" not in s.errs, "exit code 0 must not be recorded as an error"


def test_stats_record_counts_a_failure_as_both_a_run_and_an_error():
    """A failing chain must still count as a run. Counting only errors, or only runs,
    makes a crashing chain indistinguishable from one that never fired."""
    s = loop.Stats()
    s.record("issue", 1)
    assert s.counts["issue"] == 1
    assert s.errs["issue"] == 1


def test_stats_record_accumulates_across_runs():
    s = loop.Stats()
    for code in (0, 1, 0, 2):
        s.record("juris", code)
    assert s.counts["juris"] == 4
    assert s.errs["juris"] == 2
