"""Enforcement for #671: the uncertainty model lives in the SCHEMA, so a
document with no provenance sidecar -- the new-user case epic #659 exists to
serve -- still gets a non-empty ranking.

**These tests deliberately do NOT need Track 1 (#660).** A document with no
``provenance`` block must work standalone, so this file exercises the whole
new-user path with the real schema and the real engine, and it runs in CI today
rather than skipping. (The one test that IS about the sidecar -- that document
provenance OVERRIDES the schema default -- lives in
``tests/test_voi_661.py`` behind an ``importorskip``.)

What is asserted here:

1. **The new-user path is not empty** (#671's headline defect). Before #671 a
   sidecar-less document ranked *nothing*: the engine was correct but had no
   fuel, and it failed hardest for the user it was built for.
2. **Identity/structural leaves are never candidates** -- and by construction
   (opt-in via ``x-uncertainty``), not by a blocklist naming them.
3. **A correct engine refusal is not a crash.** ``schema/example.json`` is a
   FOUR-generation household and ``to_internal_config`` rightly refuses it
   (#598/#643); VOI must report that as a first-class outcome. The repo's own
   flagship example is currently unsimulable, so the non-empty-ranking property
   above is asserted against a fabricated two-adults-plus-children fixture
   instead.
4. **The behavioural $0 findings are reconciled against #582's static dead-key
   analysis** (``tests/test_schema_coverage.py``: 150 dead / 173 consumed), in
   the only direction that is logically sound -- see
   ``test_behavioural_live_leaves_are_classified_consumed``.

All figures fabricated / round-numbered, role-based ids (DP#4/DP#15).
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import input_contract as ic
import voi


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _new_user_contract() -> dict:
    """The shipped example trimmed to the couple + their children -- the shape
    the Phase-1 engine can actually simulate (#598) -- with the ``provenance``
    sidecar REMOVED. This is the brand-new user: nothing measured, no sidecar,
    every leaf a guess."""
    with open(ic.EXAMPLE_PATH) as fh:
        doc = json.load(fh)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
    doc["accounts"] = [a for a in doc["accounts"] if _owner_ids(a["owner"]) <= keep]
    doc["liabilities"] = [l for l in doc["liabilities"] if _owner_ids(l["owner"]) <= keep]
    doc["properties"] = [p for p in doc["properties"] if _owner_ids(p["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}
    ]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep
    ]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep
    ]
    doc.pop("provenance", None)          # <- the whole point: no sidecar
    ic.validate_contract(doc)
    return doc


#: Four tests below (sections 1 & 4) sweep the SAME new-user contract under the
#: SAME arguments (default objective, jobs=4, cross_objective=False). ``voi.sweep``
#: is a pure function of its arguments (DP#3, verified in voi.py's docstring), so
#: the result is identical whether computed once or four times. Computing it once
#: per module (~120s -> ~25s) does not weaken any assertion: every test only
#: READS ``report``/``doc`` (no mutation), and ``voi.render_report`` is pure. The
#: fixture returns the ``(doc, report)`` pair so tests that map pointers via
#: ``_static_key(doc, ...)`` use the exact doc that produced the report.
#: Module-scoped so the single sweep is shared across the whole file.
@pytest.fixture(scope="module")
def _default_sweep():
    doc = _new_user_contract()
    report = voi.sweep(doc, jobs=4, cross_objective=False)
    return doc, report


# ═══════════════════════════════════════════════════════════════════════════
# 1. The new-user path has fuel
# ═══════════════════════════════════════════════════════════════════════════

def test_new_user_with_no_provenance_still_gets_a_non_empty_ranking(_default_sweep):
    """#671's headline: before the schema carried the uncertainty model, a
    document with no sidecar ranked NOTHING."""
    doc, report = _default_sweep
    assert "provenance" not in doc

    assert report.ranked, (
        "a document with no provenance sidecar ranked NOTHING -- the new-user path "
        "has no fuel (#671). Every uncertain leaf must inherit its plausible_range/"
        "domain from the schema's x-uncertainty annotation."
    )
    assert all(f.spread > 0 for f in report.ranked)
    # Every ranked row must be actionable: a question and something to go do.
    for f in report.ranked:
        assert f.question and f.resolved_by, f"{f.pointer} ranked without a question/resolved_by"


def test_the_top_ranked_questions_come_from_the_schema_not_the_document(_default_sweep):
    _doc, report = _default_sweep
    assert {f.spec_source for f in report.ranked} == {"schema"}


# ═══════════════════════════════════════════════════════════════════════════
# 2. Identity / structural leaves are never candidates
# ═══════════════════════════════════════════════════════════════════════════

IDENTITY_LEAF_NAMES = {
    "id", "label", "legal_name", "schema_version", "currency", "dollars",
    "country", "province", "type", "kind", "as_of", "description",
}


def test_no_identity_or_structural_leaf_is_ever_a_voi_candidate():
    doc = _new_user_contract()
    candidates, _unranked, structural = voi.collect_candidates(doc)

    leaked = [
        c.pointer for c in candidates
        if c.pointer.split("/")[-1] in IDENTITY_LEAF_NAMES
    ]
    assert not leaked, (
        "identity/structural leaves offered as things to 'go find out' (#671): "
        f"{leaked}. These are identifiers, enums and scaffolding, not facts a "
        "household looks up."
    )
    assert structural > 0, "the structural-skip count must be reported, not hidden"


def test_candidacy_is_opt_in_by_construction_not_a_blocklist():
    """Every candidate carries an uncertainty spec. Nothing is a candidate
    'unless blocked' -- the exclusion is structural, so a NEW identity field
    added to the schema tomorrow is excluded automatically."""
    doc = _new_user_contract()
    candidates, _, _ = voi.collect_candidates(doc)
    assert candidates
    for c in candidates:
        assert c.spec.sample_values(), f"{c.pointer} is a candidate with nothing to sweep"


def test_structural_count_plus_candidates_plus_unranked_covers_every_leaf():
    """No leaf silently vanishes between the three buckets."""
    doc = _new_user_contract()
    candidates, unranked, structural = voi.collect_candidates(doc)
    total_leaves = sum(1 for _ in voi._walk(doc))
    assert len(candidates) + len(unranked) + structural == total_leaves


# ═══════════════════════════════════════════════════════════════════════════
# 2b. UNRANKED: declared an unknown, but with no width declared anywhere
# ═══════════════════════════════════════════════════════════════════════════

def test_a_leaf_declared_uncertain_with_no_range_is_unranked_never_zero():
    """An ``x-uncertainty`` annotation that names the question but declares no
    ``plausible_range``/``domain`` cannot be priced. That leaf must be reported
    UNRANKED -- a finding that the *annotation* is incomplete -- and must never
    be rendered as $0 (DP#32: absence is not zero).

    ``resolvability`` IS supplied here (#673 requires it on every annotation,
    width-less or not -- see ``test_voi_673_belief_vs_fact.py`` for that
    enforcement); this test is only about the ORTHOGONAL width dimension."""
    doc = _new_user_contract()
    schema = ic.compose_schema()

    # An annotation that says "this is an unknown" but not how wide it is.
    schema["$defs"]["date"] = {
        "type": "string",
        "x-uncertainty": {
            "question": "As of when is this document true?",
            "resolvability": "document",
        },
    }

    candidates, unranked, structural = voi.collect_candidates(doc, schema=schema)

    assert unranked, "a width-less x-uncertainty annotation must produce UNRANKED leaves"
    assert not any(c.pointer in unranked for c in candidates)

    report = voi.VOIReport(
        ranked=[], unread=[], inert=[], unranked_pointers=unranked, dropped_pointers=[],
        structural_skipped=structural, objective_name="max_net_benefit", baseline_score=0.0,
        dead_key_pass_ran=True, cross_objective_checked=False,
    )
    text = voi.render_report(report)
    assert f"UNRANKED ({len(unranked)})" in text
    assert "NOT $0 -- not computed" in text


def test_an_explicit_x_uncertainty_false_opts_a_leaf_out():
    doc = _new_user_contract()
    schema = ic.compose_schema()
    assert voi.schema_spec(schema, "/assumptions/inflation") is not None

    schema["$defs"]["assumptions"]["properties"]["inflation"] = {
        "$ref": "#/$defs/rate", "x-uncertainty": False,
    }
    assert voi.schema_spec(schema, "/assumptions/inflation") is None
    assert voi.schema_annotation(schema, "/assumptions/inflation") is None


# ═══════════════════════════════════════════════════════════════════════════
# 3. A correct refusal is not a crash (#598/#643)
# ═══════════════════════════════════════════════════════════════════════════

def test_engine_refusal_is_reported_not_raised_as_a_traceback():
    """schema/example.json is a four-generation, multi-couple household (six
    adults); #698/Step 8 relaxed the boundary to admit additional DEPENDENT
    generations but still refuses additional ADULTS (a second couple) the
    two-slot compute cannot hold (#706/Step 9). VOI must SAY so, not blow up --
    and must not claim any leaf is worth $0 on the way out."""
    with open(ic.EXAMPLE_PATH) as fh:
        doc = json.load(fh)

    report = voi.sweep(doc, cross_objective=False)      # must not raise

    assert report.refusal is not None
    assert "Additional ADULTS" in report.refusal
    assert report.ranked == [] and report.unread == [] and report.inert == []
    assert report.dead_key_pass_ran is False

    text = voi.render_report(report)
    assert "CANNOT COMPUTE" in text
    assert "correct refusal, not a crash" in text
    assert "No leaf is claimed to be worth $0" in text


def test_dead_key_pass_that_did_not_run_says_so_instead_of_printing_zero():
    """DP#32, applied to VOI itself: 'not computed' must never be rendered as
    '(none found)'. That was the #671 defect -- a silent zero inside the tool
    built to catch silent zeros."""
    with open(ic.EXAMPLE_PATH) as fh:
        doc = json.load(fh)
    report = voi.sweep(doc, cross_objective=False)
    assert report.dead_key_pass_ran is False
    text = voi.render_report(report)
    assert "none found" not in text.lower()


# ═══════════════════════════════════════════════════════════════════════════
# 4. Reconciliation with #582's static dead-key analysis
# ═══════════════════════════════════════════════════════════════════════════

def _static_key(doc: dict, pointer: str) -> str:
    """A VOI JSON Pointer -> the dotted leaf key test_schema_coverage.py uses
    (arrays collapse to ``[]``; the polymorphic ``accounts``/``liabilities``
    arrays carry their ``kind`` discriminator, per #647/#654)."""
    tokens = pointer.split("/")[1:]
    out: list[str] = []
    node = doc
    for tok in tokens:
        if tok.isdigit():
            i = int(tok)
            if out in (["accounts"], ["liabilities"]):
                out = [f"{out[0]}[kind={node[i]['kind']}]"]
            else:
                out[-1] = out[-1] + "[]"
            node = node[i]
        else:
            out.append(tok)
            node = node[tok]
    return ".".join(out)


#: Leaves the behavioural sweep finds have NO effect on the engine's input for
#: the fixture document, yet #582 classifies as CONSUMED. This is NOT a
#: contradiction and NOT a dead key -- it is document-scoped inertness, and each
#: entry must say exactly why, with the mechanism verified.
_INERT_FOR_THIS_DOCUMENT = {
    "estate.rollover_overrides[].spousal_rollover": (
        "#600",
        "CONSUMED by input_contract._weighted_rolled_fraction, but inert in THIS "
        "fixture: the only override names a SPOUSE-owned account (spousal_rrsp_p2) "
        "while the primary dies first, and the estate math reads the FIRST-TO-DIE's "
        "rolled fraction. Verified live in general: moving the same override onto a "
        "primary-owned account swings registered_rolled_fraction 1.0 -> 0.07. So the "
        "code does read this key -- it just cannot matter for this household.",
    ),
}


def test_behavioural_live_leaves_are_classified_consumed(_default_sweep):
    """The sound direction of the cross-check: if changing a leaf demonstrably
    moves the engine, #582's static analysis MUST classify it consumed. A leaf
    that is behaviourally LIVE but statically DEAD is a real bug in the static
    dead-key analysis -- which is exactly the independent check #661 promised.

    (The converse does NOT hold and is deliberately not asserted: behaviourally
    inert-for-this-document does not imply the code never reads the key. See
    ``_INERT_FOR_THIS_DOCUMENT``.)
    """
    import test_schema_coverage as tsc

    doc, report = _default_sweep

    live_keys = {_static_key(doc, f.pointer) for f in report.ranked + report.inert}
    wrongly_dead = sorted(live_keys & set(tsc.DEAD_ALLOWLIST))
    assert not wrongly_dead, (
        "leaf(s) that demonstrably MOVE the engine are classified dead by "
        f"tests/test_schema_coverage.py's DEAD_ALLOWLIST: {wrongly_dead}. Either the "
        "static analysis is wrong, or VOI is. Reconcile before shipping."
    )
    unclassified = sorted(live_keys - set(tsc.CONSUMED) - set(tsc.DEAD_ALLOWLIST))
    assert not unclassified, (
        f"leaf(s) VOI proves live are not classified at all by #582: {unclassified}"
    )


def test_behaviourally_inert_but_statically_consumed_leaves_are_justified(_default_sweep):
    """Every disagreement in the OTHER direction must carry an explicit, cited
    justification -- it cannot accumulate silently (the DP#18/DP#32 allowlist
    discipline)."""
    import test_schema_coverage as tsc

    doc, report = _default_sweep

    inert_keys = {_static_key(doc, f.pointer) for f in report.unread}
    # A key with ANY live instance is live -- don't count it as inert.
    live_keys = {_static_key(doc, f.pointer) for f in report.ranked + report.inert}
    inert_keys -= live_keys

    disagreements = sorted(inert_keys & set(tsc.CONSUMED))
    untriaged = [k for k in disagreements if k not in _INERT_FOR_THIS_DOCUMENT]
    assert not untriaged, (
        "leaf(s) VOI finds cannot affect this document, yet #582 calls consumed, "
        f"with no justification recorded: {untriaged}. Add each to "
        "_INERT_FOR_THIS_DOCUMENT with the mechanism, once verified."
    )
    stale = [k for k in _INERT_FOR_THIS_DOCUMENT if k not in disagreements]
    assert not stale, (
        f"_INERT_FOR_THIS_DOCUMENT entries no longer disagree (fixed, or moved): {stale}"
    )
    for key, (issue, reason) in _INERT_FOR_THIS_DOCUMENT.items():
        assert issue.startswith("#") and issue[1:].isdigit(), f"{key}: cite a '#NNN' issue"
        assert len(reason) > 40, f"{key}: give a real reason, not a shrug"


# ═══════════════════════════════════════════════════════════════════════════
# 5. The inert-under-this-objective finding is MEASURED, never guessed
# ═══════════════════════════════════════════════════════════════════════════

def test_a_leaf_inert_under_one_objective_names_the_objective_that_prices_it():
    """A leaf that is $0 under the default objective (max_net_benefit) but priced
    under max_after_tax_estate. Reporting it as 'nothing reads this' would be a
    false statement about the engine (#671). The objectives named must come from
    actually RUNNING them.

    #1034 retired ``/estate/default_spousal_rollover`` as the example leaf:
    compute_net_benefit now prices the SM sleeve's terminal deemed disposition
    via the SAME estate code path compute_after_tax_estate uses (DP#9), so the
    spousal-rollover election -- which the SM sleeve mirrors, via the non-reg
    pot's rollover -- now MOVES net_benefit for this leveraged fixture (it is
    RANKED, no longer INERT). The leaf that still exhibits the #671 pattern --
    $0 under max_net_benefit, priced under the estate objectives -- is the
    mortality ``assumed_death_age``: the estate is a point-in-time valuation at
    the projection's terminal year, so when a member dies does not move the SM
    sleeve's deemed disposition (or any other estate-priced pot) under
    max_net_benefit, but it DOES move the estate objectives. The disclosure
    mechanism this test guards -- voi names the objective that prices an inert
    leaf -- is unchanged; only the example leaf moved, because #1034 closed the
    rollover's inertness under the default objective."""
    doc = _new_user_contract()
    report = voi.sweep(doc, jobs=4, cross_objective=True)

    # #1034 sanity: the rollover is NO LONGER inert under max_net_benefit for
    # this leveraged fixture -- it is RANKED (priced via the SM sleeve).
    assert not any(f.pointer == "/estate/default_spousal_rollover" for f in report.inert), (
        "/estate/default_spousal_rollover is still INERT under max_net_benefit -- "
        "#1034 should have made compute_net_benefit price the SM sleeve's deemed "
        "disposition via the estate, moving the rollover for a leveraged household"
    )
    assert any(f.pointer == "/estate/default_spousal_rollover" for f in report.ranked), (
        "/estate/default_spousal_rollover is not RANKED under max_net_benefit -- "
        "#1034's cross-objective alignment on the SM sleeve is not wired"
    )

    # The leaf that still exhibits the #671 inert-under-one-objective pattern.
    inert_leaf = [f for f in report.inert
                  if f.pointer == "/assumptions/mortality/0/assumed_death_age"]
    assert inert_leaf, (
        "/assumptions/mortality/0/assumed_death_age must be reported as "
        "INERT-under-this-objective (the engine reads it), never as an unread/dead key"
    )
    assert "max_after_tax_estate" in inert_leaf[0].moves_under

    assert not any(f.pointer == "/assumptions/mortality/0/assumed_death_age" for f in report.unread), (
        "the mortality leaf must NOT be reported as 'nothing in the engine reads this'"
    )

    text = voi.render_report(report)
    assert "INERT UNDER THIS OBJECTIVE" in text
    assert "--objective max_after_tax_estate" in text


def test_the_rollover_election_is_actually_priced_under_the_estate_objective():
    """The positive control for the test above: swept under max_after_tax_estate,
    the same leaf carries a real, six-figure-ish price."""
    from objective import OBJECTIVES

    doc = _new_user_contract()
    report = voi.sweep(
        doc, objective=OBJECTIVES["max_after_tax_estate"], jobs=4, cross_objective=False,
    )
    priced = [f for f in report.ranked if f.pointer == "/estate/default_spousal_rollover"]
    assert priced, "the rollover election must be RANKED under max_after_tax_estate"
    assert priced[0].spread > 10_000
