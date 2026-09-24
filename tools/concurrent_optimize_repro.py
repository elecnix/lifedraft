#!/usr/bin/env python3
"""Reproduce (or rule out) the concurrent ``optimize.py`` hang of issue #292.

WHAT IT DOES
------------
Starts ``--runs N`` copies of ``optimize.py --input <contract> --json <out>``
AT THE SAME TIME, watches every one of them (and every process each one
spawns) until a ``--wall-clock`` deadline, and then:

* a run that finished is checked: exit status 0, and a ``--json`` file that is
  BYTE-IDENTICAL to a serial (``--workers 1``) reference run of the same
  contract;
* a run that is still alive at the deadline is a STALL. Before it is killed,
  every process in its tree (deepest first) has its ``/proc/<pid>/wchan`` and
  ``State`` recorded, is dumped with ``py-spy dump --native`` when py-spy is on
  PATH, and is sent SIGABRT so that ``PYTHONFAULTHANDLER=1`` (set for every
  child) writes the Python stack of every thread into that run's log. Then
  anything left is SIGKILLed.

So the report records WHERE a frozen process is blocked, instead of a guess.

EXIT CODES (loud, never "looks fine")
-------------------------------------
0  every run finished, every JSON equals the serial reference, and every run
   was seen with at least one child process (its worker pool engaged);
2  at least one run stalled (the stall report names the logs);
3  the serial preflight was refused or timed out -- the repro would measure
   nothing;
4  a run exited non-zero;
5  a run's JSON differs from the serial reference (first differing byte shown);
6  a run finished but was never seen with a child process: the pool never
   engaged, so the repro measured nothing about pools.

WHY NOT schema/example.json AS-IS
---------------------------------
``optimize.py`` refuses the shipped 4-generation example (the extra adults
decumulate, which #901 does not model yet): it exits in about a second with a
ContractAdaptationError and never builds a pool. A repro on it would report
"no stalls" after measuring nothing. The default input is therefore the
two-generation trim of that same synthetic example that the test suite uses
(``tests/test_input_contract._two_generation_subset``), and a serial
preflight fails loudly (exit 3) if the contract is refused anyway.

Linux only: the process tree is read from ``/proc``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OPTIMIZE = os.path.join(REPO_ROOT, "optimize.py")
POLL_S = 0.2

EXIT_OK = 0
EXIT_STALL = 2
EXIT_PREFLIGHT = 3
EXIT_NONZERO = 4
EXIT_JSON_DIFFERS = 5
EXIT_POOL_NEVER_ENGAGED = 6

PY_SPY_MISSING = "py-spy not on PATH: Python stacks via faulthandler only"


# ── Pure helpers ────────────────────────────────────────────────────────────

def parse_proc_stat(line: str) -> tuple:
    """``(pid, comm, state, ppid)`` from one ``/proc/<pid>/stat`` line.

    ``comm`` is wrapped in parentheses and may itself contain spaces and
    parentheses, so it is delimited by the FIRST ``(`` and the LAST ``)``."""
    open_i = line.index("(")
    close_i = line.rindex(")")
    pid = int(line[:open_i].strip())
    comm = line[open_i + 1:close_i]
    rest = line[close_i + 1:].split()
    return pid, comm, rest[0], int(rest[1])


def descendants(pid_to_ppid: Dict[int, int], root: int) -> List[int]:
    """Every descendant of ``root`` (not ``root`` itself), DEEPEST FIRST."""
    children: Dict[int, List[int]] = {}
    for pid, ppid in pid_to_ppid.items():
        children.setdefault(ppid, []).append(pid)
    found: List[tuple] = []      # (depth, pid)
    frontier = [(1, c) for c in sorted(children.get(root, []))]
    while frontier:
        depth, pid = frontier.pop()
        found.append((depth, pid))
        frontier.extend((depth + 1, c) for c in sorted(children.get(pid, [])))
    found.sort(key=lambda dp: (-dp[0], dp[1]))
    return [pid for _, pid in found]


def first_difference(a: bytes, b: bytes) -> Optional[int]:
    """Offset of the first differing byte, or None when ``a == b``."""
    if a == b:
        return None
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


@dataclass
class RunResult:
    index: int
    pid: int
    log_path: str
    json_path: str
    returncode: Optional[int] = None      # None -> still running at the deadline
    elapsed_s: Optional[float] = None
    peak_descendants: int = 0
    peak_tree_rss_kb: int = 0
    json_diff_offset: Optional[int] = None
    json_missing: bool = False
    stall_report: List[Dict] = field(default_factory=list)

    @property
    def stalled(self) -> bool:
        return self.returncode is None


def classify(results: List[RunResult]) -> int:
    """The tool's exit code, most severe first (see the module docstring)."""
    if any(r.stalled for r in results):
        return EXIT_STALL
    if any(r.returncode != 0 for r in results):
        return EXIT_NONZERO
    if any(r.json_missing or r.json_diff_offset is not None for r in results):
        return EXIT_JSON_DIFFERS
    if any(r.peak_descendants < 1 for r in results):
        return EXIT_POOL_NEVER_ENGAGED
    return EXIT_OK


# ── /proc readers ───────────────────────────────────────────────────────────

def read_process_table(proc_root: str = "/proc") -> Dict[int, int]:
    """``{pid: ppid}`` for every process visible now. A process that exits
    between the directory listing and the read is simply not in the table."""
    table: Dict[int, int] = {}
    for name in os.listdir(proc_root):
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "stat")) as fh:
                pid, _comm, _state, ppid = parse_proc_stat(fh.read())
        except (FileNotFoundError, ProcessLookupError):
            continue
        table[pid] = ppid
    return table


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path) as fh:
            return fh.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def rss_kb(pid: int) -> int:
    """VmRSS of ``pid`` in kB; 0 when the process is gone or has no RSS line
    (a zombie). Used only for the peak-memory figure in the report."""
    status = _read_text(f"/proc/{pid}/status")
    if status is None:
        return 0
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def process_snapshot(pid: int) -> Dict:
    """What the kernel says a (possibly stalled) process is doing."""
    status = _read_text(f"/proc/{pid}/status")
    state = None
    if status is not None:
        for line in status.splitlines():
            if line.startswith("State:"):
                state = line.split(":", 1)[1].strip()
    cmdline = _read_text(f"/proc/{pid}/cmdline")
    return {
        "pid": pid,
        "state": state,
        "wchan": _read_text(f"/proc/{pid}/wchan"),
        "cmdline": None if cmdline is None else cmdline.replace("\0", " ").strip(),
    }


def py_spy_dump(pid: int) -> str:
    """``py-spy dump --native`` output for ``pid``, or the explicit line saying
    py-spy is absent. Never silently skipped."""
    exe = shutil.which("py-spy")
    if exe is None:
        return PY_SPY_MISSING
    proc = subprocess.run([exe, "dump", "--native", "--pid", str(pid)],
                          capture_output=True, text=True, timeout=60)
    return (f"py-spy exit {proc.returncode}\n{proc.stdout}{proc.stderr}")


# ── Orchestration ───────────────────────────────────────────────────────────

def default_input(out_dir: str) -> str:
    """Write the two-generation trim of the synthetic example into ``out_dir``
    (see the module docstring for why the raw example cannot be used)."""
    tests_dir = os.path.join(REPO_ROOT, "tests")
    for path in (REPO_ROOT, tests_dir):
        if path not in sys.path:
            sys.path.insert(0, path)
    from test_input_contract import _load_example, _two_generation_subset
    path = os.path.join(out_dir, "two_generation_example.json")
    with open(path, "w") as fh:
        json.dump(_two_generation_subset(_load_example()), fh, indent=2)
    return path


def _command(optimize: str, input_path: str, json_path: str,
             workers: Optional[int]) -> List[str]:
    cmd = [sys.executable, optimize, "--input", input_path, "--json", json_path]
    if workers is not None:
        cmd += ["--workers", str(workers)]
    return cmd


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["PYTHONFAULTHANDLER"] = "1"
    return env


def preflight(optimize: str, input_path: str, out_dir: str,
              wall_clock: float) -> tuple:
    """One SERIAL run. ``(ok, message, json_bytes)``."""
    json_path = os.path.join(out_dir, "preflight.json")
    log_path = os.path.join(out_dir, "preflight.log")
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(_command(optimize, input_path, json_path, 1),
                                stdout=log, stderr=subprocess.STDOUT,
                                env=_child_env(), cwd=REPO_ROOT)
        try:
            rc = proc.wait(timeout=wall_clock)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return (False, f"preflight timed out after {wall_clock}s (see {log_path}); "
                           "the repro would measure nothing", None)
    if rc != 0:
        return (False, f"preflight refused (exit {rc}, see {log_path}); "
                       "the repro would measure nothing", None)
    if not os.path.exists(json_path):
        return (False, f"preflight exited 0 but wrote no {json_path}; "
                       "the repro would measure nothing", None)
    with open(json_path, "rb") as fh:
        return True, "preflight ok", fh.read()


def _stall_dump(root_pid: int) -> List[Dict]:
    """Record, dump and kill a stalled run's whole tree (deepest first)."""
    tree = descendants(read_process_table(), root_pid) + [root_pid]
    report = []
    for pid in tree:
        snap = process_snapshot(pid)
        snap["py_spy"] = py_spy_dump(pid)
        report.append(snap)
    for pid in tree:
        try:
            os.kill(pid, signal.SIGABRT)     # faulthandler writes the stacks
        except ProcessLookupError:
            pass                             # already gone: nothing to dump
    time.sleep(2.0)
    for pid in tree:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass                             # SIGABRT already ended it
    return report


def run_concurrently(optimize: str, input_path: str, out_dir: str, runs: int,
                     workers: Optional[int], wall_clock: float,
                     reference: bytes) -> List[RunResult]:
    procs = []
    results = []
    logs = []
    start = time.monotonic()
    for i in range(runs):
        json_path = os.path.join(out_dir, f"run{i}.json")
        log_path = os.path.join(out_dir, f"run{i}.log")
        log = open(log_path, "wb")
        logs.append(log)
        proc = subprocess.Popen(_command(optimize, input_path, json_path, workers),
                                stdout=log, stderr=subprocess.STDOUT,
                                env=_child_env(), cwd=REPO_ROOT)
        procs.append(proc)
        results.append(RunResult(index=i, pid=proc.pid, log_path=log_path,
                                 json_path=json_path))

    deadline = start + wall_clock
    while time.monotonic() < deadline:
        table = read_process_table()
        for proc, res in zip(procs, results):
            if res.returncode is not None:
                continue
            rc = proc.poll()
            if rc is not None:
                res.returncode = rc
                res.elapsed_s = round(time.monotonic() - start, 2)
                continue
            tree = descendants(table, proc.pid)
            res.peak_descendants = max(res.peak_descendants, len(tree))
            res.peak_tree_rss_kb = max(res.peak_tree_rss_kb,
                                       sum(rss_kb(p) for p in tree + [proc.pid]))
        if all(r.returncode is not None for r in results):
            break
        time.sleep(POLL_S)

    for proc, res in zip(procs, results):
        if res.returncode is None:
            rc = proc.poll()
            if rc is not None:
                res.returncode = rc
                res.elapsed_s = round(time.monotonic() - start, 2)
    for proc, res in zip(procs, results):
        if res.returncode is None:
            res.stall_report = _stall_dump(proc.pid)
            proc.wait()
    for log in logs:
        log.close()

    for res in results:
        if res.stalled or res.returncode != 0:
            continue
        if not os.path.exists(res.json_path):
            res.json_missing = True
            continue
        with open(res.json_path, "rb") as fh:
            res.json_diff_offset = first_difference(reference, fh.read())
    return results


def render(results: List[RunResult], code: int, header: Dict) -> str:
    out = ["concurrent_optimize_repro report (#292)"]
    for k, v in header.items():
        out.append(f"  {k}: {v}")
    for r in results:
        status = "STALLED" if r.stalled else f"exit {r.returncode} in {r.elapsed_s}s"
        out.append(f"run {r.index} pid {r.pid}: {status}; peak child processes "
                   f"{r.peak_descendants}; peak tree RSS {r.peak_tree_rss_kb} kB; "
                   f"log {r.log_path}")
        if r.json_missing:
            out.append(f"  JSON MISSING: {r.json_path}")
        elif r.json_diff_offset is not None:
            out.append(f"  JSON DIFFERS from the serial reference at byte "
                       f"{r.json_diff_offset}: {r.json_path}")
        for snap in r.stall_report:
            out.append(f"  pid {snap['pid']} state={snap['state']} "
                       f"wchan={snap['wchan']} cmd={snap['cmdline']}")
            for line in snap["py_spy"].splitlines():
                out.append(f"    {line}")
        if r.stall_report:
            out.append(f"  faulthandler stacks (SIGABRT) are in {r.log_path}")
    out.append(f"verdict: exit {code} "
               f"({_VERDICTS[code]})")
    return "\n".join(out)


_VERDICTS = {
    EXIT_OK: "every run finished, byte-identical to serial, pool engaged",
    EXIT_STALL: "at least one run STALLED",
    EXIT_NONZERO: "a run exited non-zero",
    EXIT_JSON_DIFFERS: "a run's JSON differs from the serial reference",
    EXIT_POOL_NEVER_ENGAGED: "a run never spawned a child process: the repro measured nothing",
}


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
    return value


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {text}")
    return value


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=_positive_int, required=True,
                        help="How many optimize.py runs to start at once.")
    parser.add_argument("--workers", type=_positive_int, default=None,
                        help="--workers passed to every run. Omitted: each run uses the "
                             "engine's default sizing (what the issue observed).")
    parser.add_argument("--wall-clock", type=_positive_float, required=True,
                        help="Seconds before a still-running run is declared stalled.")
    parser.add_argument("--input", default=None,
                        help="Contract to optimize (default: the two-generation trim "
                             "of schema/example.json, written into --out-dir).")
    parser.add_argument("--optimize", default=DEFAULT_OPTIMIZE,
                        help="optimize.py to run (point it at another checkout to "
                             "compare before/after).")
    parser.add_argument("--out-dir", required=True,
                        help="Directory for the logs, JSON outputs and report.")
    args = parser.parse_args(argv)

    out_dir = os.path.abspath(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    input_path = args.input if args.input is not None else default_input(out_dir)
    input_path = os.path.abspath(input_path)
    optimize = os.path.abspath(args.optimize)

    ok, message, reference = preflight(optimize, input_path, out_dir, args.wall_clock)
    if not ok:
        print(f"concurrent_optimize_repro: {message}", file=sys.stderr)
        return EXIT_PREFLIGHT

    results = run_concurrently(optimize, input_path, out_dir, args.runs,
                               args.workers, args.wall_clock, reference)
    code = classify(results)
    header = {
        "optimize": optimize,
        "input": input_path,
        "runs": args.runs,
        "workers": "engine default" if args.workers is None else args.workers,
        "wall_clock_s": args.wall_clock,
        "python": sys.version.split()[0],
        "cpus_visible": len(os.sched_getaffinity(0)),
        "py_spy": "not on PATH" if shutil.which("py-spy") is None else shutil.which("py-spy"),
    }
    text = render(results, code, header)
    print(text)
    with open(os.path.join(out_dir, "report.txt"), "w") as fh:
        fh.write(text + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
