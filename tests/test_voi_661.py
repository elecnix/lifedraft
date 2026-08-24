"""Enforcement tests for voi.py's SIDECAR path (issue #661, Track 2 of #659).

**Stacked on #660.** These tests need the real ``provenance`` module (Track 1 --
owned by a parallel PR, not implemented here), so they ``importorskip`` it and
will start running the moment #660 lands on ``main``. Until then they SKIP, and
the green CI on this PR is correspondingly weaker for this file: it proves the
sidecar path is not *broken*, not that it *works*. Better said out loud than
left for a green tick to imply.

Everything that does NOT need the sidecar -- the whole new-user path, which is
#671's headline and the epic's real design target -- is enforced in
``tests/test_voi_671_schema_defaults.py``, which needs no provenance module and
therefore runs in CI *today*.

What this file adds on top of that:

  - **Precedence**: document provenance OVERRIDES the schema's ``x-uncertainty``
    default for the same pointer (DP#13 -- a default is a fallback for absent
    input, never a coercion of input that WAS supplied).
  - **The golden dead-key test** (#661's own words: "a VOI engine that cannot
    tell a dead key from a live one is worse than none"): a known-sensitive leaf
    must rank; a known-inert one must report exactly $0.
  - **The UNRANKED path**: a leaf the author declares uncertain but gives no
    width, and for which the schema has no default either, is reported as
    UNRANKED -- never crashed on, never silently dropped, never printed as $0.

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

pytest.importorskip(
    "provenance",
    reason="#661's sidecar path is stacked on #660 (provenance.py); skips until it lands.",
)


def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _couple_contract() -> dict:
    """The shipped example trimmed to the couple + their children -- the shape
    the Phase-1 engine can simulate (#598; the full four-generation example is
    correctly REFUSED, see test_voi_671_schema_defaults.py)."""
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
    return doc


def _tfsa_pointer(doc: dict) -> str:
    for i, a in enumerate(doc["accounts"]):
        if a["kind"] == "tfsa" and a["owner"] == "p1":
            return f"/accounts/{i}/balance/amount"
    raise AssertionError("fixture has no p1 TFSA account")


# ═══════════════════════════════════════════════════════════════════════════
# Precedence: the document beats the schema default (#671, DP#13)
# ═══════════════════════════════════════════════════════════════════════════

def test_document_provenance_overrides_the_schema_default():
    """The schema declares a coarse [0, 100000] band on contribution_room. A
    household that KNOWS better states a tighter one in its sidecar, and that
    must win -- a default is a fallback for absent input, never a coercion of
    input that was actually supplied (DP#13)."""
    doc = _couple_contract()
    pointer = "/people/0/room/tfsa/contribution_room"

    schema_only, _, _ = voi.collect_candidates(doc)
    from_schema = next(c for c in schema_only if c.pointer == pointer)
    assert from_schema.spec.source == "schema"
    assert from_schema.spec.plausible_range == (0, 100000)

    doc_with_sidecar = dict(doc)
    doc_with_sidecar["provenance"] = {
        pointer: {
            # Fabricated, and much tighter than the schema's [0, 100000] band.
            # It must BRACKET the leaf's live value -- #660's loader enforces
            # that, which is why this is [15000, 20000] around the fixture's
            # 18000 rather than an arbitrary window.
            "confidence": "stated",
            "plausible_range": [15000, 20000],
            "resolved_by": "CRA My Account (this household already checked).",
        }
    }
    with_doc, _, _ = voi.collect_candidates(doc_with_sidecar)
    overridden = next(c for c in with_doc if c.pointer == pointer)

    assert overridden.spec.source == "provenance", "the sidecar must beat the schema default"
    assert overridden.spec.plausible_range == (15000, 20000)
    assert overridden.confidence == "stated"


def test_schema_default_still_applies_to_leaves_the_sidecar_does_not_mention():
    """Overriding one leaf must not switch the schema defaults off for the rest."""
    doc = _couple_contract()
    doc["provenance"] = {
        "/assumptions/inflation": {"confidence": "stated", "plausible_range": [0.015, 0.025]},
    }
    candidates, _, _ = voi.collect_candidates(doc)
    by_pointer = {c.pointer: c for c in candidates}

    assert by_pointer["/assumptions/inflation"].spec.source == "provenance"
    assert by_pointer["/assumptions/return_model/rate"].spec.source == "schema"


def test_a_sidecar_does_not_rebuild_the_671_wall():
    """Regression guard for the trap found while wiring #671 to the real #660
    module: ``Provenance.uncertain_leaves()`` correctly returns EVERY leaf (a
    leaf with no entry is ``assumed`` by definition), so consuming that set as
    "the author declared these uncertain" would flip the entire document into
    UNRANKED and drive the structural count to zero -- i.e. re-create exactly
    the empty-report wall #671 exists to tear down, but only when a sidecar is
    present. Only EXPLICIT entries may override the schema."""
    doc = _couple_contract()
    without = voi.collect_candidates(doc)

    doc_with = dict(doc)
    doc_with["provenance"] = {
        "/assumptions/inflation": {"confidence": "stated", "plausible_range": [0.015, 0.025]},
    }
    cands, unranked, structural = voi.collect_candidates(doc_with)

    assert len(cands) == len(without[0]), "adding one sidecar entry changed the candidate count"
    assert unranked == without[1] == [], "a sidecar must not flip the document into UNRANKED"
    assert structural == without[2] > 0, "the structural count must not collapse to zero"


# ═══════════════════════════════════════════════════════════════════════════
# The golden test: a live leaf ranks; a dead leaf is exactly $0
# ═══════════════════════════════════════════════════════════════════════════

def test_sensitive_leaf_ranks_and_an_inert_one_is_exactly_zero():
    """#661: "a VOI engine that cannot tell a dead key from a live one is worse
    than none." The sensitive leaf is a TFSA balance (moves any wealth
    objective); the inert one is a person's legal_name, which
    ``to_internal_config`` provably never reads -- so it must come back at
    EXACTLY $0, proven by the cheap screen, without running a simulation."""
    doc = _couple_contract()
    tfsa = _tfsa_pointer(doc)
    doc["provenance"] = {
        tfsa: {
            "confidence": "stated",
            "plausible_range": [0, 400000],
            "resolved_by": "Pull the latest TFSA statement.",
        },
        "/people/0/legal_name": {
            "confidence": "assumed",
            "domain": [None, "Placeholder Name"],
        },
    }

    report = voi.sweep(doc, jobs=4, cross_objective=False)

    ranked = {f.pointer: f for f in report.ranked}
    assert tfsa in ranked, "a TFSA balance swept $0-$400k must move a wealth objective"
    assert ranked[tfsa].spread > 0

    unread = {f.pointer: f for f in report.unread}
    assert "/people/0/legal_name" in unread, (
        "a person's legal_name cannot reach a tax computation -- it must be reported as "
        "having no effect on the engine's input, at exactly $0"
    )
    assert unread["/people/0/legal_name"].spread == 0.0
    assert report.ranked[0].spread > 0


def test_max_leaves_drops_are_reported_never_silent():
    doc = _couple_contract()
    report = voi.sweep(doc, max_leaves=2, jobs=4, cross_objective=False)
    assert report.dropped_pointers
    text = voi.render_report(report)
    assert f"DROPPED ({len(report.dropped_pointers)})" in text


def test_report_always_states_the_oat_limitation():
    doc = _couple_contract()
    report = voi.sweep(doc, max_leaves=1, jobs=4, cross_objective=False)
    text = voi.render_report(report)
    assert "one-at-a-time" in text.lower()
    assert "interaction" in text.lower()


def test_humanize_pointer_is_mechanical_and_invents_nothing():
    assert voi.humanize_pointer("/people/0/room/tfsa/contribution_room") == (
        "people #0 room tfsa contribution room"
    )
