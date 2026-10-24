"""Issue #384: a single engine run must not SILENTLY substitute declared decisions.

Found by the #319 planner (PR #383) and confirmed on ``main``. A single
``FamilySimulation.run()`` of a contract document substitutes decisions the
document DECLARED, with no refusal and no disclosure, whether the consumer is a
direct engine call or any CLI that runs one fold.

Two substitutions, two very different blast radii:

1. **Retirement age collapses to the first candidate.** ``contract_people``
   maps ``member["retirement_age"] = cand["candidate_ages"][0]``; the engine
   simulates that value (``simulation.py`` reads ``member["retirement_age"]``).
   A household declaring ``[60, 62, 65]`` is simulated at 60 and nothing says
   the others were dropped. The OPTIMIZER is a legitimate consumer of the first
   value -- DP#5 makes candidate 0 the ANCHOR a sweep then varies, and the
   optimizer enumerates the rest via ``discover_anchors`` -- so this is not a
   bug there. It IS a bug for a single fold, which evaluates exactly one point.

   The single-run SURFACE already refuses such a document (``tools/examples.py``
   ``simulate_input_problems`` / ``SIMULATE_DECISIONS["retirement_age"] ==
   "single_candidate"``). What that guard does NOT cover is a DIRECT engine call
   (``input_contract.to_internal_config`` -> ``SimulationConfig.from_dict`` ->
   ``FamilySimulation.run()``) that bypasses the surface, which is the path this
   module pins.

2. **The implicit default strategy is the Smith Manoeuvre.** With no strategy
   passed, ``FamilySimulation.__init__`` falls back to
   ``adapter.get_default_strategy()``, which returns
   ``STRATEGY_READVANCE_PRIORITY`` -- a LEVERAGED allocation (55% non-reg,
   ``prioritize_readvanceable=True``, ``deduct_later=True``). A leveraged
   posture as the silent fallback for ABSENT input is an opinion, not a
   fallback (DP#13), and the household declared none of it.

These are engine-driven tests: each drives ``input_contract.load_and_map`` ->
``SimulationConfig.from_dict`` -> ``FamilySimulation.run()`` and asserts on what
the engine actually did, never on an intermediate the test built by hand
(DP#11). The contract is the shipped two-adult example, trimmed to the couple
(DP#15: fabricated round numbers, role-based ids only).
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
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.adapter import CanadaAdapter

_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "schema", "example.json")


def _couple_doc() -> dict:
    """The shipped example trimmed to the couple + children (the shape the
    Phase-1 engine simulates; the full four-generation example is correctly
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


def _single_run(doc: dict):
    """The path #384 is about: map a contract, then run ONE fold, choosing no
    strategy (so the engine takes its own default)."""
    cfg = input_contract.to_internal_config(copy.deepcopy(doc))
    config = SimulationConfig.from_dict(cfg)
    return FamilySimulation(config, adapter=CanadaAdapter(config)), config


class SingleRunRetirementAgeSubstitution(unittest.TestCase):
    """Half 1 of #384: a multi-candidate retirement age must not silently become
    ``candidate_ages[0]`` for a single fold.

    STILL OPEN, and deliberately pinned as an expected failure rather than
    dropped: the fix needs a seam this PR does not take. ``contract_people``
    must keep writing candidate 0 as the anchor the optimizer's sweep varies
    (DP#5), so the refusal cannot live in the mapping without also carrying the
    full candidate list onto the member and opting the optimizer's fold sites
    in explicitly. That is a contract change across ``contract_people`` ->
    ``SimulationConfig`` -> ``FamilySimulation`` and belongs in its own PR with
    the maintainer's sign-off (issue #384 offers two designs: refuse, or
    single-point-with-disclosure).

    Note the SHIPPED single-run surface is already covered: ``tools/examples.py``
    ``simulate_input_problems`` refuses a multi-candidate document before it
    maps (#383). What is missing is a DIRECT engine call, which is what these
    two tests exercise. When someone fixes the seam they must delete the
    ``expectedFailure`` markers -- an "unexpected success" here is the signal
    that the open half landed and the ticket can close.
    """

    def _multi_candidate_doc(self) -> dict:
        doc = _couple_doc()
        for entry in doc["decisions"]["retirement_age"]:
            entry["candidate_ages"] = [60, 65]
        return doc

    @unittest.expectedFailure
    def test_a_direct_single_run_REFUSES_a_multi_candidate_retirement_age(self):
        """The loud failure is the fix: this run evaluates ONE point, and
        candidate 0 is not that household's choice. The refusal names the
        person and the declared candidates (DP#32: never silently coerce)."""
        doc = self._multi_candidate_doc()
        with self.assertRaises(Exception) as ctx:
            _single_run(doc)
        message = str(ctx.exception)
        self.assertIn("60", message)
        self.assertIn("65", message)

    def test_a_single_declared_candidate_still_runs(self):
        """Control (NOT expected to fail): one candidate is not a substitution --
        the run proceeds and simulates exactly that age. Any fix for the open
        half must not fire on an honest single-point declaration."""
        doc = _couple_doc()
        for entry in doc["decisions"]["retirement_age"]:
            entry["candidate_ages"] = [65]
        sim, config = _single_run(doc)
        results = sim.run()
        primary = next(m for m in config.adults() if m["role"] == "primary")
        self.assertEqual(primary["retirement_age"], 65)
        self.assertTrue(results)


class SingleRunDefaultStrategyIsNotLeveraged(unittest.TestCase):
    """Half 2: the implicit default strategy must be the documented NEUTRAL
    baseline, not the Smith Manoeuvre (DP#13: a default is a fallback for
    absent input, never an opinion)."""

    def test_the_default_strategy_does_not_prioritize_readvancing(self):
        sim, _ = _single_run(_couple_doc())
        self.assertFalse(
            sim.strategy.prioritize_readvanceable,
            f"the implicit single-run default strategy is leveraged "
            f"({sim.strategy.name!r}); a household that declared no strategy "
            f"must not silently get the Smith Manoeuvre (DP#13, issue #384)",
        )
        self.assertFalse(sim.strategy.deduct_later)

    def test_the_default_is_the_neutral_baseline_strategy(self):
        from countries.canada.strategies import STRATEGY_NO_READVANCE
        sim, _ = _single_run(_couple_doc())
        self.assertEqual(sim.strategy.name, STRATEGY_NO_READVANCE.name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()