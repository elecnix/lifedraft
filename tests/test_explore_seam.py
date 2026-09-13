#!/usr/bin/env python3
"""Issue #232 (slice 2): ``explore(dimension, cfg)`` is one seam over the
existing exploration entry points -- and proves it returns IDENTICAL rankings
for every dimension before anything is deleted.

Slice 2's contract is: introduce ``explore()`` alongside the old entry
points, dispatch to the existing sweep logic, and prove the new seam
produces exactly what the old path produces. Each test below runs the SAME
dimension through BOTH paths -- the old ``run_*_exploration`` entry point
and ``explore()`` -- on deep-copied identical configs, and asserts the
returned lists are equal: same rows, same order, same keys, same values.
List equality is the strongest ranking-equality claim available: the ranked
rows carry each cell's ``net_benefit``/``objective_score`` and every tag the
reports read (``ltv``, ``structure_id``, ``income_scenario_id``,
``property_funding_id``, ``borrow_to_invest_id``), so a seam that reordered,
dropped, or re-tagged a single row fails this test.

The configs are the exact fixtures the corresponding issue tests use
(LTV: tests/test_optimize.py; income scenarios: #665; mortgage structure:
#687; property funding: #1011; borrow-to-invest: #1036) -- fabricated round
numbers and role-based names (DP#4/DP#15).

The dimension is a small closed set of strings (DP#8: data, not a class
hierarchy), validated loudly: an unknown dimension raises (DP#32), it never
silently returns an empty ranking that reads as a clean result.

This enforcement lands with the seam it pins. Slice 4 deletes the entry
points this file compares against; it will be rewritten then, not carried
forward as dead comparison code.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
import optimize
from objective import MAX_NET_BENEFIT


def _ltv_cfg():
    """Same LTV-ladder fixture as TestRunLTVExploration (test_optimize.py)."""
    from test_optimize import _make_test_cfg
    return _make_test_cfg(house_value=500_000, mortgage_balance=100_000)


def _income_scenario_cfg():
    """Same three-scenario fixture as test_issue_665."""
    from test_issue_665_income_scenarios import _fixture_cfg
    return _fixture_cfg([
        {"id": "stay", "label": "Stay at current job", "members": []},
        {"id": "salary_cut", "label": "Salary cut to $90k",
         "members": [{"role": "primary", "gross_income": 90000,
                      "kind": "employment", "from": "2026-01-01",
                      "to": None}]},
        {"id": "job_loss", "label": "Job loss, EI only",
         "members": [{"role": "primary", "gross_income": 24000,
                      "kind": "ei", "from": "2026-01-01", "to": None}]},
    ])


def _structure_cfg():
    """Same (refinance option x structure) fixture as test_issue_687."""
    from test_issue_687_mortgage_structure import (
        ALL_MORTGAGE,
        READVANCEABLE,
        SPLIT_WITH_LINE,
        _two_gen_doc,
    )
    doc = _two_gen_doc()
    doc["decisions"]["mortgage"]["structure_options"] = [
        ALL_MORTGAGE, READVANCEABLE, SPLIT_WITH_LINE]
    return ic.to_internal_config(doc)


def _property_funding_cfg():
    """Same (funding option x income) fixture as test_issue_1011."""
    from test_input_contract import _load_example, _two_generation_subset
    from test_issue_1011_property_funding_decision import (
        _ALL_CASH,
        _MORTGAGE_20,
        _MORTGAGE_50,
        _add_rental,
        _cfg,
    )
    base = _two_generation_subset(_load_example())
    return _cfg(_add_rental(
        base, funding_options=[_ALL_CASH, _MORTGAGE_20, _MORTGAGE_50]))


def _borrow_to_invest_cfg():
    """Same mortgage-free-HELOC fixture as test_issue_1036."""
    from test_issue_1036 import _btv_option, _mortgage_free_doc
    doc = _mortgage_free_doc()
    doc["decisions"]["borrow_to_invest"] = [
        _btv_option("btv_50k", "Draw $50k", 50_000),
        _btv_option("btv_100k", "Draw $100k", 100_000),
    ]
    return ic.to_internal_config(doc)


# (dimension, old entry point, cfg builder, dimension-specific kwargs).
# The kwargs ride through ``explore()``'s ``**kwargs`` unchanged, so each arm
# exercises the same forwarding the production caller (main()) will rely on.
_DIMENSIONS = [
    # A custom ladder exercises kwargs forwarding too: explore('ltv', ...,
    # ltv_steps=...) must honour the caller's ladder, not silently substitute
    # the DEFAULT_LTV_LADDER.
    ("ltv", optimize.run_ltv_exploration, _ltv_cfg,
     {"ltv_steps": [0.0, 0.50]}),
    ("income_scenario", optimize.run_income_scenario_exploration,
     _income_scenario_cfg, {}),
    ("mortgage_structure", optimize.run_mortgage_structure_exploration,
     _structure_cfg, {}),
    ("property_funding", optimize.run_property_funding_exploration,
     _property_funding_cfg, {}),
    ("borrow_to_invest", optimize.run_borrow_to_invest_exploration,
     _borrow_to_invest_cfg, {}),
]


class TestExploreMatchesOldEntryPoint(unittest.TestCase):
    """The slice-2 contract: for every dimension, explore() returns EXACTLY
    the old entry point's ranked rows, on identical input."""

    def test_identical_rankings_per_dimension(self):
        for dimension, entry, builder, kwargs in _DIMENSIONS:
            with self.subTest(dimension=dimension):
                cfg_new = builder()
                cfg_old = copy.deepcopy(cfg_new)
                old = entry(cfg_old, objective=MAX_NET_BENEFIT, **kwargs)
                self.assertTrue(old, f"{dimension}: fixture produced no rows")
                new = optimize.explore(
                    dimension, cfg_new, objective=MAX_NET_BENEFIT, **kwargs)
                self.assertEqual(
                    new, old,
                    f"explore({dimension!r}) rankings differ from the old "
                    f"entry point -- identical inputs must produce identical "
                    f"ranked rows (issue #232 slice 2)")


class TestExploreRejectsUnknownDimension(unittest.TestCase):
    """DP#32: an unknown dimension is refused loudly -- never a silently
    empty ranking that reads as a clean result."""

    def test_unknown_dimension_raises_value_error(self):
        cfg = _income_scenario_cfg()
        with self.assertRaises(ValueError) as ctx:
            optimize.explore("not_a_dimension", cfg)
        message = str(ctx.exception)
        self.assertIn("unknown exploration dimension", message)
        # The refusal names the closed set so a wrong spelling self-corrects.
        self.assertIn("income_scenario", message)
        self.assertIn("borrow_to_invest", message)


if __name__ == "__main__":
    unittest.main()
