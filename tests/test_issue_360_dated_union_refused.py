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


class CoupleDiscoveryFallbacks(unittest.TestCase):
    """The two DISCOVERY fallbacks in ``_find_primary_and_spouse``.

    ``decisions.horizon.person`` names the primary in every real document, so the
    "no horizon person" and "spouse only found by the reciprocal edge" paths are
    reached by no ordinary contract -- and nothing pinned them, which is how a
    baseline can drift when the suite's collection order changes. They are
    behaviour, not dead code: a document whose horizon names someone the engine
    cannot find, or whose primary declares no ``spouse_of`` edge but whose spouse
    declares one back, must still form the right couple.
    """

    def test_a_horizon_person_the_engine_cannot_find_falls_back_to_a_declared_spouse(self):
        doc = _couple_doc()
        doc["decisions"]["horizon"]["person"] = "nobody_declared"  # schema-valid id, absent person
        primary_id, spouse_id = _find_primary_and_spouse(doc)
        # p1 is the first person declaring a spouse_of edge, so the fallback
        # finds the same couple the horizon person would have named.
        self.assertEqual(primary_id, "p1")
        self.assertEqual(spouse_id, "p2")

    def test_a_primary_without_its_own_edge_finds_the_spouse_reciprocally(self):
        """A document may declare the union from EITHER side. The shipped example
        declares p1 -> p2 only; a household whose spouse declares the edge back
        at the primary (and the primary declares none) must still form the
        couple, via the reciprocal scan."""
        doc = _couple_doc()
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        p1["relationships"] = [r for r in p1["relationships"] if r["type"] != "spouse_of"]
        p2["relationships"] = list(p2["relationships"]) + [
            {"type": "spouse_of", "person": "p1", "from": "2008-06-21", "to": None},
        ]
        primary_id, spouse_id = _find_primary_and_spouse(doc)
        self.assertEqual(primary_id, "p1")
        self.assertEqual(spouse_id, "p2")

    def test_a_document_with_no_union_at_all_forms_a_single_adult(self):
        """The last fallback: no spouse_of edge anywhere, so the first person is
        the primary and there is no spouse (a genuinely single household)."""
        doc = _couple_doc()
        for person in doc["people"]:
            person["relationships"] = [r for r in person["relationships"]
                                       if r["type"] != "spouse_of"]
        primary_id, spouse_id = _find_primary_and_spouse(doc)
        self.assertIsNotNone(primary_id)
        self.assertIsNone(spouse_id)


class SeveralDeclaredUnionsAreRefused(unittest.TestCase):
    """This engine forms ONE couple, so two DISTINCT partners makes "which union
    is this?" ambiguous -- and the dated eligibility check reads one union's
    dates. Review surfaced this on both paths: only the first edge was checked,
    and the reciprocal scan validated the side it happened to use."""

    @staticmethod
    def _second_partner(doc: dict, person_id: str = "p1") -> None:
        person = next(p for p in doc["people"] if p["id"] == person_id)
        person["relationships"] = list(person["relationships"]) + [
            {"type": "spouse_of", "person": "p2", "from": "2008-06-21", "to": None},
            {"type": "spouse_of", "person": "ca", "from": "2015-01-01", "to": "2020-06-01"},
        ]

    def test_a_person_with_two_distinct_partners_is_refused(self):
        doc = _couple_doc()
        self._second_partner(doc)
        with self.assertRaises(ContractAdaptationError) as ctx:
            _find_primary_and_spouse(doc)
        message = str(ctx.exception)
        self.assertIn("ca", message)      # names the partners
        self.assertIn("p2", message)

    def test_declaring_the_same_union_from_both_sides_stays_legal(self):
        """The ORDINARY reciprocal declaration (p1 -> p2 and p2 -> p1) is ONE
        partner, not two -- refusing it would break every household that states
        the union on both sides."""
        doc = _couple_doc()
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        p2["relationships"] = list(p2["relationships"]) + [
            {"type": "spouse_of", "person": "p1", "from": "2008-06-21", "to": None},
        ]
        self.assertEqual(_find_primary_and_spouse(doc), ("p1", "p2"))

    def test_a_duplicate_edge_to_the_same_partner_is_not_two_unions(self):
        """Two edges naming the SAME partner are one union stated twice."""
        doc = _couple_doc()
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1["relationships"] = list(p1["relationships"]) + [
            {"type": "spouse_of", "person": "p2", "from": "2008-06-21", "to": None},
        ]
        self.assertEqual(_find_primary_and_spouse(doc), ("p1", "p2"))


class BothMembersAreCheckedForMultiplePartners(unittest.TestCase):
    """The refusal must reach the SPOUSE, not just the primary.

    `_refuse_multiple_partners` originally ran on `people[primary_id]` only, and
    BEFORE the spouse was resolved -- so a document whose SPOUSE declared two
    distinct partners, while its primary declared one, loaded silently, and the
    #357 eligibility gate then read only the first edge's history. Measured
    before the fix: with no new adult introduced (so nothing else could refuse
    it), such a document produced NO refusal at all.

    The second partner here is an EXISTING declared person, deliberately: adding
    a new one would trip the unrelated N-adult placement refusal (#698/#901) and
    mask what this test is about.
    """

    @staticmethod
    def _spouse_with_two_partners():
        doc = _couple_doc()
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        # The shipped example declares the union ONE WAY (p1 -> p2), so p2 has
        # no spouse_of edge at all. Give it two: the reciprocal one back to p1,
        # plus a second partner -- which is the ambiguous shape under test.
        p2["relationships"] = list(p2.get("relationships", [])) + [
            {"type": "spouse_of", "person": "p1", "from": "2008-06-21", "to": None},
            {"type": "spouse_of", "person": "ca", "from": "2021-01-01", "to": None},
        ]
        return doc

    def test_a_spouse_with_two_partners_is_refused(self):
        doc = self._spouse_with_two_partners()
        with self.assertRaises(ContractAdaptationError) as ctx:
            _find_primary_and_spouse(doc)
        message = str(ctx.exception)
        self.assertIn("p2", message)
        self.assertIn("ca", message, "the refusal must name the SECOND partner")

    def test_the_primary_still_gets_checked(self):
        """The original case, kept so the fix cannot regress the side that
        already worked."""
        doc = _couple_doc()
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1["relationships"] = list(p1["relationships"]) + [
            {"type": "spouse_of", "person": "ca", "from": "2021-01-01", "to": None},
        ]
        with self.assertRaises(ContractAdaptationError):
            _find_primary_and_spouse(doc)

    def test_the_reciprocal_declaration_stays_legal(self):
        """One union stated from both sides is ONE partner, not two: p1 -> p2 and
        p2 -> p1 must keep loading, or the fix would break the ordinary case."""
        doc = _couple_doc()
        p2 = next(p for p in doc["people"] if p["id"] == "p2")
        p2["relationships"] = list(p2.get("relationships", [])) + [
            {"type": "spouse_of", "person": "p1", "from": "2008-06-21", "to": None},
        ]
        self.assertEqual(_find_primary_and_spouse(doc), ("p1", "p2"))


class TheAsOfBoundaryIsDeliberate(unittest.TestCase):
    """Both edges of ``as_of`` are CONVENTIONS, and a review flagged both as
    suspicious -- so they are pinned here rather than left incidental.

    The engine compares DATES, not instants: a union that ENDS on ``as_of`` has
    already ended at that date (the separation has occurred, so this is not a
    couple to model), while one that STARTS on ``as_of`` has already begun (so it
    is). Read the other way round, each of those documents would be accepted or
    refused for the wrong reason, and neither reading is discoverable from the
    engine.
    """

    def _ongoing_union(self, doc: dict) -> dict:
        for _person, rel in _spouse_edges(doc):
            if rel.get("from") is not None:
                rel["from"] = "2008-06-21"
            if rel.get("to") is not None:
                rel["to"] = None
        return doc

    def test_a_union_ending_exactly_on_as_of_is_refused(self):
        """``to == as_of``: the separation has happened at the snapshot date, so
        modelling them as a couple would grant spousal splitting and a survivor
        estate on the day the union ended."""
        doc = self._ongoing_union(_couple_doc())
        as_of = doc["as_of"]
        for _person, rel in _spouse_edges(doc):
            rel["to"] = as_of
        with self.assertRaises(ContractAdaptationError):
            _find_primary_and_spouse(doc)

    def test_a_union_starting_exactly_on_as_of_is_accepted(self):
        """``from == as_of``: they married on the snapshot date, so they ARE a
        couple at as_of and the engine models exactly what the document states."""
        doc = self._ongoing_union(_couple_doc())
        as_of = doc["as_of"]
        for _person, rel in _spouse_edges(doc):
            rel["from"] = as_of
        primary_id, spouse_id = _find_primary_and_spouse(doc)
        self.assertEqual((primary_id, spouse_id), ("p1", "p2"))

    def test_a_union_ending_the_day_after_as_of_is_still_ongoing(self):
        """The other side of the first boundary: one day later is a different
        answer, and it must not be swept in with the equal case."""
        doc = self._ongoing_union(_couple_doc())
        year, month, day = (int(x) for x in doc["as_of"].split("-"))
        later = f"{year}-12-31" if (month, day) != (12, 31) else f"{year}-01-01"
        for _person, rel in _spouse_edges(doc):
            rel["to"] = later
        primary_id, spouse_id = _find_primary_and_spouse(doc)
        self.assertEqual((primary_id, spouse_id), ("p1", "p2"))


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