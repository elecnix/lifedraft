#!/usr/bin/env python3
"""Tests for issue #136: the mixed-pot MER limitation is disclosed at runtime.

When a household declares `mer` on accounts of a growth-pot kind where OTHER
accounts of the same kind declare NO `mer`, AND every MER-flagged account opens
at $0, the loader sets that kind's `mer_rate` to 0.0 (the #136 mixed-pot
fallback). The engine tracks per-owner pots, not per-account sub-balances, so
the loader cannot know what share of future contributions flows to the flagged
accounts — charging the whole pot the flagged rate would tax the non-flagged
money a fee it never declared. Setting `mer_rate = 0.0` is the honest
mechanism, but the limitation must be DISCLOSED at runtime (model_fidelity),
not silent: the flagged accounts' fee is unmodeled for the whole run.

These tests drive the contract loader end-to-end (``input_contract.to_internal_config``)
and assert the ``mer_mixed_pot_zero_fee_unmodeled`` approximation fires only
when it should. All test data uses fabricated round numbers (DP#13/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import contract_schema
import input_contract as ic
import model_fidelity
from test_issue_691_mer import _load_example_doc


APPROX_ID = 'mer_mixed_pot_zero_fee_unmodeled'


def _active_ids(legacy_cfg):
    """The ids of model_fidelity approximations active for this run's config."""
    return {a.id for a in model_fidelity.active_approximations(legacy_cfg,
                                                              objective_name=None)}


def _findings(legacy_cfg):
    """The per-run findings lines for the #136 approximation (empty if not active)."""
    ctx = model_fidelity.FidelityContext(cfg=legacy_cfg, objective_name=None)
    for a in model_fidelity.active_approximations(legacy_cfg,
                                                  objective_name=None):
        if a.id == APPROX_ID:
            return a.findings_for(ctx)
    return []


class TestMerMixedPotDisclosure(unittest.TestCase):
    def _rrsps(self, doc):
        return [a for a in doc["accounts"] if a["kind"] == "rrsp"]

    def test_disclosure_fires_for_mixed_pot_zero_flagged_account(self):
        """A RRSP with a declared MER opening at $0 coexists with a non-MER
        RRSP -> the mixed-pot fallback sets mer_rate=0.0 and the #136
        approximation MUST fire, naming the flagged and non-flagged accounts."""
        doc = _load_example_doc()
        rrsps = self._rrsps(doc)
        # p1_rrsp: declare a MER and open at $0 (flagged, $0).
        rrsps[0]["mer"] = 0.0116
        rrsps[0]["balance"]["amount"] = 0.0
        # p2_rrsp: NO mer (non-flagged) -> the pot is MIXED.
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)

        self.assertIn(APPROX_ID, _active_ids(legacy))
        findings = _findings(legacy)
        self.assertEqual(len(findings), 1)
        # The finding names the kind and both the flagged and non-flagged ids.
        self.assertIn("'rrsp'", findings[0])
        self.assertIn("'p1_rrsp'", findings[0])
        self.assertIn("'p2_rrsp'", findings[0])

    def test_disclosure_does_not_fire_for_single_flagged_zero_pot(self):
        """Case (i): a SINGLE-flagged pot (every RRSP declares a MER) opening
        at $0 -> mer_rate falls back to the max declared MER (not 0.0), so the
        fee IS modeled. The #136 approximation MUST NOT fire."""
        doc = _load_example_doc()
        for a in self._rrsps(doc):
            a["mer"] = 0.0116
            a["balance"]["amount"] = 0.0
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)

        self.assertNotIn(APPROX_ID, _active_ids(legacy))
        # And the loader did not record a disclosure on assumptions.
        self.assertNotIn("mer_mixed_pot", legacy.get("assumptions", {}))

    def test_disclosure_does_not_fire_without_mer_declarations(self):
        """Case (ii): no account declares a `mer` at all -> no mer_drag, no
        fallback, no disclosure. The #136 approximation MUST NOT fire."""
        doc = _load_example_doc()
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)

        self.assertNotIn(APPROX_ID, _active_ids(legacy))
        self.assertNotIn("mer_mixed_pot", legacy.get("assumptions", {}))

    def test_disclosure_does_not_fire_for_funded_mixed_pot(self):
        """Bonus: a MIXED pot where the flagged account is FUNDED ($0 opening
        is the trigger) -> mer_rate is the balance-weighted average (non-zero),
        the fee IS modeled, so the #136 approximation MUST NOT fire."""
        doc = _load_example_doc()
        rrsps = self._rrsps(doc)
        # p1_rrsp: declare a MER, keep its $210k opening balance (funded).
        rrsps[0]["mer"] = 0.0116
        # p2_rrsp: NO mer (non-flagged) -> mixed pot, but flagged account is funded.
        contract_schema.validate_contract(doc)
        legacy = ic.to_internal_config(doc)

        self.assertNotIn(APPROX_ID, _active_ids(legacy))


if __name__ == "__main__":
    unittest.main()