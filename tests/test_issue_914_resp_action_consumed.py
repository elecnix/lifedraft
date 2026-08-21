#!/usr/bin/env python3
"""Issue #914: declared ``resp_action`` (EAP vs collapse) must be CONSUMED.

A household's declared RESP action resolves to net proceeds that
``apply_overlay`` books as ``property.free_cash`` and ``evaluate_overlay``
threads to ``FamilySimulation(free_cash=...)``. Before this fix the engine
stored ``self.free_cash`` and never read it, so collapsing an RESP produced
BYTE-IDENTICAL output to keeping it -- a dead READ (the #846 class, caught by
the #883 dead-read guard).

These lean, invariant-based tests pin the fix:

1. ``free_cash`` is invested as a year-0 lump and LIFTS terminal net worth --
   more proceeds -> more terminal ``total_assets`` (the whole point of asking
   the tool to rank collapse vs keep).
2. Wiring it does NOT invent a solvency shortfall: a household WITH a declared
   living-cost budget is not falsely ruined by the year-0 free-cash
   contribution (the inflow funds the outflow, like ``borrowed_investment``).
3. Absence is a no-op (DP#32): no ``free_cash`` == ``free_cash=0.0``,
   byte-identical trajectory.

Run with: python3 -m pytest tests/test_issue_914_resp_action_consumed.py -v
"""

import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import FamilySimulation
from simulation_config import SimulationConfig


def _config(*, living_costs=None):
    return SimulationConfig(
        projection_years=15,
        investment_return=0.06,
        house_value=700000, mortgage_balance=250000, mortgage_rate=0.05,
        start_year=2026,
        savings_rate=0.15,
        living_costs=living_costs,
        family_members=[
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1985,
             'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 50000},
            {'role': 'spouse', 'gross_income': 95000, 'birth_year': 1986,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 50000},
        ],
        children=[],
    )


def _terminal_assets(free_cash):
    sim = FamilySimulation(_config(), free_cash=free_cash)
    return sim.run()[-1].total_assets


class TestFreeCashIsConsumed(unittest.TestCase):
    """The #914 dead READ: free_cash must actually enter the simulation."""

    def test_free_cash_lifts_terminal_net_worth(self):
        """Collapse proceeds invested at year 0 raise terminal total_assets."""
        base = _terminal_assets(0.0)
        with_proceeds = _terminal_assets(50000.0)
        self.assertGreater(
            with_proceeds, base,
            "declared RESP-collapse proceeds (free_cash) were discarded: the "
            "engine stored self.free_cash and never read it (#914)")

    def test_more_proceeds_more_terminal(self):
        """EAP (55,500) vs collapse (36,833) must NOT rank identically."""
        eap = _terminal_assets(55500.0)
        collapse = _terminal_assets(36833.0)
        self.assertGreater(eap, collapse)

    def test_free_cash_does_not_invent_a_shortfall(self):
        """A budgeted household is not falsely ruined by the year-0 inflow."""
        budgeted = FamilySimulation(
            _config(living_costs=60000), free_cash=50000.0).run()
        # The invested free cash is an inflow AND an outflow in year 0 -- the
        # solvency identity must not force-sell it back off (a false ruin).
        self.assertGreater(budgeted[-1].total_assets, 0)
        base = FamilySimulation(
            _config(living_costs=60000), free_cash=0.0).run()
        self.assertGreater(budgeted[-1].total_assets, base[-1].total_assets)


class TestAbsenceIsNoOp(unittest.TestCase):
    """DP#32: no free_cash declared == free_cash=0.0, byte-identical."""

    def test_default_equals_zero(self):
        default = FamilySimulation(_config()).run()
        explicit_zero = FamilySimulation(_config(), free_cash=0.0).run()
        self.assertEqual(
            [dataclasses.asdict(r) for r in default],
            [dataclasses.asdict(r) for r in explicit_zero],
        )


if __name__ == "__main__":
    unittest.main()
