"""Contract reachability: every key the input contract accepts must provably
reach the engine (issues #713, #714; DP#14, DP#18, DP#32).

## The defect family

This repo's founding thesis: *the engine silently substitutes zero*. A user
declares something, the adapter parses it, and then **nothing reads it** -- the
run completes, the tests pass, and a confident wrong number gets printed.
#652, #653, #665 and #670 were all this bug. #713 and #714 are two more.

## Why the EXISTING guards did not catch #713 or #714

There were, before this file, three overlapping registries claiming to track
what the contract declares but the engine ignores:

1. ``tests/test_schema_coverage.py``'s ``DEAD_ALLOWLIST`` / ``CONSUMED``.
2. ``input_contract.UNMAPPED_CONTRACT_KEYS`` (deleted by this PR -- #644,
   DP#9: it was a second spelling of the same rule, and it had drifted into
   asserting things that were false).
3. ``tests/architecture/test_dp_income_scenario_reaches_engine.py`` -- the
   right idea (#665), but hand-written for exactly one block.

Every one of them asserts reachability **in prose, checked by a keyword
grep**. That is the flaw. A ``DEAD_ALLOWLIST`` entry is a *claim* that a leaf
is dead, and nothing ever re-checked the claim against the code; a ``CONSUMED``
entry is a *claim* that a leaf is alive, checked only by "does this keyword
string still appear in that file" -- which stays green when the keyword is a
``for`` loop that iterates a list without ever reading the leaf in question
(see ``people[].relationships[].from``, corrected in this PR), or when one
generic helper's keyword vouches for six different kinds at once (see
``accounts[kind=resp].owner.joint[].pct``, likewise).

So both #713 and #714 sat *inside* ``DEAD_ALLOWLIST``, with a paragraph of
justification each, and the build was green. Growing an allowlist to make the
build go green is exactly how the original bugs got in (AGENTS.md).

## What this file does instead: it MEASURES

No prose. For every leaf of the contract that carries a **number, a boolean or
a date**, build a second document differing *only* at that leaf and run it
through the real adapter. Then ask two questions -- the same two hops a value
must survive, which is precisely where the codebase's own history says it dies
("Parsed, mapped, then never passed", AGENTS.md):

    HOP 1 -- does the value reach the internal config at all?
        Mutate the leaf; run ``input_contract.to_internal_config``; diff the
        result. No diff => the adapter DROPPED it. This is #713: the whole
        ``decisions.contribution_strategy[]`` block was parsed by the schema
        and then never mapped, so a user's authored savings strategy never
        reached the optimizer.

    HOP 2 -- does the engine read the container it lands in?
        A value can reach the config and still evaporate, by landing on a key
        nothing downstream ever names. This is #714: ``study_periods`` was
        mapped onto ``child['study_periods']`` and no production file outside
        the adapter mentioned that key, so RESP wind-down used the *global*
        ``assumptions.resp.study_start_age`` for every child instead.

HOP 2 is a static property of the source, not of any fixture: if no production
file NAMES the container, the write is dead for every possible document. That
makes ``test_no_dead_writes`` sound -- it has no allowlist at all, and it must
stay empty.

## Honest limits of the instrument (stated, not hidden)

- **It probes numbers, booleans and dates.** Strings/enums/ids are not safely
  mutable (an id is referential; an enum has a closed domain), so they remain
  covered by ``test_schema_coverage.py``'s citation registry. The probe-able
  types are exactly the ones that can carry a silently-substituted zero.
- **"The engine names this key" is necessary, not sufficient.** A key named
  only by a pure dict->dataclass loader is *parsed*, not *consumed*. So HOP 2
  proves deadness, never liveness; the citation registry still carries the
  liveness claim. (``simulation_config.py`` -- the pure loader -- is excluded
  from the reader set for exactly this reason, the same exclusion
  ``test_schema_coverage.py`` makes.)
- **A DROPPED verdict is relative to the probe document.** A leaf whose code
  path this one household never exercises can look dead when it is not -- e.g.
  ``estate.rollover_overrides[].spousal_rollover``, whose only override in
  ``schema/example.json`` names an account owned by the *survivor*, so it can
  never bind. Those are listed, with the reason, in
  ``NOT_EXERCISED_BY_EXAMPLE`` -- a short, reviewed list, not a silent bag.
"""
from __future__ import annotations

import ast
import copy
import json
import logging
import os
import re
import sys
import unittest
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
import repo_scan
from test_dp_income_scenario_reaches_engine import _two_generation_subset

sys.path.insert(0, os.path.join(repo_scan.ROOT, "tests"))
import test_schema_coverage as tsc


# ═══════════════════════════════════════════════════════════════════════════
# The declared exceptions. Both lists are SHORT, issue-linked, and
# mechanically re-verified below -- a stale entry fails the build just as
# loudly as a missing one.
# ═══════════════════════════════════════════════════════════════════════════

# A `decisions.*` leaf is the user's authored CHOICE -- the thing DP#5 calls an
# anchor and the thing the optimizer exists to rank. A decision the engine
# never sees is not a decision; it is a lie told to the user. So the rule is:
# every probe-able decisions.* leaf must reach the internal config. These are
# the remaining exceptions, each a known, filed gap -- and each one is a
# standing invitation to delete it by wiring the block for real.
DECISIONS_NOT_WIRED = {
    "decisions.estate_elections[].spousal_rollover": (
        "#600",
        "decisions.estate_elections[] has no consumer at all: the estate's rollover election "
        "is taken from estate.default_spousal_rollover + estate.rollover_overrides[] (both "
        "WIRED, and both measured as reaching), never from this parallel decisions-side block. "
        "Two spellings of one fact (DP#9); the estate.* one wins. This block should be deleted "
        "or wired -- not left as a decision the engine cannot see.",
    ),
    "decisions.mortgage.refinance_options[].ltv": (
        "#601",
        "scenario_discovery._convert_refinance_scenarios DERIVES ltv from cash_out and "
        "house_value ((mortgage_balance + cash_out) / house_value) instead of reading the "
        "stated one, so a declared ltv that disagrees with its own cash_out is silently "
        "ignored. Two spellings of one fact (#595); the derived one wins.",
    ),
    "decisions.resp_action[].cash_out": (
        "#603",
        "to_internal_config's resp_action_scenarios mapping carries only id/label; cash_out is "
        "dropped at the adapter, so a declared RESP cash-out amount never reaches the engine.",
    ),
}

# Leaves the canonical example document cannot exercise, so a DROPPED verdict
# on them says something about the FIXTURE, not about the code. Each one is
# verified by reading the consumer, and each names why the fixture cannot
# reach it. This is the one place the oracle defers to a human reading -- so
# it is kept as small as the evidence allows.
NOT_EXERCISED_BY_EXAMPLE = {
    "estate.rollover_overrides[].spousal_rollover": (
        "#600",
        "GENUINELY CONSUMED (input_contract._weighted_rolled_fraction's "
        "`rolls = overrides[acc['id']] ...`), but schema/example.json's only override names "
        "`spousal_rrsp_p2`, an account owned by p2 -- while assumptions.mortality makes p1 the "
        "first to die. Only the FIRST-TO-DIE's accounts can roll, so this override can never "
        "bind in this document and flipping it moves nothing. A fixture limit, not a dead key.",
    ),
    "properties[].designated_principal_residence_years[].from": (
        "#695",
        "GENUINELY CONSUMED (input_contract._map_pre_property_gains -> "
        "countries.canada.pre_designation reads each period's from/to to count a property's "
        "designated years and set its per-property taxable fraction, ITA s.40(2)(b)). But the "
        "per-year PRE allocation only ENGAGES when the COUPLE owns two or more properties: with a "
        "single property there is no exemption to contest, so the legacy presence-flag path runs "
        "and the ranges move nothing. In the two-generation subset the couple (p1/p2) owns only "
        "the principal residence -- the cottage is the GRANDPARENTS' -- so this fixture cannot "
        "reach the allocation. A fixture limit, not a dead key (test_issue_695 exercises it).",
    ),
    # NB: the sibling `.to` leaf is `null` in the shipped example (an open, still-
    # in-effect designation), so the oracle cannot mutate it (its probe set is
    # numbers/booleans/dates) -- it is UNPROBEABLE here, not "dropped", so it does
    # NOT belong in this list; its CONSUMED citation stands on its own.
}


# ═══════════════════════════════════════════════════════════════════════════
# The oracle
# ═══════════════════════════════════════════════════════════════════════════

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# The pure dict->dataclass parser. A citation inside it proves parsing, not
# consumption (DP#32) -- the same exclusion tests/test_schema_coverage.py
# makes for the same reason. input_contract.py is the WRITER: it is excluded
# from the reader set because a key the adapter writes and only the adapter
# names is, by definition, a dead write.
_WRITER = {"input_contract.py"}
_PURE_LOADER = {"simulation_config.py"}


def _mutations(value: Any) -> List[Any]:
    """Schema-plausible, materially different values for a probe-able leaf.

    Several candidates per leaf on purpose: a window/lapse/eligibility gate
    only reacts when the mutation CROSSES its boundary, so a single nudge can
    make a live leaf look dead (this is not hypothetical -- an earlier version
    of this oracle wrongly accused ``estate.life_insurance[].term_end_date``,
    whose lapse check simply never saw a date on the far side of the horizon).
    """
    if isinstance(value, bool):
        return [not value]
    if isinstance(value, (int, float)):
        if 0.0 <= value <= 1.0:  # a rate/percent: stay inside the domain
            return [round(0.5 - value / 2 + 0.113, 4), 0.0, 1.0]
        return [value * 1.37 + 11, value * 0.31 + 3, 0]
    if isinstance(value, str) and _DATE_RE.match(value):
        return [str(int(value[:4]) + d) + value[4:] for d in (-8, 9, 22, 60, -40)]
    return []


def _iter_doc_leaves(node: Any, path: List, out: List) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if not key.startswith("$"):
                _iter_doc_leaves(val, path + [key], out)
    elif isinstance(node, list):
        for i, val in enumerate(node):
            _iter_doc_leaves(val, path + [i], out)
    else:
        out.append((list(path), node))


def _leaf_name(pointer: List, doc: Dict) -> str:
    """The kind-aware dotted name test_schema_coverage.py uses, so the two
    registries speak the same language (#647: a bare ``accounts[].balance``
    silently re-certifies all twelve kinds from one citation)."""
    parts: List[str] = []
    cur: Any = doc
    for seg in pointer:
        if isinstance(seg, int):
            parent = parts[-1]
            item = cur[seg]
            if parent in ("accounts", "liabilities") and isinstance(item, dict) and "kind" in item:
                parts[-1] = f"{parent}[kind={item['kind']}]"
            else:
                parts[-1] = parent + "[]"
        else:
            parts.append(seg)
        cur = cur[seg]
    return ".".join(parts)


def _set_at(doc: Dict, pointer: List, value: Any) -> None:
    cur = doc
    for seg in pointer[:-1]:
        cur = cur[seg]
    cur[pointer[-1]] = value


def _flatten(node: Any, path: Tuple, out: Dict) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            _flatten(val, path + (str(key),), out)
    elif isinstance(node, list):
        for i, val in enumerate(node):
            _flatten(val, path + (str(i),), out)
    else:
        out[path] = node


def _internal_config(doc: Dict) -> Dict[Tuple, Any]:
    flat: Dict[Tuple, Any] = {}
    _flatten(ic.to_internal_config(doc), (), flat)
    return flat


def _names_used_in_code(tree: ast.AST) -> set:
    """Every identifier/string a module actually USES. Comments are not in the
    AST at all, and docstrings/bare string statements are skipped -- so a key
    merely *mentioned in a comment* can never masquerade as a reader. (It very
    nearly did: an early version of this oracle counted `study_periods`'s own
    obituary, written in a comment, as proof that something read it.)
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue  # docstring / bare string literal: not code
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _build_reader_index() -> Tuple[Dict[str, str], set]:
    """(names the ENGINE reads -> the file, names ANY source file mentions)."""
    readers: Dict[str, str] = {}
    anywhere: set = set()
    for rel in repo_scan.iter_source_files(repo_scan.ROOT):
        try:
            with open(os.path.join(repo_scan.ROOT, rel), encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
        except SyntaxError:
            continue
        used = _names_used_in_code(tree)
        anywhere |= used
        if os.path.basename(rel) in _WRITER | _PURE_LOADER:
            continue
        for name in used:
            readers.setdefault(name, rel)
    return readers, anywhere


_READERS, _ANY_SOURCE = _build_reader_index()


def _unread_containers(path: Tuple) -> List[str]:
    """Containers on this internal path that the engine never names.

    To reach a nested value the engine must NAME every container on the way
    down. A segment no source file mentions AT ALL is a dynamic map key (a
    product name, an RFC-6901 pointer) -- unnameable by construction, so
    exempt. A segment the WRITER names but no READER names is a dead write.
    """
    return [
        seg for seg in path
        if not seg.isdigit() and seg not in _READERS and seg in _ANY_SOURCE
    ]


class Verdicts:
    """One sweep of the whole contract, shared by every gate below."""

    def __init__(self) -> None:
        logging.disable(logging.WARNING)  # the adapter warns about far-future tax years
        try:
            with open(ic.EXAMPLE_PATH) as fh:
                doc = json.load(fh)
            # #598: to_internal_config REFUSES a 4-generation document outright
            # (loudly -- which is correct). The two-generation couple is the
            # document this engine can actually run.
            self.doc = _two_generation_subset(doc)
            base = _internal_config(self.doc)

            leaves: List = []
            _iter_doc_leaves(self.doc, [], leaves)

            # provenance.* is a PARALLEL sidecar (provenance.py, #660),
            # deliberately outside to_internal_config by design -- an
            # internal-config observable is structurally blind to it, so a
            # DROPPED verdict there would say nothing. Its own guard is
            # tests/test_provenance.py.
            #
            # sensitivity.sweeps.* (#771) is the SAME shape: sweep.py reads the
            # sweep axes off the contract DOCUMENT directly and re-maps ONE
            # config PER swept value -- the base document's to_internal_config
            # never reflects the sweeps block (it is the per-value iteration
            # that maps, not the base), so a single-document internal-config
            # diff is structurally blind to it, exactly as it is to provenance.
            # Its own behavioural guard is
            # tests/test_issue_771_generalized_sweeps.py.
            def _consumed_outside_the_adapter(p: List) -> bool:
                return (bool(p) and p[0] == "provenance") or p[:2] == ["sensitivity", "sweeps"]
            leaves = [(p, v) for (p, v) in leaves if not _consumed_outside_the_adapter(p)]

            self.reaches: Dict[str, Tuple] = {}
            self.dropped: set = set()
            self.dead_writes: Dict[str, List[str]] = {}
            self.unprobeable: set = set()

            moved: Dict[str, set] = {}
            probed: set = set()
            for pointer, value in leaves:
                name = _leaf_name(pointer, self.doc)
                moved.setdefault(name, set())
                for new in [m for m in _mutations(value) if m != value]:
                    mutant = copy.deepcopy(self.doc)
                    _set_at(mutant, pointer, new)
                    try:
                        after = _internal_config(mutant)
                    except Exception:
                        continue  # mutation the adapter refuses: not a probe
                    delta = {
                        k for k in set(base) | set(after)
                        if base.get(k, _ABSENT) != after.get(k, _ABSENT)
                    }
                    if delta:
                        # A change proves reach; no need to prove the mutation
                        # was schema-legal (the safe direction -- it can only
                        # ever ACQUIT a leaf, never accuse one).
                        probed.add(name)
                        moved[name] |= delta
                        continue
                    # No change => this is the ACCUSATION path, so now it must
                    # be proven that a real user could have authored this value.
                    try:
                        ic.validate_contract(mutant)
                    except Exception:
                        continue  # illegal value: proves nothing
                    probed.add(name)

            for name, delta in moved.items():
                if name not in probed:
                    self.unprobeable.add(name)
                elif not delta:
                    self.dropped.add(name)
                else:
                    live = [p for p in delta if not _unread_containers(p)]
                    if live:
                        self.reaches[name] = sorted(live)[0]
                    else:
                        worst = sorted(delta)[0]
                        self.dead_writes[name] = _unread_containers(worst)
        finally:
            logging.disable(logging.NOTSET)


class _Absent:
    def __repr__(self) -> str:
        return "<absent>"


_ABSENT = _Absent()

VERDICTS = Verdicts()


def _runnable_config() -> Dict:
    return ic.to_internal_config(copy.deepcopy(VERDICTS.doc))


# ═══════════════════════════════════════════════════════════════════════════
# The gates
# ═══════════════════════════════════════════════════════════════════════════

class NoDeadWritesTest(unittest.TestCase):
    """GATE 1 (#714). A contract value that reaches the internal config and
    lands on a key NO production file ever names has evaporated -- DP#18's
    dead write, at the config-adapter boundary instead of the overlay one.

    There is deliberately NO allowlist here. Unlike a DROPPED verdict (which
    is relative to the probe document), this one is a static property of the
    source: if nothing names the container, the write is dead for EVERY
    possible document. So the only honest size for this list is zero.
    """

    def test_no_dead_writes(self):
        self.assertEqual(
            {}, VERDICTS.dead_writes,
            "Contract value(s) mapped onto an internal key NO production code "
            "reads -- parsed, mapped, then never passed (AGENTS.md). Wire the "
            "consumer, or stop mapping the key; do NOT add an allowlist:\n"
            + "\n".join(
                f"  {leaf}: nothing reads container(s) {containers}"
                for leaf, containers in sorted(VERDICTS.dead_writes.items())
            ),
        )


class EveryDecisionReachesTheEngineTest(unittest.TestCase):
    """GATE 2 (#713). DP#5: decisions are the user's anchors, and the optimizer
    exists to rank them. A ``decisions.*`` leaf the adapter drops is a choice
    the user made and the engine never saw.

    Why this gate asks "does it reach the config" and not "does it move the
    optimizer's ranked output" -- the stronger-sounding question I tried first
    and threw away:

    A "the output did not move, therefore the key is dead" accusation is only
    sound if the consumer was actually REACHABLE in the probe household. It
    frequently is not. ``allocation.spousal_rrsp_pct`` is genuinely read by
    ``StrategyEngine.allocate`` -- behind ``if state.bracket_gap >
    s.min_bracket_gap``, which this couple does not clear, so no mutation of it
    can move a number. ``allocation.resp_pct`` is read, and then capped by the
    CESG match, which binds. ``decisions.income[].overrides[].amount`` reaches
    the engine through ``run_income_scenario_exploration``, an entry point
    ``run_optimization`` never calls. An output-level probe calls all three
    dead. All three are alive, and #665 already proved the last one is.

    So the accusation is made where it can be defended: at the ADAPTER. "The
    user's value never reached the internal config" needs no assumption about
    which downstream branch a household takes -- and that is exactly, and
    literally, what was wrong in #713.
    """

    def test_every_decision_reaches_the_engine(self):
        dropped = {
            leaf for leaf in VERDICTS.dropped | set(VERDICTS.dead_writes)
            if leaf.startswith("decisions.")
        }
        undeclared = dropped - set(DECISIONS_NOT_WIRED)
        self.assertFalse(
            undeclared,
            "decisions.* leaf/leaves the engine NEVER SEES: the user authored a "
            "choice, the schema accepted it, and to_internal_config dropped it "
            "on the floor. Wire it to the optimizer (or, if it is genuinely "
            "unimplementable today, add it to DECISIONS_NOT_WIRED with an issue "
            "number and a reason):\n  " + "\n  ".join(sorted(undeclared)),
        )

    def test_decisions_not_wired_has_no_stale_entries(self):
        """The list can only shrink. An entry that has since been wired must be
        DELETED, not left behind to rot -- that rot is #644 exactly."""
        dropped = VERDICTS.dropped | set(VERDICTS.dead_writes)
        stale = set(DECISIONS_NOT_WIRED) - dropped
        self.assertFalse(
            stale,
            "DECISIONS_NOT_WIRED entries that now REACH the engine (or no longer "
            "exist). They are fixed -- delete the entry so the list keeps telling "
            f"the truth: {sorted(stale)}",
        )


class AuthoredStrategiesAreRankedTest(unittest.TestCase):
    """#713, end to end, on the real optimizer -- the POSITIVE half of the
    claim, and the one the issue actually asks for ("a user-authored
    contribution_strategy[].allocation reaches the optimizer and is honored").

    This is an EXISTENCE assertion (every declared strategy appears in the
    ranking), not a "nothing moved, therefore dead" one -- so it is immune to
    the conditional-gating problem described above, and it is the same shape as
    #665's test for decisions.income[].

    Before the fix, the ranked table a user got back contained the engine's
    built-in `balanced`/`rrsp_max` -- never the strategies they wrote down.
    """

    def test_every_authored_strategy_appears_in_the_ranked_output(self):
        import optimize

        declared = {s["id"] for s in VERDICTS.doc["decisions"]["contribution_strategy"]}
        self.assertTrue(declared, "fixture declares no strategies -- this test proves nothing")

        logging.disable(logging.WARNING)
        try:
            rows = optimize.run_optimization(_runnable_config())
        finally:
            logging.disable(logging.NOTSET)
        ranked = {str(r.get("strategy")) for r in rows}

        missing = declared - ranked
        self.assertFalse(
            missing,
            f"strategies the household AUTHORED that the optimizer never ranked: "
            f"{sorted(missing)}. Declared={sorted(declared)}, ranked={sorted(ranked)} "
            f"(#713: parsed by the schema, dropped by the adapter, so the ranking "
            f"was of the engine's built-in strategies instead of the user's).",
        )

    def test_the_engines_builtin_default_no_longer_displaces_them(self):
        """The other half: once a household authors strategies, the built-in
        default set must not still be what gets ranked (DP#13 -- a default is a
        fallback for ABSENT input, never a way to overrule a supplied one)."""
        import optimize

        logging.disable(logging.WARNING)
        try:
            rows = optimize.run_optimization(_runnable_config())
        finally:
            logging.disable(logging.NOTSET)
        ranked = {str(r.get("strategy")) for r in rows}

        declared = {s["id"] for s in VERDICTS.doc["decisions"]["contribution_strategy"]}
        self.assertNotIn(
            "rrsp_max", ranked - declared,
            "the optimizer is still ranking its built-in 'rrsp_max' strategy even "
            "though this household authored its own set -- custom_strategies is "
            "not reaching discover_strategies (#713).",
        )

    def test_decisions_not_wired_entries_cite_an_issue_and_a_reason(self):
        for leaf, value in DECISIONS_NOT_WIRED.items():
            self.assertEqual(2, len(value), f"{leaf}: must be (issue, reason)")
            issue, reason = value
            self.assertRegex(issue, r"^#\d+$", f"{leaf}: must cite a '#NNN' issue")
            self.assertGreaterEqual(
                len(reason.strip()), 40,
                f"{leaf}: a one-word excuse is how #713 hid for so long -- "
                f"say what the engine reads INSTEAD, and why",
            )


class ConsumedCitationsAreBehaviourallyTrueTest(unittest.TestCase):
    """GATE 3 (#644). A ``CONSUMED`` citation is a claim that a leaf reaches a
    decision, and until now it was checked only by "does this keyword still
    appear in that file" -- which stays green for a citation that points at a
    ``for`` loop iterating a list it never reads a field of.

    This re-checks every citation against the ENGINE: a leaf the adapter
    provably never maps cannot be CONSUMED, whatever the keyword says.
    """

    def test_no_consumed_citation_names_an_unreachable_leaf(self):
        liars = {
            leaf for leaf in VERDICTS.dropped | set(VERDICTS.dead_writes)
            if leaf in tsc.CONSUMED
        } - set(NOT_EXERCISED_BY_EXAMPLE)
        self.assertFalse(
            liars,
            "test_schema_coverage.CONSUMED cites these leaves as reaching a "
            "decision, but mutating them provably moves NOTHING in the internal "
            "config. The citation passes for the wrong reason (it names a line "
            "that iterates the block without reading the leaf, or one generic "
            "helper vouching for several kinds at once). Move each to "
            "DEAD_ALLOWLIST, or cite the line that really reads it:\n  "
            + "\n  ".join(f"{k} -> cited to {tsc.CONSUMED[k]}" for k in sorted(liars)),
        )

    def test_not_exercised_list_has_no_stale_entries(self):
        dropped = VERDICTS.dropped | set(VERDICTS.dead_writes)
        stale = set(NOT_EXERCISED_BY_EXAMPLE) - dropped
        self.assertFalse(
            stale,
            "NOT_EXERCISED_BY_EXAMPLE entries the oracle now measures as "
            f"REACHING -- the fixture got stronger; delete the entry: {sorted(stale)}",
        )

    def test_not_exercised_entries_cite_an_issue_and_a_reason(self):
        for leaf, value in NOT_EXERCISED_BY_EXAMPLE.items():
            self.assertEqual(2, len(value), f"{leaf}: must be (issue, reason)")
            issue, reason = value
            self.assertRegex(issue, r"^#\d+$", f"{leaf}: must cite a '#NNN' issue")
            self.assertGreaterEqual(
                len(reason.strip()), 40,
                f"{leaf}: name the consumer AND why this document cannot reach it",
            )


class OracleIsActuallyLookingTest(unittest.TestCase):
    """A detector that cannot fail has never been tested. These are the
    instrument's own vital signs: if the probe silently stopped probing, every
    gate above would go green for the worst possible reason."""

    def test_the_sweep_probed_a_meaningful_number_of_leaves(self):
        probed = len(VERDICTS.reaches) + len(VERDICTS.dropped) + len(VERDICTS.dead_writes)
        self.assertGreater(
            probed, 100,
            f"the oracle probed only {probed} leaves -- it has gone blind "
            f"(schema/example.json shrank, or the mutation set stopped matching "
            f"the schema's types). Every gate in this file is worthless now.",
        )

    def test_known_live_leaves_are_measured_as_reaching(self):
        """Positive controls. If mutating a mortgage rate or a salary does not
        move the config, the oracle is broken -- not the engine."""
        for leaf in (
            "liabilities[kind=mortgage].rate",
            "people[].incomes[].amount",
            "properties[].value.amount",
            "assumptions.savings_rate",
        ):
            self.assertIn(
                leaf, VERDICTS.reaches,
                f"positive control {leaf!r} measured as NOT reaching the engine "
                f"-- the oracle is broken, or something very large just regressed",
            )

    def test_every_leaf_lands_in_exactly_one_bucket(self):
        buckets = [set(VERDICTS.reaches), VERDICTS.dropped, set(VERDICTS.dead_writes)]
        for i, a in enumerate(buckets):
            for b in buckets[i + 1:]:
                self.assertFalse(a & b, f"leaf in two buckets at once: {sorted(a & b)}")


# ═══════════════════════════════════════════════════════════════════════════
# GATE 4 (#763): every liability_kind the schema accepts is consumed by the
# engine OR refused loudly -- never silently dropped.
# ═══════════════════════════════════════════════════════════════════════════

_SCHEMA_ENUM = json.loads(ic.UNIVERSAL_SCHEMA_PATH.read_text())[
    "$defs"]["liability_kind"]["enum"]

# Kinds whose material facts reach the engine through the property block
# (wired before #763). Their reachability is already measured by the oracle
# on the shipped example; this gate re-asserts it on a dedicated per-kind
# fixture so a regression in any one kind's wiring fails HERE, not only via
# the example's incidental coverage.
_PROPERTY_WIRED = {
    "mortgage": lambda cfg, liab: cfg["property"].get("mortgage_balance")
                                    == liab["balance"]["amount"],
    "heloc": lambda cfg, liab: cfg["property"].get("heloc_rate") == liab["rate"],
    "line_of_credit": lambda cfg, liab: cfg["property"].get("credit_facility_limit")
                                       == liab["limit"],
}
# Closed-end consumer kinds wired by #763 into the consumer_loans list.
_CONSUMER_WIRED = {"car_loan", "student_loan", "personal_loan"}


def _valid_liability_for_kind(kind: str) -> Dict:
    """A schema-valid liability of `kind` with fabricated round-number facts."""
    liab: Dict[str, Any] = {
        "id": f"{kind}_gate",
        "owner": "p1",
        "kind": kind,
        "balance": {"amount": 12_000, "as_of": "2026-01-01"},
        "rate": 0.06,
        "rate_type": "fixed",
        "collateral": None,
    }
    if kind in ("heloc", "line_of_credit"):
        liab["limit"] = 25_000
    if kind == "heloc":
        liab["readvanceable"] = False
        liab["capitalize_interest"] = False
    if kind == "mortgage":
        liab["collateral"] = "principal_residence"
        liab["amortization"] = {"years": 20, "payment_monthly": 800}
        liab["renewal_date"] = "2029-01-01"
        liab["term_start_date"] = "2024-01-01"
    if kind in ("car_loan", "student_loan", "personal_loan", "intergenerational_loan"):
        liab["amortization"] = {"years": 4, "payment_monthly": 280}
    return liab


class EveryLiabilityKindIsConsumedOrRefusedTest(unittest.TestCase):
    """GATE 4 (#763). The schema's ``$defs.liability_kind`` enum is the set of
    debt kinds a user may declare. Every one of them must, at the ONE contract
    loading boundary, either reach the engine's internal config (consumed) or
    raise ``ContractAdaptationError`` (refused loudly). The third state --
    parsed, schema-validated, accepted, then silently dropped before the
    engine sees it -- is exactly the founding defect this repo exists to kill
    (DP#32), and it is what happened to car_loan/student_loan/personal_loan
    before this gate's fix.

    This generalizes the per-kind reachability the oracle measures on the
    shipped example to the WHOLE enum (the example does not carry every kind),
    so a new kind added to the enum without a mapping or an explicit refusal
    fails the build here -- not silently ships as a dropped liability.
    """

    def _doc_with_one_liability(self, kind: str) -> Dict:
        doc = _two_generation_subset(json.loads(ic.EXAMPLE_PATH.read_text()))
        liab = _valid_liability_for_kind(kind)
        # Keep the example's mortgage only when testing a NON-mortgage kind,
        # so a mortgage kind fixture replaces it rather than colliding.
        if kind == "mortgage":
            doc["liabilities"] = [liab]
        else:
            doc["liabilities"] = [
                l for l in doc["liabilities"] if l["kind"] == "mortgage"
            ] + [liab]
        return doc

    def test_every_schema_liability_kind_is_consumed_or_refused(self):
        logging.disable(logging.WARNING)
        try:
            for kind in _SCHEMA_ENUM:
                doc = self._doc_with_one_liability(kind)
                liab = next(l for l in doc["liabilities"] if l["kind"] == kind)
                try:
                    cfg = ic.to_internal_config(doc)
                except ic.ContractAdaptationError:
                    # Refused loudly is an acceptable terminal state (DP#32) --
                    # the kind is not silently dropped. intergenerational_loan
                    # is the current refusal (#703 lender field).
                    continue
                except ic.ContractValidationError as e:
                    self.fail(
                        f"liability kind {kind!r} produced a schema validation "
                        f"error from a fixture this gate believes is valid -- "
                        f"fix _valid_liability_for_kind: {e}")
                # Loaded: assert the kind's material facts REACHED the config.
                if kind in _CONSUMER_WIRED:
                    kinds = [c["kind"] for c in cfg.get("consumer_loans", [])]
                    self.assertIn(
                        kind, kinds,
                        f"liability kind {kind!r} loaded but is NOT in the "
                        f"engine's consumer_loans list -- silently dropped (#763).")
                elif kind in _PROPERTY_WIRED:
                    self.assertTrue(
                        _PROPERTY_WIRED[kind](cfg, liab),
                        f"liability kind {kind!r} loaded but its material fact "
                        f"did not reach the property block -- silently dropped.")
                else:
                    self.fail(
                        f"liability kind {kind!r} is in the schema enum but this "
                        f"gate does not know whether it should be consumed or "
                        f"refused -- add it (a new kind must be wired or refused, "
                        f"never silently dropped, DP#32/#763).")
        finally:
            logging.disable(logging.NOTSET)

    def test_the_gate_covers_the_full_enum(self):
        """A detector that cannot fail has never been tested. If the enum grew
        and the gate's kind sets were not updated, test_every_schema_liability
        _kind_is_consumed_or_refused's `else` branch is the backstop -- this
        assertion makes the coverage expectation explicit so it is not missed."""
        self.assertEqual(
            set(_SCHEMA_ENUM),
            set(_PROPERTY_WIRED) | _CONSUMER_WIRED | {"intergenerational_loan"},
            "_SCHEMA_ENUM changed but this gate's kind sets were not updated. "
            "Every new liability_kind must be added to _PROPERTY_WIRED, "
            "_CONSUMER_WIRED, or asserted as refused here (DP#32/#763).",
        )


# ═══════════════════════════════════════════════════════════════════════════
# GATE 5 (#841 bite 1): a CHILD is a first-class savings subject. A child-owned
# registered account (rrsp/tfsa/fhsa) must provably REACH the internal config,
# attributed to that child -- or, when its owner resolves to no declared
# person, be REFUSED loudly. Never silently dropped (DP#32).
#
# The shipped example carries no child-owned account (the golden household has
# no child savers, so bite 1 is a no-op for it and the oracle above never
# probes this path). So, exactly like GATE 4, this gate builds a DEDICATED
# fixture -- one child-owned account per kind -- and measures reach directly at
# the adapter boundary, rather than relying on the example's incidental
# coverage. That is what makes "a declared child-owned account reaches the
# engine" a provable, regression-guarded fact instead of a claim.
# ═══════════════════════════════════════════════════════════════════════════

# The registered kinds a child may own that #841 bite 1 promotes to a per-child
# savings subject. spousal_rrsp is deliberately excluded: it is structurally
# tied to the household spouse's RRIF minimum and is still refused for a child
# owner (a child cannot own a spousal RRSP), covered in test_input_contract.py.
_CHILD_SAVER_KINDS = ("rrsp", "tfsa", "fhsa")


def _child_owned_account(kind: str, owner: str) -> Dict:
    """A schema-valid child-owned account of `kind` with fabricated round
    facts (DP#15: no personal data)."""
    acc: Dict[str, Any] = {
        "id": f"{owner}_{kind}",
        "owner": owner,
        "kind": kind,
        "balance": {"amount": 3_000, "as_of": "2026-01-01"},
        "acb": None,
        "holdings": [],
        "beneficiary": None,
        "successor_holder": None,
    }
    if kind == "fhsa":
        acc["fhsa"] = {"opened_date": "2025-06-01", "first_time_buyer_since": "2025-06-01"}
    return acc


class ChildOwnedAccountReachesTheEngineTest(unittest.TestCase):
    """GATE 5 (#841 bite 1). Every registered kind a child may own reaches the
    child's own per-member dict in the internal config; an account owned by no
    declared person is refused loudly. Same shape as GATE 4's per-kind gate."""

    def _base_doc(self) -> Dict:
        # The two-generation subset keeps the couple p1/p2 AND their children
        # ca/cb, so "ca" is a declared child this document can attribute to.
        return copy.deepcopy(VERDICTS.doc)

    def test_a_child_owned_account_reaches_that_child(self):
        logging.disable(logging.WARNING)
        try:
            for kind in _CHILD_SAVER_KINDS:
                doc = self._base_doc()
                doc["accounts"].append(_child_owned_account(kind, "ca"))
                cfg = ic.to_internal_config(doc)
                child = next(
                    (c for c in cfg["family"]["children"] if c["id"] == "ca"), None)
                self.assertIsNotNone(
                    child, f"child 'ca' vanished from the config for kind={kind!r}")
                self.assertEqual(
                    child.get(f"{kind}_balance"), 3_000,
                    f"a declared child-owned {kind!r} account (owner=ca) did NOT "
                    f"reach the child's per-member dict -- parsed, then dropped "
                    f"(#841 bite 1 / DP#32). Child config: {child}",
                )
        finally:
            logging.disable(logging.NOTSET)

    def test_an_account_owned_by_no_declared_person_is_refused(self):
        """DP#32: the child-saver promotion must not silently absorb a balance
        owned by nobody the document declares. `owner` matches the person_id
        pattern but names no declared person -- it must refuse, not vanish."""
        doc = self._base_doc()
        doc["accounts"].append(_child_owned_account("rrsp", "undeclared_owner"))
        logging.disable(logging.WARNING)
        try:
            with self.assertRaises(ic.ContractAdaptationError):
                ic.to_internal_config(doc)
        finally:
            logging.disable(logging.NOTSET)

    def test_the_fixture_actually_declares_a_child(self):
        """A detector that cannot fail has never been tested: if the probe
        document ever loses its children, this gate would pass vacuously."""
        cfg = ic.to_internal_config(self._base_doc())
        self.assertTrue(
            any(c["id"] == "ca" for c in cfg["family"]["children"]),
            "the probe document declares no child 'ca' -- this gate proves "
            "nothing; fix the fixture, do not weaken the gate.",
        )


if __name__ == "__main__":
    unittest.main()
