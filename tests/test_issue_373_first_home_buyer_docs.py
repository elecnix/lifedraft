"""Issue #373: the published contract must not contradict what the engine accepts.

The engine has supported an ADULT first-time home buyer since #931:
``contract_transfers._map_first_home_purchases`` resolves ``buyer`` against
``child_ids | adult_ids``, and ``simulation_state.apply_adult_first_home_purchases``
sends an adult purchase to the household FHSA store and the buyer's own RRSP /
HBP slot.

The schema descriptions were never updated. ``first_home_purchase.buyer`` still
said the buyer must be "a declared CHILD" and that adult wiring "is a follow-up",
and ``first_home_purchases`` said "(today: a CHILD)". So a contract author — and
any schema-driven tool or model reading those strings — would conclude the CFFP
"first home as a single adult" scenario cannot be encoded, when it can.

**No computed value was wrong.** The defect is in the published contract: the
documentation a household reads to decide what is expressible.

These tests pin the TEXT, because the behavioural tests already pin the
BEHAVIOUR and the two drifted apart exactly once. The guard is deliberately
scoped to the first-home buyer wording: other descriptions legitimately use
"is a follow-up" (a mortgage renewal schedule, an amortization model) and a
blanket ban would be wrong.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contract_schema

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _composed_schema_text() -> str:
    """The composed contract schema as text — the thing a contract author (or a
    schema-driven tool) actually reads, assembled from the fragments."""
    composed = contract_schema.compose_schema()
    return json.dumps(composed)


class BuyerIsNotDescribedAsChildOnly(unittest.TestCase):
    """The `buyer` description must name BOTH a child and an adult member."""

    def test_the_composed_schema_does_not_say_today_a_child(self):
        text = _composed_schema_text()
        self.assertNotIn(
            'today: a CHILD', text,
            "issue #373: `first_home_purchases` still describes the buyer as "
            "child-only, so the contract says the single-adult CFFP scenario "
            "cannot be encoded when the engine has supported it since #931")

    def test_the_buyer_leaf_does_not_defer_adult_wiring_to_a_followup(self):
        """Scoped to the BUYER leaf: "is a follow-up" is legitimate elsewhere in
        the schema (a mortgage renewal schedule), so this reads the leaf rather
        than banning the phrase outright."""
        with open(os.path.join(_ROOT, "schema", "defs", "properties.json"),
                  encoding="utf-8") as fh:
            props = json.load(fh)
        buyer = props["$defs"]["first_home_purchase"]["properties"]["buyer"]
        description = buyer["description"]
        self.assertNotIn(
            "is a follow-up", description,
            "issue #373: the buyer leaf still calls adult wiring a follow-up; "
            "it shipped with #931")
        self.assertIn("adult", description.lower(),
                      "the buyer leaf must say an ADULT member may buy")

    def test_the_buyer_leaf_names_the_adult_wiring_actually_used(self):
        with open(os.path.join(_ROOT, "schema", "defs", "properties.json"),
                  encoding="utf-8") as fh:
            props = json.load(fh)
        description = props["$defs"]["first_home_purchase"]["properties"]["buyer"]["description"]
        # The household FHSA store and the buyer's OWN RRSP slot are what the
        # adult fold actually uses (#931), so the description should say so.
        self.assertRegex(description, r"HHSA|FHSA")
        self.assertRegex(description, r"RRSP|HBP")


class TheBehaviourItselfIsUnchanged(unittest.TestCase):
    """The behavioural tests for this already exist (#704/#931). This module
    only fixes TEXT, so the boundary it pins is that the text now agrees with
    them: an adult buyer is accepted, and only an id matching no member is
    refused."""

    def test_an_adult_buyer_still_reaches_the_internal_config(self):
        import input_contract
        from test_issue_704_child_fhsa_hbp import _two_generation_doc
        doc = _two_generation_doc([{"buyer": "p1", "year": 2027}])
        cfg = input_contract.to_internal_config(doc)
        self.assertEqual(cfg["family"]["first_home_purchases"],
                         [{"buyer": "p1", "year": 2027}])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()