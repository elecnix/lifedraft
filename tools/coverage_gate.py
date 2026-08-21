#!/usr/bin/env python3
"""Per-file coverage gate: zero-coverage detection + an uncovered-line ratchet.

WHY THIS EXISTS
---------------
This codebase was rebuilt because the engine *silently did nothing*: a rule that
was implemented and never called, an input that was parsed and never read. The
run completed, green, printing a confident wrong number.

An untested file is the natural habitat of exactly that bug. Coverage is one of
the instruments that should scream -- but only if it is pointed at the right
number.

WHY NOT A REPO-WIDE PERCENTAGE
------------------------------
The previous config carried `fail_under = 93`. It was never executed (coverage
was not installed and no workflow invoked it), which made it an instance of the
very defect class this repo exists to eliminate. But even had it run, it would
have been theatre: at ~40k statements, 200 brand-new untested lines move the
global number by roughly half a percent. A threshold loose enough not to fire on
noise is loose enough to let a whole dead module through.

WHY THE RATCHET COUNTS LINES, NOT PERCENT
-----------------------------------------
Gate B ratchets on the UNCOVERED LINE COUNT per file, not on the per-file
percentage. Both were considered:

  * A percentage is a ratio of two moving numbers, so it moves for reasons that
    have nothing to do with test quality. The masking case is the one that
    matters here: a file at 90/100 statements (90%) that gains 20 well-tested
    lines and 1 untested guard clause lands at 110/121 = 90.9% -- the percentage
    goes UP and a percent-ratchet PASSES, while an untested guard clause (a rule
    that can silently do nothing -- the exact bug) slips in. The line-count
    ratchet goes 10 -> 11, fires, and names the line.
  * A percentage also trips spuriously on small files: deleting one covered line
    from a 4-line file takes it from 75% to 67% and fails a ratchet for a change
    that removed code. Handling that requires a rounding fudge / minimum-file-size
    rule, i.e. a second tunable to argue about.
  * The uncovered-line count is monotone and unambiguous: it is unmoved by adding
    *covered* code (which is what we want to encourage), it rises if and only if
    untested statements enter the file, and it is immediately actionable because
    it names the lines.

So: "the number of uncovered lines in this file must not increase." No rounding
problem, no tunable, no masking.

THE GATES
---------
  Gate A -- zero coverage. A tracked production file with statements but ZERO
            covered lines fails. Nothing imports it; nothing runs it. Entries in
            `zero_coverage_allowlist` are exempt, and each one must carry a
            reason and an issue link -- visible debt, not an invisible carve-out.

  Gate B -- the ratchet. A file's uncovered-line count may not exceed its
            baseline. A file absent from the baseline is a NEW file and its
            allowance is 0: new code arrives tested, or you regenerate the
            baseline and the debt shows up as a number going up in a committed,
            reviewable file.

  Gate C -- the pragma ratchet. `# pragma: no cover` is an allowlist entry that
            hides in the source instead of in a reviewable file. Its per-file
            count may not increase either.

AUTO-TIGHTENING
---------------
If a file's uncovered count drops BELOW its baseline, the baseline is
stale-loose: it is now permitting slack that the code no longer uses, and
coverage could quietly drift back down to it. That is a FAILURE, not a pass, and
the fix is to run the regenerate command below in the same PR. The ratchet only
ever clicks one way.

USAGE
-----
    # measure (writes coverage.json)
    pytest -q --cov --cov-report=json:coverage.json --cov-report=term-missing

    # enforce (CI)
    python tools/coverage_gate.py --check

    # regenerate the baseline after coverage legitimately changes, then commit it
    python tools/coverage_gate.py --update

    # findings report: zero-coverage files, worst offenders
    python tools/coverage_gate.py --report
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_JSON = REPO_ROOT / "coverage.json"
BASELINE_PATH = REPO_ROOT / "tools" / "coverage_baseline.json"

_PRAGMA_RE = re.compile(r"pragma:\s*no cover")

REGEN_HINT = "python tools/coverage_gate.py --update   # then commit tools/coverage_baseline.json"


# --------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------
def tracked_python_files() -> set[str]:
    """Git-tracked .py paths, so a developer's untracked scratch file can never
    fail (or silently satisfy) the gate. `source = ["."]` makes coverage.py walk
    the working tree, which would otherwise pick such files up."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def load_coverage(path: Path) -> dict[str, dict]:
    """Read coverage.json -> {relpath: {covered, uncovered, statements}}.

    Only tracked, non-test files are returned. A file coverage.py reports with
    zero statements (e.g. an empty __init__.py) is dropped: it cannot be covered,
    so failing it under Gate A would be meaningless.
    """
    if not path.exists():
        sys.exit(
            f"error: {path} not found. Run the measurement first:\n"
            f"  pytest -q --cov --cov-report=json:coverage.json"
        )
    raw = json.loads(path.read_text())
    tracked = tracked_python_files()

    files: dict[str, dict] = {}
    for name, data in raw.get("files", {}).items():
        rel = str(Path(name).as_posix())
        if rel not in tracked:
            continue
        summary = data["summary"]
        statements = summary["num_statements"]
        if statements == 0:
            continue
        files[rel] = {
            "covered": summary["covered_lines"],
            "uncovered": summary["missing_lines"],
            "statements": statements,
            "missing": data.get("missing_lines", []),
        }
    return files


def count_pragmas() -> dict[str, int]:
    """Per-file `# pragma: no cover` counts across tracked production source."""
    counts: dict[str, int] = {}
    for rel in sorted(tracked_python_files()):
        if rel.startswith("tests/") or "/tests/" in rel or rel.startswith("tools/"):
            continue
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        n = len(_PRAGMA_RE.findall(text))
        if n:
            counts[rel] = n
    return counts


def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {"files": {}, "zero_coverage_allowlist": {}, "pragma_no_cover": {}}
    return json.loads(BASELINE_PATH.read_text())


# --------------------------------------------------------------------------
# gates
# --------------------------------------------------------------------------
def run_gates(files: dict[str, dict], baseline: dict, pragmas: dict[str, int]) -> list[str]:
    """Return a list of failure messages. Empty list == the gate passes."""
    failures: list[str] = []
    allowlist = baseline.get("zero_coverage_allowlist", {})
    base_files = baseline.get("files", {})
    base_pragmas = baseline.get("pragma_no_cover", {})

    # ---- Gate A: zero-coverage files -------------------------------------
    zero = sorted(f for f, d in files.items() if d["covered"] == 0)
    for f in zero:
        if f in allowlist:
            continue
        d = files[f]
        failures.append(
            f"[Gate A] {f}: ZERO covered lines ({d['statements']} statements). "
            f"Nothing imports or executes this file. Write a test -- do NOT add it "
            f"to zero_coverage_allowlist to go green."
        )

    # An allowlist entry that is no longer needed must not linger: it would keep a
    # file exempt from Gate A forever after someone finally tested it.
    for f in sorted(allowlist):
        if f in files and files[f]["covered"] > 0:
            failures.append(
                f"[Gate A] {f}: is in zero_coverage_allowlist but now HAS coverage "
                f"({files[f]['covered']} lines). Remove the allowlist entry.\n"
                f"          {REGEN_HINT}"
            )
        elif f not in files:
            failures.append(
                f"[Gate A] {f}: is in zero_coverage_allowlist but no longer exists. "
                f"Remove the stale entry.\n          {REGEN_HINT}"
            )

    # ---- Gate B: the uncovered-line ratchet -------------------------------
    for f in sorted(files):
        current = files[f]["uncovered"]
        # A file absent from the baseline is new; its allowance is zero.
        allowed = base_files.get(f)
        if allowed is None:
            if current > 0:
                failures.append(
                    f"[Gate B] {f}: NEW file with {current} uncovered lines "
                    f"(lines {_fmt_lines(files[f]['missing'])}). New code arrives "
                    f"tested. If this debt is deliberate, regenerate the baseline so "
                    f"it is visible in review:\n          {REGEN_HINT}"
                )
            continue
        if current > allowed:
            failures.append(
                f"[Gate B] {f}: uncovered lines {allowed} -> {current} (+{current - allowed}). "
                f"Uncovered: {_fmt_lines(files[f]['missing'])}. Write a test for the new "
                f"lines -- do NOT regenerate the baseline to absorb a regression."
            )
        elif current < allowed:
            # Auto-tighten: coverage improved, so the baseline is now stale-loose and
            # would silently permit a drift back down to `allowed`. Click the ratchet.
            failures.append(
                f"[Gate B] {f}: coverage IMPROVED ({allowed} -> {current} uncovered) but the "
                f"baseline still permits {allowed}. That slack would let coverage quietly "
                f"drift back down. Tighten the ratchet:\n          {REGEN_HINT}"
            )

    for f in sorted(base_files):
        if f not in files:
            failures.append(
                f"[Gate B] {f}: in the baseline but no longer measured (deleted or "
                f"omitted). Remove the stale entry:\n          {REGEN_HINT}"
            )

    # ---- Gate C: the pragma ratchet ---------------------------------------
    for f in sorted(set(pragmas) | set(base_pragmas)):
        current = pragmas.get(f, 0)
        allowed = base_pragmas.get(f, 0)
        if current > allowed:
            failures.append(
                f"[Gate C] {f}: `# pragma: no cover` count {allowed} -> {current}. "
                f"A pragma is an allowlist entry that hides in the source instead of in "
                f"a reviewable file. Write the test instead."
            )
        elif current < allowed:
            failures.append(
                f"[Gate C] {f}: `# pragma: no cover` count dropped {allowed} -> {current}. "
                f"Tighten the ratchet:\n          {REGEN_HINT}"
            )

    return failures


def _fmt_lines(missing: list[int], limit: int = 12) -> str:
    if not missing:
        return "-"
    shown = ", ".join(str(n) for n in missing[:limit])
    if len(missing) > limit:
        shown += f", ... (+{len(missing) - limit} more)"
    return shown


# --------------------------------------------------------------------------
# baseline regeneration
# --------------------------------------------------------------------------
def update_baseline(files: dict[str, dict], baseline: dict, pragmas: dict[str, int]) -> dict:
    """Rewrite the baseline from the current measurement.

    Allowlist entries are CARRIED OVER, never invented: a file that newly has zero
    coverage does not get silently exempted by running --update. It will keep
    failing Gate A until a human adds an entry with a reason and an issue link,
    which is a visible, reviewable act.
    """
    allowlist = dict(baseline.get("zero_coverage_allowlist", {}))
    # drop allowlist entries that are no longer zero-coverage or no longer exist
    for f in list(allowlist):
        if f not in files or files[f]["covered"] > 0:
            del allowlist[f]

    return {
        "_comment": (
            "Per-file coverage ratchet. `files` maps a tracked production source file to "
            "the number of UNCOVERED lines it is permitted to have. The count may never "
            "increase (Gate B); if it decreases, regenerate this file so the ratchet "
            "tightens. A file absent from `files` is new and is allowed ZERO uncovered "
            "lines. Regenerate with: python tools/coverage_gate.py --update"
        ),
        "_metric": "uncovered_lines_per_file",
        "_why_not_percent": (
            "A percentage moves when the denominator moves, so adding covered code can "
            "mask newly-added uncovered code. See the module docstring in "
            "tools/coverage_gate.py."
        ),
        "zero_coverage_allowlist": allowlist,
        "pragma_no_cover": dict(sorted(pragmas.items())),
        "files": {f: files[f]["uncovered"] for f in sorted(files)},
    }


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def print_report(files: dict[str, dict], baseline: dict) -> None:
    allowlist = baseline.get("zero_coverage_allowlist", {})
    total_stmt = sum(d["statements"] for d in files.values())
    total_unc = sum(d["uncovered"] for d in files.values())
    pct = 100.0 * (total_stmt - total_unc) / total_stmt if total_stmt else 100.0

    print("=" * 78)
    print("COVERAGE FINDINGS")
    print("=" * 78)
    print(f"tracked production files measured : {len(files)}")
    print(f"statements                        : {total_stmt}")
    print(f"uncovered statements              : {total_unc}")
    print(f"overall line coverage             : {pct:.2f}%  (context only -- NOT a gate)")
    print()

    zero = sorted(f for f, d in files.items() if d["covered"] == 0)
    print(f"-- ZERO-COVERAGE FILES ({len(zero)}) " + "-" * 40)
    if not zero:
        print("   none")
    for f in zero:
        mark = "ALLOWLISTED" if f in allowlist else "FAILING    "
        note = ""
        if f in allowlist:
            e = allowlist[f]
            note = f"  [{e.get('issue', 'no issue')}] {e.get('reason', '')}"
        print(f"   {mark}  {f}  ({files[f]['statements']} stmts){note}")
    print()

    worst = sorted(files.items(), key=lambda kv: -kv[1]["uncovered"])[:15]
    print("-- WORST 15 BY UNCOVERED LINE COUNT " + "-" * 30)
    for f, d in worst:
        fpct = 100.0 * d["covered"] / d["statements"]
        print(f"   {d['uncovered']:>6} uncovered  {fpct:5.1f}%  {f}")
    print()


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="enforce the gates (CI); exit 1 on failure")
    g.add_argument("--update", action="store_true", help="regenerate the committed baseline")
    g.add_argument("--report", action="store_true", help="print the human-readable findings report")
    ap.add_argument("--coverage-json", type=Path, default=COVERAGE_JSON)
    args = ap.parse_args()

    files = load_coverage(args.coverage_json)
    baseline = load_baseline()
    pragmas = count_pragmas()

    if args.report:
        print_report(files, baseline)
        return 0

    if args.update:
        new = update_baseline(files, baseline, pragmas)
        BASELINE_PATH.write_text(json.dumps(new, indent=2, sort_keys=False) + "\n")
        total = sum(new["files"].values())
        print(f"wrote {BASELINE_PATH.relative_to(REPO_ROOT)}")
        print(f"  {len(new['files'])} files, {total} uncovered lines permitted")
        print(f"  {len(new['zero_coverage_allowlist'])} zero-coverage allowlist entries")
        return 0

    # --check
    failures = run_gates(files, baseline, pragmas)
    if not failures:
        total = sum(d["uncovered"] for d in files.values())
        print(
            f"coverage gate PASSED: {len(files)} production files, "
            f"{total} uncovered lines, none regressed."
        )
        return 0

    print("COVERAGE GATE FAILED", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    for f in failures:
        print(f"  {f}", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    print(f"{len(failures)} failure(s).", file=sys.stderr)
    print(
        "\nWhen this gate fires, the answer is to WRITE A TEST -- not to grow the "
        "allowlist. See AGENTS.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
