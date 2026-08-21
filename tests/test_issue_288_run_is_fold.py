#!/usr/bin/env python3
"""Tests for issue #288 (DP#26): FamilySimulation.run() is a fold over steps.

DP#26: the simulation step is a pure function over explicit state and ``run`` is
a fold over steps. ``run`` now reduces ``_simulate_year_step`` over the
projection years, threading the immutable ``SimState`` from year to year with no
imperative accumulation in the loop body.

These tests guard:
1. Structural — each annual step is pure: it neither mutates the incoming state
   nor ``self``, and yields the same (result, next_state) for the same inputs.
2. Behavioural — re-folding ``_simulate_year_step`` by hand reproduces exactly
   what ``run()`` returns, and the final ``self._state`` matches the folded
   state (the run-is-a-fold equality).

Run with: python3 -m pytest tests/test_issue_288_run_is_fold.py -v
"""

import copy
import dataclasses
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from functools import reduce

from simulation import FamilySimulation
from simulation_config import SimulationConfig


def _make_config(projection_years=12):
    """Quebec household with a CRI/LIRA so the LIF-conversion path is exercised."""
    return SimulationConfig(
        projection_years=projection_years,
        investment_return=0.06,
        house_value=600000, mortgage_balance=300000, mortgage_rate=0.05,
        margin_available=80000, start_year=2026,
        savings_rate=0.20,
        family_members=[
            {'role': 'primary', 'gross_income': 120000, 'birth_year': 1979,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 60000,
             'retirement_age': 65},
            {'role': 'spouse', 'gross_income': 85000, 'birth_year': 1982,
             'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 55000,
             'retirement_age': 65},
        ],
        children=[],
        lira_data={'balance': 90000, 'birth_year': 1979,
                   'jurisdiction': 'quebec', 'reference_rate': 0.06},
    )


def _asdict_list(results):
    return [dataclasses.asdict(r) for r in results]


class TestRunIsAFoldOverSteps(unittest.TestCase):
    """run() == reduce(_simulate_year_step) over the projection years."""

    def test_run_equals_manual_fold(self):
        sim = FamilySimulation(_make_config(12))

        # Reproduce run() by hand as an explicit fold over the same pure step.
        def step(acc, year):
            results, state = acc
            result, next_state = sim._simulate_year_step(state, year)
            results.append(result)
            return results, next_state

        folded_results, folded_state = reduce(
            step, range(sim.config.projection_years), ([], sim._state))

        # run() must yield byte-identical YearResults and the same final state.
        run_results = sim.run()
        self.assertEqual(_asdict_list(run_results), _asdict_list(folded_results))
        self.assertEqual(
            dataclasses.asdict(sim._state),
            dataclasses.asdict(folded_state),
        )

    def test_run_is_deterministic(self):
        """Determinism guard: two fresh runs produce identical year-by-year output."""
        a = _asdict_list(FamilySimulation(_make_config(25)).run())
        b = _asdict_list(FamilySimulation(_make_config(25)).run())
        self.assertEqual(a, b)


class TestStepIsPure(unittest.TestCase):
    """_simulate_year_step must not mutate the incoming state or self."""

    def test_step_does_not_mutate_inputs(self):
        sim = FamilySimulation(_make_config(12))
        state0 = sim._state
        state0_before = dataclasses.asdict(state0)

        result_a, next_a = sim._simulate_year_step(state0, 0)
        # The incoming state object is unchanged (immutable fold input).
        self.assertEqual(dataclasses.asdict(state0), state0_before)
        # self._state is untouched by an individual step (only run() publishes).
        self.assertEqual(dataclasses.asdict(sim._state), state0_before)

        # Same inputs → same outputs (referential transparency).
        result_b, next_b = sim._simulate_year_step(state0, 0)
        self.assertEqual(dataclasses.asdict(result_a), dataclasses.asdict(result_b))
        self.assertEqual(dataclasses.asdict(next_a), dataclasses.asdict(next_b))


if __name__ == "__main__":
    unittest.main()
