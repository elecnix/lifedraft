#!/usr/bin/env python3
"""Enforcement tests for issue #657 -- a crashing strategy must SURFACE.

The optimizer's per-scenario fold used to wrap the whole simulation in a bare
``except Exception: score = -inf``. Any failure -- a ``KeyError`` from a mis-
mapped config, a ``ZeroDivisionError``, an assertion in a rule -- was converted
into a ``-inf`` row and silently ranked LAST, indistinguishable from a strategy
that was evaluated and simply found bad.

That is the project's core failure mode (epic #603): absence/error produces a
quieter, wrong-er answer instead of an error. A bug that breaks exactly the
strategies that would otherwise win leaves the optimizer confidently
recommending the runner-up, with nothing in the output saying a scenario
crashed.

The contract, enforced here:

  - a GENUINE crash (a bug) PROPAGATES out of ``optimize()`` -- fail loud
    (DP#32), never a silent ``-inf`` row (``TestCrashPropagates``);
  - a typed, deliberate INFEASIBILITY is still caught narrowly and SURFACED as
    ``is_infeasible`` with a reason -- the legitimate case is unchanged
    (``TestInfeasibilityStillSurfaced``).

Fabricated round numbers, role-based names only (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.strategies import STRATEGY_BALANCED
from objective import MAX_NET_BENEFIT
from optimizer import GridOptimizer
from simulation_config import SimulationConfig


def _household(**overrides):
    cfg = dict(
        projection_years=5, house_value=800_000, mortgage_balance=100_000,
        margin_available=100_000, mortgage_rate=0.05, heloc_rate=0.055,
        amortization_years=25, refinance_amortization_years=25,
        start_year=2026, investment_return=0.06, savings_rate=0.10,
        family_members=[{'role': 'primary', 'birth_year': 1980,
                          'gross_income': 150_000, 'retirement_age': 65,
                          'rrsp_room_accumulated': 0,
                          'tfsa_room_accumulated': 20_000}],
    )
    cfg.update(overrides)
    return SimulationConfig(**cfg)


class TestCrashPropagates:
    def test_a_crashing_strategy_is_not_silently_ranked_last(self):
        """The heart of #657: a strategy whose simulation raises a plain bug
        (here a ``KeyError``, standing in for a mis-mapped config) must NOT be
        swallowed into a ``-inf`` ranked row. The exception must propagate so
        the crash is seen, not buried at the bottom of a table."""
        opt = GridOptimizer(_household())

        def _boom(*args, **kwargs):
            raise KeyError("mis-mapped config key 'nonexistent'")

        opt._run_simulation = _boom  # a strategy that crashes

        with pytest.raises(KeyError):
            opt.optimize(
                strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT,
                income_overrides=[None], ltv_levels=[0.0],
                use_readvanceable_options=[False], deduct_later_options=[False],
            )

    def test_a_crash_does_not_produce_a_ranked_minus_inf_row(self):
        """The negative of the above stated on the OUTPUT: no ranked list is
        returned at all when a scenario crashes, so no ``-inf`` row can ever
        masquerade as a legitimately-bad option."""
        opt = GridOptimizer(_household())

        def _boom(*args, **kwargs):
            raise ZeroDivisionError("division by zero in a rule")

        opt._run_simulation = _boom

        with pytest.raises(ZeroDivisionError):
            opt.optimize(
                strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT,
                income_overrides=[None], ltv_levels=[0.0],
                use_readvanceable_options=[False], deduct_later_options=[False],
            )


class TestInfeasibilityStillSurfaced:
    def test_typed_infeasibility_is_caught_and_reported_not_propagated(self):
        """The legitimate case is unchanged: a deliberately-typed infeasibility
        (an LTV with no declared refinance amortization) is still caught
        narrowly and surfaced as ``is_infeasible`` with a reason -- it is NOT a
        crash, so it does NOT propagate."""
        cfg = _household(mortgage_balance=100_000, refinance_amortization_years=None)
        ranked = GridOptimizer(cfg).optimize(
            strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT,
            income_overrides=[None], ltv_levels=[0.0, 0.70],
            use_readvanceable_options=[False], deduct_later_options=[False],
        )
        refused = [r for r in ranked if r.config_overrides.get('ltv') == 0.70]
        assert refused, "expected the LTV=70% candidates to be evaluated"
        for r in refused:
            assert r.is_infeasible
            assert r.score == float('-inf')
            assert 'MissingRefinanceAmortizationError' in r.infeasible_reason

    def test_readvanceable_without_property_is_reported_infeasible_not_a_crash(self):
        """The grid sweeping ``use_readvanceable=[True, False]`` over a
        household with NO declared property must NOT crash the whole run on the
        structurally-impossible True branch, nor swallow it into a silent -inf.
        The engine's deliberate refusal is a TYPED infeasibility -- surfaced as
        ``is_infeasible`` with a reason -- while the False branch is feasible.
        This is the #657 companion to the crash-propagation contract: a
        DELIBERATE refusal is reported; only a BUG propagates."""
        cfg = _household(house_value=0)  # no property -> readvanceable is refused
        ranked = GridOptimizer(cfg).optimize(
            strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT,
            income_overrides=[None], ltv_levels=[0.0],
            use_readvanceable_options=[True, False], deduct_later_options=[False],
        )
        sm_on = [r for r in ranked if r.config_overrides.get('use_readvanceable')]
        sm_off = [r for r in ranked if not r.config_overrides.get('use_readvanceable')]
        assert sm_on and sm_off, "both readvanceable branches should be evaluated"
        for r in sm_on:
            assert r.is_infeasible
            assert 'ReadvanceableWithoutPropertyError' in r.infeasible_reason
        for r in sm_off:
            assert not r.is_infeasible
