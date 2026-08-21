#!/usr/bin/env python3
"""
voi.py -- Value of Information (issue #661 + #671, Track 2 of epic #659).

"What should I go find out?" Rank every unknown fact about a household by how
many dollars it is worth resolving.

## Where the uncertainty model lives (#671)

**In the SCHEMA, not in the document.** "How uncertain is a TFSA contribution
room?" has the same answer for every Canadian household -- it is a property of
the DOMAIN, not of one person's file. So each uncertain leaf carries an
``x-uncertainty`` annotation in ``schema/input_schema.json`` (or a
jurisdiction overlay):

    "default_spousal_rollover": {
      "type": "boolean",
      "x-uncertainty": { "domain": [true, false],
                         "resolved_by": "Read the will. ...",
                         "question": "Is a spousal rollover elected ...?" }
    }

This is what makes the tool work for a **brand-new user, who has no provenance
sidecar at all** -- the case epic #659 exists to serve. Before #671, such a
document ranked *nothing*: the engine was correct but had no fuel. Now every
document inherits the domain's uncertainty model for free, and the first run
produces the handful of things worth going to look up, each with a price on it.

**Precedence (DP#13: a default is a fallback for absent input, never a
coercion of input that was supplied):**

    document provenance (#660)  >  schema x-uncertainty default  >  nothing

A leaf with neither stays UNRANKED, and says so.

## Candidacy is OPT-IN, by construction (#671 defect 2)

A leaf is a VOI candidate **only if** it carries an uncertainty spec (from the
schema or from the sidecar). ``/schema_version``, ``/currency``,
``/people/0/id``, ``/people/0/label``, relationship ``type`` -- identifiers,
enums and structure -- are not facts a household goes and looks up, and they
are excluded because nothing opted them in, not because a blocklist named
them. The report states how many leaves were skipped this way, so the
exclusion is visible rather than silent.

## Resolvability: a belief is not a fact you forgot to look up (#673)

The sweep used to rank every uncertain leaf in ONE list, and the wrong kind of
uncertainty won every time: a 3%-9% long-run return band swings a 44-year
projection by tens of millions, so it buried genuinely actionable items --
contribution room, a rollover election -- three screens down, under a
question no document can ever answer.

So every ``x-uncertainty`` block MUST also declare, alongside
``plausible_range``/``domain``/``resolved_by``:

    "resolvability": "document" | "belief"

  - ``"document"`` -- a RESOLVABLE FACT. Some real document ends the
    uncertainty and drives it to zero: contribution room (CRA My Account), a
    rollover election (the will), an interest rate (the statement), gross
    income (a pay stub). Go get the document.
  - ``"belief"`` -- IRREDUCIBLE. No document will ever settle it: a long-run
    return assumption, inflation, longevity, a savings-rate target. You can
    only *choose* a value and *bound* the consequence. Several beliefs are
    PARTLY narrowable -- a savings rate is a belief in the schema but a fact
    in your own bank statements; longevity is a belief, but family history
    narrows it -- which is why ``resolved_by`` on a belief describes how to
    *narrow* it, and never implies a lookup that would *settle* it.

This is a property of the DOMAIN ("how uncertain is a TFSA contribution
room?" has the same answer for every Canadian household), so it belongs in
the SCHEMA, exactly like the range does -- and it does NOT follow document
provenance's override precedence the way the range/domain does: a household
that states a tighter guess for its own savings rate has narrowed a belief,
not converted it into a fact, so the classification always comes from the
schema leaf, never from the document (see ``_resolvability_for``). A
provenance-only candidate -- one whose schema leaf carries no ``x-uncertainty``
annotation at all, so the whole spec came from Track 1's sidecar (#660) --
defaults to ``"document"``: every Track 1 confidence level (measured/stated/
derived/assumed) is by construction a claim about a verifiable fact, never a
forward-looking belief; beliefs live on schema-anchored leaves
(``assumptions.*``, ``return_model.rate``, ``mortality_belief.*``), which
always carry their own annotation and are handled by the branch above.

No silent default (DP#32): a leaf whose ``x-uncertainty`` block omits
``resolvability``, or declares one outside ``{"document", "belief"}``, FAILS
SCHEMA VALIDATION -- ``UncertaintyAnnotationError``, raised the moment that
annotation is read, never guessed at render time. ``validate_uncertainty_
annotations`` additionally walks the WHOLE schema up front (every call to
``collect_candidates``/``sweep``), so a bad annotation is caught even on a
schema leaf no example document happens to exercise.

The report (``render_report``) splits ``THE BIG QUESTIONS`` into two ranked
sections on exactly this classification:

    GO AND FIND OUT                -- resolvable; a document ends the uncertainty
    IRREDUCIBLE -- YOU MUST CHOOSE -- no document will settle these

No ``belief`` leaf can ever appear in the first section; that is #673's whole
point, and it is enforced by construction (the sections are built by
filtering on ``Finding.resolvability``), not by convention.

## Method

One-at-a-time (OAT) sweep. For each candidate leaf: run the objective once per
``domain`` value, or at both ends of the ``plausible_range``, holding every
other leaf at the document's baseline. The spread (max - min) in the objective
is that leaf's value of information. Ranked descending.

## The three ways a leaf can be worth $0 -- and they are NOT the same thing

This distinction is the whole point of #661's enforcement, and getting it wrong
is a silent zero inside the tool built to catch silent zeros (DP#32). Measured
on the example household, ``/estate/default_spousal_rollover`` is:

  - **$0 under ``max_net_benefit``** (the default objective), and yet
  - **$84,998 under ``max_after_tax_estate``**.

Reporting that as "nothing reads this" would be a false statement about the
engine. So the report separates:

  1. ``NO EFFECT ON THE ENGINE'S INPUT`` -- the mapped engine config is
     byte-for-byte identical for every sampled value, **for this document**.
     Proven *without running any simulation* (the cheap screen below).

     Note the careful scope: this does **not** say "the code never reads this
     key." A differently-shaped document can make the very same leaf live, and
     one demonstrably does -- ``/estate/rollover_overrides/0/spousal_rollover``
     shows no effect on the example household only because its single override
     names a *spouse-owned* account while the primary dies first; move the same
     override onto a primary-owned account and the registered rolled fraction
     swings 1.0 -> 0.07. Claiming "nothing reads this" there would be a false
     statement about the engine. Cross-checking against #582's *static* dead-key
     analysis is therefore sound in one direction only: anything that moves the
     engine here MUST be classified consumed there (see
     ``tests/test_voi_671_schema_defaults.py``).
  2. ``INERT UNDER THIS OBJECTIVE`` -- the engine *does* read it (the mapped
     config changes), but the chosen objective does not price it. That is a
     finding about the *objective*, not about the household: the report names
     the objectives that DO move it, verified by re-running them, never guessed.
  3. ``UNRANKED`` -- no range or domain declared anywhere, so no value can be
     computed. Not $0. Not computed. Never printed as $0.

## Honesty requirements (#661)

  - The OAT limitation is printed in the report itself, always (``OAT_LIMITATION``).
  - No silent caps: ``--max-leaves`` prints exactly what it dropped.
  - A refusal is not a crash: when the engine legitimately refuses a document
    (``ContractAdaptationError`` -- e.g. the four-generation ``schema/example.json``
    the two-adults-plus-children engine cannot yet represent, #598/#643), that
    is reported as a first-class outcome, not an unhandled traceback.
  - When the behavioural dead-key pass does not run, it says "NOT COMPUTED",
    never "(none found)".

## Performance

Every candidate is screened for free before any simulation: the document is
mapped to the engine's internal config (``input_contract.to_internal_config``
-- schema validation and a structural walk, no simulation) once per sampled
value. If the mapped config is identical across all of them, the engine cannot
possibly produce a different answer for this document (DP#3/DP#26: the step is
a pure, deterministic fold over that config), so the leaf is reported as having
no effect on the engine's input and the expensive run is skipped entirely. Only
leaves that survive the screen pay for a simulation. ``--jobs`` parallelises
those (independent pure evaluations, no shared state).
"""
from __future__ import annotations

import argparse
import copy
import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import input_contract
from objective import OBJECTIVES, ObjectiveFunction
from optimize import run_optimization


OAT_LIMITATION = (
    "LIMITATION: this is a one-at-a-time (OAT) sweep -- each fact is varied alone, "
    "everything else held at the document's baseline. OAT cannot see INTERACTIONS "
    "(e.g. the value of knowing the rollover election depends on who dies first). "
    "Treat this ranking as a starting point, not the final word -- a Morris/Sobol "
    "screening pass is the natural refinement, not shipped here."
)

DEFAULT_OBJECTIVE_NAME = "max_net_benefit"

#: The annotation keyword (#671). Lives on the schema leaf, not in the document.
UNCERTAINTY_KEYWORD = "x-uncertainty"

#: The only two legal values of an ``x-uncertainty`` block's ``resolvability``
#: (#673). "document" = a real document ends the uncertainty. "belief" = no
#: document ever will; the household can only choose a value and bound it.
RESOLVABILITY_VALUES = ("document", "belief")


class EngineRefusal(Exception):
    """The engine legitimately refuses to simulate this document. Not a crash,
    not a bug in VOI -- a first-class outcome the report states plainly."""


class UncertaintyAnnotationError(ValueError):
    """An ``x-uncertainty`` block violates the annotation contract (#673): DP#32
    forbids a silent default, so a block that omits ``resolvability`` -- or
    declares one outside ``RESOLVABILITY_VALUES`` -- fails loudly the moment it
    is read, rather than being guessed at render time."""


def _validate_resolvability(entry: dict, where: str) -> None:
    """Every ``x-uncertainty`` dict MUST declare a valid ``resolvability``
    (#673). Called everywhere an annotation is read, so no path can silently
    skip the check. Pure (DP#3): raises, never defaults."""
    value = entry.get("resolvability")
    if value not in RESOLVABILITY_VALUES:
        raise UncertaintyAnnotationError(
            f"{where}: x-uncertainty block must declare "
            f'resolvability: "document" | "belief" (#673 -- DP#32 forbids a '
            f"silent default here). Got: {value!r}."
        )


def validate_uncertainty_annotations(schema: Dict) -> None:
    """Walk the WHOLE schema -- not just one document's leaves -- and validate
    every ``x-uncertainty`` block it contains. This is what makes 'fails
    schema validation' true regardless of which document is swept: a bad
    annotation is caught even on a schema leaf no example document happens to
    touch. Pure (DP#3); raises ``UncertaintyAnnotationError`` on the first
    violation found."""
    def _walk_schema(node: Any, path: str) -> None:
        if isinstance(node, dict):
            entry = node.get(UNCERTAINTY_KEYWORD)
            if isinstance(entry, dict):
                _validate_resolvability(entry, path)
            for key, value in node.items():
                if key == UNCERTAINTY_KEYWORD:
                    continue
                _walk_schema(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                _walk_schema(value, f"{path}[{i}]")

    _walk_schema(schema, "$")


# ═══════════════════════════════════════════════════════════════════════════
# 1. The uncertainty model: schema defaults, overridden by document provenance
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class UncertaintySpec:
    """What we know about how uncertain one leaf is, and what resolves it."""
    plausible_range: Optional[Tuple[Any, Any]]
    domain: Optional[Tuple[Any, ...]]
    resolved_by: Optional[str]
    question: Optional[str]
    source: str                    # "provenance" | "schema"

    def sample_values(self) -> Optional[List[Any]]:
        """Domain values, or the two range endpoints, or None (nothing to sweep)."""
        if self.domain:
            return list(self.domain)
        if self.plausible_range:
            lo, hi = self.plausible_range
            return [lo, hi]
        return None


def _spec_from_mapping(entry: Any, source: str) -> Optional[UncertaintySpec]:
    if not isinstance(entry, dict):
        return None
    rng = entry.get("plausible_range")
    dom = entry.get("domain")
    if rng is None and dom is None:
        return None
    return UncertaintySpec(
        plausible_range=tuple(rng) if rng is not None else None,
        domain=tuple(dom) if dom is not None else None,
        resolved_by=entry.get("resolved_by"),
        question=entry.get("question"),
        source=source,
    )


# ── JSON-Pointer -> schema node (following $ref / properties / items / anyOf) ──

def _deref(node: Any, root: Dict) -> Any:
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 32:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            return node
        target: Any = root
        for token in ref[2:].split("/"):
            if not isinstance(target, dict) or token not in target:
                return node
            target = target[token]
        # JSON Schema 2020-12 allows keywords ALONGSIDE $ref, and they apply.
        # That is exactly how a shared $def (money, rate) gets a *site-specific*
        # x-uncertainty: the annotation sits next to the $ref, and wins.
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        node = {**target, **siblings} if isinstance(target, dict) else target
        seen += 1
    return node


def _branches(node: Any, root: Dict) -> List[Dict]:
    """A node plus every subschema it unions over (anyOf/oneOf/allOf), so a leaf
    declared as ``anyOf: [{$ref: room}, {type: null}]`` still resolves."""
    node = _deref(node, root)
    if not isinstance(node, dict):
        return []
    out = [node]
    for key in ("anyOf", "oneOf", "allOf"):
        # DP#32: explicit absence-test, never `x.get(k, []) or []` -- an empty
        # list is a legitimate value here and must not be conflated with absent.
        subs = node.get(key)
        if not isinstance(subs, list):
            continue
        for sub in subs:
            sub = _deref(sub, root)
            if isinstance(sub, dict):
                out.append(sub)
    return out


def _child(node: Any, token: str, root: Dict) -> Optional[Dict]:
    """Descend one JSON-Pointer token into a schema node."""
    for branch in _branches(node, root):
        if token.isdigit():
            items = branch.get("items")
            if items is not None:
                return _deref(items, root)
            continue
        props = branch.get("properties")
        if isinstance(props, dict) and token in props:
            return _deref(props[token], root)
        addl = branch.get("additionalProperties")
        if isinstance(addl, dict):
            return _deref(addl, root)
    return None


def schema_annotation(schema: Dict, pointer: str) -> Optional[dict]:
    """The raw ``x-uncertainty`` annotation declared for ``pointer``'s schema
    leaf, or None if the leaf never opted in. Pure (DP#3).

    An annotation that declares the leaf uncertain but states no
    ``plausible_range``/``domain`` is returned as-is: that leaf is UNRANKED (we
    know it is an unknown; we do not know how wide), which is a finding about
    the *schema*, not a $0."""
    node: Any = schema
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        node = _child(node, token, schema)
        if node is None:
            return None
    for branch in _branches(node, schema):
        entry = branch.get(UNCERTAINTY_KEYWORD)
        if entry is False:          # explicit opt-OUT
            return None
        if isinstance(entry, dict):
            _validate_resolvability(entry, pointer)   # #673, DP#32: fail loudly, not later
            return entry
    return None


def schema_spec(schema: Dict, pointer: str) -> Optional[UncertaintySpec]:
    """The sweepable spec (range/domain) for ``pointer``'s schema leaf, or None."""
    return _spec_from_mapping(schema_annotation(schema, pointer), source="schema")


def _resolvability_for(schema: Dict, pointer: str) -> str:
    """"document" | "belief" for ``pointer`` (#673). Always sourced from the
    SCHEMA's ``x-uncertainty`` annotation when one exists for this pointer --
    never from document provenance, even when provenance supplied the
    range/domain: resolvability is a property of the domain, and a document
    that narrows a belief's stated width has not turned it into a fact (see
    the module docstring). Only when NO schema annotation exists at all for
    this pointer -- the candidacy came from Track 1's sidecar (#660) alone --
    do we fall back to "document": every Track 1 confidence level is by
    construction a claim about a verifiable fact, never a forward-looking
    belief."""
    entry = schema_annotation(schema, pointer)
    if entry is not None:
        return entry["resolvability"]      # validated by schema_annotation
    return "document"


# ── Document leaves ──────────────────────────────────────────────────────────

def _walk(node: Any, prefix: str = ""):
    if isinstance(node, dict):
        for key, value in node.items():
            if prefix == "" and key == "provenance":
                continue            # the sidecar is not a document leaf
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield from _walk(value, f"{prefix}/{token}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{prefix}/{i}")
    else:
        yield prefix, node


def _document_specs(doc: Dict) -> Tuple[Dict[str, UncertaintySpec], Dict[str, str]]:
    """Uncertainty specs declared by the document's own ``provenance`` sidecar
    (#660 -- Compass's module, NOT reimplemented here), plus each uncertain
    leaf's confidence.

    A document with **no sidecar** needs no provenance module at all: that is
    the new-user path (#671), and it must work standalone. Only a document that
    actually carries a sidecar requires ``provenance`` to be importable -- and
    if the module is missing then, that fails loudly rather than silently
    ignoring the sidecar the author wrote (DP#32).
    """
    if "provenance" not in doc:
        return {}, {}

    import provenance   # Track 1 (#660). Missing + sidecar present => fail loudly.

    prov = provenance.load_provenance(doc)

    # ONLY leaves the author EXPLICITLY wrote an entry for.
    #
    # Not every leaf ``uncertain_leaves()`` returns: by #660's design (and
    # correctly), a leaf with NO entry comes back as `assumed` with no range --
    # so `uncertain_leaves()` yields essentially the whole document. Treating
    # that set as "declared uncertain" would rebuild #671's exact wall the
    # moment a sidecar exists: ~430 UNRANKED pointers and a structural count of
    # zero. The sidecar's *explicit* entries are what override the schema; the
    # rest of the document falls through to the schema defaults, as it must.
    explicit = set(prov.entries)

    specs: Dict[str, UncertaintySpec] = {}
    confidence: Dict[str, str] = {}
    for leaf in prov.uncertain_leaves(doc):
        if leaf.pointer not in explicit:
            continue
        confidence[leaf.pointer] = leaf.confidence
        spec = _spec_from_mapping(
            {
                "plausible_range": list(leaf.plausible_range) if leaf.plausible_range else None,
                "domain": list(leaf.domain) if leaf.domain else None,
                "resolved_by": leaf.resolved_by,
            },
            source="provenance",
        )
        if spec is not None:
            specs[leaf.pointer] = spec
    return specs, confidence


@dataclass(frozen=True)
class Candidate:
    pointer: str
    current_value: Any
    spec: UncertaintySpec
    confidence: str
    resolvability: str             # "document" | "belief" (#673)


def collect_candidates(
    doc: Dict, schema: Optional[Dict] = None
) -> Tuple[List[Candidate], List[str], int]:
    """Every leaf that opted in to being a VOI candidate, the UNRANKED pointers,
    and the count of leaves skipped as structural.

    Precedence (#671): document provenance > schema x-uncertainty > nothing.
    Pure (DP#3).
    """
    if schema is None:
        schema = input_contract.compose_schema()
    validate_uncertainty_annotations(schema)   # #673, DP#32: fail loudly, up front
    doc_specs, doc_confidence = _document_specs(doc)

    candidates: List[Candidate] = []
    unranked: List[str] = []
    structural = 0

    for pointer, value in _walk(doc):
        spec = doc_specs.get(pointer)             # the document wins ...
        if spec is None:
            spec = schema_spec(schema, pointer)   # ... else the schema default
        if spec is not None:
            candidates.append(Candidate(
                pointer=pointer,
                current_value=value,
                spec=spec,
                confidence=doc_confidence.get(pointer, "assumed"),
                resolvability=_resolvability_for(schema, pointer),
            ))
        elif pointer in doc_confidence or schema_annotation(schema, pointer) is not None:
            # Declared an unknown -- by an explicit sidecar entry or by an
            # x-uncertainty annotation -- but with no plausible_range/domain
            # anywhere, so it cannot be priced. A finding (the declaration is
            # incomplete), NOT a $0.
            unranked.append(pointer)
        else:
            # Nothing opted this leaf in: an id, a label, an enum, scaffolding.
            structural += 1
    return candidates, unranked, structural


# ═══════════════════════════════════════════════════════════════════════════
# 2. Running the objective
# ═══════════════════════════════════════════════════════════════════════════

def _strip_sidecar(doc: Dict) -> Dict:
    return {k: v for k, v in doc.items() if k != "provenance"}


def _mapped_config(doc: Dict) -> Dict:
    """Contract document -> engine internal config. Cheap: no simulation.
    A legitimate engine refusal surfaces as EngineRefusal, never a traceback."""
    try:
        return input_contract.to_internal_config(_strip_sidecar(doc))
    except input_contract.ContractAdaptationError as exc:
        raise EngineRefusal(str(exc)) from exc


def _with_value(doc: Dict, pointer: str, value: Any) -> Dict:
    """Pure JSON-Pointer set. Delegates to provenance (#660) when that module is
    importable, so both tracks share one pointer implementation; falls back to a
    local set only on the new-user path (no sidecar, module not installed), which
    must not require Track 1 to be present."""
    try:
        import provenance
    except ModuleNotFoundError:
        pass
    else:
        return provenance.Provenance.with_value(doc, pointer, value)

    new_doc = copy.deepcopy(doc)
    tokens = [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]
    node: Any = new_doc
    for token in tokens[:-1]:
        node = node[int(token)] if isinstance(node, list) else node[token]
    last = tokens[-1]
    if isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return new_doc


def _strategy_scores(internal_cfg: Dict, objective: Optional[ObjectiveFunction]) -> Dict[str, float]:
    """Every strategy's objective score, keyed by strategy name (DP#22: the
    optimizer ranks; VOI reads the WHOLE ranking, not only its top). The argmax
    is ``max(...values())``; the rest of the ranking is what lets VOI tell a
    lever that moves *suboptimal* strategies (but not the winner) apart from one
    that truly moves nothing (#782)."""
    return {
        r["strategy"]: r.get("objective_score", 0.0)
        for r in run_optimization(internal_cfg, objective=objective,
                                 include_year_by_year=False)
    }


def _best(scores: Dict[str, float]) -> float:
    """Top of a strategy ranking -- the argmax objective value (DP#22)."""
    return max(scores.values(), default=0.0)


def _score(internal_cfg: Dict, objective: Optional[ObjectiveFunction]) -> float:
    """Best objective score the optimizer reaches on this config (the argmax
    over strategies)."""
    return _best(_strategy_scores(internal_cfg, objective))


def _strategy_sensitivity(per_config: List[Dict[str, float]]) -> Tuple[float, int]:
    """How much a leaf moves the WHOLE strategy set, not just its argmax (#782).

    Given one strategy-score mapping per sampled value, return
    ``(max_per_strategy_spread, strategies_moved)``: for each strategy present in
    every sample, the spread (max - min) of its own score across the samples;
    the return is the largest such spread and how many strategies moved at all.
    This is the signal the argmax hides -- a fact can leave the winner untouched
    (argmax spread $0) yet swing other strategies by tens of thousands."""
    if not per_config:
        return 0.0, 0
    common = set(per_config[0])
    for scores in per_config[1:]:
        common &= set(scores)
    max_spread = 0.0
    moved = 0
    for name in common:
        vals = [scores[name] for scores in per_config]
        spread = max(vals) - min(vals)
        if spread != 0.0:
            moved += 1
            max_spread = max(max_spread, spread)
    return max_spread, moved


def _worker(args: Tuple[Dict, Optional[ObjectiveFunction]]) -> Dict[str, float]:
    cfg, objective = args
    return _strategy_scores(cfg, objective)


def _run_all(cfgs: List[Dict], objective: Optional[ObjectiveFunction], jobs: int) -> List[Dict[str, float]]:
    """One strategy-score mapping per config (see ``_strategy_scores``)."""
    if jobs > 1 and len(cfgs) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            return list(pool.map(_worker, [(c, objective) for c in cfgs]))
    return [_strategy_scores(c, objective) for c in cfgs]


# ═══════════════════════════════════════════════════════════════════════════
# 3. Findings
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Finding:
    pointer: str
    question: str
    current_value: Any
    confidence: str
    spec_source: str               # "provenance" | "schema"
    range_label: str
    resolved_by: Optional[str]
    spread: float
    low_label: Any
    high_label: Any
    resolvability: str             # "document" | "belief" (#673)
    #: Objectives (other than the swept one) under which this leaf DOES move.
    #: Only ever populated by an actual re-run -- never inferred, never guessed.
    moves_under: Tuple[str, ...] = ()
    #: #782: for a leaf that is optimum-NEUTRAL under the swept objective (spread
    #: 0.0, so it lands in ``inert``), the largest amount it moves ANY single
    #: strategy, and how many strategies it moves at all. "The optimum doesn't
    #: move" is a different statement from "nothing moves": a lever can leave the
    #: argmax untouched yet swing suboptimal strategies by tens of thousands, and
    #: reporting that as a flat $0 tells the household it doesn't matter when it
    #: does. Both are measured (a real re-read of the per-strategy scores), never
    #: guessed. 0.0 / 0 means the leaf moves no strategy at all under this
    #: objective -- genuinely inert, not merely optimum-neutral.
    strategy_spread: float = 0.0
    strategies_moved: int = 0


@dataclass(frozen=True)
class VOIReport:
    ranked: List[Finding]
    #: Mapped engine config identical for every sampled value, FOR THIS DOCUMENT.
    #: Not a claim that the code never reads the key (see the module docstring).
    unread: List[Finding]
    inert: List[Finding]           # engine reads it; THIS objective doesn't price it
    unranked_pointers: List[str]
    dropped_pointers: List[str]
    structural_skipped: int
    objective_name: str
    baseline_score: float
    #: False when the behavioural dead-key pass did not run (no candidates). The
    #: report then says "NOT COMPUTED" -- never "(none found)" (DP#32).
    dead_key_pass_ran: bool
    cross_objective_checked: bool
    refusal: Optional[str] = None


def _fmt(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:,.0f}" if value == int(value) else f"{value:,.4g}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, str):
        return f'"{value}"'
    return str(value)


def _range_label(spec: UncertaintySpec) -> str:
    if spec.domain:
        return "domain: {" + ", ".join(_fmt(v) for v in spec.domain) + "}"
    if spec.plausible_range:
        lo, hi = spec.plausible_range
        return f"range: {_fmt(lo)} - {_fmt(hi)}"
    return ""


def humanize_pointer(pointer: str) -> str:
    """A JSON Pointer turned into words -- mechanical, not narrative."""
    parts = [p for p in pointer.split("/") if p]
    return " ".join(f"#{p}" if p.isdigit() else p.replace("_", " ") for p in parts)


def question_for(candidate: Candidate) -> str:
    """The question in plain language. Taken from the schema/sidecar annotation
    where one exists; otherwise derived mechanically from the pointer. Never
    invents a fact the annotation did not state (#661)."""
    if candidate.spec.question:
        return candidate.spec.question
    path = humanize_pointer(candidate.pointer) or candidate.pointer
    return f"What is the true value of {path}?"


def _finding(cand: Candidate, spread: float, low: Any, high: Any) -> Finding:
    return Finding(
        pointer=cand.pointer,
        question=question_for(cand),
        current_value=cand.current_value,
        confidence=cand.confidence,
        spec_source=cand.spec.source,
        range_label=_range_label(cand.spec),
        resolved_by=cand.spec.resolved_by,
        spread=spread,
        low_label=low,
        high_label=high,
        resolvability=cand.resolvability,
    )


def _objectives_that_move(mapped: List[Dict], swept_objective: str, jobs: int) -> Tuple[str, ...]:
    """Which OTHER built-in objectives actually respond to this leaf. Measured,
    not inferred: every name returned was produced by really running that
    objective on the same sampled configs (#671 -- "do not guess")."""
    movers: List[str] = []
    for name, obj in OBJECTIVES.items():
        if name == swept_objective:
            continue
        best = [_best(s) for s in _run_all(mapped, obj, jobs)]
        if max(best) - min(best) != 0.0:
            movers.append(name)
    return tuple(movers)


# ═══════════════════════════════════════════════════════════════════════════
# 4. The sweep
# ═══════════════════════════════════════════════════════════════════════════

def sweep(
    doc: Dict,
    objective: Optional[ObjectiveFunction] = None,
    max_leaves: Optional[int] = None,
    jobs: int = 1,
    cross_objective: bool = True,
    schema: Optional[Dict] = None,
) -> VOIReport:
    """Run the OAT value-of-information sweep. Pure function of its arguments
    (DP#3): no file I/O, no globals. The CLI is the only thing that touches disk.
    """
    objective_name = objective.name if objective is not None else DEFAULT_OBJECTIVE_NAME

    candidates, unranked, structural = collect_candidates(doc, schema=schema)

    try:
        baseline_score = _score(_mapped_config(doc), objective)
    except EngineRefusal as refusal:
        # A correct refusal must not look like a crash (#671; #598/#643).
        return VOIReport(
            ranked=[], unread=[], inert=[],
            unranked_pointers=unranked, dropped_pointers=[],
            structural_skipped=structural,
            objective_name=objective_name, baseline_score=0.0,
            dead_key_pass_ran=False, cross_objective_checked=False,
            refusal=str(refusal),
        )

    dropped: List[str] = []
    if max_leaves is not None and len(candidates) > max_leaves:
        dropped = [c.pointer for c in candidates[max_leaves:]]
        candidates = candidates[:max_leaves]

    # ── Cheap screen: prove UNREAD without simulating ──────────────────────
    unread: List[Finding] = []
    live: List[Tuple[Candidate, List[Any], List[Dict]]] = []
    for cand in candidates:
        values = cand.spec.sample_values()
        if values is None:
            unranked.append(cand.pointer)
            continue
        mapped = [_mapped_config(_with_value(doc, cand.pointer, v)) for v in values]
        if all(m == mapped[0] for m in mapped[1:]):
            unread.append(_finding(cand, 0.0, None, None))
        else:
            live.append((cand, values, mapped))

    # ── The expensive part ─────────────────────────────────────────────────
    ranked: List[Finding] = []
    inert: List[Finding] = []
    if live:
        flat: List[Dict] = []
        owner: List[int] = []
        for i, (_, _, mapped) in enumerate(live):
            for cfg in mapped:
                flat.append(cfg)
                owner.append(i)
        scores = _run_all(flat, objective, jobs)

        grouped: Dict[int, List[Dict[str, float]]] = {i: [] for i in range(len(live))}
        for i, s in zip(owner, scores):
            grouped[i].append(s)

        for i, (cand, values, mapped) in enumerate(live):
            ss = grouped[i]
            best = [_best(s) for s in ss]                      # argmax per sample
            lo_i = min(range(len(best)), key=lambda k: best[k])
            hi_i = max(range(len(best)), key=lambda k: best[k])
            spread = best[hi_i] - best[lo_i]
            finding = _finding(cand, spread, values[lo_i], values[hi_i])
            if spread == 0.0:
                # The optimum does not move. That is NOT the same as "nothing
                # moves" (#782): the leaf may still swing suboptimal strategies.
                # Measure that from the per-strategy scores we already have --
                # never guessed -- so the report can distinguish an optimum-
                # neutral-but-material lever from a genuinely inert one.
                strat_spread, strat_moved = _strategy_sensitivity(ss)
                # The engine READS this leaf (the mapping differs) but this
                # objective's optimum does not price it. Verify which objectives
                # DO move the optimum -- by running them, never guessing (#671).
                movers: Tuple[str, ...] = ()
                if cross_objective:
                    movers = _objectives_that_move(mapped, objective_name, jobs)
                inert.append(replace(
                    finding, moves_under=movers,
                    strategy_spread=strat_spread, strategies_moved=strat_moved,
                ))
            else:
                ranked.append(finding)

    ranked.sort(key=lambda f: f.spread, reverse=True)

    return VOIReport(
        ranked=ranked,
        unread=unread,
        inert=inert,
        unranked_pointers=unranked,
        dropped_pointers=dropped,
        structural_skipped=structural,
        objective_name=objective_name,
        baseline_score=baseline_score,
        dead_key_pass_ran=bool(candidates),
        cross_objective_checked=cross_objective and bool(inert),
    )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Report
# ═══════════════════════════════════════════════════════════════════════════

def render_report(report: VOIReport) -> str:
    out: List[str] = []

    if report.refusal:
        out.append("CANNOT COMPUTE VALUE-OF-INFORMATION")
        out.append("")
        out.append("  The engine REFUSES this document. This is a correct refusal, not a crash:")
        out.append(f"    {report.refusal}")
        out.append("")
        out.append("  Nothing below was computed. No leaf is claimed to be worth $0.")
        out.append(f"  ({len(report.unranked_pointers)} leaves were declared uncertain with no range; "
                   f"{report.structural_skipped} were structural.)")
        return "\n".join(out)

    # ── THE BIG QUESTIONS, split by resolvability (#673) ────────────────────
    # A belief (long-run return, inflation, longevity, savings rate) dominates
    # a document-resolvable fact on raw dollar spread every time -- a 3%-9%
    # return band swings a 44-year projection by tens of millions -- but no
    # document ever settles it. Ranking both kinds in one list buries the
    # genuine to-do items under questions nobody can go answer. So the two
    # kinds are ranked SEPARATELY: by construction (filtering on
    # Finding.resolvability), no belief can ever land in GO AND FIND OUT.
    findable = [f for f in report.ranked if f.resolvability == "document"]
    beliefs = [f for f in report.ranked if f.resolvability == "belief"]
    assert len(findable) + len(beliefs) == len(report.ranked), (
        "a ranked finding carried a resolvability outside {'document', 'belief'} -- "
        "unreachable if validate_uncertainty_annotations ran, but never silently drop one"
    )

    out.append(f"GO AND FIND OUT ({len(findable)}) -- resolvable: a document ends the "
               f"uncertainty   (objective: {report.objective_name})")
    out.append(f"  {OAT_LIMITATION}")
    out.append("")
    if not findable:
        out.append("  (nothing ranked -- no document-resolvable candidate moved this objective)")
        out.append("")
    for f in findable:
        out.append(f"  ${f.spread:>12,.0f}   {f.question}")
        src = "" if f.spec_source == "provenance" else "   [schema default]"
        out.append(f"                 now: {f.confidence} {_fmt(f.current_value)}     {f.range_label}{src}")
        if f.resolved_by:
            out.append(f"                 -> {f.resolved_by}")
        out.append(f"                 ({f.pointer})")
        out.append("")

    out.append(f"IRREDUCIBLE -- YOU MUST CHOOSE ({len(beliefs)}) -- no document will settle "
               f"these; this is the range you live with")
    out.append("")
    if not beliefs:
        out.append("  (none -- no belief-classified candidate moved this objective)")
        out.append("")
    for f in beliefs:
        out.append(f"  ±${f.spread:>12,.0f}   {f.question}")
        src = "" if f.spec_source == "provenance" else "   [schema default]"
        out.append(f"                 now: {f.confidence} {_fmt(f.current_value)}     {f.range_label}{src}")
        if f.resolved_by:
            out.append(f"                 narrow it by: {f.resolved_by}")
        out.append(f"                 ({f.pointer})")
        out.append("")

    # ── $0, three distinct kinds, never conflated ──────────────────────────
    out.append(f"NO EFFECT ON THE ENGINE'S INPUT ({len(report.unread)}) -- $0 for THIS document.")
    out.append("  Proven WITHOUT simulating: the mapped engine config is byte-for-byte identical for")
    out.append("  every sampled value. This does NOT say the code never reads the key -- a differently")
    out.append("  shaped document can make the same leaf live (see voi.py's docstring for a measured")
    out.append("  case). It says: for YOUR file, resolving this cannot change the answer.")
    if not report.dead_key_pass_ran:
        out.append("  NOT COMPUTED -- there were no candidate leaves to check.")
    elif not report.unread:
        out.append("  (none)")
    for f in report.unread:
        out.append(f"  $0  {f.pointer}")
    out.append("")

    out.append(f"INERT UNDER THIS OBJECTIVE ({len(report.inert)}) -- $0 at the OPTIMUM under "
               f"{report.objective_name}, but the engine DOES read it.")
    out.append("  A finding about the OBJECTIVE, not about your household. '$0 at the optimum'")
    out.append("  is NOT '$0': a lever can leave the winning strategy untouched yet swing")
    out.append("  suboptimal strategies -- that is disclosed per leaf below, never as a flat $0 (#782).")
    if not report.dead_key_pass_ran:
        out.append("  NOT COMPUTED -- there were no candidate leaves to check.")
    elif not report.inert:
        out.append("  (none)")
    for f in report.inert:
        out.append(f"  $0 at optimum  {f.pointer}")
        if f.strategy_spread > 0.0:
            out.append(f"      OPTIMUM-NEUTRAL BUT STRATEGY-SENSITIVE: the optimum doesn't move, but this "
                       f"moves {f.strategies_moved} strateg"
                       f"{'y' if f.strategies_moved == 1 else 'ies'} by up to ${f.strategy_spread:,.0f}")
            out.append("      -- it is not 'doesn't matter'; a different strategy choice would feel it.")
        else:
            out.append("      moves NO strategy under this objective -- genuinely inert here.")
        if f.moves_under:
            out.append(f"      but it IS priced under: {', '.join(f.moves_under)}")
            out.append(f"      -> re-run with --objective {f.moves_under[0]}")
        elif report.cross_objective_checked:
            out.append("      no built-in objective prices its optimum (every one was checked, by running it)")
        else:
            out.append("      (cross-objective check NOT run -- drop --no-cross-objective to compute it)")
    out.append("")

    out.append(f"UNRANKED ({len(report.unranked_pointers)}) -- declared uncertain, but no plausible "
               f"range or domain anywhere (sidecar or schema).")
    out.append("  NOT $0 -- not computed.")
    for pointer in report.unranked_pointers[:20]:
        out.append(f"  {pointer}")
    if len(report.unranked_pointers) > 20:
        out.append(f"  ... and {len(report.unranked_pointers) - 20} more")
    if not report.unranked_pointers:
        out.append("  (none)")
    out.append("")

    out.append(f"SKIPPED AS STRUCTURAL ({report.structural_skipped}) -- ids, labels, enums, dates and "
               f"other scaffolding.")
    out.append("  Not facts a household goes and looks up. Excluded by construction: a leaf is a")
    out.append(f"  candidate only if it opts in via an '{UNCERTAINTY_KEYWORD}' annotation (#671).")
    out.append("")

    if report.dropped_pointers:
        out.append(f"DROPPED ({len(report.dropped_pointers)}) -- cut by --max-leaves, never silently:")
        for pointer in report.dropped_pointers:
            out.append(f"  {pointer}")
        out.append("")

    out.append(f"(baseline objective score: ${report.baseline_score:,.0f})")
    return "\n".join(out)


# ═══════════════════════════════════════════════════════════════════════════
# 6. CLI -- voi.py owns its own entry point; optimize.py is untouched (#661)
# ═══════════════════════════════════════════════════════════════════════════

def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Value of information: rank every unresolved fact about a household by "
                    "how many dollars resolving it is worth (#661/#671)."
    )
    parser.add_argument("--input", required=True, help="Path to a contract JSON document.")
    parser.add_argument("--objective", choices=sorted(OBJECTIVES), default=None,
                        help=f"Objective to sweep (default: {DEFAULT_OBJECTIVE_NAME}).")
    parser.add_argument("--max-leaves", type=int, default=None,
                        help="Cap the candidates swept. Whatever is dropped is printed, never silently.")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel workers for the simulation runs.")
    parser.add_argument("--no-cross-objective", action="store_true",
                        help="Skip the check of which OTHER objectives price a leaf that is inert under "
                             "the swept one. The report then says so, rather than implying $0.")
    args = parser.parse_args(argv)

    with open(args.input) as fh:
        doc = json.load(fh)

    objective = OBJECTIVES[args.objective] if args.objective else None
    report = sweep(
        doc,
        objective=objective,
        max_leaves=args.max_leaves,
        jobs=args.jobs,
        cross_objective=not args.no_cross_objective,
    )
    print(render_report(report))


if __name__ == "__main__":
    main()
