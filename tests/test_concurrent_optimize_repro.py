"""tools/concurrent_optimize_repro.py (issue #292) -- the hang reproduction.

The tool is only worth anything if each of its verdicts is REAL: a stall must
come back as a stall with a stack in the log, a refused contract must not read
as "no stalls", and a run whose pool never engaged must not read as a pass.
Each verdict is driven here with a tiny stub standing in for ``optimize.py``
(``--optimize`` points the tool at it), so these tests exercise the tool's
own logic in seconds without running the engine. The pure helpers are tested
directly.
"""

import os
import subprocess
import sys
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(REPO_ROOT, "tools", "concurrent_optimize_repro.py")
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

import concurrent_optimize_repro as repro  # noqa: E402


# ── Pure helpers ────────────────────────────────────────────────────────────

def test_parse_proc_stat_plain():
    assert repro.parse_proc_stat("123 (python3) S 45 123 45 0 -1") == (123, "python3", "S", 45)


def test_parse_proc_stat_name_with_spaces_and_parentheses():
    line = "4242 (odd (name) with) spaces) R 7 4242 7 0 -1 4194560"
    assert repro.parse_proc_stat(line) == (4242, "odd (name) with) spaces", "R", 7)


def test_descendants_deepest_first_and_excludes_root_and_strangers():
    table = {10: 1, 11: 10, 12: 10, 13: 11, 14: 13, 99: 1, 100: 99}
    assert repro.descendants(table, 10) == [14, 13, 11, 12]
    assert repro.descendants(table, 12) == []


def test_first_difference():
    assert repro.first_difference(b"abc", b"abc") is None
    assert repro.first_difference(b"abc", b"abd") == 2
    assert repro.first_difference(b"abc", b"abcd") == 3


def _result(**kw):
    base = dict(index=0, pid=1, log_path="l", json_path="j", returncode=0,
                peak_descendants=2)
    base.update(kw)
    return repro.RunResult(**base)


def test_classify_ranks_the_most_severe_verdict_first():
    assert repro.classify([_result()]) == repro.EXIT_OK
    assert repro.classify([_result(peak_descendants=0)]) == repro.EXIT_POOL_NEVER_ENGAGED
    assert repro.classify([_result(json_diff_offset=5, peak_descendants=0)]) == repro.EXIT_JSON_DIFFERS
    assert repro.classify([_result(json_missing=True)]) == repro.EXIT_JSON_DIFFERS
    assert repro.classify([_result(returncode=1, json_diff_offset=5)]) == repro.EXIT_NONZERO
    assert repro.classify([_result(), _result(returncode=None)]) == repro.EXIT_STALL


def test_py_spy_absence_is_reported_not_skipped(monkeypatch):
    monkeypatch.setattr(repro.shutil, "which", lambda name: None)
    assert repro.py_spy_dump(os.getpid()) == repro.PY_SPY_MISSING
    assert "py-spy not on PATH" in repro.PY_SPY_MISSING


# ── Stub-driven verdicts ────────────────────────────────────────────────────

# Every stub parses the two flags the tool passes and behaves differently for
# the SERIAL preflight (``--workers 1``) and the concurrent runs.
_STUB_HEADER = textwrap.dedent("""\
    import os, subprocess, sys, time
    argv = sys.argv[1:]
    out = argv[argv.index("--json") + 1]
    preflight = "--workers" in argv and argv[argv.index("--workers") + 1] == "1"
    def write(text):
        with open(out, "w") as fh:
            fh.write(text)
    """)

STUBS = {
    "stall": "if preflight:\n    write('{}')\nelse:\n    time.sleep(600)\n",
    # Writes its JSON AND exits 1, so only the exit status can refuse it.
    "refused": "write('{}')\nsys.exit(1)\n",
    # Exits 0 but writes no JSON: the reference is missing.
    "no_json": "sys.exit(0)\n",
    "nonzero": "if preflight:\n    write('{}')\nelse:\n    sys.exit(1)\n",
    "differs": ("if preflight:\n    write('{}')\nelse:\n"
                "    subprocess.run(['sleep', '0.6'])\n    write(str(os.getpid()))\n"),
    "no_child": "write('{}')\ntime.sleep(0.6)\n",
    "happy": "if not preflight:\n    subprocess.run(['sleep', '0.6'])\nwrite('{}')\n",
}


def _run_tool(tmp_path, stub, *extra, timeout=120):
    stub_path = tmp_path / f"stub_{stub}.py"
    stub_path.write_text(_STUB_HEADER + STUBS[stub])
    dummy = tmp_path / "input.json"
    dummy.write_text("{}")
    out_dir = tmp_path / "out"
    cmd = [sys.executable, TOOL, "--runs", "2", "--wall-clock", "5",
           "--input", str(dummy), "--optimize", str(stub_path),
           "--out-dir", str(out_dir), *extra]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc, out_dir


@pytest.mark.timeout(120)
def test_stall_exits_2_with_a_faulthandler_stack_in_the_log(tmp_path):
    proc, out_dir = _run_tool(tmp_path, "stall")
    assert proc.returncode == repro.EXIT_STALL, proc.stdout + proc.stderr
    assert "STALLED" in proc.stdout
    assert "state=" in proc.stdout and "wchan=" in proc.stdout
    log = (out_dir / "run0.log").read_text()
    # faulthandler (PYTHONFAULTHANDLER=1 + SIGABRT) names the frame it was in.
    assert "File " in log and "line " in log and "<module>" in log, log
    if repro.shutil.which("py-spy") is None:
        assert repro.PY_SPY_MISSING in proc.stdout


@pytest.mark.timeout(120)
def test_preflight_refusal_exits_3(tmp_path):
    proc, _ = _run_tool(tmp_path, "refused")
    assert proc.returncode == repro.EXIT_PREFLIGHT, proc.stdout + proc.stderr
    assert "the repro would measure nothing" in proc.stderr


@pytest.mark.timeout(120)
def test_preflight_without_json_exits_3(tmp_path):
    proc, _ = _run_tool(tmp_path, "no_json")
    assert proc.returncode == repro.EXIT_PREFLIGHT, proc.stdout + proc.stderr
    assert "wrote no" in proc.stderr


@pytest.mark.timeout(120)
def test_nonzero_run_exits_4(tmp_path):
    proc, _ = _run_tool(tmp_path, "nonzero")
    assert proc.returncode == repro.EXIT_NONZERO, proc.stdout + proc.stderr


@pytest.mark.timeout(120)
def test_json_differing_from_serial_exits_5(tmp_path):
    proc, _ = _run_tool(tmp_path, "differs")
    assert proc.returncode == repro.EXIT_JSON_DIFFERS, proc.stdout + proc.stderr
    assert "JSON DIFFERS" in proc.stdout


@pytest.mark.timeout(120)
def test_pool_never_engaged_exits_6(tmp_path):
    proc, _ = _run_tool(tmp_path, "no_child")
    assert proc.returncode == repro.EXIT_POOL_NEVER_ENGAGED, proc.stdout + proc.stderr


@pytest.mark.timeout(120)
def test_happy_path_exits_0(tmp_path):
    proc, out_dir = _run_tool(tmp_path, "happy", "--workers", "2")
    assert proc.returncode == repro.EXIT_OK, proc.stdout + proc.stderr
    assert (out_dir / "report.txt").read_text().startswith("concurrent_optimize_repro report")
