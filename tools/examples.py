#!/usr/bin/env python3
"""Runnable, research-backed examples: discovery, regeneration, projection, checks.

Issue #300. Each example lives in ``examples/<source>/<slug>/`` and holds
exactly five files::

    input.json   a contract document (validates + maps through input_contract)
    report.json  a compact projection of the engine's output (this module)
    report.md    optimize mode: the engine's own ``optimize.py --md`` output
                 (trimmed only if large); simulate mode: a deterministic render
                 of report.json (``render_simulation_markdown``)
    README.md    source, publication claim, encoding, comparison, verdict
    meta.json    source URL, publication id, retrieval date, verdict, linked
                 issues, and the example's ``mode``

Modes (issue #319)
------------------
``meta.json`` must declare ``"mode"``: ``"simulate"`` or ``"optimize"``. There
is no default (DP#32): a missing or unknown mode fails the static contract,
and ``read_mode`` / ``regenerate`` raise before any engine runs.

* ``optimize``: the #300 behaviour, unchanged. ``regen`` runs the REAL
  ``optimize.py --json/--md`` in a subprocess and projects it with
  ``project_report``. For examples whose claim is a strategy ranking (~30-70 s).
* ``simulate``: ``regen`` runs this module's own ``simulate-once`` subcommand
  in a subprocess, which drives exactly ONE real engine fold,
  ``input_contract.load_and_map -> SimulationConfig.from_dict ->
  FamilySimulation(config, adapter=CanadaAdapter(config)).run()``, with no
  strategy, rate-path or readvance override (the engine's DP#16 auto-detection
  decides, and the report's ``run`` block records what it chose). The result
  is projected by ``project_simulation`` (see below). For examples whose claim
  is one household, one computed outcome (~1 s).

  A single run cannot honour a declared sweep: the contract maps
  ``candidate_ages[0]`` and ignores every other candidate, and FamilySimulation
  never reads ``decisions.contribution_strategy`` / ``income`` / mortgage
  options. So ``simulate_input_problems`` refuses (in the static check AND
  before regen spawns anything) any input that declares more than one point,
  per the classification table ``SIMULATE_DECISIONS`` (DP#33; the "parsed,
  mapped, then never passed" trap).

``python tools/examples.py regen [<path>...]`` rewrites report.json / report.md.
This module never simulates, ranks or scores in the guard's or regen's own
process (DP#11): the engine runs in a child process (``optimize.py`` or
``simulate-once``), and this module only projects and renders what the child
wrote. The CI guard ``tests/test_examples_guard.py`` calls the very same
``regenerate()`` and compares the result byte for byte against what is
committed, so a report cannot be hand-edited and cannot drift from what the
engine now produces.

The optimize projection contract (``project_report``, PROJECTION_VERSION = 1)
-----------------------------------------------------------------------------
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
* ``projection``: the version, this function's name, and the byte length and
  typed digest (``full_report_digest`` = ``"sha256:<64 hex>"``) of the FULL
  report. Because the digest is committed, byte-identity of report.json also
  pins every byte of the unprojected 13 MB output. The digest is TYPED, never
  a bare hex string: CI's secret scan (detect-secrets HexHighEntropyString)
  flags any quoted string made only of hex digits, and the digest changes on
  every legitimate regen, so a bare hash would need a fresh .secrets.baseline
  entry in every engine PR that moves an example. ``dump_report_json`` refuses
  any bare hex string of BARE_HEX_MIN_LEN or more characters anywhere in the
  report, so that shape fails locally at regen time, not in CI.

Any change to the rules above bumps PROJECTION_VERSION and regenerates every
example in the same PR.

``project_markdown`` keeps the engine's ``--md`` output verbatim when it is at
most REPORT_MD_CAP bytes; otherwise it cuts at the last ``## `` section
boundary that fits and appends a visible marker naming how much was kept.

Reports are canonical under ONE Python minor version (CANONICAL_PYTHON, the
version CI's PR leg runs): report.json floats differ by one unit in the last
place under 3.10/3.11 (measured; see the constant's comment). ``regen``
refuses to write under another version (``simulate-once`` does not: the
nightly guard legs spawn it); the guard byte-compares under the canonical
version and uses ``compare_reports_near`` on the nightly legs, in both modes.

The simulate projection contract (``project_simulation``, SIMULATE_PROJECTION_VERSION = 1)
--------------------------------------------------------------------------------------
``simulate-once`` writes one canonical JSON document (sorted keys, no
whitespace, NaN refused) with exactly the top-level keys ``engine_entry`` (the
chain above, as text), ``run`` (read from the engine objects: the strategy and
rate-path names FamilySimulation chose, ``use_readvanceable``,
``deduct_later``, ``start_year``, ``projection_years`` and each adult's mapped
``retirement_age``) and ``year_by_year`` (``dataclasses.asdict`` of every
``YearResult``). ``project_simulation`` reduces it, deterministically:

* The top-level key set must equal ``SIM_TOP_VERBATIM | SIM_TOP_PROJECTED``
  exactly; an unknown or missing key raises ExamplesError (classify it and
  bump SIMULATE_PROJECTION_VERSION). ``engine_entry`` and ``run`` are copied
  verbatim.
* ``series``: every row, in the ENGINE's order, restricted to
  ``SIMULATE_SERIES_COLUMNS`` by strict key (a missing column raises).
  ``year`` is the engine's 1-indexed offset from ``run.start_year``.
* ``terminal``: every scalar field of the LAST row, copied unrounded.
* ``years``: the row count; an empty series raises.
* ``projection``: version, function, source, and the byte length and typed
  digest (``full_report_digest`` = ``"sha256:<64 hex>"``) of the child's full
  document, so byte-identity of report.json pins every byte of it. The key
  names match the optimize projection's so ``compare_reports_near`` serves
  both modes.

report.md is ``render_simulation_markdown(report)``: a pure function of the
projected report (floats to 2 decimals). It is byte-compared on every leg, so a
last-place float drift on a nightly 3.10/3.11 leg that crosses a rounding
boundary would fail that leg loudly; that is accepted, never tolerated away.

Loud failure (DP#32): discovery raises on a missing or empty ``examples/``
tree; ``run_optimize`` and ``run_simulation`` refuse a non-zero exit AND an
exit 0 that wrote no output (``optimize.main()`` prints "Error: ..." and
returns 0 on an objective selection error); static checks return every
problem, never a silent pass.
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
EXAMPLES_PY = Path(__file__).resolve()

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
    "mode",
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

# Issue #319: every example declares how it is regenerated. No default
# (DP#32, DP#9): a missing or unknown mode is refused, never guessed.
EXAMPLE_MODES = ("simulate", "optimize")

# ------------------------------------------------ simulate mode: input contract
# Every property of schema/defs/decisions.json $defs.decisions, classified for
# a single FamilySimulation.run(). tests/test_examples_tool.py fails when the
# schema grows a property this table does not classify, so a new decisions
# key can never slip through a single run unexamined.
#
#   consumed          the single run reads it (proved by
#                     test_simulate_consumed_decisions_reach_engine)
#   single_candidate  a list of {person, candidate_ages}; each entry must hold
#                     exactly ONE age, because contract_people maps
#                     candidate_ages[0] and silently drops the rest
#   empty_list        a sweep the single run never reads: must be [] (or
#                     absent where the schema allows)
#   empty_options     an object whose option lists (MORTGAGE_OPTION_KEYS) must
#                     all be []
#   absent            not consumed by a single run: must not be declared
#
# "Never reads" is measured, not assumed (#319): on the single-run example,
# declaring ONE candidate of the seed's contribution_strategy, income,
# resp_action, estate_elections, or of each mortgage option list left every
# field of every YearResult identical, and
# test_simulate_empty_list_decisions_are_not_read_by_single_run keeps that
# measurement live. deposit_products and borrow_to_invest are read only by the
# optimizer layer (scenario_discovery / explore), per simulation_config.py.
SIMULATE_DECISIONS = {
    "horizon": "consumed",
    "retirement_age": "single_candidate",
    "contribution_strategy": "empty_list",
    "income": "empty_list",
    "resp_action": "empty_list",
    "estate_elections": "empty_list",
    "deposit_products": "empty_list",
    "borrow_to_invest": "empty_list",
    "mortgage": "empty_options",
    "objective": "absent",
    "superficial_loss": "absent",
}
# Every property of schema/defs/decisions.json $defs.mortgage_decisions (a
# test enforces the equality).
MORTGAGE_OPTION_KEYS = ("refinance_options", "renewal_options", "structure_options")
# Root-required ``sensitivity`` is an optimizer sweep (sweep.py) and overlay
# presets (--overlay); a single run reads neither. Measured for #319 on the
# single-run example: the seed's full presets+sweeps vs {} gave the identical
# terminal total_assets (7149906.735427627 both). Both must be {} so a
# document cannot look like it sweeps something it does not.
SIMULATE_SENSITIVITY_KEYS = ("presets", "sweeps")
# Sweeps declared OUTSIDE decisions: a property purchase's funding_options
# (#1011, schema/defs/properties.json) is ranked by the optimizer.
SIMULATE_REFUSED_ANYWHERE = ("funding_options",)
_SWEEP_HINT = (
    "simulate mode runs one FamilySimulation.run(); this declares a sweep the "
    "single run would silently collapse; use mode optimize or declare one point"
)

SIMULATE_PROJECTION_VERSION = 1
SIMULATE_PROJECTION_FUNCTION = "tools/examples.py::project_simulation"
SIMULATE_PROJECTION_SOURCE = "FamilySimulation.run()"
SIMULATE_ENGINE_ENTRY = (
    "input_contract.load_and_map -> SimulationConfig.from_dict -> "
    "FamilySimulation(config, adapter=CanadaAdapter(config)).run()"
)
SIM_TOP_VERBATIM = frozenset({"engine_entry", "run"})
SIM_TOP_PROJECTED = frozenset({"year_by_year"})
# KEY_YEAR_COLUMNS minus ``net_benefit``, plus the retirement-income columns.
# ``YearResult.net_benefit`` is a field default (0.0) that FamilySimulation.run()
# never writes: net benefit is an optimizer comparison against a baseline
# (optimize.py), not a per-year engine output. Measured for #319: it is 0.0 in
# every row of the single run AND of the optimize seed's winner rows. A series
# column that is always zero would invite a README to cite a number no
# computation produced, so it is left out here (``terminal`` still copies it
# verbatim, like every scalar of the last row).
SIMULATE_SERIES_COLUMNS = tuple(c for c in KEY_YEAR_COLUMNS if c != "net_benefit") + (
    "cpp_income",
    "oas_income",
    "gis_income",
    "lif_balance",
    "lira_balance",
)

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
_CROSS_VERSION_UNPINNED = ("full_report_bytes", "full_report_digest")
OUTPUT_TAIL_LINES = 40

# A JSON string made only of hex digits, this long or longer, is what CI's
# detect-secrets HexHighEntropyString plugin (limit 3.0, see .secrets.baseline)
# reports as a secret. Measured with detect-secrets 1.5.0: the bare 64-hex
# sha256 was flagged, the same digest written "sha256:<hex>" was not.
BARE_HEX_MIN_LEN = 16
BARE_HEX_RE = re.compile(r"[0-9a-fA-F]+")
DIGEST_PREFIX = "sha256:"


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


def _run_hermetic(
    cmd: list[str], workdir: Path, outputs: tuple[Path, ...], program: str
) -> None:
    """Run one engine child process hermetically and refuse any doubtful end.

    Hermetic: HOME is an empty directory inside ``workdir``, because
    tax_data.TaxDataProvider reads ~/.cache/lifedraft/tax/*.json for years it
    has no registered fallback for; a contributor's stale cache must not leak
    into a committed report. cwd is ``workdir``, so nothing lands next to the
    example.

    Refuses a non-zero exit AND an exit 0 that left any of ``outputs`` missing
    or empty (the CLI's silent-success path)."""
    home = workdir / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    proc = subprocess.run(
        cmd, cwd=workdir, capture_output=True, text=True, env=env, check=False
    )
    detail = (
        f"command: {' '.join(cmd)}\n--- stdout (tail) ---\n{_tail(proc.stdout)}\n"
        f"--- stderr (tail) ---\n{_tail(proc.stderr)}"
    )
    if proc.returncode != 0:
        raise ExamplesError(f"{program} exited {proc.returncode}\n{detail}")
    for out in outputs:
        if not out.is_file() or out.stat().st_size == 0:
            raise ExamplesError(
                f"{program} exited 0 but wrote no {out.name} (silent-success CLI "
                f"path; read its stdout)\n{detail}"
            )


def run_optimize(
    input_path: Path,
    workdir: Path,
    *,
    workers: int | None,
    optimize_py: Path = OPTIMIZE_PY,
) -> tuple[Path, Path]:
    """Run the real optimize.py on ``input_path`` and return (full.json, full.md),
    hermetically (see ``_run_hermetic``). ``--save-session`` is never passed."""
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
    _run_hermetic(cmd, workdir, (full_json, full_md), "optimize.py")
    return full_json, full_md


def run_simulation(
    input_path: Path, workdir: Path, *, examples_py: Path = EXAMPLES_PY
) -> Path:
    """Run ``examples_py simulate-once`` (one real FamilySimulation.run(), see
    ``simulate_once``) on ``input_path`` in a child process and return the
    path of the full document it wrote, hermetically (see ``_run_hermetic``).
    ``examples_py`` is injectable so a stub script can stand in for tests."""
    full_json = workdir / "full.json"
    cmd = [
        sys.executable,
        str(examples_py),
        "simulate-once",
        "--input",
        str(input_path.resolve()),
        "--out",
        str(full_json),
    ]
    _run_hermetic(cmd, workdir, (full_json,), "tools/examples.py simulate-once")
    return full_json


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
            "full_report_digest": DIGEST_PREFIX
            + hashlib.sha256(full_bytes).hexdigest(),
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


def bare_hex_strings(obj, path: str = "") -> list[str]:
    """JSON-pointer-ish paths of every string value (or key) in ``obj`` that is
    made only of hex digits and is at least BARE_HEX_MIN_LEN long: the shape
    CI's secret scan reports as a "Hex High Entropy String"."""
    found = []
    if isinstance(obj, str):
        if len(obj) >= BARE_HEX_MIN_LEN and BARE_HEX_RE.fullmatch(obj):
            found.append(path if path else "/")
    elif isinstance(obj, dict):
        for key, value in obj.items():
            found += bare_hex_strings(key, f"{path}/{key} (key)")
            found += bare_hex_strings(value, f"{path}/{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found += bare_hex_strings(value, f"{path}/{index}")
    return found


def _bare_hex_message(where: str, paths: list[str]) -> str:
    return (
        f"{where} holds bare hex string(s) at {paths}; CI's secret scan "
        f"(detect-secrets HexHighEntropyString) fails on that shape. Write a "
        f"digest typed, as {DIGEST_PREFIX!r} + hex, never as a bare hash, and "
        f"never add it to .secrets.baseline"
    )


def dump_report_json(obj: dict) -> str:
    """Canonical serialisation: sorted keys, 2-space indent, trailing newline.
    A NaN or infinity is refused, never written as a non-JSON token, and so is a
    bare hex string (see BARE_HEX_MIN_LEN)."""
    hex_paths = bare_hex_strings(obj)
    if hex_paths:
        raise ExamplesError(_bare_hex_message("report.json", hex_paths))
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


# --------------------------------------------------------------- mode
def read_mode(example_dir: Path) -> str:
    """The example's declared ``meta.json`` mode, one of EXAMPLE_MODES.

    Raises ExamplesError on a missing or unreadable meta.json, a non-object,
    an absent ``mode`` key, or any value outside EXAMPLE_MODES (case-sensitive).
    There is no default (DP#32): guessing a mode would run the wrong engine
    path and commit its output as the truth."""
    label = f"{example_id(example_dir)}/meta.json"
    path = example_dir / "meta.json"
    if not path.is_file():
        raise ExamplesError(
            f"{label} is missing; it must declare mode simulate | optimize "
            f"(there is no default, DP#32)"
        )
    meta, err = _load_json(path)
    if err is not None:
        raise ExamplesError(f"{example_id(example_dir)}: {err}")
    if not isinstance(meta, dict):
        raise ExamplesError(f"{label} is not a JSON object")
    if "mode" not in meta:
        raise ExamplesError(
            f"{label} has no 'mode'; declare simulate | optimize (there is no "
            f"default, DP#32)"
        )
    mode = meta["mode"]
    if not (isinstance(mode, str) and mode in EXAMPLE_MODES):
        raise ExamplesError(
            f"{label}: mode {mode!r} is not one of simulate | optimize (declare "
            f"it; there is no default, DP#32)"
        )
    return mode


def _refused_anywhere(obj, path: str, out: list[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in SIMULATE_REFUSED_ANYWHERE:
                out.append(f"{path}/{key}")
            _refused_anywhere(value, f"{path}/{key}", out)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _refused_anywhere(value, f"{path}/{index}", out)


def simulate_input_problems(doc: object) -> list[str]:
    """Why a single FamilySimulation.run() cannot honour ``doc`` (empty list =
    it can). One message per violation, and one per decisions / mortgage /
    sensitivity key that is not classified (see SIMULATE_DECISIONS)."""
    if not isinstance(doc, dict):
        return ["input.json is not a JSON object"]
    problems: list[str] = []
    if "decisions" not in doc or not isinstance(doc["decisions"], dict):
        problems.append("decisions must be an object (schema-required)")
    else:
        decisions = doc["decisions"]
        for key in sorted(decisions):
            value = decisions[key]
            where = f"decisions.{key}"
            if key not in SIMULATE_DECISIONS:
                problems.append(
                    f"{where} is not classified for simulate mode (add it to "
                    f"SIMULATE_DECISIONS in tools/examples.py); {_SWEEP_HINT}"
                )
                continue
            rule = SIMULATE_DECISIONS[key]
            if rule == "consumed":
                continue
            if rule == "absent":
                problems.append(
                    f"{where} is declared, but a single run does not consume it "
                    f"(it steers the optimizer); {_SWEEP_HINT}"
                )
            elif rule == "empty_list":
                if value != []:
                    problems.append(
                        f"{where} must be [] in simulate mode (FamilySimulation "
                        f"never reads it); {_SWEEP_HINT}"
                    )
            elif rule == "single_candidate":
                if not isinstance(value, list):
                    problems.append(f"{where} must be a list")
                    continue
                for index, entry in enumerate(value):
                    ages = entry["candidate_ages"] if (
                        isinstance(entry, dict) and "candidate_ages" in entry) else None
                    if not (isinstance(ages, list) and len(ages) == 1):
                        problems.append(
                            f"{where}[{index}].candidate_ages must hold exactly one "
                            f"age in simulate mode (got {ages!r}; the contract maps "
                            f"candidate_ages[0] and drops the rest); {_SWEEP_HINT}"
                        )
            elif rule == "empty_options":
                if not isinstance(value, dict):
                    problems.append(f"{where} must be an object")
                    continue
                for sub in sorted(value):
                    if sub not in MORTGAGE_OPTION_KEYS:
                        problems.append(
                            f"{where}.{sub} is not classified for simulate mode "
                            f"(add it to MORTGAGE_OPTION_KEYS); {_SWEEP_HINT}"
                        )
                    elif value[sub] != []:
                        problems.append(
                            f"{where}.{sub} must be [] in simulate mode; {_SWEEP_HINT}"
                        )
            else:
                raise ExamplesError(f"SIMULATE_DECISIONS[{key!r}] has unknown rule {rule!r}")
    if "sensitivity" not in doc or not isinstance(doc["sensitivity"], dict):
        problems.append("sensitivity must be an object (schema-required)")
    else:
        sensitivity = doc["sensitivity"]
        for key in sorted(sensitivity):
            if key not in SIMULATE_SENSITIVITY_KEYS:
                problems.append(
                    f"sensitivity.{key} is not classified for simulate mode; "
                    f"{_SWEEP_HINT}"
                )
            elif sensitivity[key] != {}:
                problems.append(
                    f"sensitivity.{key} must be {{}} in simulate mode (a single run "
                    f"reads no sweep or overlay preset); {_SWEEP_HINT}"
                )
    refused: list[str] = []
    _refused_anywhere({k: v for k, v in doc.items() if k != "decisions"}, "", refused)
    for where in refused:
        problems.append(
            f"{where} is an optimizer-ranked alternative; {_SWEEP_HINT}"
        )
    return problems


# --------------------------------------------------------------- simulate child
def _canonical_json_bytes(obj: dict) -> bytes:
    try:
        text = json.dumps(
            obj, sort_keys=True, indent=None, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except ValueError as err:
        raise ExamplesError(f"the single run produced a non-finite float: {err}")
    return text.encode("utf-8")


def simulate_once(input_path: Path) -> bytes:
    """Drive exactly ONE real engine fold on ``input_path`` and return the
    canonical JSON bytes of ``{engine_entry, run, year_by_year}`` (see the
    module docstring). Runs in the ``simulate-once`` child process; the parent
    (``run_simulation``) isolates HOME. Engine exceptions propagate."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    import dataclasses

    import input_contract
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation, SimulationConfig

    doc, err = _load_json(input_path)
    if err is not None:
        raise ExamplesError(err)
    problems = simulate_input_problems(doc)
    if problems:
        raise ExamplesError(
            f"{input_path.name} cannot be run in simulate mode:\n- "
            + "\n- ".join(problems)
        )
    cfg = input_contract.load_and_map(str(input_path))
    config = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(config, adapter=CanadaAdapter(config))
    results = sim.run()
    run = {
        "strategy": sim.strategy.name,
        "rate_path": sim.rate_path.name,
        "use_readvanceable": sim.use_readvanceable,
        "deduct_later": sim.deduct_later,
        "start_year": config.start_year,
        "projection_years": config.projection_years,
        "retirement_ages": [
            {"id": m["id"], "role": m["role"], "retirement_age": m["retirement_age"]}
            for m in config.adults()
        ],
    }
    return _canonical_json_bytes({
        "engine_entry": SIMULATE_ENGINE_ENTRY,
        "run": run,
        "year_by_year": [dataclasses.asdict(r) for r in results],
    })


# --------------------------------------------------------------- simulate projection
def project_simulation(full: dict, *, full_bytes: bytes) -> dict:
    """Project the ``simulate-once`` document (see the module docstring for the
    contract). Pure; raises ExamplesError on any shape it was not written for."""
    if not isinstance(full, dict):
        raise ExamplesError("simulate-once output is not a JSON object")
    expected = SIM_TOP_VERBATIM | SIM_TOP_PROJECTED
    actual = set(full)
    if actual != expected:
        raise ExamplesError(
            f"simulate-once output top-level keys changed: unknown "
            f"{sorted(actual - expected)}, missing {sorted(expected - actual)}; "
            f"classify it in project_simulation and bump SIMULATE_PROJECTION_VERSION"
        )
    rows = full["year_by_year"]
    if not isinstance(rows, list) or not rows:
        raise ExamplesError("simulate-once output has an empty 'year_by_year' series")
    series = []
    for index, row in enumerate(rows):
        projected = {}
        for column in SIMULATE_SERIES_COLUMNS:
            if column not in row:
                raise ExamplesError(
                    f"simulate year_by_year row {index} has no column {column!r}; "
                    f"update SIMULATE_SERIES_COLUMNS and bump SIMULATE_PROJECTION_VERSION"
                )
            projected[column] = row[column]
        series.append(projected)
    report = {
        "projection": {
            "version": SIMULATE_PROJECTION_VERSION,
            "function": SIMULATE_PROJECTION_FUNCTION,
            "source": SIMULATE_PROJECTION_SOURCE,
            "full_report_bytes": len(full_bytes),
            "full_report_digest": DIGEST_PREFIX + hashlib.sha256(full_bytes).hexdigest(),
            "series_columns": list(SIMULATE_SERIES_COLUMNS),
        },
        "mode": "simulate",
        "years": len(rows),
        "series": series,
        "terminal": _scalars(rows[-1]),
    }
    for key in sorted(SIM_TOP_VERBATIM):
        report[key] = full[key]
    return report


def _md_cell(value) -> str:
    if isinstance(value, bool) or value is None:
        text = json.dumps(value)
    elif isinstance(value, float):
        text = f"{value:,.2f}"
    elif isinstance(value, (int, str)):
        text = str(value)
    else:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return text.replace("|", "\\|")


def render_simulation_markdown(report: dict) -> str:
    """report.md for simulate mode: a pure, deterministic render of the
    PROJECTED report (no timestamp, no path). Floats print to 2 decimals;
    report.json keeps them unrounded."""
    columns = report["projection"]["series_columns"]
    lines = [
        "# Single-run simulation report",
        "",
        f"Rendered by `tools/examples.py::render_simulation_markdown` from "
        f"report.json (simulate projection v{report['projection']['version']}). "
        f"Engine: `{report['engine_entry']}`. Floats are rounded to cents here "
        f"and unrounded in report.json.",
        "",
        "## Run",
        "",
        "| field | value |",
        "|---|---|",
    ]
    run = report["run"]
    for key in sorted(run):
        lines.append(f"| {_md_cell(key)} | {_md_cell(run[key])} |")
    lines += [
        "",
        "## Terminal year",
        "",
        f"Year {_md_cell(report['terminal']['year'])} of {report['years']} "
        f"(`year` is a 1-indexed offset from run.start_year).",
        "",
        "| column | value |",
        "|---|---|",
    ]
    for column in columns:
        lines.append(f"| {column} | {_md_cell(report['terminal'][column])} |")
    lines += [
        "",
        "## Year by year",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
    ]
    for row in report["series"]:
        lines.append("| " + " | ".join(_md_cell(row[c]) for c in columns) + " |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- regenerate
def regenerate(
    example_dir: Path,
    *,
    workers: int | None,
    optimize_py: Path = OPTIMIZE_PY,
    examples_py: Path = EXAMPLES_PY,
) -> tuple[str, str]:
    """Run the engine on ``example_dir/input.json`` in the example's declared
    mode and return the (report.json, report.md) texts it projects to. Writes
    nothing.

    The mode is read FIRST (``read_mode`` raises on a missing or unknown one),
    so no engine runs for an example that has not said how it is run.
    ``workers`` applies to optimize mode only: the single run is serial by
    construction."""
    mode = read_mode(example_dir)
    input_path = example_dir / "input.json"
    if not input_path.is_file():
        raise ExamplesError(f"{example_dir} has no input.json to regenerate from")
    if mode == "optimize":
        with tempfile.TemporaryDirectory(prefix="lifedraft-example-") as tmp:
            full_json, full_md = run_optimize(
                input_path, Path(tmp), workers=workers, optimize_py=optimize_py
            )
            full_bytes = full_json.read_bytes()
            md_text = full_md.read_text(encoding="utf-8")
        full = json.loads(full_bytes)
        json_text = dump_report_json(project_report(full, full_bytes=full_bytes))
        return json_text, project_markdown(md_text)
    if mode == "simulate":
        doc, err = _load_json(input_path)
        if err is not None:
            raise ExamplesError(f"{example_id(example_dir)}: {err}")
        problems = simulate_input_problems(doc)
        if problems:
            raise ExamplesError(
                f"{example_id(example_dir)}/input.json cannot be run in simulate "
                f"mode:\n- " + "\n- ".join(problems)
            )
        with tempfile.TemporaryDirectory(prefix="lifedraft-example-") as tmp:
            full_json = run_simulation(input_path, Path(tmp), examples_py=examples_py)
            full_bytes = full_json.read_bytes()
        report = project_simulation(json.loads(full_bytes), full_bytes=full_bytes)
        return dump_report_json(report), project_markdown(render_simulation_markdown(report))
    raise ExamplesError(f"read_mode returned unknown mode {mode!r}")


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
    if "mode" not in meta:
        problems.append(
            f"{label}: 'mode' is required: declare simulate | optimize (there is "
            f"no default, DP#32)"
        )
    elif not (isinstance(meta["mode"], str) and meta["mode"] in EXAMPLE_MODES):
        problems.append(
            f"{label}: mode {meta['mode']!r} is not one of simulate | optimize "
            f"(declare it; there is no default, DP#32)"
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


def check_no_bare_hex(example_dir: Path) -> list[str]:
    """No JSON file of the example may carry a bare hex string: CI's secret
    scan would fail the PR on it (see BARE_HEX_MIN_LEN)."""
    problems = []
    for name in ("input.json", "report.json", "meta.json"):
        path = example_dir / name
        if not path.is_file():
            continue
        doc, err = _load_json(path)
        if err is not None:
            if name == "report.json":
                # input.json and meta.json parse errors are already reported
                # by check_input / static_problems; report.json has no other
                # static reader.
                problems.append(err)
            continue
        hex_paths = bare_hex_strings(doc)
        if hex_paths:
            problems.append(
                _bare_hex_message(f"{example_id(example_dir)}/{name}", hex_paths)
            )
    return problems


def static_problems(example_dir: Path) -> list[str]:
    """Every static contract problem of one example (empty list = clean),
    including, for a ``simulate``-mode example, every sweep a single run would
    collapse (``simulate_input_problems``)."""
    problems = check_files(example_dir)
    input_doc, input_problems = check_input(example_dir)
    problems += input_problems
    meta = None
    loaded = None
    meta_path = example_dir / "meta.json"
    if meta_path.is_file():
        loaded, err = _load_json(meta_path)
        if err is not None:
            problems.append(err)
        else:
            meta_problems = check_meta(example_dir, loaded)
            problems += meta_problems
            meta = loaded if not meta_problems else None
    if (
        input_doc is not None
        and isinstance(loaded, dict)
        and "mode" in loaded
        and loaded["mode"] == "simulate"
    ):
        problems += [
            f"{example_id(example_dir)}/input.json: {p}"
            for p in simulate_input_problems(input_doc)
        ]
    problems += check_readme(example_dir, meta, input_doc)
    problems += check_no_personal_paths(example_dir)
    problems += check_no_bare_hex(example_dir)
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


def _simulate_once_cli(input_path: Path, out_path: Path) -> int:
    try:
        data = simulate_once(input_path)
    except ExamplesError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    out_path.write_bytes(data)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    root: Path = EXAMPLES_ROOT,
    optimize_py: Path = OPTIMIZE_PY,
    examples_py: Path = EXAMPLES_PY,
    python_version: tuple[int, int] = sys.version_info[:2],
) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/examples.py",
        description="Regenerate examples/<source>/<slug>/report.{json,md} "
        "through the real engine, in each example's declared mode (issues "
        "#300, #319).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    regen = sub.add_parser("regen", help="rewrite report.json and report.md")
    regen.add_argument("paths", nargs="*", help="example directories (default: all)")
    regen.add_argument(
        "--workers",
        type=int,
        default=None,
        help="optimize mode only: passed to optimize.py --workers "
        "(default: optimize.py's own)",
    )
    once = sub.add_parser(
        "simulate-once",
        help="run ONE FamilySimulation.run() on a contract document and write "
        "the full canonical JSON (the simulate-mode child; regen spawns it)",
    )
    once.add_argument("--input", required=True, type=Path)
    once.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.command == "simulate-once":
        # Not version-gated: the nightly 3.10/3.11 guard legs spawn this child
        # and near-compare its projection; only `regen` WRITES reports.
        return _simulate_once_cli(args.input, args.out)

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
                example_dir,
                workers=args.workers,
                optimize_py=optimize_py,
                examples_py=examples_py,
            )
            write_reports(example_dir, json_text, md_text)
            digest = json.loads(json_text)["projection"]["full_report_digest"]
            print(
                f"regenerated {example_id(example_dir)} ({read_mode(example_dir)} "
                f"mode): report.json "
                f"{len(json_text.encode('utf-8'))} B, report.md "
                f"{len(md_text.encode('utf-8'))} B, full report "
                f"{digest[:len(DIGEST_PREFIX) + 12]}"
            )
    except ExamplesError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
