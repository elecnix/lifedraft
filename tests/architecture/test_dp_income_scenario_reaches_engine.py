"""Architecture enforcement for issue #665: "decisions.income[] is parsed,
mapped, and then silently dropped."

``tests/test_schema_coverage.py`` already certifies ``decisions.income[]``'s
LEAVES as CONSUMED (``decisions.income[].overrides[].amount`` is cited to
``input_contract.py``'s ``sc_members.append(...)`` line). That citation is
TRUE and was never wrong -- ``input_contract.py`` really does read the leaf.
But #665's whole point is that a leaf being read by the contract ADAPTER is
not the same claim as the BLOCK reaching the ENGINE: ``input_contract.py``
mapped the leaf onto ``cfg['scenarios']['income']`` and nothing downstream
ever read *that* key, so a household's declared income scenarios never
influenced a single simulated number. The schema-coverage guard was, and
remains, green throughout.

This is the same distinction DP#18's dead-write tests draw for overlays
(``tests/architecture/test_dp18_dead_write.py``): "not verified by a test
that asserts the merged config changed; verified by a test that runs the
ENGINE ... and asserts the OUTPUT changed." This file applies that same
behavioural standard to ``decisions.income[]`` specifically, end-to-end from
the real contract document (not a hand-built internal dict), closing the
exact gap #665 identifies.

Scope note: the issue asks for an architecture test covering "every
decisions.* block declared in the schema." Generalizing that fully is a
larger, separate investment -- several decisions.* blocks (e.g.
decisions.contribution_strategy, decisions.estate_elections) are HONESTLY
documented in test_schema_coverage.py's DEAD_ALLOWLIST as not wired to any
consumer yet, which is the correct, non-silent state for them today, not a
bug. This file targets the ONE block #665 is about, with the reach itself
verified behaviourally rather than by citation -- the template the same
sweep can be extended with as each further decisions.* block gets wired.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import input_contract as ic
import optimize
import contract_schema

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _two_generation_subset(doc: dict) -> dict:
    """Trim the shipped example.json (a fabricated 4-generation household,
    DP#4/DP#15) down to the couple + their direct children the legacy engine
    can run (#598's documented Phase 1 limit) -- same technique
    tests/test_input_contract.py uses to exercise the real adapter output
    against a runnable config. schema/example.json's own decisions.income
    already declares two scenarios ("stay at current jobs" and "primary
    promoted to director") -- untouched by this trim, since decisions.* is
    not people-scoped."""
    doc = copy.deepcopy(doc)
    keep_people = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep_people]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep_people]
    doc["accounts"] = [a for a in doc["accounts"] if _owner_ids(a["owner"]) <= keep_people]
    doc["liabilities"] = [l for l in doc["liabilities"] if _owner_ids(l["owner"]) <= keep_people]
    doc["properties"] = [p for p in doc["properties"] if _owner_ids(p["owner"]) <= keep_people]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}
    ]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep_people
    ]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep_people
    ]
    return doc


def _load_runnable_cfg() -> dict:
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = json.load(f)
    doc = _two_generation_subset(doc)
    return ic.to_internal_config(doc)


class TestDecisionsIncomeReachesTheEngine(unittest.TestCase):
    """Behavioural, not citation-based: prove decisions.income[] -- via the
    REAL contract document, the REAL adapter, and the REAL optimizer
    pipeline -- changes simulated output. A leaf-read citation in
    test_schema_coverage.py cannot make this claim; only running the engine
    can (DP#18's own standard, applied here)."""

    def test_contract_declares_multiple_income_scenarios(self):
        """Sanity check on the fixture itself: schema/example.json's
        decisions.income has more than one entry, or this test proves
        nothing."""
        with open(contract_schema.EXAMPLE_PATH) as f:
            doc = json.load(f)
        self.assertGreater(len(doc["decisions"]["income"]), 1)

    def test_every_declared_scenario_reaches_the_ranked_output(self):
        """This is #665's exact symptom, inverted: 'a contract declaring
        three income scenarios produced a ranked table containing none of
        them.' Every declared scenario id must now appear."""
        with open(contract_schema.EXAMPLE_PATH) as f:
            doc = json.load(f)
        declared_ids = {sc["id"] for sc in doc["decisions"]["income"]}

        cfg = _load_runnable_cfg()
        results = optimize.run_income_scenario_exploration(cfg)
        reached_ids = {r["income_scenario_id"] for r in results}

        self.assertEqual(
            declared_ids, reached_ids,
            "every id in decisions.income[] must appear in the ranked "
            "optimizer output -- a leaf being read by input_contract.py is "
            "not the same as the block reaching the engine (#665)."
        )

    def test_income_scenarios_move_the_engines_actual_output(self):
        """The DP#18 standard: not 'the merged config changed', but 'the
        engine's OUTPUT changed'. schema/example.json's 'p1_promotion'
        scenario raises the primary's income; the best net_benefit under it
        must differ from the 'stay' scenario's -- proof the override
        actually reached simulate_year_pure, not just a results dict key."""
        cfg = _load_runnable_cfg()
        results = optimize.run_income_scenario_exploration(cfg)

        def best_net_benefit(scenario_id):
            rows = [r for r in results if r["income_scenario_id"] == scenario_id]
            self.assertTrue(rows, f"no ranked results for scenario {scenario_id!r}")
            return max(r.get("net_benefit", 0) for r in rows)

        stay_best = best_net_benefit("stay")
        promotion_best = best_net_benefit("p1_promotion")
        self.assertNotEqual(
            round(stay_best), round(promotion_best),
            "decisions.income[] scenarios produced IDENTICAL engine output "
            "-- the block is a dead write (#665-class bug: parsed, mapped, "
            "never actually consumed by the simulation)."
        )


if __name__ == "__main__":
    unittest.main()
