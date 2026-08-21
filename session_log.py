#!/usr/bin/env python3
"""
Session Log — Append-only JSONL store for optimization results (issue #239).

Each optimization run is serialized as one JSON line into a session file
(`optimisations.jsonl` by default) so that successive runs can be compared
against each other, against the input.json that produced them, and against
the code commit that generated them.

Per issue #239, each record contains:

- ``timestamp``: ISO-8601 UTC timestamp of the run.
- ``git_commit``: the code commit id (short SHA) that produced the result.
- ``git_dirty``: whether the working tree had uncommitted changes at run time.
- ``input_path``: path to the input.json that was optimized.
- ``input``: the full input config dict (verbatim from input.json).
- ``objective``: name of the objective function used for ranking.
- ``optimizer_mode``: optional dict describing the optimizer mode (DP#8).
- ``scenarios``: list of per-scenario dicts, each containing:
    - ``name``: scenario name.
    - ``config_overrides``: the overrides applied to the base config.
    - ``score``: objective score.
    - ``summary``: the optimizer summary dict (TFSA, RRSP, net_benefit, ...).
    - ``year_results``: list of year-by-year YearResult dicts (actions,
      returns, balances, contribution rights, tax savings, ...).

The session file is written next to the input by default (inside the
scenario folder of the inputs repo) so that private financial data never
leaks into the public code repo. The caller may override the destination
path via the ``session_path`` argument or the ``--save-session`` CLI flag.

Usage (programmatic):

    from session_log import save_session, build_session_record
    record = build_session_record(
        input_path="inputs/scenarios/my-scenario/input.json",
        input_cfg=cfg,
        results=results,          # list of RankedScenario or summary dicts
        objective=objective,
    )
    save_session(record, session_path="inputs/scenarios/my-scenario/optimisations.jsonl")

Usage (CLI):

    python optimize.py --input .../input.json --save-session
"""

import copy
import dataclasses
import datetime
import json
import subprocess
from pathlib import Path
from typing import Any

# =============================================================================
# Git helpers
# =============================================================================

def _git_commit_sha(repo_path: str = ".") -> str | None:
    """Return the short SHA of HEAD, or None if git is unavailable."""
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_path, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _git_dirty(repo_path: str = ".") -> bool:
    """Return True if the working tree has uncommitted changes."""
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_path, "status", "--porcelain"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return bool(out.strip())
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


# =============================================================================
# YearResult serialization
# =============================================================================

def year_result_to_dict(year_result) -> dict[str, Any]:
    """Serialize a YearResult dataclass to a plain dict.

    Uses dataclasses.asdict so nested dicts (e.g. contributions) survive
    the round-trip.
    """
    return dataclasses.asdict(year_result)


# =============================================================================
# Session record builder
# =============================================================================

def build_session_record(
    input_path: str,
    input_cfg: dict,
    results: list[dict | Any],
    objective_name: str = "",
    optimizer_mode: dict | None = None,
    git_repo_path: str = ".",
    extra: dict | None = None,
) -> dict[str, Any]:
    """Build a single JSONL-serializable record for one optimization run.

    Args:
        input_path: Path to the input.json that was optimized.
        input_cfg: The full input config dict (verbatim from input.json).
        results: List of result objects. Each item may be:
            - a RankedScenario (from optimizer.optimize()) carrying
              year_results and config_overrides, or
            - a plain summary dict (from evaluate_strategy_with_simulation),
              optionally augmented with a ``year_results`` key of YearResult
              objects and a ``config_overrides`` key.
        objective_name: Name of the objective function used for ranking.
        optimizer_mode: Optional dict describing the optimizer mode (DP#8).
        git_repo_path: Path to the git repo for commit id capture.
        extra: Optional dict of additional top-level keys to merge in.

    Returns:
        A dict suitable for json.dumps() / JSONL line emission.
    """
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    commit = _git_commit_sha(git_repo_path)
    dirty = _git_dirty(git_repo_path)

    scenarios = []
    for r in results:
        scenarios.append(_scenario_to_dict(r))

    record: dict[str, Any] = {
        "timestamp": timestamp,
        "git_commit": commit,
        "git_dirty": dirty,
        "input_path": str(input_path),
        "input": copy.deepcopy(input_cfg),
        "objective": objective_name,
        "scenarios": scenarios,
    }
    if optimizer_mode is not None:
        record["optimizer_mode"] = optimizer_mode
    if extra:
        record.update(extra)
    return record


def _scenario_to_dict(scenario: dict | Any) -> dict[str, Any]:
    """Convert one scenario result (RankedScenario or summary dict) to a dict.

    A RankedScenario exposes: scenario_name, score, objective_name,
    config_overrides, results (List[YearResult]), risk_measures.
    A summary dict (from evaluate_strategy_with_simulation) exposes strategy,
    net_benefit, objective_score, and optionally year_results / config_overrides.
    """
    # RankedScenario dataclass (optimizer module)
    if hasattr(scenario, "scenario_name"):
        out: dict[str, Any] = {
            "name": scenario.scenario_name,
            "score": scenario.score,
            "objective_name": getattr(scenario, "objective_name", ""),
            "config_overrides": getattr(scenario, "config_overrides", {}) or {},
            "summary": _ranked_scenario_summary(scenario),
            "year_results": [year_result_to_dict(yr) for yr in scenario.results],
        }
        rm = getattr(scenario, "risk_measures", None)
        if rm is not None:
            out["risk_measures"] = dataclasses.asdict(rm) if dataclasses.is_dataclass(rm) else dict(rm)
        return out

    # Plain summary dict (evaluate_strategy_with_simulation output)
    out = dict(scenario)  # shallow copy of the summary fields
    year_results = scenario.get("year_results") if isinstance(scenario, dict) else None
    if year_results:
        out["year_results"] = [year_result_to_dict(yr) for yr in year_results]
    else:
        out["year_results"] = []
    # Normalize name
    if "name" not in out and "strategy" in out:
        out["name"] = out["strategy"]
    return out


def _ranked_scenario_summary(scenario: Any) -> dict[str, Any]:
    """Extract a summary dict from a RankedScenario.

    Mirrors the summary fields produced by evaluate_strategy_with_simulation
    (TFSA, RRSP, net_benefit, ...) using the final YearResult.
    """
    results = getattr(scenario, "results", [])
    if not results:
        return {"score": scenario.score}
    final = results[-1]
    total_rrsp_savings = sum(yr.rrsp_tax_savings for yr in results)
    total_sm_savings = sum(yr.readvance_tax_savings for yr in results)
    return {
        "year": final.year,
        "total_assets": final.total_assets,
        "total_debt": final.total_debt,
        "total_rrsp": final.total_rrsp,
        "total_tfsa": final.total_tfsa,
        "resp_balance": final.resp_balance,
        "non_reg_balance": final.non_reg_balance,
        "rrsp_total_savings": total_rrsp_savings,
        "sm_total_savings": total_sm_savings,
        "net_benefit": getattr(scenario, "score", 0.0),
        "objective_name": getattr(scenario, "objective_name", ""),
    }


# =============================================================================
# JSONL writer
# =============================================================================

def save_session(record: dict[str, Any], session_path: str | Path) -> str:
    """Append a session record as one JSON line to ``session_path``.

    Creates parent directories if needed. Returns the path written to.
    """
    path = Path(session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.write("\n")
    return str(path)


def default_session_path(input_path: str | Path) -> str:
    """Return the default session file path next to the input.json.

    Per issue #239, the session file lives inside the scenario folder
    (the inputs repo), so it is version-controlled alongside the private
    financial data and never enters the public code repo.
    """
    p = Path(input_path).resolve()
    return str(p.parent / "optimisations.jsonl")


def load_session(session_path: str | Path) -> list[dict[str, Any]]:
    """Read a JSONL session file and return the list of parsed records.

    Skips blank lines. Useful for tests and comparison tooling.
    """
    records = []
    with open(session_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records
