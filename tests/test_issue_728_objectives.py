#!/usr/bin/env python3
"""Tests for issue #728: max_after_tax_income and max_probability_success
ObjectiveFunction instances (DP#22 -- optimization objectives are pluggable
data).

Both objectives are verified to:
- Exist as ObjectiveFunction instances in the OBJECTIVES registry.
- Rank a small fabricated scenario set sensibly (higher score = better).
- Consume the retirement drawdown machinery's outputs READ-ONLY
  (max_after_tax_income sums YearResult fields the drawdown rules surface;
  max_probability_success reads shortfall fields the solvency/drawdown rules
  surface). No drawdown rule code is exercised or re-derived here.

max_probability_success is additionally verified to:
- Return a per-path 1.0/0.0 success indicator deterministically.
- Aggregate to P(success) across a Monte Carlo distribution via
  ``rank_from_distribution`` -- and MonteCarloOptimizer ranks by that
  probability instead of by expected net benefit (DP#22/DP#29).

All test data uses fabricated round numbers per DP#13/DP#15.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from objective import (
    MAX_AFTER_TAX_INCOME,
    MAX_NET_BENEFIT,
    MAX_PROBABILITY_SUCCESS,
    OBJECTIVES,
    get_objective,
)
from simulation_config import YearResult

# ── Helpers ─────────────────────────────────────────────────────────────────

def _retired_year(year, *, after_tax_income=0.0, drawdown_net_delivered=0.0,
                  drawdown_shortfall=0.0, solvency_shortfall=0.0):
    """A retired year (any_retired=True) with fabricated round numbers."""
    return YearResult(
        year=year,
        any_retired=True,
        after_tax_income=after_tax_income,
        drawdown_net_delivered=drawdown_net_delivered,
        drawdown_shortfall=drawdown_shortfall,
        solvency_shortfall=solvency_shortfall,
    )


def _working_year(year, *, after_tax_income=120000.0):
    """A pre-retirement working year (any_retired=False)."""
    return YearResult(year=year, any_retired=False, after_tax_income=after_tax_income)


# ── Registry / lookup ───────────────────────────────────────────────────────

class TestRegistry(unittest.TestCase):
    def test_max_after_tax_income_registered(self):
        self.assertIn('max_after_tax_income', OBJECTIVES)
        self.assertIs(OBJECTIVES['max_after_tax_income'], MAX_AFTER_TAX_INCOME)
        self.assertEqual(MAX_AFTER_TAX_INCOME.name, 'max_after_tax_income')

    def test_max_probability_success_registered(self):
        self.assertIn('max_probability_success', OBJECTIVES)
        self.assertIs(OBJECTIVES['max_probability_success'], MAX_PROBABILITY_SUCCESS)
        self.assertEqual(MAX_PROBABILITY_SUCCESS.name, 'max_probability_success')

    def test_get_objective_lookups(self):
        self.assertIs(get_objective('max_after_tax_income'), MAX_AFTER_TAX_INCOME)
        self.assertIs(get_objective('max_probability_success'), MAX_PROBABILITY_SUCCESS)

    def test_get_objective_unknown_raises(self):
        with self.assertRaises(ValueError):
            get_objective('does_not_exist')

    def test_default_objective_unchanged(self):
        """DP#22 / issue #728 constraint: the default ranking objective stays
        max_net_benefit so the golden household is unaffected."""
        self.assertEqual(MAX_NET_BENEFIT.name, 'max_net_benefit')


# ── max_after_tax_income ────────────────────────────────────────────────────

class TestMaxAfterTaxIncome(unittest.TestCase):
    def test_sums_after_tax_income_plus_drawdown_over_retired_years(self):
        results = [
            _working_year(1, after_tax_income=120000),   # pre-retirement: excluded
            _retired_year(2, after_tax_income=30000, drawdown_net_delivered=60000),
            _retired_year(3, after_tax_income=0, drawdown_net_delivered=55000),
        ]
        # Only retired years count: (30000+60000) + (0+55000) = 145000
        self.assertAlmostEqual(
            MAX_AFTER_TAX_INCOME.evaluate(results), 145000.0)

    def test_pre_retirement_horizon_returns_zero(self):
        """A horizon where nobody retired honestly returns 0.0 -- no retirement
        income was produced, not a drawdown failure (DP#32)."""
        results = [_working_year(i) for i in range(1, 6)]
        self.assertEqual(MAX_AFTER_TAX_INCOME.evaluate(results), 0.0)

    def test_empty_results_returns_zero(self):
        self.assertEqual(MAX_AFTER_TAX_INCOME.evaluate([]), 0.0)

    def test_ranks_sensibly(self):
        """Strategy A delivers more after-tax retirement income than B, so A
        ranks ahead under max_after_tax_income (higher = better)."""
        strategy_a = [
            _retired_year(1, after_tax_income=40000, drawdown_net_delivered=70000),
            _retired_year(2, after_tax_income=40000, drawdown_net_delivered=70000),
        ]
        strategy_b = [
            _retired_year(1, after_tax_income=20000, drawdown_net_delivered=45000),
            _retired_year(2, after_tax_income=20000, drawdown_net_delivered=45000),
        ]
        score_a = MAX_AFTER_TAX_INCOME.evaluate(strategy_a)
        score_b = MAX_AFTER_TAX_INCOME.evaluate(strategy_b)
        self.assertGreater(score_a, score_b)
        # Ranking a small scenario set by this objective orders A first.
        ranked = sorted(
            [('A', strategy_a), ('B', strategy_b)],
            key=lambda kv: MAX_AFTER_TAX_INCOME.evaluate(kv[1]), reverse=True)
        self.assertEqual([name for name, _ in ranked], ['A', 'B'])

    def test_distinct_from_net_benefit(self):
        """A strategy that spends the portfolio down to fund retirement ranks
        well on after-tax income but lower on terminal wealth -- the two
        objectives answer different questions (DP#22)."""
        spend_down = [
            # High retirement income, but terminal assets drained.
            _retired_year(1, after_tax_income=0, drawdown_net_delivered=90000),
            YearResult(year=2, any_retired=True, after_tax_income=0,
                       drawdown_net_delivered=90000,
                       total_assets=100000, total_debt=0),
        ]
        hoard = [
            # No drawdown (leaves the portfolio intact), high terminal wealth.
            _retired_year(1, after_tax_income=0, drawdown_net_delivered=0),
            YearResult(year=2, any_retired=True, after_tax_income=0,
                       drawdown_net_delivered=0,
                       total_assets=1000000, total_debt=0),
        ]
        # max_after_tax_income prefers the spend-down strategy.
        self.assertGreater(
            MAX_AFTER_TAX_INCOME.evaluate(spend_down),
            MAX_AFTER_TAX_INCOME.evaluate(hoard))
        # max_terminal_wealth (assets - debt) prefers the hoard strategy.
        from objective import MAX_TERMINAL_WEALTH
        self.assertGreater(
            MAX_TERMINAL_WEALTH.evaluate(hoard),
            MAX_TERMINAL_WEALTH.evaluate(spend_down))


# ── max_probability_success (deterministic per-path indicator) ──────────────

class TestMaxProbabilitySuccessDeterministic(unittest.TestCase):
    def test_no_shortfall_succeeds(self):
        results = [
            _retired_year(1, drawdown_net_delivered=60000, drawdown_shortfall=0),
            _retired_year(2, drawdown_net_delivered=60000, drawdown_shortfall=0),
        ]
        self.assertEqual(MAX_PROBABILITY_SUCCESS.evaluate(results), 1.0)

    def test_drawdown_shortfall_fails(self):
        """A year the drawdown could not fund the spending target (#707) fails
        the plan."""
        results = [
            _retired_year(1, drawdown_shortfall=0),
            _retired_year(2, drawdown_shortfall=25000),
        ]
        self.assertEqual(MAX_PROBABILITY_SUCCESS.evaluate(results), 0.0)

    def test_solvency_shortfall_fails(self):
        """A year the cash-flow identity was breached (#679) fails the plan."""
        results = [
            _retired_year(1, solvency_shortfall=0),
            _retired_year(2, solvency_shortfall=15000),
        ]
        self.assertEqual(MAX_PROBABILITY_SUCCESS.evaluate(results), 0.0)

    def test_no_targets_declared_trivially_succeeds(self):
        """A household that declared no spending need and no living-cost budget
        has zero shortfalls on every path -> P(success)=1.0 (DP#32: the absence
        of a target is not a target the plan can miss)."""
        results = [_working_year(i) for i in range(1, 6)]
        self.assertEqual(MAX_PROBABILITY_SUCCESS.evaluate(results), 1.0)

    def test_empty_results_returns_zero(self):
        self.assertEqual(MAX_PROBABILITY_SUCCESS.evaluate([]), 0.0)

    def test_ranks_sensibly_deterministic(self):
        """Deterministic ranking: a scenario that meets its targets (1.0) ranks
        ahead of one that fails (0.0)."""
        ok = [_retired_year(1, drawdown_shortfall=0)]
        bad = [_retired_year(1, drawdown_shortfall=10000)]
        ranked = sorted(
            [('ok', ok), ('bad', bad)],
            key=lambda kv: MAX_PROBABILITY_SUCCESS.evaluate(kv[1]), reverse=True)
        self.assertEqual([name for name, _ in ranked], ['ok', 'bad'])


# ── max_probability_success (Monte Carlo aggregation) ───────────────────────

class TestMaxProbabilitySuccessAggregation(unittest.TestCase):
    def test_has_rank_from_distribution(self):
        """DP#22/DP#29: the objective carries the aggregator as DATA, so the
        optimizer ranks by P(success) without a hardcoded special case."""
        self.assertIsNotNone(MAX_PROBABILITY_SUCCESS.rank_from_distribution)

    def test_aggregate_all_success(self):
        self.assertEqual(
            MAX_PROBABILITY_SUCCESS.aggregate([1.0, 1.0, 1.0]), 1.0)

    def test_aggregate_all_failure(self):
        self.assertEqual(
            MAX_PROBABILITY_SUCCESS.aggregate([0.0, 0.0, 0.0]), 0.0)

    def test_aggregate_mixed_is_fraction(self):
        """2 of 3 paths succeed -> P(success) = 2/3."""
        self.assertAlmostEqual(
            MAX_PROBABILITY_SUCCESS.aggregate([1.0, 0.0, 1.0]), 2 / 3)

    def test_aggregate_empty_returns_zero(self):
        self.assertEqual(MAX_PROBABILITY_SUCCESS.aggregate([]), 0.0)

    def test_rank_from_distribution_empty_returns_zero(self):
        """The ``rank_from_distribution`` aggregator itself must handle an empty
        score list. MonteCarloOptimizer calls ``aggregate``, whose own empty
        guard short-circuits before dispatch -- so this exercises the
        aggregator's empty branch directly."""
        self.assertEqual(
            MAX_PROBABILITY_SUCCESS.rank_from_distribution([], {}), 0.0)

    def test_net_benefit_aggregate_falls_back_to_mean(self):
        """Objectives without rank_from_distribution keep the historical
        expected-value ranking (mean of per-path scores)."""
        self.assertIsNone(MAX_NET_BENEFIT.rank_from_distribution)
        self.assertAlmostEqual(
            MAX_NET_BENEFIT.aggregate([1.0, 2.0, 3.0, 4.0]), 2.5)

    def test_aggregate_is_reproducible(self):
        """DP#23: the aggregation is a pure function of the per-path scores --
        reproducibility of the underlying MC comes from the reproducible seeds
        in MonteCarloOptimizer, not from any hidden state here."""
        scores = [1.0, 0.0, 1.0, 1.0, 0.0]
        first = MAX_PROBABILITY_SUCCESS.aggregate(scores)
        second = MAX_PROBABILITY_SUCCESS.aggregate(scores)
        self.assertEqual(first, second)


# ── MonteCarloOptimizer wiring ──────────────────────────────────────────────

class TestMonteCarloOptimizerWiring(unittest.TestCase):
    """Issue #728 (DP#22/DP#29): MonteCarloOptimizer must rank by the
    objective's aggregation of per-path scores, so max_probability_success
    ranks strategies by P(success) instead of by expected net benefit."""

    def _make_config(self):
        from simulation_config import SimulationConfig
        return SimulationConfig(
            projection_years=3, investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
            ],
            children=[{'name': 'Kid', 'age': 10, 'gross_income': 0}],
            mortgage_balance=100000, mortgage_rate=0.05,
            house_value=800000,
        )

    def test_mc_ranks_by_probability_when_objective_is_probability(self):
        """When the objective is max_probability_success, the RankedScenario
        score is a probability in [0, 1] (the fraction of MC paths that met
        targets), not an expected net benefit."""
        from countries.canada.strategies import STRATEGY_BALANCED
        from monte_carlo_optimizer import MonteCarloOptimizer
        opt = MonteCarloOptimizer(self._make_config(), n_simulations=12)
        results = opt.optimize(strategies=[STRATEGY_BALANCED],
                               objective=MAX_PROBABILITY_SUCCESS)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.objective_name, 'max_probability_success')
            # A probability is bounded in [0, 1].
            self.assertGreaterEqual(r.score, 0.0)
            self.assertLessEqual(r.score, 1.0)
            # The MC distribution was actually run (n_simulations > 1).
            self.assertEqual(r.risk_measures.n_simulations, 12)

    def test_mc_ranks_by_expected_value_for_net_benefit(self):
        """The historical behaviour is preserved for max_net_benefit: the score
        is the mean of per-path net benefits (an unbounded dollar figure), not
        a probability. This is the no-regression guard for the wiring change."""
        from countries.canada.strategies import STRATEGY_BALANCED
        from monte_carlo_optimizer import MonteCarloOptimizer
        cfg = self._make_config()
        opt = MonteCarloOptimizer(cfg, n_simulations=10, seed_base=42)
        results = opt.optimize(strategies=[STRATEGY_BALANCED],
                               objective=MAX_NET_BENEFIT)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertEqual(r.objective_name, 'max_net_benefit')
            # Net benefit is a dollar figure, not bounded in [0, 1].
            self.assertNotAlmostEqual(r.score, 0.0)
            self.assertEqual(r.risk_measures.n_simulations, 10)

    def test_mc_reproducible_with_same_seed(self):
        """DP#23: same seed_base -> same P(success) ranking."""
        from countries.canada.strategies import STRATEGY_BALANCED
        from monte_carlo_optimizer import MonteCarloOptimizer
        cfg = self._make_config()
        r1 = MonteCarloOptimizer(cfg, n_simulations=10, seed_base=42).optimize(
            strategies=[STRATEGY_BALANCED], objective=MAX_PROBABILITY_SUCCESS)
        r2 = MonteCarloOptimizer(cfg, n_simulations=10, seed_base=42).optimize(
            strategies=[STRATEGY_BALANCED], objective=MAX_PROBABILITY_SUCCESS)
        self.assertEqual(len(r1), len(r2))
        for a, b in zip(r1, r2, strict=True):
            self.assertAlmostEqual(a.score, b.score)


if __name__ == '__main__':
    unittest.main()
