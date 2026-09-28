#!/usr/bin/env python3
"""Generalized sensitivity sweeps (issue #771).

``sensitivity.sweeps`` is a GENERAL map ``{ contract-path -> [values] }``. For
each declared path the sweep sets that leaf to each value in a fresh copy of the
contract document, re-maps the copy to the engine's internal config, runs the
optimizer, and records the objective the optimizer reaches AND the first
decumulation-shortfall year (#707/#770) for that value. That makes "how does the
answer change as X varies?" expressible IN the contract, for any leaf X -- not
just the three axes the schema used to hard-code (#771's whole point).

Three properties this module exists to guarantee:

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

* **A row's drawdown facts NAME the candidate they describe** (issue #386).
  ``run_optimization`` RANKS candidates and this module used to read
  ``results[0]`` -- the objective's argmax -- and publish its shortfall summary as
  though it described the swept axis. An argmax need not vary monotonically in
  the swept value: adjacent values can select different winners, so the published
  sequence could move in either direction and nothing in the row said which
  candidate it quoted. Every row now carries BOTH families, each keyed by the
  candidate it came from:

  * ``plan_*`` -- the PASS-1 candidate the optimizer ranks first on the contract
    **as declared**, held constant across the sweep (DP#5: anchor the decision,
    overlay the sensitivity). The decision is frozen -- including the
    household's own declared drawdown order, which pass 2 (#618) exists to search
    around -- so these facts move with the swept axis ALONE; this is the frontier
    a reader locates.
  * ``winner_*`` -- whatever the objective ranks first at that value, WITH its
    identity (``strategy`` / ``drawdown_order_id`` / ``draw_fraction``), so a
    reader can see when adjacent rows stopped describing the same plan.

  A plan candidate missing from a value's ranking is reported as
  ``plan_present = False`` -- an explicit absence, never a silently substituted
  winner (DP#32) -- and the table prints it as such rather than as a zero.

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


# The ``drawdown_order_id`` ``run_optimization`` stamps on every PASS-1 row: the
# row priced at the household's OWN configured drawdown order
# (``optimize.py``'s pass-1 loop; the same id ``scenario_discovery
# .discover_drawdown_orders`` gives its gated-off single candidate). Pass 2 is the
# #618 search: it re-runs ONLY the pass-1 winner under OTHER orders and renames
# the row ``"<strategy> + <order label>"``. Used below to keep the anchored plan
# a pass-1 candidate -- see :func:`_anchor_identity`.
_CONFIGURED_DRAWDOWN_ORDER_ID = "configured"


def _candidate_identity(result: Dict) -> Optional[Tuple[Any, Any, Any]]:
    """The identity that names ONE candidate across swept values (issue #386).

    ``(strategy, drawdown_order_id, draw_fraction)`` -- the three fields
    ``run_optimization`` stamps on every ranked result (the accumulation
    strategy's name, the pass-2 drawdown order's id, and the #735 draw
    fraction).

    The guard is on the row's NAME, not on an optional block, and the identity is
    built before it is returned: a row with no ``strategy`` name cannot be NAMED
    as one candidate, and a tuple of Nones would otherwise match another unnamed
    row by accident. That distinction is also why this is not the shape of the
    "optional block, then one key" read ``asset_location_optimize
    ._cross_member_sleeve`` performs (DP#32) -- a blind-rename clone detector
    paired the two on shape alone before this was made explicit."""
    strategy = result.get("strategy")
    identity = (strategy, result.get("drawdown_order_id"), result.get("draw_fraction"))
    return identity if strategy is not None else None


def _anchor_identity(doc: Dict, objective: Optional[ObjectiveFunction]):
    """The PLAN's identity: the PASS-1 candidate the optimizer ranks first on the
    contract AS DECLARED (issue #386, DP#5 ``anchor decisions, overlay
    sensitivities``).

    The anchor is the decision the model reaches on the household's OWN declared
    numbers; freezing it is what lets the ``plan_*`` facts move with the swept
    axis ALONE. Computed once per axis (once per report in :func:`run_sweeps`),
    never re-derived per swept value -- a per-value anchor would be the argmax
    again, which is the defect this exists to remove.

    It is deliberately the top-ranked PASS-1 row, not the overall winner: the
    household's ``assumptions.retirement.drawdown_order`` is a DECLARED
    arrangement, and pass 2 (#618) is the optimizer searching arrangements the
    household did not declare, around pass 1's winner alone. Anchoring on a
    pass-2 row would make the plan itself an artifact of that search -- and would
    make the plan VANISH wherever a different accumulation strategy wins pass 1,
    since pass 2 never runs for the losers. Measured on the shipped example, the
    overall winner is a pass-2 row at every spend; the pass-1 anchor survives
    every value while the pass-2 rows come and go (issue #386's absence case).

    Returns None when the declared run yields no pass-1 candidate at all, or a
    candidate carrying no name; :func:`_row` then reports the plan as ABSENT
    rather than substituting the winner (DP#32)."""
    internal = input_contract.to_internal_config(doc)
    results = run_optimization(internal, objective=objective,
                               include_year_by_year=False)  # score-only, #1058
    # Results arrive ``ranking_key``-ordered, so the FIRST pass-1 row is pass 1's
    # own winner -- the accumulation decision, read at the declared drawdown
    # order.
    for result in results:
        if result.get("drawdown_order_id") == _CONFIGURED_DRAWDOWN_ORDER_ID:
            return _candidate_identity(result)
    return None


def _row(doc: Dict, axis: str, path_group: List[str], value: Any,
         objective: Optional[ObjectiveFunction], anchor) -> Dict[str, Any]:
    """Set every concrete path for one swept value, re-map, optimize, and read
    the drawdown facts off BOTH the frozen plan and the value's winner.

    The optimizer already sorts its results ``ranking_key``-first, so a
    trajectory that went bankrupt sorts BELOW a solvent one regardless of
    headline score (#707); ``results[0]`` is therefore the answer the optimizer
    would report -- but it is an ARGMAX, and an argmax need not vary
    monotonically in the swept value (issue #386). So the frozen plan's facts
    (``plan_*``, found by ``anchor`` identity, not by rank) are published
    alongside the winner's, each under a name that says which it is."""
    doc_v = doc
    for path in path_group:
        # voi._with_value deep-copies, so the base doc is never mutated and each
        # path lands on the copy the previous one returned (DP#9: one shared
        # pointer-set implementation, not a second spelling here).
        doc_v = voi._with_value(doc_v, _to_pointer(path), value)
    internal = input_contract.to_internal_config(doc_v)
    results = run_optimization(internal, objective=objective,
                               include_year_by_year=False)  # score-only caller -- skip year_by_year serialization, #1058
    winner = results[0] if results else None
    # The plan is looked up by IDENTITY, never by rank: a candidate absent from
    # this value's ranking leaves the plan ABSENT (DP#32), it does not fall back
    # to the winner. `anchor is None` means the contract's own declared run
    # ranked no named candidate to freeze.
    plan = None
    if anchor is not None:
        plan = next((r for r in results if _candidate_identity(r) == anchor), None)
    return {
        "axis": axis,
        "value": value,
        # `plan_present` says whether the frozen candidate was RANKED here; the
        # plan's identity below is the anchor's either way, so a row whose plan
        # is absent still NAMES the plan it could not find.
        "plan_present": plan is not None,
        **_candidate_fields("plan", plan, identity=anchor),
        "winner_present": winner is not None,
        **_candidate_fields("winner", winner),
    }


def _candidate_fields(prefix: str, result: Optional[Dict], identity=None) -> Dict[str, Any]:
    """One candidate's identity, objective and drawdown facts, keyed under
    ``prefix`` (``plan``/``winner``) so no field can be read as the other's
    (issue #386: the whole defect was two families of facts sharing no name).

    ``identity`` overrides the identity read off ``result``: the PLAN passes the
    frozen anchor's identity, so the plan stays NAMED even at a value whose
    ranking did not contain it (``plan_present`` is then False). An absent
    candidate gets all-None identity/objective and the un-engaged summary -- the
    "not checked" values, never a zero standing in for one that was not there
    (DP#32)."""
    if identity is None and result is not None:
        identity = _candidate_identity(result)
    strategy, order_id, draw_fraction = (identity if identity is not None
                                         else (None, None, None))
    if result is None:
        summary = summarize_drawdown_shortfall([])
    else:
        # Explicit absence test (DP#32): shortfall_of returns None for a row that
        # carries no summary, never a present-but-empty dict to coerce.
        summary = shortfall_of(result)
        if summary is None:
            summary = summarize_drawdown_shortfall([])
    return {
        f"{prefix}_strategy": strategy,
        f"{prefix}_drawdown_order_id": order_id,
        f"{prefix}_draw_fraction": draw_fraction,
        f"{prefix}_objective_score": (result.get("objective_score", result.get("net_benefit"))
                                      if result is not None else None),
        f"{prefix}_label": result.get("label") if result is not None else None,
        **_shortfall_fields(prefix, summary),
    }


def _shortfall_fields(prefix: str, summary: Dict) -> Dict[str, Any]:
    return {
        f"{prefix}_engaged": summary.get("engaged", False),
        f"{prefix}_exhausted": summary.get("exhausted", False),
        f"{prefix}_first_shortfall_year": summary.get("first_shortfall_year"),
        f"{prefix}_shortfall_years": summary.get("shortfall_years", 0),
    }


def _prepare_axis(doc: Dict, axis: str, values: List[Any]) -> List[str]:
    """Validate one axis -- non-empty values, every target path resolving on
    THIS document -- and return its concrete path group.

    Raises :class:`SweepPathError` BEFORE any simulation runs, so a typo costs
    no optimizer pass (DP#32). Called for EVERY declared axis before the anchor
    run, so a bad path anywhere in ``sensitivity.sweeps`` is caught first."""
    if not values:
        raise SweepPathError(
            f"sweep axis {axis!r} declares an empty value list -- a sweep over no "
            f"values is not a sweep (DP#32)."
        )
    path_group = _expand_axis(doc, axis)
    for path in path_group:
        resolve_leaf(doc, path)  # loud failure here if any target is bad
    return path_group


def _axis_rows(doc: Dict, axis: str, path_group: List[str], values: List[Any],
               objective: Optional[ObjectiveFunction], anchor) -> List[Dict[str, Any]]:
    return [_row(doc, axis, path_group, v, objective, anchor) for v in values]


def run_axis_sweep(doc: Dict, axis: str, values: List[Any],
                   objective: Optional[ObjectiveFunction] = None) -> List[Dict[str, Any]]:
    """Sweep one axis: one result row per value (DP#3, pure).

    Validates the axis resolves to real leaf(s) on ``doc`` BEFORE running any
    simulation -- a bad path fails loudly here, naming it, rather than after
    burning a full optimizer pass (DP#32). ``values`` must be non-empty: a sweep
    over nothing is not a sweep.

    One extra optimizer pass is spent up front on the contract AS DECLARED: the
    top-ranked PASS-1 candidate it returns is the frozen plan every row's
    ``plan_*`` facts come from (issue #386; see :func:`_anchor_identity` for why
    it is pass 1 and not the overall winner)."""
    path_group = _prepare_axis(doc, axis, values)
    return _axis_rows(doc, axis, path_group, values, objective,
                      _anchor_identity(doc, objective))


def run_sweeps(doc: Dict,
               objective: Optional[ObjectiveFunction] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Run every axis declared under ``sensitivity.sweeps`` (DP#3, pure).

    Returns ``{axis -> rows}``. An absent/empty ``sweeps`` block yields ``{}``:
    a household that declared no sweep is a no-op, not an error (DP#13).
    Explicit absence tests, not truthiness fallbacks -- a present-but-empty
    ``sweeps`` block is a legitimate "no axes declared", not a value to coerce
    (DP#32).

    Every axis is validated first, then the frozen plan's identity is resolved
    ONCE for the whole report (the anchor depends on the contract and the
    objective, never on the axis or the swept value), so the extra #386 anchor
    pass is paid once rather than once per axis."""
    sensitivity = doc.get("sensitivity")
    if sensitivity is None:
        return {}
    sweeps = sensitivity.get("sweeps")
    if not sweeps:  # absent (None) or explicitly empty ({}) -> no axes to run
        return {}
    prepared = {axis: _prepare_axis(doc, axis, values)
                for axis, values in sweeps.items()}
    anchor = _anchor_identity(doc, objective)
    return {axis: _axis_rows(doc, axis, path_group, sweeps[axis], objective, anchor)
            for axis, path_group in prepared.items()}


# ── Readable output (acceptance criterion 4) ────────────────────────────────


def _value_cell(row: Dict[str, Any]) -> str:
    val = row.get("value")
    if isinstance(val, (int, float)):
        # Dollar-scale figures read as grouped integers; sub-100 values are
        # rates/fractions and keep their decimals.
        return f"{val:,.0f}" if abs(val) >= 100 else f"{val:g}"
    return str(val)


def _candidate_cells(row: Dict[str, Any], prefix: str) -> Tuple[str, str, str, str]:
    """The ``(objective, first year, shortfall years, exhausted)`` cells for ONE
    candidate block. Each kind of absence renders as itself: a candidate the
    sweep never ranked (``<prefix>_present`` False), an un-engaged drawdown
    (the module never ran), and a drawdown that never exhausted all read
    differently -- none of them is a zero (DP#32)."""
    if not row.get(f"{prefix}_present"):
        return "n/a", "n/a (absent)", "n/a", "n/a"
    score = row.get(f"{prefix}_objective_score")
    score_txt = f"{score:,.0f}" if isinstance(score, (int, float)) else "n/a"
    year = row.get(f"{prefix}_first_shortfall_year")
    if not row.get(f"{prefix}_engaged"):
        year_txt = "n/a (no drawdown)"
    elif year is None:
        year_txt = "none"
    else:
        year_txt = str(year)
    years_txt = str(row.get(f"{prefix}_shortfall_years", 0))
    exhausted_txt = "YES" if row.get(f"{prefix}_exhausted") else "no"
    return score_txt, year_txt, years_txt, exhausted_txt


def format_sweep_table(axis: str, rows: List[Dict[str, Any]],
                       objective_name: str = "max_net_benefit") -> str:
    """A single sweep axis as a readable curve (issue #386).

    TWO blocks, because the two families of facts measure different things and
    the artifact must say which is which:

    * **PLAN** -- the frozen decision's facts (DP#5). The decision is the
      candidate the optimizer ranked first on the contract AS DECLARED, held
      constant across every swept value, so the swept axis is the only thing
      moving. This block is the frontier ("which value is still fundable?").
    * **WINNER** -- the objective's argmax at EACH value (#707), with its
      identity. An argmax need not vary monotonically in the swept value, so
      adjacent rows can quote different plans; this block is the optimizer's
      answer, not a frontier."""
    lines = [
        f"Sweep: {axis}   (objective: {objective_name})",
        "",
        "PLAN - the decision FROZEN (DP#5): the candidate the optimizer ranks FIRST on",
        "the contract AS DECLARED, held constant across the sweep. The swept axis alone",
        "moves these facts, so this block is the frontier.",
    ]
    # The plan's NAME is carried on every row (``identity=anchor``), so the header
    # can name the frozen candidate even when some -- or every -- swept value
    # never ranked it. That is deliberate, and it is why the header states the
    # COVERAGE: a named candidate with no measurements beside its name would
    # otherwise read as a plan that was measured.
    #
    # The name comes from the rows' AGREEMENT, not from ``rows[0]``: every row of
    # an axis takes its identity from one anchor, so this is constant by
    # construction -- but a caller that concatenated two sweeps (or hand-built
    # rows) must not see one row's candidate crowned as "the decision FROZEN"
    # beside a count describing another (DP#32).
    ranked = sum(1 for r in rows if r.get("plan_present"))
    unmeasured = len(rows) - ranked
    status = ("measured" if unmeasured == 0
              else "ABSENT" if ranked == 0
              else "PARTIAL-ABSENT")
    coverage = f"[plan {status}: ranked {ranked} of {len(rows)} swept values]"
    identities = {(r.get("plan_strategy"), r.get("plan_drawdown_order_id"),
                   r.get("plan_draw_fraction")) for r in rows}
    identity = identities.pop() if len(identities) == 1 else None
    if identity is None or identity[0] is None:
        reason = ("these rows name DIFFERENT frozen candidates" if identities
                  else "no named candidate was ranked on the contract as declared")
        lines.append(f"  plan candidate: n/a - {reason}")
    else:
        strategy, order_id, draw_fraction = identity
        fraction_txt = (f"{draw_fraction:.0%}"
                        if isinstance(draw_fraction, (int, float)) else str(draw_fraction))
        lines.append(f"  plan candidate: {strategy} / drawdown {order_id} / "
                     f"draw {fraction_txt}   {coverage}")
    if unmeasured:
        lines.append("  (a value that did not rank the plan was NOT checked on it; each such")
        lines.append("   row's plan cells read n/a (absent) and its winner is that value's)")
    lines.append(f"  {'value':>16} | {'objective':>15} | {'first yr':>13} | "
                 f"{'shortfall yrs':>13} | exhausted")
    lines.append(f"  {'-' * 16}-+-{'-' * 15}-+-{'-' * 13}-+-{'-' * 13}-+----------")
    for r in rows:
        score, year, years, exhausted = _candidate_cells(r, "plan")
        lines.append(f"  {_value_cell(r):>16} | {score:>15} | {year:>13} | "
                     f"{years:>13} | {exhausted}")
    lines += [
        "",
        "WINNER - the objective's argmax at EACH value (#707). Adjacent values may select",
        "different winners, so this is the optimizer's answer, NOT a frontier.",
        f"  {'value':>16} | {'objective':>15} | {'first yr':>13} | exhausted | winner strategy",
        f"  {'-' * 16}-+-{'-' * 15}-+-{'-' * 13}-+-----------+----------------",
    ]
    for r in rows:
        score, year, _years, exhausted = _candidate_cells(r, "winner")
        winner = r.get("winner_strategy")
        lines.append(f"  {_value_cell(r):>16} | {score:>15} | {year:>13} | "
                     f"{exhausted:>9} | {winner if winner is not None else 'n/a'}")
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
