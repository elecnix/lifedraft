#!/usr/bin/env python3
"""Tests for issue #993: sleeve return beliefs are pluggable INPUT (DP#21).

Two halves, because the belief's journey has two hops and #993's fix landed in
two PRs:

1. The ENGINE half (#1028, already on main): ``risk_allocation`` resolves the
   sleeve means/sigmas through ``_resolve_return_beliefs`` -- config block
   ``assumptions.return_beliefs`` over the documented module defaults, with an
   explicit ``return_beliefs=`` parameter winning over both (DP#13: a supplied
   value is never coerced). Those behaviours are pinned by
   ``tests/test_issue_474_risk_allocation.py``; not re-spelled here (DP#9).

2. The REACHABILITY half (this file): #1028 left the config block undeclarable
   -- the schema's ``additionalProperties: false`` REFUSED
   ``assumptions.return_beliefs`` at the one loading boundary, so a contract
   document could not actually plug its beliefs in. These tests drive the REAL
   path -- a (fabricated, DP#15) contract document through
   ``input_contract.validate_contract`` / ``to_internal_config`` into
   ``risk_allocation.recommend_allocation`` -- to pin that a declared belief
   block loads, reaches the internal config verbatim, and changes the
   recommended mix's Monte Carlo metrics; that a PARTIAL declaration merges
   over the defaults instead of zeroing the undeclared sleeves (DP#32); and
   that an explicit parameter still beats the document (DP#13).

The example document declares the block AT the engine defaults, so the shipped
fixture stays behaviour-neutral; the guards
(``tests/test_schema_coverage.py``'s CONSUMED citations and
``tests/architecture/test_contract_reachability.py``'s mutation probe) keep
the mapping from silently dying.
"""

import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tests", "architecture"))

import input_contract as ic
from test_dp_income_scenario_reaches_engine import _two_generation_subset

import risk_allocation

# Fabricated non-default beliefs (DP#4/DP#15): round, role-free numbers a
# household could plausibly declare instead of the 6.8%/16% / 3.0%/5% defaults.
DECLARED_BELIEFS = {
    "equity_mean": 0.05,
    "equity_sigma": 0.12,
    "fixed_income_mean": 0.04,
    "fixed_income_sigma": 0.03,
}


def _document_with_beliefs(beliefs=None):
    """The two-generation example document, optionally declaring a
    ``return_beliefs`` block (``None`` removes it entirely -- the pre-#993
    shape)."""
    doc = _two_generation_subset(json.loads(ic.EXAMPLE_PATH.read_text()))
    doc = copy.deepcopy(doc)
    if beliefs is None:
        doc["assumptions"].pop("return_beliefs", None)
    else:
        doc["assumptions"]["return_beliefs"] = dict(beliefs)
    return doc


class DocumentLoadTest(unittest.TestCase):
    """HOP 1: the block is declarable and reaches the internal config."""

    def test_declared_block_loads_and_maps_verbatim(self):
        doc = _document_with_beliefs(DECLARED_BELIEFS)
        ic.validate_contract(doc)  # must not refuse the block
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["assumptions"]["return_beliefs"], DECLARED_BELIEFS)

    def test_absent_block_leaves_no_key(self):
        """DP#32: absence is absence -- no coerced empty-dict placeholder that
        a downstream ``or`` could later mistake for a declaration."""
        cfg = ic.to_internal_config(_document_with_beliefs(None))
        self.assertNotIn("return_beliefs", cfg["assumptions"])

    def test_partial_declaration_maps_just_what_was_declared(self):
        doc = _document_with_beliefs({"equity_mean": 0.09})
        ic.validate_contract(doc)
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["assumptions"]["return_beliefs"], {"equity_mean": 0.09})

    def test_unknown_key_inside_the_block_is_refused(self):
        doc = _document_with_beliefs(dict(DECLARED_BELIEFS, small_cap_mean=0.09))
        with self.assertRaises(Exception):
            ic.validate_contract(doc)

    def test_negative_sigma_is_refused(self):
        doc = _document_with_beliefs(dict(DECLARED_BELIEFS, equity_sigma=-0.1))
        with self.assertRaises(Exception):
            ic.validate_contract(doc)

    def test_negative_mean_is_a_legal_belief(self):
        """A deflationary/crash belief for a sleeve is representable, not a
        schema error (DP#32: zero and below-zero are values)."""
        doc = _document_with_beliefs(dict(DECLARED_BELIEFS, fixed_income_mean=-0.01))
        ic.validate_contract(doc)
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["assumptions"]["return_beliefs"]["fixed_income_mean"], -0.01)


class BeliefsReachTheRecommendationTest(unittest.TestCase):
    """HOP 2: the mapped block changes what ``recommend_allocation`` reports.

    The mapped document config lacks only ``portfolio.risk_tolerance`` -- the
    adapter does not thread it from contract documents yet (#917 gate, noted
    on ``optimize._record_risk_allocation``). This test injects it in the
    internally-built-config shape, exactly the reach the module has today, so
    the whole documented path (document -> adapter -> ``recommend_allocation``
    -> ``_resolve_return_beliefs`` -> ``_mc_risk_metrics``) is driven for real;
    no engine state is hand-built.
    """

    def _recommend(self, beliefs, **kwargs):
        cfg = ic.to_internal_config(_document_with_beliefs(beliefs))
        cfg.setdefault("portfolio", {})["risk_tolerance"] = "balanced"
        return risk_allocation.recommend_allocation(cfg, **kwargs)

    def test_declared_beliefs_change_the_risk_metrics(self):
        rec = self._recommend(DECLARED_BELIEFS)
        metrics = rec["risk_metrics"]
        mix = rec["recommended_mix"]
        # The reported blended mean must be the DECLARED blend, not the default
        # blend -- the whole point of DP#21's pluggability.
        expected = (mix["equity_pct"] * DECLARED_BELIEFS["equity_mean"]
                    + mix["fixed_income_pct"] * DECLARED_BELIEFS["fixed_income_mean"])
        self.assertAlmostEqual(metrics["blended_mean"], expected, places=12)
        self.assertNotAlmostEqual(
            metrics["blended_mean"],
            (mix["equity_pct"] * risk_allocation.EQUITY_MEAN
             + mix["fixed_income_pct"] * risk_allocation.FIXED_INCOME_MEAN),
            places=12)

    def test_no_declaration_reproduces_the_default_blend(self):
        """Byte-identical default behaviour when the document declares nothing
        (and the shipped example, which declares the block AT the defaults,
        blends to the same numbers)."""
        mix = {"equity_pct": 0.5, "fixed_income_pct": 0.5}
        absent = risk_allocation._mc_risk_metrics(mix, 20)
        at_defaults = risk_allocation._mc_risk_metrics(
            mix, 20, return_beliefs=dict(risk_allocation._DEFAULT_RETURN_BELIEFS))
        self.assertEqual(absent, at_defaults)

    def test_explicit_parameter_beats_the_document(self):
        """DP#13: an explicit ``return_beliefs=`` is never silently overridden
        by the config block (or the constants)."""
        override = {"equity_mean": 0.12, "equity_sigma": 0.20,
                    "fixed_income_mean": 0.01, "fixed_income_sigma": 0.02}
        rec = self._recommend(DECLARED_BELIEFS, return_beliefs=override)
        mix = rec["recommended_mix"]
        expected = (mix["equity_pct"] * override["equity_mean"]
                    + mix["fixed_income_pct"] * override["fixed_income_mean"])
        self.assertAlmostEqual(rec["risk_metrics"]["blended_mean"], expected, places=12)

    def test_partial_declaration_merges_over_defaults(self):
        """Declaring ONLY equity_mean must not zero the other three sleeves
        (DP#32): the undeclared keys keep the documented defaults."""
        rec = self._recommend({"equity_mean": 0.09})
        mix = rec["recommended_mix"]
        resolved = risk_allocation._resolve_return_beliefs(
            {"assumptions": {"return_beliefs": {"equity_mean": 0.09}}})
        self.assertEqual(resolved["equity_mean"], 0.09)
        self.assertEqual(resolved["equity_sigma"],
                         risk_allocation._DEFAULT_RETURN_BELIEFS["equity_sigma"])
        self.assertEqual(resolved["fixed_income_mean"],
                         risk_allocation._DEFAULT_RETURN_BELIEFS["fixed_income_mean"])
        self.assertEqual(resolved["fixed_income_sigma"],
                         risk_allocation._DEFAULT_RETURN_BELIEFS["fixed_income_sigma"])
        expected = (mix["equity_pct"] * 0.09
                    + mix["fixed_income_pct"] * resolved["fixed_income_mean"])
        self.assertAlmostEqual(rec["risk_metrics"]["blended_mean"], expected, places=12)

    def test_example_document_declares_the_defaults(self):
        """The shipped example instantiates the block AT the engine defaults --
        behaviour-neutral by construction, present so the guards measure the
        mapping."""
        doc = json.loads(ic.EXAMPLE_PATH.read_text())
        self.assertEqual(doc["assumptions"]["return_beliefs"],
                         risk_allocation._DEFAULT_RETURN_BELIEFS)


class LoadAndMapEndToEndTest(unittest.TestCase):
    """The one loading boundary every CLI script uses: ``load_and_map``."""

    def test_load_and_map_carries_the_block(self):
        doc = _document_with_beliefs(DECLARED_BELIEFS)
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f)
            cfg = ic.load_and_map(path)
        finally:
            os.remove(path)
        self.assertEqual(cfg["assumptions"]["return_beliefs"], DECLARED_BELIEFS)


if __name__ == "__main__":
    unittest.main()
