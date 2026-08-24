#!/usr/bin/env python3
"""Generalized sensitivity sweeps (issue #771).

``sensitivity.sweeps`` is a GENERAL map ``{ contract-path -> [values] }``. For
each declared path the sweep sets that leaf to each value in a fresh copy of the
contract document, re-maps the copy to the engine's internal config, runs the
optimizer, and records the objective the optimizer reaches AND the first
decumulation-shortfall year (#707/#770) for that value. That makes "how does the
answer change as X varies?" expressible IN the contract, for any leaf X -- not
just the three axes the schema used to hard-code (#771's whole point).

Two properties this module exists to guarantee:

* **A path that does not resolve fails loudly, naming the path** (DP#32). A
  mistyped ``assumptions.retirement.spendign_target`` must raise
  :class:`SweepPathError` naming the bad path, never silently produce a
  single-point run that looks like a sweep -- that silent no-op is precisely the
  "engine substitutes nothing" defect this repo exists to kill (#591/#593/DP#18).
  A leaf is only settable if it ALREADY EXISTS in the document: inventing a new
  key would be an overlay written to a key nothing reads.

* **The three legacy axes are sugar over the SAME resolver** (DP#9 -- no second
  spelling of a sweep path). ``investment_return`` / ``mortgage_rate`` /
  ``savings_rate`` are aliases for the canonical contract path(s) they always
  meant; there is no separate hardcoded per-axis code path. A legacy axis that
  cannot resolve on THIS household (e.g. ``mortgage_rate`` with no mortgage
  liability) fails loudly too.

Pure functions (DP#3): :func:`run_sweeps` is a pure function of ``(doc,
objective)``; only the ``__main__`` CLI touches disk. The JSON-Pointer *set*
itself is delegated to the one shared implementation (``voi._with_value`` ->
``provenance.Provenance.with_value``), so this module adds a loud *validation*
layer, never a second pointer-set (DP#9). This mirrors ``voi.sweep`` (#661),
which does the same set-leaf/re-map/score dance to price value-of-information;
the difference is the axis set (an author's declared ``sensitivity.sweeps`` here
vs. every uncertain leaf there) and the reported facts (objective + shortfall
year here vs. the objective spread there).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import input_contract
import voi
from decumulation import summarize_drawdown_shortfall, shortfall_of
from objective import ObjectiveFunction
from optimize import run_optimization
import contract_schema


class SweepPathError(ValueError):
    """A declared sweep path does not resolve to an existing contract leaf.

    Raised loudly, naming the offending path, rather than letting a mistyped or
    inapplicable path silently collapse a sweep into a single-point run (DP#32).
    """


# ── Legacy-axis sugar (DP#9) ────────────────────────────────────────────────
# The three names the schema used to hard-code are now aliases for the canonical
# contract path(s) they always meant. `investment_return`/`savings_rate` each
# resolve to exactly one scalar leaf; `mortgage_rate` broadcasts to EVERY
# mortgage liability's rate (a household may carry more than one mortgage against
# the same or different charges -- returning the first match would silently drop
# the rest, the exact "returned the first match" trap AGENTS.md lists). All three
# go through the same resolver as any author-written path below; nothing about
# them is special once expanded.

_SCALAR_ALIASES = {
    "investment_return": "assumptions.return_model.rate",
    "savings_rate": "assumptions.savings_rate",
}


def _expand_axis(doc: Dict, axis: str) -> List[str]:
    """Expand a sweep key into the concrete dotted contract path(s) it sets.

    A legacy short-name is sugar (one or more canonical paths); anything else is
    taken literally (a dotted path or an RFC-6901 JSON Pointer). Raises
    :class:`SweepPathError` for a legacy axis that has no target on THIS
    household (e.g. ``mortgage_rate`` with no mortgage) -- an inapplicable axis
    is a loud error, not an empty sweep (DP#32)."""
    if axis in _SCALAR_ALIASES:
        return [_SCALAR_ALIASES[axis]]
    if axis == "mortgage_rate":
        paths = [
            f"liabilities.{i}.rate"
            for i, liab in enumerate(doc.get("liabilities", []))
            if liab.get("kind") == "mortgage"
        ]
        if not paths:
            raise SweepPathError(
                "sweep axis 'mortgage_rate' does not resolve: this household "
                "declares no liability of kind 'mortgage' to sweep the rate of. "
                "Sweep a specific liability's rate by its path "
                "(e.g. 'liabilities.0.rate') instead."
            )
        return paths
    return [axis]


# ── Path resolution + validation (DP#32) ────────────────────────────────────


def _to_pointer(path: str) -> str:
    """A dotted path or an already-RFC-6901 JSON Pointer -> a JSON Pointer.

    ``assumptions.retirement.spending_target`` -> ``/assumptions/retirement/
    spending_target``; ``liabilities.0.rate`` -> ``/liabilities/0/rate``. A path
    already starting with ``/`` is taken as a pointer verbatim."""
    if path.startswith("/"):
        return path
    return "/" + "/".join(path.split("."))


def resolve_leaf(doc: Dict, path: str) -> Tuple[Any, Any]:
    """Return ``(container, key)`` for an EXISTING leaf at ``path``.

    Walks the document to the leaf's parent container and confirms the final
    key/index is already present. Raises :class:`SweepPathError` -- naming the
    full path and the segment that failed -- if any segment is missing, indexes
    past a list, or the final leaf does not already exist. Requiring prior
    existence is deliberate: a sweep may only vary a value the household actually
    declared, never invent a new key the engine would ignore (DP#18/DP#32)."""
    pointer = _to_pointer(path)
    # A dotted/pointer path always yields at least one token (an empty path maps
    # to the single empty token ''), so a missing/empty final key falls through
    # to the "key absent" failure below with the path named -- no empty-list case
    # to special-case.
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]

    node: Any = doc
    for depth, token in enumerate(tokens[:-1]):
        node = _descend(node, token, path)
        if node is _MISSING:
            failed = "/".join(tokens[: depth + 1])
            raise SweepPathError(
                f"sweep path {path!r} does not resolve: no contract leaf at "
                f"/{failed}. Fix the path or remove the sweep (DP#32: a mistyped "
                f"path must not silently produce a single-point run)."
            )

    last = tokens[-1]
    if isinstance(node, list):
        idx = _as_index(last, path)
        if idx is None or not (0 <= idx < len(node)):
            raise SweepPathError(
                f"sweep path {path!r} does not resolve: index {last!r} is out of "
                f"range for the {len(node)}-element list at "
                f"/{'/'.join(tokens[:-1])} (DP#32)."
            )
        return node, idx
    if isinstance(node, dict):
        if last not in node:
            raise SweepPathError(
                f"sweep path {path!r} does not resolve: key {last!r} is absent "
                f"from the object at /{'/'.join(tokens[:-1])}. Fix the path or "
                f"remove the sweep (DP#32: a mistyped path must not silently "
                f"produce a single-point run)."
            )
        return node, last
    raise SweepPathError(
        f"sweep path {path!r} does not resolve: /{'/'.join(tokens[:-1])} is a "
        f"scalar, not a container that can hold {last!r} (DP#32)."
    )


_MISSING = object()


def _descend(node: Any, token: str, path: str) -> Any:
    if isinstance(node, dict):
        return node.get(token, _MISSING)
    if isinstance(node, list):
        idx = _as_index(token, path)
        if idx is None or not (0 <= idx < len(node)):
            return _MISSING
        return node[idx]
    return _MISSING


def _as_index(token: str, path: str) -> Optional[int]:
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


# ── The sweep ───────────────────────────────────────────────────────────────


def _row(doc: Dict, axis: str, path_group: List[str], value: Any,
         objective: Optional[ObjectiveFunction]) -> Dict[str, Any]:
    """Set every concrete path for one swept value, re-map, optimize, and read
    the winner's objective + first-shortfall year off the ranked top.

    The optimizer already sorts its results ``ranking_key``-first, so a
    trajectory that went bankrupt sorts BELOW a solvent one regardless of
    headline score (#707); ``results[0]`` is therefore the answer the optimizer
    would report, and the shortfall summary it carries is that same winner's."""
    doc_v = doc
    for path in path_group:
        # voi._with_value deep-copies, so the base doc is never mutated and each
        # path lands on the copy the previous one returned (DP#9: one shared
        # pointer-set implementation, not a second spelling here).
        doc_v = voi._with_value(doc_v, _to_pointer(path), value)
    internal = input_contract.to_internal_config(doc_v)
    results = run_optimization(internal, objective=objective,
                               include_year_by_year=False)  # score-only caller — skip year_by_year serialization, #1058
    if not results:
        summary = summarize_drawdown_shortfall([])
        return {"axis": axis, "value": value, "objective_score": None,
                "label": None, **_shortfall_fields(summary)}
    best = results[0]
    # Explicit absence test (DP#32): shortfall_of returns None for a row that
    # carries no summary, never a present-but-empty dict to coerce.
    summary = shortfall_of(best)
    if summary is None:
        summary = summarize_drawdown_shortfall([])
    return {
        "axis": axis,
        "value": value,
        "objective_score": best.get("objective_score", best.get("net_benefit")),
        "label": best.get("label"),
        **_shortfall_fields(summary),
    }


def _shortfall_fields(summary: Dict) -> Dict[str, Any]:
    return {
        "engaged": summary.get("engaged", False),
        "exhausted": summary.get("exhausted", False),
        "first_shortfall_year": summary.get("first_shortfall_year"),
        "shortfall_years": summary.get("shortfall_years", 0),
    }


def run_axis_sweep(doc: Dict, axis: str, values: List[Any],
                   objective: Optional[ObjectiveFunction] = None) -> List[Dict[str, Any]]:
    """Sweep one axis: one result row per value (DP#3, pure).

    Validates the axis resolves to real leaf(s) on ``doc`` BEFORE running any
    simulation -- a bad path fails loudly here, naming it, rather than after
    burning a full optimizer pass (DP#32). ``values`` must be non-empty: a sweep
    over nothing is not a sweep."""
    if not values:
        raise SweepPathError(
            f"sweep axis {axis!r} declares an empty value list -- a sweep over no "
            f"values is not a sweep (DP#32)."
        )
    path_group = _expand_axis(doc, axis)
    for path in path_group:
        resolve_leaf(doc, path)  # loud failure here if any target is bad
    return [_row(doc, axis, path_group, v, objective) for v in values]


def run_sweeps(doc: Dict,
               objective: Optional[ObjectiveFunction] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Run every axis declared under ``sensitivity.sweeps`` (DP#3, pure).

    Returns ``{axis -> rows}``. An absent/empty ``sweeps`` block yields ``{}``:
    a household that declared no sweep is a no-op, not an error (DP#13).
    Explicit absence tests, not truthiness fallbacks -- a present-but-empty
    ``sweeps`` block is a legitimate "no axes declared", not a value to coerce
    (DP#32)."""
    sensitivity = doc.get("sensitivity")
    if sensitivity is None:
        return {}
    sweeps = sensitivity.get("sweeps")
    if not sweeps:  # absent (None) or explicitly empty ({}) -> no axes to run
        return {}
    return {axis: run_axis_sweep(doc, axis, values, objective=objective)
            for axis, values in sweeps.items()}


# ── Readable output (acceptance criterion 4) ────────────────────────────────


def format_sweep_table(axis: str, rows: List[Dict[str, Any]],
                       objective_name: str = "max_net_benefit") -> str:
    """A single sweep axis as a readable curve/table, so a break-even is legible
    -- which value first causes (or avoids) a decumulation shortfall."""
    lines = [
        f"Sweep: {axis}   (objective: {objective_name})",
        f"  {'value':>16}  {'objective':>18}  {'first shortfall yr':>18}  {'exhausted':>9}",
        f"  {'-' * 16}  {'-' * 18}  {'-' * 18}  {'-' * 9}",
    ]
    for r in rows:
        score = r.get("objective_score")
        score_txt = f"{score:,.0f}" if isinstance(score, (int, float)) else "n/a"
        yr = r.get("first_shortfall_year")
        if not r.get("engaged"):
            yr_txt = "n/a (no drawdown)"
        elif yr is None:
            yr_txt = "none"
        else:
            yr_txt = str(yr)
        exhausted = "YES" if r.get("exhausted") else "no"
        val = r.get("value")
        if isinstance(val, (int, float)):
            # Dollar-scale figures read as grouped integers; sub-100 values are
            # rates/fractions and keep their decimals.
            val_txt = f"{val:,.0f}" if abs(val) >= 100 else f"{val:g}"
        else:
            val_txt = str(val)
        lines.append(f"  {val_txt:>16}  {score_txt:>18}  {yr_txt:>18}  {exhausted:>9}")
    return "\n".join(lines)


def format_all(report: Dict[str, List[Dict[str, Any]]],
               objective_name: str = "max_net_benefit") -> str:
    if not report:
        return "No sweeps declared under sensitivity.sweeps."
    return "\n\n".join(
        format_sweep_table(axis, rows, objective_name) for axis, rows in report.items()
    )


def _main() -> None:
    import argparse
    # Issue #1017 (DP#22): resolve the objective the SAME way optimize.py does
    # -- CLI --objective wins over the contract's decisions.objective, which
    # wins over the historical max_net_benefit default -- instead of hard-coding
    # MAX_NET_BENEFIT. A sweep of ``assumptions.retirement.spending_target`` was
    # reporting the WEALTH-MAXIMISING strategy's first_shortfall_year per value,
    # so the die-with-zero frontier came out non-monotone ($150k->yr32, $250k
    # ->15, $400k->12, $600k->25): the objective picked a different winner at
    # each spend than the estate-minimising one the user asking "when can we
    # retire and burn savings to ~zero by death?" actually wanted. Resolution is
    # delegated to optimize.resolve_objective (DP#9 -- one spelling of the
    # objective-choice, including its loud refusal of an unknown name, DP#32);
    # absent both the flag and decisions.objective it returns MAX_NET_BENEFIT,
    # byte-identical to the previous behaviour (the golden household declares
    # no objective).
    from optimize import ObjectiveSelectionError, resolve_objective

    parser = argparse.ArgumentParser(
        description="Run the sensitivity.sweeps declared in a contract (#771).")
    parser.add_argument("--input", default="input.json",
                        help="Path to the contract document.")
    parser.add_argument(
        "--objective", default=None,
        help="Override the objective used to rank each swept value. Defaults "
             "to the contract's decisions.objective, then max_net_benefit -- "
             "the same resolution as optimize.py (issue #1017).")
    args = parser.parse_args()

    doc = contract_schema.load_contract_json(args.input)
    contract_schema.validate_contract(doc)
    # ``decisions.objective`` is carried onto the internal config as
    # ``cfg['objective']`` by input_contract (the single ingestion boundary
    # that validates the name); resolve_objective reads it from there, so the
    # contract-sourced name is validated once at ingestion and re-validated
    # here only for a hand-built internal config (DP#32).
    internal = input_contract.to_internal_config(doc)
    try:
        objective = resolve_objective(args.objective, internal)
    except ObjectiveSelectionError as exc:
        print(f"Error: {exc}")
        return
    report = run_sweeps(doc, objective=objective)
    print(format_all(report, objective_name=objective.name))


if __name__ == "__main__":
    _main()
