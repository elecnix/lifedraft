"""Issue #360: a DATED union the engine cannot honour must be REFUSED, not dropped.

The contract schema dates a union -- ``relationship.from`` ("Marriage date for
spouse_of") and ``relationship.to`` ("Divorce/dissolution date for spouse_of") --
and the engine read NEITHER. ``contract_people._find_primary_and_spouse`` picked
a spouse on ``type == "spouse_of"`` alone, so the pair it formed held for every
year of the projection. Reported on the shipped example: a couple separated in
2020, a couple that only marries in 2040, and an ongoing couple produced
**byte-identical** internal configs.

That is DP#32's silent default -- a declared fact is dropped and the run reports
a confident wrong answer: spousal income splitting, spousal RRSP, couple GIS
tables and a survivor estate, all for a union that does not exist at ``as_of``.

Modelling a dated union properly is a separate, larger piece of work (the
household-status series, custody shares and support-payment kinds that #360
describes as scopes two and three). This module pins **scope one**: the immediate
safety fix, so a dated union is refused LOUDLY at the ingestion boundary instead
of being silently coerced into an ongoing couple.

Tests are engine-driven through the one loading boundary
(``input_contract.to_internal_config``), asserting on what the boundary does --
never on a hand-built internal dict (DP#11). Figures come from the shipped
schema example; no personal data (DP#4/DP#15).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contract_schema
import input_contract
from contract_errors import ContractAdaptationError
from contract_people import _find_primary_and_spouse


def _couple_doc() -> dict:
    """The shipped example trimmed to the couple + their children -- the shape the
    Phase-1 engine simulates (the full four-generation example is correctly
    REFUSED). Mirrors tests/test_issue_771_generalized_sweeps._couple_doc."""
    with open(contract_schema.EXAMPLE_PATH) as fh:
        doc = json.load(fh)
    keep = {"p1", "p2", "ca", "cb"}

    def owner_ids(owner):
        return {j["person"] for j in owner["joint"]} if isinstance(owner, dict) else {owner}

    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
    for coll in ("accounts", "liabilities", "properties"):
        doc[coll] = [x for x in doc[coll] if owner_ids(x["owner"]) <= keep]
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


def _spouse_edges(doc: dict):
    """Every ``spouse_of`` edge as ``(person, relationship)``, in document
    order, so a test can date or inspect them."""
    out = []
    for p in doc["people"]:
        for r in p.get("relationships", []):
            if r["type"] == "spouse_of":
                out.append((p["id"], r))
    return out


class OngoingUnionStillLoads(unittest.TestCase):
    """Control (NOT expected to fail): an undated-or-ongoing union is what the
    engine actually supports, and the refusal must not touch it."""

    def test_an_ongoing_union_still_forms_a_couple(self):
        doc = _couple_doc()
        edges = _spouse_edges(doc)
        self.assertTrue(edges, "fixture must declare a spouse_of edge")
        for _person, rel in edges:
            if rel.get("from") is not None:
                rel["from"] = "2008-06-21"   # a long-ongoing union
            if rel.get("to") is not None:
                rel["to"] = None             # never ends
        cfg = input_contract.to_internal_config(copy.deepcopy(doc))
        self.assertEqual(len(cfg["family"]["members"]), 2)


class OnlyTheCoupleActuallyFormedIsChecked(unittest.TestCase):
    """The refusal must not be over-broad.

    This engine forms ONE couple and never reads any other ``spouse_of`` edge in
    the document, so a divorce among people it does not model is not a fact this
    run acts on -- refusing such a contract would refuse a household the engine
    was never going to model that couple for anyway.

    The control is the SHIPPED fixture rather than an invented one: the repo's
    own ``schema/example.json`` carries a divorced ``ggm``/``ggf`` union (ended
    2019-11-03, against ``as_of`` 2026-07-12) alongside the ongoing
    ``p1``/``p2`` union the engine actually forms. If the refusal scanned every
    edge, the repo's canonical example would stop loading.
    """

    def test_the_shipped_examples_divorced_edge_does_not_refuse_the_document(self):
        with open(contract_schema.EXAMPLE_PATH) as fh:
            doc = json.load(fh)
        ggm = next(p for p in doc["people"] if p["id"] == "ggm")
        divorced = next(r for r in ggm["relationships"] if r["type"] == "spouse_of")
        self.assertIsNotNone(divorced.get("to"),
                             "fixture must carry the ended union under test")
        self.assertLessEqual(str(divorced["to"])[:10], str(doc["as_of"])[:10])

        primary_id, spouse_id = _find_primary_and_spouse(doc)

        # The couple actually formed is the ongoing one, and it loaded.
        self.assertEqual(primary_id, doc["decisions"]["horizon"]["person"])
        self.assertEqual(spouse_id, "p2")

    def test_dating_THAT_couple_wrongly_still_refuses(self):
        """Control on the control: dating the couple the engine DOES form still
        refuses, so the exemption above is not the check quietly switching off."""
        with open(contract_schema.EXAMPLE_PATH) as fh:
            doc = json.load(fh)
        primary = next(p for p in doc["people"] if p["id"] == "p1")
        for rel in primary["relationships"]:
            if rel["type"] == "spouse_of":
                rel["to"] = "2020-06-01"
        with self.assertRaises(ContractAdaptationError):
            _find_primary_and_spouse(doc)


class DatedUnionIsRefused(unittest.TestCase):
    """Scope one of #360: a union the engine cannot honour fails loudly."""

    def test_a_union_that_ENDED_before_as_of_is_refused(self):
        """Separated before the snapshot: the engine has no separation rule, so
        modelling them as a couple would grant spousal splitting and a survivor
        estate for a union that is over."""
        doc = _couple_doc()
        for _person, rel in _spouse_edges(doc):
            rel["to"] = "2020-06-01"          # six years before as_of
        with self.assertRaises(ContractAdaptationError) as ctx:
            input_contract.to_internal_config(copy.deepcopy(doc))
        message = str(ctx.exception)
        self.assertIn("2020-06-01", message)   # names the date that was dropped

    def test_a_union_that_STARTS_after_as_of_is_refused(self):
        """Marrying in 2040 on a 2026 snapshot is equally unhonourable."""
        doc = _couple_doc()
        for _person, rel in _spouse_edges(doc):
            rel["from"] = "2040-01-01"
        with self.assertRaises(ContractAdaptationError) as ctx:
            input_contract.to_internal_config(copy.deepcopy(doc))
        self.assertIn("2040-01-01", str(ctx.exception))

    def test_the_refusal_says_what_would_be_needed(self):
        """DP#32: name the unsupported fact AND the way out, so the household is
        not left guessing. It must not read as a validation error about their
        data being malformed -- their data is perfectly well-formed, the ENGINE
        cannot price it yet."""
        doc = _couple_doc()
        for _person, rel in _spouse_edges(doc):
            rel["to"] = "2020-06-01"
        with self.assertRaises(ContractAdaptationError) as ctx:
            input_contract.to_internal_config(copy.deepcopy(doc))
        message = str(ctx.exception).lower()
        self.assertIn("spouse_of", message)
        self.assertTrue(
            "not modelled" in message or "unsupported" in message or "cannot" in message,
            f"the refusal must say the union form is unsupported, got: {message!r}",
        )

    def test_a_dated_union_is_NO_LONGER_IDENTICAL_to_an_ongoing_one(self):
        """The regression this issue was filed on: the two contracts used to
        serialise byte-identically. Whatever the resolution, they must not."""
        ongoing = _couple_doc()
        for _person, rel in _spouse_edges(ongoing):
            if rel.get("from") is not None:
                rel["from"] = "2008-06-21"
            if rel.get("to") is not None:
                rel["to"] = None

        dated = _couple_doc()
        for _person, rel in _spouse_edges(dated):
            rel["to"] = "2020-06-01"

        with self.assertRaises(ContractAdaptationError):
            input_contract.to_internal_config(copy.deepcopy(dated))
        # (the ongoing one loads -- asserted by OngoingUnionStillLoads above; the
        # point is that the dated one no longer silently produces it)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()