#!/usr/bin/env python3
"""Issue #232: ``explore(dimension, cfg)`` is the ONLY exploration surface.

Slice 2 (c55cb89) introduced the seam alongside the five
``run_*_exploration`` entry points and proved, dimension by dimension, that
it returned their rankings byte-for-byte. Slice 4 deleted those entry
points, so the equivalence arms this file used to carry are gone with them
-- an equality assertion needs two sides, and one side no longer exists.
What survives of the original contract is the part that is still testable:
each dimension, driven through its real fixture, returns more than one
scored row -- a sweep that silently returned ``[]`` (or one row) would read
as a clean result while ranking nothing (DP#32) -- and ``explore()``'s
``**kwargs`` reach the sweep (kwargs forwarding was the half of slice 2's
contract that a deletion cannot retire).

The equivalence itself is not re-asserted here because it cannot be: the
proof is the slice-2 commit, and the behavioural guard that the seam still
moves the engine is ``tests/architecture/test_dp18_dead_read.py``, whose
``refinance``/``income`` probes drive ``explore()`` and assert two declared
leaf values rank differently.

What is NEW here is the enforcement slice 4 owes: the five entry points are
GONE and must not come back as re-export shims or deprecation aliases
(DP#9). ``TestOldEntryPointsAreGone`` fails on a ``run_ltv_exploration``
reappearing anywhere on the module, so the deletion is pinned by a test
rather than by memory.

The configs are the exact fixtures the corresponding issue tests use
(LTV: tests/test_optimize.py; income scenarios: #665; mortgage structure:
#687; property funding: #1011; borrow-to-invest: #1036) -- fabricated round
numbers and role-based names (DP#4/DP#15).

The dimension is a small closed set of strings (DP#8: data, not a class
hierarchy), validated loudly: an unknown dimension raises (DP#32), it never
silently returns an empty ranking that reads as a clean result.
"""
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


# (dimension, cfg builder, dimension-specific kwargs). The kwargs ride
# through ``explore()``'s ``**kwargs`` unchanged, so each arm exercises the
# same forwarding the production caller (main()) relies on.
_DIMENSIONS = [
    # A custom ladder exercises kwargs forwarding too: explore('ltv', ...,
    # ltv_steps=...) must honour the caller's ladder, not silently substitute
    # the DEFAULT_LTV_LADDER.
    ("ltv", _ltv_cfg, {"ltv_steps": [0.0, 0.50]}),
    ("income_scenario", _income_scenario_cfg, {}),
    ("mortgage_structure", _structure_cfg, {}),
    ("property_funding", _property_funding_cfg, {}),
    ("borrow_to_invest", _borrow_to_invest_cfg, {}),
]


class TestEveryDimensionSweepsAndRanks(unittest.TestCase):
    """The surviving half of slice 2's contract: each declared dimension,
    driven through its own fixture, actually produces ranked rows. An arm
    that silently returned ``[]`` -- or a single row, which ranks nothing --
    would read as a clean result (DP#32), so the row-count assertion is the
    load-bearing part."""

    def test_every_dimension_returns_non_empty_ranked_rows(self):
        for dimension, builder, kwargs in _DIMENSIONS:
            with self.subTest(dimension=dimension):
                rows = optimize.explore(
                    dimension, builder(), objective=MAX_NET_BENEFIT, **kwargs)
                self.assertGreater(
                    len(rows), 1,
                    f"explore({dimension!r}) returned {len(rows)} row(s) for "
                    f"its own fixture -- the dimension is not wired to the "
                    f"seam, or it produced nothing to rank")
                for row in rows:
                    self.assertIn("objective_score", row)
                    self.assertIn("net_benefit", row)


class TestExploreForwardsDimensionKwargs(unittest.TestCase):
    """kwargs forwarding: the caller's options reach the sweep, rather than
    being replaced by the module's default for that dimension."""

    def test_ltv_steps_replaces_the_default_ladder(self):
        rows = optimize.explore(
            "ltv", _ltv_cfg(), objective=MAX_NET_BENEFIT,
            ltv_steps=[0.0, 0.50])
        self.assertTrue(rows, "fixture produced no rows")
        self.assertEqual(
            sorted({r["ltv"] for r in rows}), [0.0, 0.50],
            "explore('ltv', ..., ltv_steps=[...]) must sweep the caller's "
            "ladder, not DEFAULT_LTV_LADDER")


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


# The five entry points slice 4 deleted (issue #232). Names only -- the point
# is that the module no longer carries them.
_DELETED_ENTRY_POINTS = (
    "run_ltv_exploration",
    "run_income_scenario_exploration",
    "run_mortgage_structure_exploration",
    "run_property_funding_exploration",
    "run_borrow_to_invest_exploration",
)


class TestOldEntryPointsAreGone(unittest.TestCase):
    """DP#9: no backward compatibility, no shims, no deprecation aliases.

    ``explore()`` replaced five public entry points; the deletion is only
    real if something fails when one comes back. A future "back-compat"
    ``run_ltv_exploration = _sweep_ltv`` re-export makes this test the thing
    that catches it, rather than a reviewer's memory of a commit message."""

    def test_no_deleted_entry_point_survives_on_the_module(self):
        for name in _DELETED_ENTRY_POINTS:
            with self.subTest(name=name):
                self.assertFalse(
                    hasattr(optimize, name),
                    f"optimize.{name} still exists -- slice 4 deletes the old "
                    f"entry points; callers use explore() (DP#9: no re-export "
                    f"shim, no deprecation alias)")

    def test_the_seam_routes_every_declared_dimension(self):
        """A dimension in EXPLORE_DIMENSIONS with no dispatch entry would
        pass the ValueError check and then KeyError; a dispatch entry for an
        undeclared dimension would be dead routing. Both are caught by
        asserting the two sets equal."""
        self.assertEqual(
            set(optimize.EXPLORE_DIMENSIONS), set(optimize._EXPLORE_DISPATCH),
            "EXPLORE_DIMENSIONS and _EXPLORE_DISPATCH must be the same closed "
            "set: every declared dimension routed, and nothing routed that "
            "the seam does not declare")
        self.assertEqual(
            set(optimize.EXPLORE_DIMENSIONS),
            {dimension for dimension, _, _ in _DIMENSIONS},
            "every dimension the seam declares must be exercised by an arm "
            "of this test, or a new dimension lands unpinned")


if __name__ == "__main__":
    unittest.main()
