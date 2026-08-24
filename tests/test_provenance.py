#!/usr/bin/env python3
"""Tests for provenance.py (issue #660, epic #659 Track 1).

Covers: RFC 6901 JSON Pointer get/set round-trips (including array indices
and escaped tokens), every documented validation rejection, the "a leaf
with no entry is assumed by definition" property that #660 exists to
guarantee, and the --provenance report's worst-first ordering.

All test data uses fake/synthetic names and round numbers (DP#15) -- none
of it comes from the private household repo.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import provenance as prov
import contract_schema

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_example():
    with open(contract_schema.EXAMPLE_PATH) as f:
        return json.load(f)


def _doc(**overrides):
    """A minimal fabricated document with a couple of leaves to point at.
    Not a valid input contract (that's input_contract's job to test) --
    just enough structure for JSON-Pointer resolution and cross-checks."""
    base = {
        "people": [
            {"id": "p1", "label": "primary", "legal_name": None, "room": {"tfsa": {"contribution_room": 5000}}},
            {"id": "p2", "label": "spouse"},
        ],
        "accounts": [
            {"id": "acc1", "balance": {"amount": 210000, "as_of": "2026-06-30"}},
        ],
        "estate": {"default_spousal_rollover": True},
        "weird/key~name": {"nested": 42},
        "provenance": {},
    }
    base.update(overrides)
    return base


# ── 1. RFC 6901 JSON Pointer get/set ────────────────────────────────────────


class PointerRoundTripTest(unittest.TestCase):
    def test_resolve_scalar_leaf(self):
        doc = _doc()
        self.assertEqual(prov.resolve_pointer(doc, "/accounts/0/balance/amount"), 210000)

    def test_resolve_whole_document(self):
        doc = _doc()
        self.assertEqual(prov.resolve_pointer(doc, ""), doc)

    def test_resolve_array_index(self):
        doc = _doc()
        self.assertEqual(prov.resolve_pointer(doc, "/people/1/id"), "p2")

    def test_resolve_missing_key_raises(self):
        doc = _doc()
        with self.assertRaises(prov.PointerResolutionError):
            prov.resolve_pointer(doc, "/people/0/nonexistent")

    def test_resolve_index_out_of_range_raises(self):
        doc = _doc()
        with self.assertRaises(prov.PointerResolutionError):
            prov.resolve_pointer(doc, "/people/99/id")

    def test_resolve_into_scalar_raises(self):
        doc = _doc()
        with self.assertRaises(prov.PointerResolutionError):
            prov.resolve_pointer(doc, "/accounts/0/balance/amount/nope")

    def test_resolve_with_default_suppresses_error(self):
        doc = _doc()
        self.assertEqual(prov.resolve_pointer(doc, "/nope", default="fallback"), "fallback")

    def test_escaped_tilde_and_slash_tokens(self):
        # RFC 6901 sec. 3: '~' encodes as '~0', '/' encodes as '~1'.
        doc = _doc()
        self.assertEqual(prov.resolve_pointer(doc, "/weird~1key~0name/nested"), 42)

    def test_escape_decode_ordering_literal_tilde_one(self):
        # A literal "~1" (tilde then digit 1) in a key must decode back
        # correctly, proving decode order (~1 before ~0) is right: the
        # encoded token for key "~1" is "~01", not "~1" (which would mean
        # the key "/").
        doc = {"~1": "literal-tilde-one", "provenance": {}}
        self.assertEqual(prov.resolve_pointer(doc, "/~01"), "literal-tilde-one")

    def test_set_pointer_returns_new_document_leaves_original_untouched(self):
        doc = _doc()
        original = copy.deepcopy(doc)
        updated = prov.set_pointer(doc, "/accounts/0/balance/amount", 999)
        self.assertEqual(doc, original)  # DP#3: pure, no mutation of the argument
        self.assertEqual(updated["accounts"][0]["balance"]["amount"], 999)
        self.assertEqual(prov.resolve_pointer(updated, "/accounts/0/balance/amount"), 999)

    def test_set_pointer_array_index(self):
        doc = _doc()
        updated = prov.set_pointer(doc, "/people/1/id", "p2-renamed")
        self.assertEqual(updated["people"][1]["id"], "p2-renamed")
        self.assertEqual(doc["people"][1]["id"], "p2")

    def test_set_pointer_array_append(self):
        doc = _doc()
        updated = prov.set_pointer(doc, "/people/-", {"id": "p3"})
        self.assertEqual(len(updated["people"]), 3)
        self.assertEqual(updated["people"][2]["id"], "p3")
        self.assertEqual(len(doc["people"]), 2)  # original untouched

    def test_set_pointer_escaped_tokens_round_trip(self):
        doc = _doc()
        updated = prov.set_pointer(doc, "/weird~1key~0name/nested", 7)
        self.assertEqual(updated["weird/key~name"]["nested"], 7)

    def test_set_pointer_missing_intermediate_raises(self):
        doc = _doc()
        with self.assertRaises(prov.PointerResolutionError):
            prov.set_pointer(doc, "/nope/deeper", 1)

    def test_get_then_set_round_trip_is_idempotent(self):
        doc = _doc()
        value = prov.resolve_pointer(doc, "/accounts/0/balance/amount")
        updated = prov.set_pointer(doc, "/accounts/0/balance/amount", value)
        self.assertEqual(updated, doc)


# ── 2. load_provenance validation ───────────────────────────────────────────


class ValidationTest(unittest.TestCase):
    def test_valid_measured_entry_accepted(self):
        doc = _doc(provenance={
            "/accounts/0/balance/amount": {
                "confidence": "measured",
                "source": "file:///fake/statement.pdf#page=1",
                "as_of": "2026-06-30",
            }
        })
        p = prov.load_provenance(doc)
        self.assertEqual(p.confidence("/accounts/0/balance/amount"), "measured")

    def test_pointer_that_does_not_resolve_is_rejected(self):
        doc = _doc(provenance={
            "/does/not/exist": {"confidence": "derived"}
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_measured_without_source_is_rejected(self):
        doc = _doc(provenance={
            "/accounts/0/balance/amount": {"confidence": "measured", "as_of": "2026-06-30"}
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_measured_without_as_of_is_rejected(self):
        doc = _doc(provenance={
            "/accounts/0/balance/amount": {
                "confidence": "measured",
                "source": "file:///fake/statement.pdf#page=1",
            }
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_assumed_without_range_or_domain_is_rejected(self):
        doc = _doc(provenance={
            "/estate/default_spousal_rollover": {"confidence": "assumed"}
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_stated_without_range_or_domain_is_rejected(self):
        doc = _doc(provenance={
            "/people/0/room/tfsa/contribution_room": {"confidence": "stated"}
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_assumed_with_domain_is_accepted(self):
        doc = _doc(provenance={
            "/estate/default_spousal_rollover": {
                "confidence": "assumed",
                "domain": [True, False],
                "resolved_by": "Read the will.",
            }
        })
        p = prov.load_provenance(doc)
        self.assertEqual(p.confidence("/estate/default_spousal_rollover"), "assumed")

    def test_plausible_range_not_bracketing_value_is_rejected(self):
        doc = _doc(provenance={
            # contribution_room is 5000; range [10000, 20000] doesn't bracket it
            "/people/0/room/tfsa/contribution_room": {
                "confidence": "stated",
                "plausible_range": [10000, 20000],
            }
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_plausible_range_bracketing_value_is_accepted(self):
        doc = _doc(provenance={
            "/people/0/room/tfsa/contribution_room": {
                "confidence": "stated",
                "plausible_range": [0, 102000],
            }
        })
        p = prov.load_provenance(doc)
        self.assertEqual(p.confidence("/people/0/room/tfsa/contribution_room"), "stated")

    def test_domain_not_containing_value_is_rejected(self):
        doc = _doc(provenance={
            "/people/0/id": {"confidence": "stated", "domain": ["someone-else"]}
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_domain_containing_value_is_accepted(self):
        doc = _doc(provenance={
            "/people/0/id": {"confidence": "stated", "domain": ["p1", "p2"]}
        })
        p = prov.load_provenance(doc)
        self.assertEqual(p.confidence("/people/0/id"), "stated")

    def test_unknown_confidence_level_string_is_rejected(self):
        doc = _doc(provenance={
            "/people/0/id": {"confidence": "guessed"}  # not a real level
        })
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)

    def test_derived_requires_nothing_extra(self):
        doc = _doc(provenance={
            "/accounts/0/balance/amount": {"confidence": "derived"}
        })
        p = prov.load_provenance(doc)
        self.assertEqual(p.confidence("/accounts/0/balance/amount"), "derived")

    def test_unknown_level_requires_nothing_extra(self):
        doc = _doc(provenance={
            "/people/0/legal_name": {"confidence": "unknown"}
        })
        p = prov.load_provenance(doc)
        self.assertEqual(p.confidence("/people/0/legal_name"), "unknown")

    def test_all_violations_reported_together_not_just_the_first(self):
        doc = _doc(provenance={
            "/accounts/0/balance/amount": {"confidence": "measured"},  # missing source + as_of
            "/estate/default_spousal_rollover": {"confidence": "assumed"},  # missing range/domain
        })
        with self.assertRaises(prov.ProvenanceValidationError) as ctx:
            prov.load_provenance(doc)
        message = str(ctx.exception)
        self.assertIn("source", message)
        self.assertIn("as_of", message)
        self.assertIn("plausible_range", message)

    def test_missing_provenance_key_defaults_to_empty(self):
        doc = _doc()
        del doc["provenance"]
        p = prov.load_provenance(doc)
        self.assertEqual(dict(p.entries), {})

    def test_provenance_not_an_object_is_rejected(self):
        doc = _doc(provenance=["not", "a", "dict"])
        with self.assertRaises(prov.ProvenanceValidationError):
            prov.load_provenance(doc)


# ── 3. confidence() / uncertain_leaves() -- the two properties #660 exists for ──


class ConfidenceAndUncertainLeavesTest(unittest.TestCase):
    def test_leaf_with_no_entry_reports_assumed(self):
        doc = _doc()  # provenance={} -- nothing declared
        p = prov.load_provenance(doc)
        self.assertEqual(p.confidence("/accounts/0/balance/amount"), "assumed")

    def test_leaf_with_no_entry_appears_in_uncertain_leaves_unranked(self):
        doc = _doc()
        p = prov.load_provenance(doc)
        leaves = {leaf.pointer: leaf for leaf in p.uncertain_leaves(doc)}
        leaf = leaves["/accounts/0/balance/amount"]
        self.assertEqual(leaf.confidence, "assumed")
        self.assertIsNone(leaf.plausible_range)
        self.assertIsNone(leaf.domain)
        self.assertEqual(leaf.current_value, 210000)

    def test_measured_leaf_is_not_in_uncertain_leaves(self):
        doc = _doc(provenance={
            "/accounts/0/balance/amount": {
                "confidence": "measured",
                "source": "file:///fake/s.pdf#page=1",
                "as_of": "2026-06-30",
            }
        })
        p = prov.load_provenance(doc)
        pointers = {leaf.pointer for leaf in p.uncertain_leaves(doc)}
        self.assertNotIn("/accounts/0/balance/amount", pointers)

    def test_derived_leaf_is_not_in_uncertain_leaves(self):
        doc = _doc(provenance={"/accounts/0/balance/amount": {"confidence": "derived"}})
        p = prov.load_provenance(doc)
        pointers = {leaf.pointer for leaf in p.uncertain_leaves(doc)}
        self.assertNotIn("/accounts/0/balance/amount", pointers)

    def test_stated_leaf_carries_its_declared_range(self):
        doc = _doc(provenance={
            "/people/0/room/tfsa/contribution_room": {
                "confidence": "stated",
                "plausible_range": [0, 102000],
            }
        })
        p = prov.load_provenance(doc)
        leaves = {leaf.pointer: leaf for leaf in p.uncertain_leaves(doc)}
        leaf = leaves["/people/0/room/tfsa/contribution_room"]
        self.assertEqual(leaf.plausible_range, (0, 102000))
        self.assertIsNone(leaf.domain)

    def test_assumed_leaf_carries_resolved_by(self):
        doc = _doc(provenance={
            "/estate/default_spousal_rollover": {
                "confidence": "assumed",
                "domain": [True, False],
                "resolved_by": "Read the will.",
            }
        })
        p = prov.load_provenance(doc)
        leaves = {leaf.pointer: leaf for leaf in p.uncertain_leaves(doc)}
        leaf = leaves["/estate/default_spousal_rollover"]
        self.assertEqual(leaf.domain, (True, False))
        self.assertEqual(leaf.resolved_by, "Read the will.")

    def test_provenance_sidecar_itself_is_excluded_from_leaves(self):
        doc = _doc(provenance={
            "/accounts/0/balance/amount": {
                "confidence": "measured",
                "source": "file:///fake/s.pdf#page=1",
                "as_of": "2026-06-30",
            }
        })
        p = prov.load_provenance(doc)
        pointers = {leaf.pointer for leaf in p.uncertain_leaves(doc)}
        self.assertFalse(any(ptr.startswith("/provenance") for ptr in pointers))


# ── 4. Provenance.resolve / with_value ──────────────────────────────────────


class ResolveWithValueTest(unittest.TestCase):
    def test_resolve_matches_module_level_function(self):
        doc = _doc()
        self.assertEqual(
            prov.Provenance.resolve(doc, "/accounts/0/balance/amount"),
            prov.resolve_pointer(doc, "/accounts/0/balance/amount"),
        )

    def test_with_value_returns_new_document(self):
        doc = _doc()
        updated = prov.Provenance.with_value(doc, "/accounts/0/balance/amount", 500000)
        self.assertEqual(prov.Provenance.resolve(updated, "/accounts/0/balance/amount"), 500000)
        self.assertEqual(prov.Provenance.resolve(doc, "/accounts/0/balance/amount"), 210000)

    def test_with_value_supports_sweep_across_a_range(self):
        # This is exactly what Track 2 needs: materialize both endpoints of
        # a plausible_range without mutating the baseline document.
        doc = _doc(provenance={
            "/people/0/room/tfsa/contribution_room": {
                "confidence": "stated",
                "plausible_range": [0, 102000],
            }
        })
        p = prov.load_provenance(doc)
        leaf = next(l for l in p.uncertain_leaves(doc) if l.pointer == "/people/0/room/tfsa/contribution_room")
        lo, hi = leaf.plausible_range
        low_doc = prov.Provenance.with_value(doc, leaf.pointer, lo)
        high_doc = prov.Provenance.with_value(doc, leaf.pointer, hi)
        self.assertEqual(prov.Provenance.resolve(low_doc, leaf.pointer), 0)
        self.assertEqual(prov.Provenance.resolve(high_doc, leaf.pointer), 102000)
        self.assertEqual(prov.Provenance.resolve(doc, leaf.pointer), 5000)  # baseline untouched


# ── 5. --provenance report ordering ─────────────────────────────────────────


class ReportOrderingTest(unittest.TestCase):
    def test_worst_first_ordering(self):
        doc = _doc(provenance={
            "/people/0/legal_name": {"confidence": "unknown"},
            "/estate/default_spousal_rollover": {
                "confidence": "assumed", "domain": [True, False],
            },
            "/people/0/room/tfsa/contribution_room": {
                "confidence": "stated", "plausible_range": [0, 102000],
            },
            "/accounts/0/balance/amount": {"confidence": "derived"},
            "/people/1/id": {
                "confidence": "measured",
                "source": "file:///fake/s.pdf#page=1",
                "as_of": "2026-06-30",
            },
            # /people/0/id gets no entry -> implicit "assumed, no range" --
            # must rank worse than the explicit "assumed" entry above.
        })
        p = prov.load_provenance(doc)
        report_lines = prov.build_report(doc, p).splitlines()[1:]  # skip header
        pointer_order = [line.split()[1] for line in report_lines]

        def rank_of(pointer):
            return pointer_order.index(pointer)

        self.assertLess(rank_of("/people/0/legal_name"), rank_of("/people/0/id"))
        self.assertLess(rank_of("/people/0/id"), rank_of("/estate/default_spousal_rollover"))
        self.assertLess(
            rank_of("/estate/default_spousal_rollover"),
            rank_of("/people/0/room/tfsa/contribution_room"),
        )
        self.assertLess(
            rank_of("/people/0/room/tfsa/contribution_room"),
            rank_of("/accounts/0/balance/amount"),
        )
        self.assertLess(rank_of("/accounts/0/balance/amount"), rank_of("/people/1/id"))


# ── 6. Architecture: the shipped example must have valid provenance ────────


class ExampleProvenanceTest(unittest.TestCase):
    def test_example_json_has_a_provenance_block(self):
        doc = _load_example()
        self.assertIn("provenance", doc)
        self.assertTrue(doc["provenance"])

    def test_example_json_provenance_validates(self):
        doc = _load_example()
        p = prov.load_provenance(doc)  # raises on any violation
        self.assertGreaterEqual(len(p.entries), 5)

    def test_example_json_demonstrates_every_confidence_level(self):
        doc = _load_example()
        p = prov.load_provenance(doc)
        levels = {entry["confidence"] for entry in p.entries.values()}
        self.assertEqual(levels, set(prov.CONFIDENCE_LEVELS))

    def test_example_json_still_passes_full_contract_validation(self):
        # The provenance block must not break input_contract's own
        # composed-schema validation (additionalProperties:false at root
        # would reject an unrecognized key outright).
        doc = _load_example()
        contract_schema.validate_contract(doc)  # raises on failure


if __name__ == "__main__":
    unittest.main()
