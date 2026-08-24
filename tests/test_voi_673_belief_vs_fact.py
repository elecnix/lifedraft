"""Enforcement for #673: VOI must not rank an irreducible BELIEF above a
RESOLVABLE FACT in one list, because the wrong kind always wins on raw dollar
spread -- a 3%-9% long-run return band swings a 44-year projection by tens of
millions, so it buries genuinely actionable items (contribution room, a
rollover election) three screens down, under a question no document can ever
answer.

## What #673 adds

Every ``x-uncertainty`` block now carries a ``resolvability``:

  - ``"document"`` -- a resolvable FACT. Some real document ends the
    uncertainty: CRA My Account, the will, a mortgage statement, a pay stub.
  - ``"belief"`` -- IRREDUCIBLE. No document ever settles it: a long-run
    return assumption, inflation, longevity, a savings-rate target.

``THE BIG QUESTIONS`` splits into two independently-ranked sections on exactly
this classification (``render_report``):

    GO AND FIND OUT                -- resolvable
    IRREDUCIBLE -- YOU MUST CHOOSE -- no document will settle these

## What is asserted here

1. Every ``x-uncertainty`` block in the REAL schema declares a valid
   ``resolvability`` (the enforcement is live, not just theoretical).
2. A block that omits ``resolvability``, or declares one outside
   ``{"document", "belief"}``, fails schema validation LOUDLY -- never a
   silent default (DP#32) -- both via the whole-schema walk
   (``validate_uncertainty_annotations``) and via the per-pointer read
   (``schema_annotation``).
3. Each real schema leaf classified as the issue's own worked examples say it
   should be (a mortgage rate/contribution room/rollover election are FACTS;
   a return/inflation/savings-rate/longevity assumption is a BELIEF).
4. The two report sections are ranked INDEPENDENTLY, and no ``belief`` leaf
   can EVER appear in the ``GO AND FIND OUT`` list -- enforced structurally
   (filtering on ``Finding.resolvability``), not by convention.
5. The belief section states its own irreducibility -- it never presents a
   ``resolved_by`` in a way that implies a lookup will settle it.

All figures fabricated / round-numbered, role-based ids (DP#4/DP#15).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voi
import contract_schema


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════

def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _fabricated_household() -> dict:
    """The shipped example trimmed to the couple + their children -- the shape
    the Phase-1 engine can actually simulate (#598) -- with the ``provenance``
    sidecar removed, so every candidate is schema-sourced. All figures in the
    shipped example are fabricated round numbers with role-based ids (DP#4/
    DP#15); nothing here is anyone's real household."""
    with open(contract_schema.EXAMPLE_PATH) as fh:
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
    doc.pop("provenance", None)
    contract_schema.validate_contract(doc)
    return doc


# ═══════════════════════════════════════════════════════════════════════════
# 1. The real schema: every annotation is complete
# ═══════════════════════════════════════════════════════════════════════════

def test_the_real_schema_passes_uncertainty_validation():
    """The whole point of DP#32's 'no silent default': this must not raise."""
    schema = contract_schema.compose_schema()
    voi.validate_uncertainty_annotations(schema)   # must not raise


def test_the_real_schema_has_both_document_and_belief_leaves():
    """A sanity check that the classification work actually happened -- not a
    schema where everything defaulted to one bucket."""
    schema = contract_schema.compose_schema()
    found = {"document": 0, "belief": 0}

    def _walk(node):
        if isinstance(node, dict):
            entry = node.get(voi.UNCERTAINTY_KEYWORD)
            if isinstance(entry, dict):
                found[entry["resolvability"]] += 1
            for key, value in node.items():
                if key != voi.UNCERTAINTY_KEYWORD:
                    _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(schema)
    assert found["document"] > 0, "no leaf classified 'document' -- the split has no facts to find"
    assert found["belief"] > 0, "no leaf classified 'belief' -- the split has no irreducible uncertainty"


# ═══════════════════════════════════════════════════════════════════════════
# 2. No silent default: a bad annotation fails loudly (DP#32)
# ═══════════════════════════════════════════════════════════════════════════

def test_missing_resolvability_fails_schema_validation():
    schema = contract_schema.compose_schema()
    schema["$defs"]["date"] = {
        "type": "string",
        "x-uncertainty": {"plausible_range": [0, 1], "resolved_by": "somewhere"},
    }
    with pytest.raises(voi.UncertaintyAnnotationError, match="resolvability"):
        voi.validate_uncertainty_annotations(schema)


def test_invalid_resolvability_value_fails_schema_validation():
    schema = contract_schema.compose_schema()
    schema["$defs"]["date"] = {
        "type": "string",
        "x-uncertainty": {"plausible_range": [0, 1], "resolvability": "vibes"},
    }
    with pytest.raises(voi.UncertaintyAnnotationError, match="resolvability"):
        voi.validate_uncertainty_annotations(schema)


def test_missing_resolvability_fails_the_moment_the_pointer_is_read():
    """Not just the whole-schema audit -- the per-pointer read path
    (``schema_annotation``) must ALSO refuse, since that is what a live
    ``collect_candidates``/``sweep`` call actually exercises."""
    schema = contract_schema.compose_schema()
    schema["$defs"]["date"] = {
        "type": "string",
        "x-uncertainty": {"plausible_range": [0, 1]},
    }
    with pytest.raises(voi.UncertaintyAnnotationError):
        voi.schema_annotation(schema, "/as_of")


def test_a_document_with_a_bad_schema_annotation_cannot_be_swept():
    """End-to-end: collect_candidates validates the WHOLE schema up front, so
    a bad annotation anywhere is caught even before the document is walked."""
    doc = _fabricated_household()
    schema = contract_schema.compose_schema()
    schema["$defs"]["date"] = {
        "type": "string",
        "x-uncertainty": {"domain": [True, False], "resolvability": "not-a-real-value"},
    }
    with pytest.raises(voi.UncertaintyAnnotationError):
        voi.collect_candidates(doc, schema=schema)


# ═══════════════════════════════════════════════════════════════════════════
# 3. Real schema leaves are classified the way the issue's own examples say
# ═══════════════════════════════════════════════════════════════════════════

#: schema_annotation navigates the SCHEMA by structural pointer tokens (any
#: digit token just descends into `items`), so these do not need to match any
#: particular document's actual array lengths.
_EXPECTED_RESOLVABILITY = {
    # Resolvable facts -- a document ends the uncertainty (#673's own list:
    # "contribution room, the rollover election, ... a mortgage rate").
    "/people/0/incomes/0/amount": "document",
    "/liabilities/0/rate": "document",
    "/estate/default_spousal_rollover": "document",
    "/estate/rollover_overrides/0/spousal_rollover": "document",
    "/people/0/room/rrsp/contribution_room": "document",
    # Irreducible beliefs -- no document ever settles these (#673's own list:
    # "long-run return, inflation, savings rate, longevity").
    "/assumptions/return_model/rate": "belief",
    "/assumptions/inflation": "belief",
    "/assumptions/salary_growth": "belief",
    "/assumptions/savings_rate": "belief",
    "/assumptions/mortality/0/assumed_death_age": "belief",
    "/assumptions/rate_paths/mortgage/rate": "belief",
}


@pytest.mark.parametrize("pointer,expected", sorted(_EXPECTED_RESOLVABILITY.items()))
def test_real_schema_leaf_classified_as_expected(pointer, expected):
    schema = contract_schema.compose_schema()
    entry = voi.schema_annotation(schema, pointer)
    assert entry is not None, f"{pointer} carries no x-uncertainty annotation at all"
    assert entry["resolvability"] == expected, (
        f"{pointer} classified {entry['resolvability']!r}, expected {expected!r}"
    )


# ═══════════════════════════════════════════════════════════════════════════
#: The four section-4/5 tests below all sweep the SAME fabricated household
#: under the SAME arguments (default objective, jobs=4, cross_objective=False).
#: ``voi.sweep`` is a pure function of its arguments (DP#3, verified in
#: voi.py's docstring), so the result is identical whether computed once or
#: four times. Computing it once per module (~75s -> ~19s) does not weaken any
#: assertion: every test only READS ``report`` (no mutation), and
#: ``voi.render_report`` is pure. Module-scoped so the single sweep is shared
#: across the whole file.
@pytest.fixture(scope="module")
def _belief_fact_report():
    doc = _fabricated_household()
    return voi.sweep(doc, jobs=4, cross_objective=False)


# 4. The two report sections: independently ranked, no belief in GO AND FIND OUT
# ═══════════════════════════════════════════════════════════════════════════

def test_two_sections_are_ranked_independently_and_belief_never_leaks_into_findable(_belief_fact_report):
    report = _belief_fact_report

    assert report.ranked, "nothing ranked -- cannot test the split with an empty ranking"
    assert any(f.resolvability == "belief" for f in report.ranked), (
        "the fabricated household's return_model/inflation/savings_rate leaves "
        "must all rank -- if none does, this test cannot prove the exclusion"
    )

    findable = [f for f in report.ranked if f.resolvability == "document"]
    beliefs = [f for f in report.ranked if f.resolvability == "belief"]

    # No third bucket: every ranked finding is classified one way or the other.
    assert len(findable) + len(beliefs) == len(report.ranked)

    # Each section is ranked independently, descending by spread.
    assert [f.spread for f in findable] == sorted((f.spread for f in findable), reverse=True)
    assert [f.spread for f in beliefs] == sorted((f.spread for f in beliefs), reverse=True)

    # The structural guarantee: NO belief leaf in the findable bucket, ever.
    assert all(f.resolvability != "belief" for f in findable)
    assert all(f.resolvability == "belief" for f in beliefs)

    text = voi.render_report(report)
    go_and_find_out, _, rest = text.partition("IRREDUCIBLE -- YOU MUST CHOOSE")
    assert "GO AND FIND OUT" in go_and_find_out
    assert rest, "the IRREDUCIBLE header must appear in the rendered report"

    # Cross-check against the rendered TEXT, not just the objects: no belief
    # leaf's pointer appears inside the GO AND FIND OUT section of the text,
    # and every findable leaf's pointer DOES appear there.
    for f in beliefs:
        assert f.pointer not in go_and_find_out, (
            f"belief leaf {f.pointer} leaked into the GO AND FIND OUT section text"
        )
    for f in findable:
        assert f.pointer in go_and_find_out, (
            f"findable leaf {f.pointer} is missing from its own GO AND FIND OUT section text"
        )


def test_the_dominant_belief_does_not_bury_findable_facts_out_of_their_own_section(_belief_fact_report):
    """#673's headline complaint: the return-rate belief's dollar spread
    dwarfs any fact's. Prove the split, not just that both exist: the
    document-resolvable section's own top item is unaffected by however large
    the belief section's numbers are."""
    report = _belief_fact_report

    findable = [f for f in report.ranked if f.resolvability == "document"]
    beliefs = [f for f in report.ranked if f.resolvability == "belief"]
    assert findable, "the fabricated household must have at least one resolvable-fact finding"
    assert beliefs, "the fabricated household must have at least one belief finding"

    # The classic #673 case: the return-rate belief's spread is enormous, and
    # a naive combined ranking would put it first. Confirm it is NOT in
    # `findable`, regardless of magnitude.
    return_rate = [f for f in beliefs if f.pointer == "/assumptions/return_model/rate"]
    assert return_rate, "/assumptions/return_model/rate must rank as a belief on this household"
    assert not any(f.pointer == "/assumptions/return_model/rate" for f in findable)


# ═══════════════════════════════════════════════════════════════════════════
# 5. The belief section states irreducibility, never a lookup
# ═══════════════════════════════════════════════════════════════════════════

def test_belief_section_states_its_own_irreducibility(_belief_fact_report):
    report = _belief_fact_report
    text = voi.render_report(report)

    header_start = text.index("IRREDUCIBLE -- YOU MUST CHOOSE")
    header_end = text.index("\n", header_start)
    header_line = text[header_start:header_end]
    assert "no document will settle these" in header_line

    body_start = header_end
    body_end = text.index("NO EFFECT ON THE ENGINE'S INPUT")
    belief_body = text[body_start:body_end]

    assert beliefs_present(report), "the fabricated household must produce belief findings"
    # A resolved_by on a belief must read as "narrow", never as a lookup that
    # would SETTLE it -- the "->" arrow is reserved for GO AND FIND OUT.
    assert "->" not in belief_body, (
        "the belief section used the '->' arrow, which implies a lookup that settles "
        "the uncertainty -- beliefs are only ever narrowed, never settled (#673)"
    )
    assert "narrow it by" in belief_body


def beliefs_present(report: voi.VOIReport) -> bool:
    return any(f.resolvability == "belief" for f in report.ranked)


def test_findable_section_uses_the_lookup_arrow(_belief_fact_report):
    report = _belief_fact_report
    text = voi.render_report(report)

    body_start = text.index("GO AND FIND OUT")
    body_end = text.index("IRREDUCIBLE -- YOU MUST CHOOSE")
    findable_body = text[body_start:body_end]

    findable = [f for f in report.ranked if f.resolvability == "document" and f.resolved_by]
    assert findable, "the fabricated household must produce at least one resolvable finding with resolved_by"
    assert "->" in findable_body
    assert "narrow it by" not in findable_body


# ═══════════════════════════════════════════════════════════════════════════
# 6. A provenance-only candidate (no schema annotation) defaults to "document"
# ═══════════════════════════════════════════════════════════════════════════

def test_provenance_only_leaf_with_no_schema_annotation_defaults_to_document():
    """Track 1's sidecar (#660) is only ever about verifiable facts -- its
    confidence vocabulary (measured/stated/derived/assumed) has no concept of
    a forward-looking belief, so a leaf that is a VOI candidate ONLY because
    the document's own provenance sidecar declared it (no matching schema
    x-uncertainty at all) must default to "document" (see
    ``_resolvability_for``'s docstring)."""
    pytest.importorskip(
        "provenance",
        reason="needs Track 1 (#660)'s sidecar module to build a provenance-only candidate",
    )
    doc = _fabricated_household()
    schema = contract_schema.compose_schema()
    # legal_name carries no x-uncertainty annotation anywhere in the schema.
    assert voi.schema_annotation(schema, "/people/0/legal_name") is None

    doc["provenance"] = {
        "/people/0/legal_name": {
            "confidence": "assumed",
            "domain": [None, "Placeholder Name"],
        },
    }
    candidates, _, _ = voi.collect_candidates(doc, schema=schema)
    cand = next(c for c in candidates if c.pointer == "/people/0/legal_name")
    assert cand.spec.source == "provenance"
    assert cand.resolvability == "document"
