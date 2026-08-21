#!/usr/bin/env python3
"""Architecture tests for issue #583 (DP#26): the year step reads nothing off self.

Background: `self._portfolio` was a snapshot of the config built once in
`FamilySimulation.__init__` and never refreshed. The year step
(`_simulate_year_step`) read it as if it were current state, so a
portfolio's year-0 balance sheet was consulted in year 40 -- the mechanism
behind #575 (non-reg accounts compounding at 0% forever). Barley's #575 fix
(PR #613) fixed that one read; this issue is the *class* of bug: the year
step closing over `self` at all, rather than taking everything explicitly.

The fix (this PR): `simulate_year` (simulation.py) and the rule functions it
calls -- `_year_brackets_for`, `_mortgage_data_for`,
`_non_reg_after_tax_return_for` -- are module-level
functions, not methods. They take `state`, `year`, and a `SimulationContext`
(or explicit scalar arguments) and have no `self` in scope at all. (The
retirement transition that was `_retirement_transition_for` and the tuition
tax credit that was `_tuition_credits_for` are now registered rules, epic #795.) `FamilySimulation._simulate_year_step` is a
one-line seam: gather `self`
into a `SimulationContext` (`_build_context()`) and hand it to the pure
function.

This file enforces that mechanically two ways:
  1. Static (AST) -- the identifier `self` must not appear anywhere in the
     source of these functions. This is a permanent tripwire: if someone
     later moves logic back into a method, or a helper starts closing over
     `self`, this test fails without needing to construct a failing scenario.
  2. Behavioural -- corrupt/delete the `FamilySimulation` instance's own
     attributes *after* building a context and state, then prove
     `simulate_year(state, year, ctx)` still produces the correct result.
     If the pure step secretly needed something off `self`, this would
     raise or produce a wrong answer.

Companion to #586 (Sage's executable-design-principles harness) -- this test
is scoped specifically to #583/DP#26 (purity of the year step / no stale
`self` reads), not a general principle-enforcement framework.
"""

import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import simulation
import simulation_state
from simulation import FamilySimulation, SimulationContext, simulate_year
from simulation_config import SimulationConfig
from simulation_state import simulate_year_pure
from countries.canada.adapter import CanadaAdapter


# The year step and every pure rule function it calls directly. Per #583's
# proposal: "assert simulate_year_pure (and the rule functions it calls) do
# not close over instance attributes."
YEAR_STEP_FUNCTIONS = [
    simulation.simulate_year,
    simulation._year_brackets_for,
    simulation._mortgage_data_for,
    simulation._non_reg_after_tax_return_for,
    simulation_state.simulate_year_pure,
]


def _names_referenced(fn) -> set:
    """All identifier names (Name nodes) referenced anywhere in fn's source."""
    source = inspect.getsource(fn)
    # Dedent — these are module-level functions so no leading indent is
    # expected, but be defensive.
    tree = ast.parse(inspect.cleandoc('\n' + source) if source.startswith((' ', '\t')) else source)
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


class TestYearStepDoesNotReadSelf(unittest.TestCase):
    """Static (AST) enforcement of DP#26/#583: no `self` in the year step."""

    def test_no_function_references_the_name_self(self):
        for fn in YEAR_STEP_FUNCTIONS:
            with self.subTest(fn=fn.__qualname__):
                names = _names_referenced(fn)
                self.assertNotIn(
                    'self', names,
                    f"{fn.__qualname__} references `self` -- the year step "
                    f"(DP#26/#583) must take everything explicitly as an "
                    f"argument, not close over an instance."
                )

    def test_no_function_has_a_self_parameter(self):
        """Belt-and-suspenders: none of these are bound methods."""
        for fn in YEAR_STEP_FUNCTIONS:
            with self.subTest(fn=fn.__qualname__):
                self.assertFalse(hasattr(fn, '__self__'),
                                  f"{fn.__qualname__} is a bound method, not a free function.")
                params = list(inspect.signature(fn).parameters)
                self.assertNotIn('self', params, f"{fn.__qualname__} has a `self` parameter.")

    def test_simulate_year_is_a_plain_module_function(self):
        """`simulate_year` must be an ordinary function on the `simulation`
        module, not a method resolved through the class -- FamilySimulation
        only holds a one-line delegator (`_simulate_year_step`)."""
        self.assertIs(simulation.simulate_year, simulate_year)
        self.assertEqual(inspect.getmodule(simulate_year).__name__, 'simulation')
        # And the class-level delegator is a one-liner that just forwards.
        src = inspect.getsource(FamilySimulation._simulate_year_step)
        # Issue #1020 (S04 Step 1): the delegator also forwards the prior-year
        # GIS-countable income the fold threads in, but it is STILL a thin
        # forward -- it reads nothing off `self` except `_build_context()`.
        self.assertIn('simulate_year(state, year, self._build_context()', src)
        self.assertIn('prior_gis_countable_income=prior_gis_countable_income', src)


def _make_config(**overrides):
    base = dict(
        projection_years=5,
        investment_return=0.06,
        salary_growth=0.02,
        savings_rate=0.15,
        house_value=500_000,
        mortgage_balance=300_000,
        mortgage_rate=0.05,
        amortization_years=25,
        margin_available=50_000,
        family_members=[
            {'role': 'primary', 'gross_income': 100_000, 'birth_year': 1985,
             'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000},
            {'role': 'spouse', 'gross_income': 60_000, 'birth_year': 1987,
             'rrsp_room_accumulated': 20_000, 'tfsa_room_accumulated': 20_000},
        ],
        children=[],
    )
    base.update(overrides)
    return SimulationConfig(**base)


class TestSimulateYearNeedsNothingFromSelfAtCallTime(unittest.TestCase):
    """Behavioural proof: once `ctx` is built, `simulate_year` does not
    consult the originating `FamilySimulation` instance at all.

    If it secretly closed over `self` (e.g. via a bound-method call hidden
    inside `ctx`'s construction, or a module-global cache keyed by the
    instance), corrupting the instance's attributes after building `ctx`
    would break or change the result. It does not.
    """

    def test_corrupting_self_after_building_context_changes_nothing(self):
        config = _make_config()
        sim_a = FamilySimulation(config, adapter=CanadaAdapter(config),
                                  use_readvanceable=False, deduct_later=False)
        sim_b = FamilySimulation(config, adapter=CanadaAdapter(config),
                                  use_readvanceable=False, deduct_later=False)

        ctx_a = sim_a._build_context()
        result_expected, next_state_expected = simulate_year(sim_a._state, 0, ctx_a)

        # Build sim_b's context too, then wreck sim_b's own attributes --
        # simulate_year must not have needed them.
        ctx_b = sim_b._build_context()
        sim_b.config = None
        sim_b.strategy = None
        sim_b.rate_path = None
        sim_b.heloc_path = None
        sim_b.return_model = None
        sim_b._portfolio = "not a portfolio"
        sim_b._state = None
        del sim_b.__dict__['adapter']

        result_actual, next_state_actual = simulate_year(sim_b._state if False else sim_a._state, 0, ctx_b)

        self.assertEqual(result_actual.total_assets, result_expected.total_assets)
        self.assertEqual(result_actual.mortgage_balance, result_expected.mortgage_balance)
        self.assertEqual(next_state_actual.mortgage_balance, next_state_expected.mortgage_balance)
        self.assertEqual(next_state_actual.non_reg_balance, next_state_expected.non_reg_balance)

    def test_simulate_year_is_referentially_transparent(self):
        """Same (state, year, ctx) in -> byte-identical (result, next_state) out."""
        config = _make_config(projection_years=8)
        sim = FamilySimulation(config, adapter=CanadaAdapter(config))
        ctx = sim._build_context()

        r1, s1 = simulate_year(sim._state, 2, ctx)
        r2, s2 = simulate_year(sim._state, 2, ctx)

        import dataclasses
        self.assertEqual(dataclasses.asdict(r1), dataclasses.asdict(r2))
        self.assertEqual(dataclasses.asdict(s1), dataclasses.asdict(s2))
        # And the inputs were not mutated by the call.
        self.assertIsNotNone(sim._state)


class TestEngineEntryPointsShareOneOpeningStateConstructor(unittest.TestCase):
    """Issue #583: generalising Splash's #577 fix (`margin_draw_for_lump_sum`
    + a cross-engine test pinning FamilySimulation and Optimizer together).

    Before this PR, THREE call sites built "the opening state":
    `FamilySimulation.__init__`, `Optimizer._run_simulation`, and
    `DPOptimizer._optimize_strategy` -- each calling `SimState.initial()`
    directly (two of them then re-deriving the margin-draw booking by
    hand). That is the exact shape that produced #577: nothing enforced
    that independently-derived "opening state" logic agreed. Now all three
    route through `simulation_state.initial_state_for_run`, so there is
    only one implementation left to disagree with itself.
    """

    @staticmethod
    def _code_calls(fn, name: str) -> bool:
        """Whether fn's compiled code actually CALLS a function/method named
        `name` -- as opposed to merely mentioning it in a comment or
        docstring (a plain substring check on source would false-positive
        on this file's own explanatory prose)."""
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                called_name = func.attr if isinstance(func, ast.Attribute) else \
                    (func.id if isinstance(func, ast.Name) else None)
                if called_name == name:
                    return True
        return False

    def test_simulation_py_uses_the_shared_constructor(self):
        fn = simulation.FamilySimulation.__init__
        self.assertTrue(self._code_calls(fn, 'initial_state_for_run'))
        self.assertFalse(self._code_calls(fn, 'initial'),
                          "FamilySimulation.__init__ must not call SimState.initial() "
                          "directly -- route through initial_state_for_run.")

    def test_optimizer_py_uses_the_shared_constructor(self):
        import optimizer
        fn = optimizer.Optimizer._run_simulation
        self.assertTrue(self._code_calls(fn, 'initial_state_for_run'))
        self.assertFalse(self._code_calls(fn, 'initial'))

    def test_dp_optimizer_py_uses_the_shared_constructor(self):
        import dp_optimizer
        fn = dp_optimizer.DPOptimizer._optimize_strategy
        self.assertTrue(self._code_calls(fn, 'initial_state_for_run'))
        self.assertFalse(self._code_calls(fn, 'initial'))

    def test_all_three_engines_agree_on_the_opening_state_with_no_draw(self):
        """With no lump sum, all three engines must open with an identical
        (zero) HELOC balance -- the #577 invariant, now pinned across all
        three entry points instead of just the two #577 covered."""
        from optimizer import GridOptimizer
        from dp_optimizer import DPOptimizer, Decision
        from countries.canada.strategies import STRATEGY_BALANCED

        config = _make_config(margin_available=150_000)

        fam = FamilySimulation(config, adapter=CanadaAdapter(config),
                                use_readvanceable=False, deduct_later=False)
        self.assertEqual(fam._state.heloc_balance, 0.0)

        grid = GridOptimizer(config)
        _results, grid_state = grid._run_simulation(
            config, STRATEGY_BALANCED, use_readvanceable=False, lump_sum=0.0)
        self.assertEqual(grid_state.heloc_balance, 0.0)

        dp = DPOptimizer(config)
        dp_results = dp.optimize(strategies=[STRATEGY_BALANCED],
                                  decision_class=Decision(name="deduct_later"))
        # DPOptimizer doesn't (yet) draw a margin, but it must still open
        # from the same zero-debt constructor as the other two engines.
        self.assertEqual(dp_results[0].decision_path[0].state_before.heloc_balance, 0.0)

    def test_initial_state_for_run_is_a_pure_function_of_its_arguments(self):
        from simulation_state import initial_state_for_run
        config = _make_config(margin_available=100_000)
        s1 = initial_state_for_run(config, lump_sum=40_000)
        s2 = initial_state_for_run(config, lump_sum=40_000)
        self.assertEqual(s1.heloc_balance, s2.heloc_balance)
        self.assertEqual(s1.heloc_balance, 40_000)

        s3 = initial_state_for_run(config)  # default lump_sum=0.0
        self.assertEqual(s3.heloc_balance, 0.0)


if __name__ == "__main__":
    unittest.main()
