#!/usr/bin/env python3
"""Runnable, research-backed examples: discovery, regeneration, projection, checks.

Issue #300. Each example lives in ``examples/<source>/<slug>/`` and holds
exactly five files::

    input.json   a contract document (validates + maps through input_contract)
    report.json  a compact projection of ``optimize.py --json`` (this module)
    report.md    the engine's own ``optimize.py --md`` output (trimmed only if large)
    README.md    source, publication claim, encoding, comparison, verdict
    meta.json    source URL, publication id, retrieval date, verdict, linked issues

``python tools/examples.py regen [<path>...]`` rewrites report.json / report.md
by running the REAL ``optimize.py`` in a subprocess (DP#11: this module never
simulates, ranks, scores or renders anything itself). The CI guard
``tests/test_examples_guard.py`` calls the very same ``regenerate()`` and
compares the result byte for byte against what is committed, so a report
cannot be hand-edited and cannot drift from what the engine now produces.

The projection contract (``project_report``, PROJECTION_VERSION = 1)
--------------------------------------------------------------------
The full ``--json`` report is ~13 MB; it is not committable per example.
``project_report`` reduces it to <= 200 KB, deterministically:

* Every top-level key of the full report is classified explicitly, as either
  copied verbatim (``TOP_VERBATIM``) or projected (``TOP_PROJECTED``). A key the
  engine adds, or one it drops, raises ``ExamplesError``: classify it here and
  bump PROJECTION_VERSION. Nothing is silently dropped.
* ``scenarios``: every scenario, in the ENGINE's order (never re-sorted, so
  ranking is not reimplemented here), carrying ``rank`` = position + 1 and
  every field whose value is a scalar (str, bool, int, float, None), copied
  unrounded. Non-scalar fields (``year_by_year``, ``solvency``, ``runway`` ...)
  are dropped. Two naming facts a reader needs: a scenario's label is its
  ``strategy`` field (only ``category_bests`` entries carry ``label``), and its
  terminal assets are ``future_value`` (which equals the last year's
  ``total_assets`` in its ``year_by_year``; the engine has no field named
  "terminal assets").
* ``category_bests``: the same scalar projection, without ``rank``.
* ``winner_year_by_year``: the #1 scenario's per-year rows restricted to
  ``KEY_YEAR_COLUMNS``, read by strict key (a missing column raises). The full
  report's top-level ``year_by_year`` must equal ``scenarios[0].year_by_year``
  (the winner-identity contract) or projection refuses.
* ``projection``: the version, this function's name, and the sha256 and byte
  length of the FULL report. Because the hash is committed, byte-identity of
  report.json also pins every byte of the unprojected 13 MB output.

Any change to the rules above bumps PROJECTION_VERSION and regenerates every
example in the same PR.

``project_markdown`` keeps the engine's ``--md`` output verbatim when it is at
most REPORT_MD_CAP bytes; otherwise it cuts at the last ``## `` section
boundary that fits and appends a visible marker naming how much was kept.

Reports are canonical under ONE Python minor version (CANONICAL_PYTHON, the
version CI's PR leg runs): report.json floats differ by one unit in the last
place under 3.10/3.11 (measured; see the constant's comment). ``regen``
refuses to write under another version; the guard byte-compares under the
canonical version and uses ``compare_reports_near`` on the nightly legs.

Loud failure (DP#32): discovery raises on a missing or empty ``examples/``
tree; ``run_optimize`` refuses a non-zero exit AND an exit 0 that wrote no
output (``optimize.main()`` prints "Error: ..." and returns 0 on an objective
selection error); static checks return every problem, never a silent pass.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES_ROOT = REPO_ROOT / "examples"
OPTIMIZE_PY = REPO_ROOT / "optimize.py"

PROJECTION_VERSION = 1
PROJECTION_FUNCTION = "tools/examples.py::project_report"
PROJECTION_SOURCE = "optimize.py --json"

REQUIRED_FILES = ("input.json", "report.json", "report.md", "README.md", "meta.json")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Per-file byte caps. report.json's 200 KB is the issue's contract; the others
# bound what a contributor can commit without a reviewer noticing.
REPORT_JSON_CAP = 200_000
REPORT_MD_CAP = 100_000
INPUT_CAP = 200_000
README_CAP = 50_000
META_CAP = 4_000
SIZE_CAPS = {
    "input.json": INPUT_CAP,
    "report.json": REPORT_JSON_CAP,
    "report.md": REPORT_MD_CAP,
    "README.md": README_CAP,
    "meta.json": META_CAP,
}

KEY_YEAR_COLUMNS = (
    "year",
    "total_assets",
    "net_benefit",
    "total_debt",
    "mortgage_balance",
    "heloc_balance",
    "total_rrsp",
    "total_tfsa",
    "non_reg_balance",
    "resp_balance",
    "total_family_income",
    "after_tax_income",
    "drawdown_income",
    "living_costs",
    "oas_clawback",
    "ruined",
)

TOP_VERBATIM = frozenset({
    "title",
    "situation",
    "model_fidelity",
    "optimal_refi_level",
    "resp_cashout",
    "equity_grants",
    "runway",
    "runway_sweep",
    "asset_location",
    "total_scenarios",
})
TOP_PROJECTED = frozenset({"scenarios", "category_bests", "year_by_year"})

REQUIRED_README_SECTIONS = (
    "Source",
    "Publication claim",
    "Encoding",
    "Engine vs publication",
    "Verdict",
)
VERDICT_RE = re.compile(
    r"^(AGREES|DIFFERS \(explained\)|DIFFERS \(engine issue #([1-9][0-9]*)\))$"
)
META_KEYS = frozenset({
    "source",
    "source_url",
    "publication_id",
    "title",
    "authors",
    "year",
    "retrieval_date",
    "verdict",
    "linked_issues",
})

HOME_PATH_RE = re.compile(r"(/home/|/Users/|/root/|[A-Za-z]:\\Users\\|~/)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SIN_RE = re.compile(r"\b\d{3}[- ]\d{3}[- ]\d{3}\b")
# report.json holds thousands of computed floats whose digit runs would trip a
# SIN-shaped pattern by coincidence; only the human-authored files are scanned.
SIN_SCANNED_FILES = ("README.md", "meta.json", "input.json")

DIFF_LINE_LIMIT = 80

# Reports are canonical under ONE CPython minor version: the one the PR leg of
# CI runs (.github/workflows/tests.yml, 3.12). Measured for issue #300: the
# seed regenerated under 3.10.15 and under 3.11.15 differs from 3.12 in about 16
# report.json floats, each by one unit in the last place (report.md is
# identical), because CPython 3.12 made the built-in sum() of floats
# compensated. numpy 2.2.6 vs 2.5.3 and scipy 1.15.3 vs 1.18.1 under 3.12 made no
# difference. So `regen` refuses to write under any other minor version, and the
# guard compares BYTES under the canonical version and falls back to
# `compare_reports_near` (floats at CROSS_VERSION_REL_TOL, hash excluded) only
# on the nightly 3.10/3.11 legs.
CANONICAL_PYTHON = (3, 12)
CROSS_VERSION_REL_TOL = 1e-9
_CROSS_VERSION_UNPINNED = ("full_report_bytes", "full_report_sha256")
OUTPUT_TAIL_LINES = 40


class ExamplesError(Exception):
    """The one error this tool raises: an example tree, a run, or a projection
    that cannot be trusted. Never caught inside this module."""


# --------------------------------------------------------------- discovery
def discover_examples(root: Path = EXAMPLES_ROOT) -> list[Path]:
    """Every directory exactly two levels below ``root`` (examples/<source>/<slug>),
    sorted. Walks DIRECTORIES, not ``*/*/input.json``: an example missing a file
    must still be collected (and then fail), never silently drop out.

    Raises ExamplesError on a missing root, zero examples, a bad slug, an
    empty source directory, or a stray file (only ``examples/README.md`` may sit
    outside an example directory)."""
    if not root.is_dir():
        raise ExamplesError(
            f"examples root {root} does not exist; the examples guard refuses to "
            f"pass vacuously (DP#32)"
        )
    found: list[Path] = []
    for source in sorted(root.iterdir()):
        if source.is_file():
            if source.parent == root and source.name == "README.md":
                continue
            raise ExamplesError(
                f"stray file {source} at depth 1 of {root}: only examples/README.md "
                f"may sit outside examples/<source>/<slug>/"
            )
        if not SLUG_RE.match(source.name):
            raise ExamplesError(
                f"source directory name {source.name!r} does not match {SLUG_RE.pattern}"
            )
        slugs = sorted(source.iterdir())
        if not slugs:
            raise ExamplesError(f"source directory {source} holds no example")
        for slug in slugs:
            if not slug.is_dir():
                raise ExamplesError(
                    f"stray file {slug} at depth 2 of {root}: every entry of "
                    f"examples/<source>/ must be an example directory"
                )
            if not SLUG_RE.match(slug.name):
                raise ExamplesError(
                    f"example directory name {slug.name!r} (in {source.name}) does "
                    f"not match {SLUG_RE.pattern}"
                )
            found.append(slug)
    if not found:
        raise ExamplesError(
            f"zero examples found under {root}: a glob that matches nothing is a "
            f"failure, not a pass (DP#32)"
        )
    return found


def example_id(example_dir: Path) -> str:
    return f"{example_dir.parent.name}/{example_dir.name}"


# --------------------------------------------------------------- run step
def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-OUTPUT_TAIL_LINES:])


def run_optimize(
    input_path: Path,
    workdir: Path,
    *,
    workers: int | None,
    optimize_py: Path = OPTIMIZE_PY,
) -> tuple[Path, Path]:
    """Run the real optimize.py on ``input_path`` and return (full.json, full.md).

    Hermetic: HOME is an empty directory inside ``workdir``, because
    tax_data.TaxDataProvider reads ~/.cache/lifedraft/tax/*.json for years it
    has no registered fallback for; a contributor's stale cache must not leak
    into a committed report. cwd is ``workdir``, so nothing lands next to the
    example. ``--save-session`` is never passed.

    Refuses a non-zero exit AND an exit 0 without both outputs (the CLI's
    silent-success path)."""
    home = workdir / "home"
    home.mkdir()
    full_json = workdir / "full.json"
    full_md = workdir / "full.md"
    cmd = [
        sys.executable,
        str(optimize_py),
        "--input",
        str(input_path.resolve()),
        "--json",
        str(full_json),
        "--md",
        str(full_md),
    ]
    if workers is not None:
        cmd += ["--workers", str(workers)]
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, env=env, check=False
    )
    detail = (
        f"command: {' '.join(cmd)}\n--- stdout (tail) ---\n{_tail(proc.stdout)}\n"
        f"--- stderr (tail) ---\n{_tail(proc.stderr)}"
    )
    if proc.returncode != 0:
        raise ExamplesError(f"optimize.py exited {proc.returncode}\n{detail}")
    for out in (full_json, full_md):
        if not out.is_file() or out.stat().st_size == 0:
            raise ExamplesError(
                f"optimize.py exited 0 but wrote no {out.name} (silent-success CLI "
                f"path; read its stdout)\n{detail}"
            )
    return full_json, full_md


# --------------------------------------------------------------- projection
def _is_scalar(value) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def _scalars(entry: dict) -> dict:
    return {k: v for k, v in sorted(entry.items()) if _is_scalar(v)}


def project_report(full: dict, *, full_bytes: bytes) -> dict:
    """Project the full ``optimize.py --json`` report (see the module docstring
    for the contract). Pure; raises ExamplesError on any shape it was not
    written for."""
    expected = TOP_VERBATIM | TOP_PROJECTED
    actual = set(full)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise ExamplesError(
            f"full report top-level keys changed: unknown {unknown}, missing "
            f"{missing}; classify it in project_report and bump PROJECTION_VERSION"
        )
    scenarios = full["scenarios"]
    if not scenarios:
        raise ExamplesError("full report has an empty 'scenarios' list")
    if full["total_scenarios"] != len(scenarios):
        raise ExamplesError(
            f"full report total_scenarios={full['total_scenarios']!r} but "
            f"len(scenarios)={len(scenarios)}"
        )
    if full["year_by_year"] != scenarios[0]["year_by_year"]:
        raise ExamplesError(
            "full report's top-level year_by_year is not scenarios[0].year_by_year "
            "(winner-identity contract broken; the #1 scenario is ambiguous)"
        )
    winner_rows = []
    for index, row in enumerate(scenarios[0]["year_by_year"]):
        projected = {}
        for column in KEY_YEAR_COLUMNS:
            if column not in row:
                raise ExamplesError(
                    f"winner year_by_year row {index} has no column {column!r}; "
                    f"update KEY_YEAR_COLUMNS and bump PROJECTION_VERSION"
                )
            projected[column] = row[column]
        winner_rows.append(projected)

    report = {
        "projection": {
            "version": PROJECTION_VERSION,
            "function": PROJECTION_FUNCTION,
            "source": PROJECTION_SOURCE,
            "full_report_bytes": len(full_bytes),
            "full_report_sha256": hashlib.sha256(full_bytes).hexdigest(),
            "key_year_columns": list(KEY_YEAR_COLUMNS),
        }
    }
    for key in sorted(TOP_VERBATIM):
        report[key] = full[key]
    report["scenarios"] = [
        {"rank": index + 1, **_scalars(scenario)}
        for index, scenario in enumerate(scenarios)
    ]
    report["category_bests"] = [_scalars(entry) for entry in full["category_bests"]]
    report["winner_year_by_year"] = winner_rows
    return report


def dump_report_json(obj: dict) -> str:
    """Canonical serialisation: sorted keys, 2-space indent, trailing newline.
    A NaN or infinity is refused, never written as a non-JSON token."""
    try:
        text = json.dumps(
            obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
    except ValueError as err:
        raise ExamplesError(f"report.json would contain a non-finite float: {err}")
    text += "\n"
    size = len(text.encode("utf-8"))
    if size > REPORT_JSON_CAP:
        raise ExamplesError(
            f"projected report.json is {size} B, over the {REPORT_JSON_CAP} B cap"
        )
    return text


def _trim_marker(kept: int, total: int) -> str:
    return (
        f"\n<!-- trimmed by tools/examples.py project_markdown v{PROJECTION_VERSION}: "
        f"kept {kept} of {total} bytes; full output via optimize.py --md -->\n"
    )


def project_markdown(md: str) -> str:
    """The engine's --md output verbatim if it fits REPORT_MD_CAP; otherwise the
    longest prefix ending just before a ``## `` line that fits with the marker."""
    data = md.encode("utf-8")
    total = len(data)
    if total <= REPORT_MD_CAP:
        return md
    budget = REPORT_MD_CAP - len(_trim_marker(total, total).encode("utf-8"))
    boundaries = [m.start() + 1 for m in re.finditer(rb"\n## ", data)]
    fitting = [b for b in boundaries if b <= budget]
    if not fitting:
        raise ExamplesError(
            f"report.md is {total} B (cap {REPORT_MD_CAP}) and no '## ' section "
            f"boundary fits under {budget} B; it cannot be trimmed deterministically"
        )
    cut = max(fitting)
    return data[:cut].decode("utf-8") + _trim_marker(cut, total)


# --------------------------------------------------------------- regenerate
def regenerate(
    example_dir: Path, *, workers: int | None, optimize_py: Path = OPTIMIZE_PY
) -> tuple[str, str]:
    """Run the engine on ``example_dir/input.json`` and return the
    (report.json, report.md) texts it projects to. Writes nothing."""
    input_path = example_dir / "input.json"
    if not input_path.is_file():
        raise ExamplesError(f"{example_dir} has no input.json to regenerate from")
    with tempfile.TemporaryDirectory(prefix="lifedraft-example-") as tmp:
        full_json, full_md = run_optimize(
            input_path, Path(tmp), workers=workers, optimize_py=optimize_py
        )
        full_bytes = full_json.read_bytes()
        md_text = full_md.read_text(encoding="utf-8")
    full = json.loads(full_bytes)
    json_text = dump_report_json(project_report(full, full_bytes=full_bytes))
    return json_text, project_markdown(md_text)


def write_reports(example_dir: Path, json_text: str, md_text: str) -> None:
    """Write both reports atomically (temp file + os.replace), and only once
    both were produced."""
    for name, text in (("report.json", json_text), ("report.md", md_text)):
        target = example_dir / name
        tmp = example_dir / f".{name}.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)


def _regen_instruction(example_dir: Path) -> str:
    return (
        f"run `python tools/examples.py regen examples/{example_id(example_dir)}` "
        f"and explain the move in the PR"
    )


def compare_reports(example_dir: Path, json_text: str, md_text: str) -> list[str]:
    """Byte comparison of the committed reports against freshly regenerated
    ones. Returns one message per mismatch (with a unified diff and the regen
    instruction); an empty list means both match."""
    problems = []
    for name, fresh in (("report.json", json_text), ("report.md", md_text)):
        path = example_dir / name
        label = f"{example_id(example_dir)}/{name}"
        if not path.is_file():
            problems.append(
                f"{label}: committed report is missing; {_regen_instruction(example_dir)}"
            )
            continue
        raw = path.read_bytes()
        try:
            committed = raw.decode("utf-8")
        except UnicodeDecodeError as err:
            problems.append(
                f"{label}: committed report is not UTF-8 ({err}); "
                f"{_regen_instruction(example_dir)}"
            )
            continue
        if committed == fresh:
            continue
        diff = list(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                fresh.splitlines(keepends=True),
                fromfile=f"committed/{label}",
                tofile=f"regenerated/{label}",
            )
        )
        shown = "".join(diff[:DIFF_LINE_LIMIT])
        if len(diff) > DIFF_LINE_LIMIT:
            shown += f"\n... {len(diff) - DIFF_LINE_LIMIT} more lines\n"
        problems.append(
            f"{label} differs from what the engine produces now "
            f"({len(raw)} B committed, {len(fresh.encode('utf-8'))} B regenerated). "
            f"Never hand-edit a report: {_regen_instruction(example_dir)}.\n{shown}"
        )
    return problems


def is_canonical_python(version: tuple[int, int] = sys.version_info[:2]) -> bool:
    return tuple(version) == CANONICAL_PYTHON


def _near_diffs(committed, fresh, path: str, out: list[str]) -> None:
    if type(committed) is not type(fresh):
        out.append(f"{path}: type {type(committed).__name__} -> {type(fresh).__name__}")
    elif isinstance(committed, dict):
        if set(committed) != set(fresh):
            out.append(
                f"{path}: keys differ (only committed {sorted(set(committed) - set(fresh))}, "
                f"only regenerated {sorted(set(fresh) - set(committed))})"
            )
        for key in sorted(set(committed) & set(fresh)):
            _near_diffs(committed[key], fresh[key], f"{path}.{key}", out)
    elif isinstance(committed, list):
        if len(committed) != len(fresh):
            out.append(f"{path}: length {len(committed)} -> {len(fresh)}")
        for index, (a, b) in enumerate(zip(committed, fresh)):
            _near_diffs(a, b, f"{path}[{index}]", out)
    elif isinstance(committed, float):
        if not math.isclose(committed, fresh, rel_tol=CROSS_VERSION_REL_TOL, abs_tol=0.0):
            out.append(f"{path}: {committed!r} -> {fresh!r}")
    elif committed != fresh:
        out.append(f"{path}: {committed!r} -> {fresh!r}")


def compare_reports_near(example_dir: Path, json_text: str, md_text: str) -> list[str]:
    """The comparison for a NON-canonical interpreter (see CANONICAL_PYTHON):
    report.md byte for byte, report.json value by value with floats equal to
    CROSS_VERSION_REL_TOL and the full-report hash/size (which pin exact bytes)
    left out. Loud on any structural change or any float that moved more than
    last-place drift."""
    problems = compare_reports(example_dir, json_text, md_text)
    problems = [p for p in problems if not p.startswith(f"{example_id(example_dir)}/report.json")]
    label = f"{example_id(example_dir)}/report.json"
    path = example_dir / "report.json"
    if not path.is_file():
        return problems + [f"{label}: committed report is missing; {_regen_instruction(example_dir)}"]
    try:
        committed = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as err:
        return problems + [f"{label}: committed report is not JSON ({err}); "
                           f"{_regen_instruction(example_dir)}"]
    fresh = json.loads(json_text)
    for side, doc in (("committed", committed), ("regenerated", fresh)):
        if not (
            isinstance(doc, dict)
            and "projection" in doc
            and isinstance(doc["projection"], dict)
            and all(key in doc["projection"] for key in _CROSS_VERSION_UNPINNED)
        ):
            return problems + [
                f"{label}: {side} report has no projection block carrying "
                f"{list(_CROSS_VERSION_UNPINNED)}; {_regen_instruction(example_dir)}"
            ]
        for key in _CROSS_VERSION_UNPINNED:
            del doc["projection"][key]
    diffs: list[str] = []
    _near_diffs(committed, fresh, "report", diffs)
    if diffs:
        shown = "\n".join(diffs[:DIFF_LINE_LIMIT])
        if len(diffs) > DIFF_LINE_LIMIT:
            shown += f"\n... {len(diffs) - DIFF_LINE_LIMIT} more differences"
        problems.append(
            f"{label} differs beyond cross-version float drift (this interpreter is "
            f"Python {sys.version_info[0]}.{sys.version_info[1]}, reports are canonical "
            f"under {CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]}). Never hand-edit a "
            f"report: {_regen_instruction(example_dir)}, under Python "
            f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]}.\n{shown}"
        )
    return problems


# --------------------------------------------------------------- static checks
def _git_ignored(path: Path) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", path.name],
        cwd=path.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    raise ExamplesError(
        f"git check-ignore failed on {path} (exit {proc.returncode}): {proc.stderr.strip()}"
    )


def check_files(example_dir: Path) -> list[str]:
    """Exactly the five required files, each under its cap, none gitignored."""
    problems = []
    label = example_id(example_dir)
    present = {entry.name: entry for entry in example_dir.iterdir()}
    missing = [name for name in REQUIRED_FILES if name not in present]
    extra = sorted(name for name in present if name not in REQUIRED_FILES)
    if missing:
        problems.append(f"{label}: missing required file(s) {missing}")
    if extra:
        problems.append(
            f"{label}: unexpected entries {extra}; an example holds exactly "
            f"{list(REQUIRED_FILES)}"
        )
    for name in REQUIRED_FILES:
        if name not in present:
            continue
        path = present[name]
        if not path.is_file():
            problems.append(f"{label}/{name} is not a regular file")
            continue
        size = path.stat().st_size
        if size > SIZE_CAPS[name]:
            problems.append(f"{label}/{name} is {size} B, over its {SIZE_CAPS[name]} B cap")
        if _git_ignored(path):
            problems.append(
                f"{label}/{name} is gitignored, so it would never be committed; add "
                f"the matching `!examples/*/*/{name}` negation to .gitignore"
            )
    return problems


def _load_json(path: Path) -> tuple[object, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as err:
        return None, f"{path.parent.name}/{path.name} is not readable JSON: {err}"


def check_input(example_dir: Path) -> tuple[dict | None, list[str]]:
    """input.json must pass the ONE loading boundary. Returns (document, problems)."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import input_contract
    from contract_errors import ContractAdaptationError, ContractValidationError

    label = example_id(example_dir)
    path = example_dir / "input.json"
    if not path.is_file():
        return None, [f"{label}: no input.json to validate"]
    doc, err = _load_json(path)
    if err is not None:
        return None, [f"{label}: {err}"]
    if not isinstance(doc, dict):
        return None, [f"{label}/input.json is not a JSON object"]
    try:
        input_contract.load_and_map(str(path))
    except (ContractValidationError, ContractAdaptationError) as refusal:
        return doc, [
            f"{label}/input.json is refused by input_contract.load_and_map "
            f"({type(refusal).__name__}): {refusal}"
        ]
    return doc, []


def check_meta(example_dir: Path, meta: object) -> list[str]:
    """meta.json schema: exact key set, typed values, verdict grammar."""
    label = f"{example_id(example_dir)}/meta.json"
    if not isinstance(meta, dict):
        return [f"{label} is not a JSON object"]
    problems = []
    keys = set(meta)
    if keys != META_KEYS:
        problems.append(
            f"{label}: keys must be exactly {sorted(META_KEYS)}; unknown "
            f"{sorted(keys - META_KEYS)}, missing {sorted(META_KEYS - keys)}"
        )

    def _nonempty_str(key: str) -> bool:
        return isinstance(meta[key], str) and meta[key].strip() != ""

    if "source" in meta and meta["source"] != example_dir.parent.name:
        problems.append(
            f"{label}: source {meta['source']!r} must equal the directory name "
            f"{example_dir.parent.name!r}"
        )
    if "source_url" in meta and not (
        isinstance(meta["source_url"], str) and meta["source_url"].startswith("https://")
    ):
        problems.append(f"{label}: source_url must be a https:// URL")
    for key in ("publication_id", "title"):
        if key in meta and not _nonempty_str(key):
            problems.append(f"{label}: {key} must be a non-empty string")
    if "authors" in meta and not (
        isinstance(meta["authors"], list)
        and meta["authors"]
        and all(isinstance(a, str) and a.strip() for a in meta["authors"])
    ):
        problems.append(f"{label}: authors must be a non-empty list of non-empty strings")
    if "year" in meta and not (
        isinstance(meta["year"], int)
        and not isinstance(meta["year"], bool)
        and 1900 <= meta["year"] <= 2100
    ):
        problems.append(f"{label}: year must be an integer in 1900..2100")
    if "retrieval_date" in meta:
        if not isinstance(meta["retrieval_date"], str):
            problems.append(f"{label}: retrieval_date must be an ISO date string")
        else:
            try:
                date.fromisoformat(meta["retrieval_date"])
            except ValueError:
                problems.append(
                    f"{label}: retrieval_date {meta['retrieval_date']!r} is not an ISO date"
                )
    issues_ok = "linked_issues" in meta and (
        isinstance(meta["linked_issues"], list)
        and all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 1
            for n in meta["linked_issues"]
        )
        and len(set(meta["linked_issues"])) == len(meta["linked_issues"])
    )
    if "linked_issues" in meta and not issues_ok:
        problems.append(
            f"{label}: linked_issues must be a list of distinct integers >= 1"
        )
    if "verdict" in meta:
        verdict = meta["verdict"]
        match = VERDICT_RE.match(verdict) if isinstance(verdict, str) else None
        if match is None:
            problems.append(
                f"{label}: verdict {verdict!r} is not one of AGREES | DIFFERS "
                f"(explained) | DIFFERS (engine issue #N) (N >= 1)"
            )
        elif match.group(2) is not None and issues_ok:
            number = int(match.group(2))
            if number not in meta["linked_issues"]:
                problems.append(
                    f"{label}: verdict names engine issue #{number} but "
                    f"linked_issues {meta['linked_issues']} does not list it"
                )
    return problems


_HEADING_RE = re.compile(r"^## (.+?)\s*$")


def parse_readme_sections(text: str) -> list[tuple[str, str]]:
    """Split a README on ``## `` headings: [(heading, body), ...] in order.
    Text before the first ``## `` heading is not a section."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            sections.append((match.group(1), []))
        elif sections:
            sections[-1][1].append(line)
    return [(heading, "\n".join(body)) for heading, body in sections]


def check_readme(
    example_dir: Path, meta: dict | None, input_doc: dict | None
) -> list[str]:
    """Required sections present exactly once and non-blank; Source cites the
    URL; Encoding names every top-level input key; Verdict agrees with meta."""
    label = f"{example_id(example_dir)}/README.md"
    path = example_dir / "README.md"
    if not path.is_file():
        return [f"{label} is missing"]
    sections = parse_readme_sections(path.read_text(encoding="utf-8"))
    problems = []
    bodies: dict[str, str] = {}
    for required in REQUIRED_README_SECTIONS:
        matches = [body for heading, body in sections if heading == required]
        if len(matches) != 1:
            problems.append(
                f"{label}: section '## {required}' must appear exactly once "
                f"(found {len(matches)})"
            )
            continue
        if not matches[0].strip():
            problems.append(f"{label}: section '## {required}' is empty")
            continue
        bodies[required] = matches[0]

    first = None
    if "Verdict" in bodies:
        lines = [ln.strip() for ln in bodies["Verdict"].splitlines() if ln.strip()]
        first = lines[0]
        if not VERDICT_RE.match(first):
            problems.append(
                f"{label}: the first line of '## Verdict' is {first!r}; it must be "
                f"exactly AGREES | DIFFERS (explained) | DIFFERS (engine issue #N)"
            )
        if first == "DIFFERS (explained)" and len(lines) < 2:
            problems.append(
                f"{label}: a 'DIFFERS (explained)' verdict needs the explanation "
                f"on the lines after it"
            )

    if meta is None:
        problems.append(
            f"{label}: Source URL and verdict were not cross-checked against "
            f"meta.json because meta.json is missing or invalid (see above)"
        )
    else:
        if "Source" in bodies and meta["source_url"] not in bodies["Source"]:
            problems.append(
                f"{label}: '## Source' must contain meta.json source_url "
                f"{meta['source_url']!r} literally"
            )
        if first is not None and first != meta["verdict"]:
            problems.append(
                f"{label}: README verdict {first!r} does not equal meta.json "
                f"verdict {meta['verdict']!r}"
            )

    if input_doc is None:
        problems.append(
            f"{label}: '## Encoding' was not checked against input.json because "
            f"input.json could not be read"
        )
    elif "Encoding" in bodies:
        unmentioned = sorted(k for k in input_doc if f"`{k}`" not in bodies["Encoding"])
        if unmentioned:
            problems.append(
                f"{label}: '## Encoding' does not mention top-level input key(s) "
                f"{unmentioned} (write each as `key`)"
            )
    return problems


def scan_personal_data(name: str, text: str) -> list[str]:
    """Home paths and e-mail addresses anywhere; SIN-shaped numbers only in the
    human-authored files (see SIN_SCANNED_FILES)."""
    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if HOME_PATH_RE.search(line):
            problems.append(f"{name}:{lineno}: absolute home path (DP#15)")
        if EMAIL_RE.search(line):
            problems.append(f"{name}:{lineno}: e-mail address (DP#15)")
        if name in SIN_SCANNED_FILES and SIN_RE.search(line):
            problems.append(f"{name}:{lineno}: SIN-shaped number (DP#15)")
    return problems


def check_no_personal_paths(example_dir: Path) -> list[str]:
    problems = []
    for path in sorted(example_dir.iterdir()):
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace")
        problems.extend(
            f"{example_id(example_dir)}/{p}" for p in scan_personal_data(path.name, text)
        )
    return problems


def static_problems(example_dir: Path) -> list[str]:
    """Every static contract problem of one example (empty list = clean)."""
    problems = check_files(example_dir)
    input_doc, input_problems = check_input(example_dir)
    problems += input_problems
    meta = None
    meta_path = example_dir / "meta.json"
    if meta_path.is_file():
        loaded, err = _load_json(meta_path)
        if err is not None:
            problems.append(err)
        else:
            meta_problems = check_meta(example_dir, loaded)
            problems += meta_problems
            meta = loaded if not meta_problems else None
    problems += check_readme(example_dir, meta, input_doc)
    problems += check_no_personal_paths(example_dir)
    return problems


# --------------------------------------------------------------- CLI
def _resolve_targets(paths: list[str], root: Path) -> list[Path]:
    known = discover_examples(root)
    if not paths:
        return known
    by_resolved = {p.resolve(): p for p in known}
    targets = []
    for raw in paths:
        resolved = Path(raw).resolve()
        if resolved not in by_resolved:
            raise ExamplesError(
                f"{raw} is not an example directory under {root} "
                f"(expected examples/<source>/<slug>)"
            )
        targets.append(by_resolved[resolved])
    return targets


def main(
    argv: list[str] | None = None,
    *,
    root: Path = EXAMPLES_ROOT,
    optimize_py: Path = OPTIMIZE_PY,
    python_version: tuple[int, int] = sys.version_info[:2],
) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/examples.py",
        description="Regenerate examples/<source>/<slug>/report.{json,md} "
        "through the real optimize.py (issue #300).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    regen = sub.add_parser("regen", help="rewrite report.json and report.md")
    regen.add_argument("paths", nargs="*", help="example directories (default: all)")
    regen.add_argument(
        "--workers",
        type=int,
        default=None,
        help="passed to optimize.py --workers (default: optimize.py's own)",
    )
    args = parser.parse_args(argv)

    if not is_canonical_python(python_version):
        print(
            f"error: tools/examples.py regen must run under Python "
            f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]} (this is "
            f"{python_version[0]}.{python_version[1]}): report floats differ in the "
            f"last place across minor versions, and CI byte-compares under "
            f"{CANONICAL_PYTHON[0]}.{CANONICAL_PYTHON[1]}. See CANONICAL_PYTHON.",
            file=sys.stderr,
        )
        return 1
    try:
        targets = _resolve_targets(args.paths, root)
        for example_dir in targets:
            json_text, md_text = regenerate(
                example_dir, workers=args.workers, optimize_py=optimize_py
            )
            write_reports(example_dir, json_text, md_text)
            digest = json.loads(json_text)["projection"]["full_report_sha256"]
            print(
                f"regenerated {example_id(example_dir)}: report.json "
                f"{len(json_text.encode('utf-8'))} B, report.md "
                f"{len(md_text.encode('utf-8'))} B, full report sha256 {digest[:12]}"
            )
    except ExamplesError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
