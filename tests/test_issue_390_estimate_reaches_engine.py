"""Issue #390 review: the estimate must REACH the engine, and a test must say so.

Every other #390 test stops at a boundary -- the pure estimator functions, or
the adapter's mapped member dict. None of them proves the number the household
actually reads: ``cpp_income`` in the trajectory.

That is the AGENTS.md trap verbatim: *"`decisions.income[]` reached the config
and stopped there, because the caller never handed it to the optimizer.
`test_schema_coverage` passed -- because the adapter read the leaf. **A leaf
being read by the adapter is not the same as the block reaching the engine.**"*
An adapter test here would still pass if ``cpp_monthly_estimated`` were spelled
so the fold ignores it, or if ``cpp_from_estimate`` stopped reading it.

So this test drives the whole chain and asserts on the ENGINE's output:

    contract document
      -> input_contract.to_internal_config
      -> SimulationConfig.from_dict
      -> FamilySimulation.run()
      -> YearResult.cpp_income > 0

for a member who has an employment income and NEITHER a benefits/entitlements
Statement NOR a declared ``earnings_history`` -- i.e. the always-on-from-incomes
path this PR adds. The control asserts the statement path still wins, so the
two can never be confused.

Every figure comes from the shipped two-adult example (fabricated round
numbers, role-based ids -- DP#4/DP#15); no hand-built engine state (DP#11).
"""
from __future__ import annotations

import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import input_contract
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.adapter import CanadaAdapter

from test_issue_390_cpp_estimate_from_incomes import (
    _load_example,
    _primary,
    _strip_statement,
    _two_generation_subset,
)


def _run_contract(doc: dict):
    """contract -> internal config -> ONE real fold (warnings suppressed: the
    adapter logs load-time notices, and this repo's pytest config turns
    ``warnings`` into errors for the ``warnings`` module only)."""
    cfg = input_contract.to_internal_config(doc)
    config = SimulationConfig.from_dict(cfg)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return FamilySimulation(config, adapter=CanadaAdapter(config)).run()


class EstimateReachesTheEngine(unittest.TestCase):
    """The composition claim, proven end to end."""

    def _no_statement_no_history_doc(self) -> dict:
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        _strip_statement(p1)
        p1.pop("earnings_history", None)
        return doc

    def test_an_estimate_with_no_statement_reaches_cpp_income(self):
        doc = self._no_statement_no_history_doc()
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        # The person has a pensionable income and nothing declared to estimate
        # from -- so the estimate must come from the income path.
        self.assertTrue(
            any(inc.get("kind") in ("employment", "self_employment")
                for inc in p1.get("incomes", [])),
            "fixture must give p1 a pensionable income",
        )

        cfg = input_contract.to_internal_config(doc)
        primary_cfg = cfg["family"]["members"][0]
        self.assertEqual(primary_cfg["cpp_benefit_source"], "estimated_from_incomes")
        self.assertGreater(primary_cfg["cpp_monthly_estimated"], 0.0)

        results = _run_contract(doc)
        retired = [r for r in results if r.cpp_income > 0]
        self.assertTrue(
            retired,
            "the estimated CPP never reached the fold: every year reports "
            "cpp_income == 0, which is the silent-zero defect this PR removes",
        )
        # Relational, not a snapshot: the amount tracks the adapter's monthly
        # figure (the engine pays a whole-year multiple of it), so a mapping
        # that stopped feeding the engine cannot pass this.
        expected_monthly = primary_cfg["cpp_monthly_estimated"]
        self.assertAlmostEqual(
            retired[0].cpp_income, expected_monthly * 12, delta=1.0
        )

    def test_a_declared_statement_still_wins_over_the_income_estimate(self):
        """Control: with a Statement declared, the fold is paid the STATEMENT
        amount and the estimate never displaces it."""
        doc = self._no_statement_no_history_doc()
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1["entitlements"] = {
            "cpp": {"estimated_monthly_at_65": 1111, "as_of": "2026-01-01",
                    "claim_age": 65},
        }
        cfg = input_contract.to_internal_config(doc)
        primary_cfg = cfg["family"]["members"][0]
        self.assertEqual(primary_cfg["cpp_benefit_source"], "statement")
        self.assertEqual(primary_cfg["cpp_monthly_estimated"], 1111)

        results = _run_contract(doc)
        retired = [r for r in results if r.cpp_income > 0]
        self.assertTrue(retired)
        self.assertAlmostEqual(retired[0].cpp_income, 1111 * 12, delta=1.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()