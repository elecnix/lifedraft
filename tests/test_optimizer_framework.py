#!/usr/bin/env python3
"""Unit tests for optimizer.py — GridOptimizer framework (DP#22, DP#25, DP#26).

Tests verify:
- GridOptimizer produces ranked results
- Different objectives produce different rankings
- RankedScenario has correct fields
- RiskMeasures default to deterministic (n_simulations=0)
- is_deterministic correctly identifies grid vs MC results
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from optimizer import GridOptimizer, RankedScenario, RiskMeasures, Optimizer
from simulation import SimulationConfig
from return_model import FixedReturn
from objective import MAX_NET_BENEFIT, MAX_TERMINAL_WEALTH, MAX_TAX_SAVINGS
from countries.canada.strategies import STRATEGY_BALANCED, STRATEGY_RRSP_MAX


def _make_config():
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
        # issue #681: this fixture declared a mortgage and (via the
        # dataclass default) a $200k HELOC margin, but NO property --
        # house_value defaulted to 0. The optimizer sweeps
        # use_readvanceable=True, and a readvanceable line is a claim on
        # the charge registered against a property: with no property
        # there is no charge and no knowable advanceable room, so the
        # readvance rule now refuses rather than advancing an unbounded
        # amount (DP#32). A fabricated $800k house makes the household
        # coherent: 80% charge = $640k, far above the $100k mortgage +
        # $200k margin.
        house_value=800000,
    )


class TestGridOptimizer(unittest.TestCase):
    def test_returns_ranked_list(self):
        opt = GridOptimizer(_make_config())
        results = opt.optimize(strategies=[STRATEGY_BALANCED], income_overrides=[None])
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertIsInstance(r, RankedScenario)

    def test_results_sorted_by_score(self):
        opt = GridOptimizer(_make_config())
        results = opt.optimize(strategies=[STRATEGY_BALANCED, STRATEGY_RRSP_MAX], income_overrides=[None])
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_objective_name_in_result(self):
        opt = GridOptimizer(_make_config())
        results = opt.optimize(strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT, income_overrides=[None])
        self.assertEqual(results[0].objective_name, 'max_net_benefit')

    def test_different_objective_different_scores(self):
        opt = GridOptimizer(_make_config())
        r1 = opt.optimize(strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT, income_overrides=[None])
        r2 = opt.optimize(strategies=[STRATEGY_BALANCED], objective=MAX_TERMINAL_WEALTH, income_overrides=[None])
        # Different objectives → different scores (though ranking may be same)
        self.assertNotEqual(r1[0].objective_name, r2[0].objective_name)


class TestRankedScenario(unittest.TestCase):
    def test_deterministic(self):
        rs = RankedScenario(scenario_name="test", score=100, objective_name="max_net_benefit")
        self.assertTrue(rs.is_deterministic)
    
    def test_stochastic(self):
        rm = RiskMeasures(n_simulations=100)
        rs = RankedScenario(scenario_name="test", score=100, objective_name="max_net_benefit",
                           risk_measures=rm)
        self.assertFalse(rs.is_deterministic)


class TestRiskMeasures(unittest.TestCase):
    def test_defaults(self):
        rm = RiskMeasures()
        self.assertEqual(rm.expected_value, 0)
        self.assertEqual(rm.probability_of_loss, 0)
        self.assertEqual(rm.n_simulations, 0)
    
    def test_deterministic_measures_available(self):
        """DP#29: n_simulations=1 means deterministic risk measures are available."""
        rm = RiskMeasures(n_simulations=1, max_drawdown=50000, probability_of_loss=0.0)
        self.assertTrue(rm.measures_available)
        self.assertEqual(rm.max_drawdown, 50000)
    
    def test_not_measured_when_zero_simulations(self):
        """DP#29: n_simulations=0 means risk was not measured."""
        rm = RiskMeasures()
        self.assertFalse(rm.measures_available)


class TestDeterministicRisk(unittest.TestCase):
    """Test Optimizer.compute_deterministic_risk (DP#29)."""
    
    def test_compute_deterministic_risk_positive_score(self):
        """Positive score → probability_of_loss = 0, risk measures computed."""
        from optimizer import Optimizer, RiskMeasures
        from simulation_config import YearResult
        results = [
            YearResult(year=0, total_assets=500000, total_debt=200000),  # net=300k
            YearResult(year=1, total_assets=550000, total_debt=220000),  # net=330k
            YearResult(year=2, total_assets=520000, total_debt=240000),  # net=280k (drawdown!)
        ]
        rm = Optimizer.compute_deterministic_risk(results, score=100000)
        self.assertEqual(rm.n_simulations, 1)
        self.assertEqual(rm.probability_of_loss, 0.0)
        self.assertGreater(rm.max_drawdown, 0)  # Drawdown from peak
        self.assertEqual(rm.expected_value, 100000)
    
    def test_compute_deterministic_risk_negative_score(self):
        """Negative score → probability_of_loss = 1.0."""
        from optimizer import Optimizer, RiskMeasures
        from simulation_config import YearResult
        results = [
            YearResult(year=0, total_assets=500000, total_debt=600000),
            YearResult(year=1, total_assets=480000, total_debt=580000),
        ]
        rm = Optimizer.compute_deterministic_risk(results, score=-50000)
        self.assertEqual(rm.probability_of_loss, 1.0)
        self.assertEqual(rm.n_simulations, 1)
    
    def test_compute_deterministic_risk_empty_results(self):
        """Empty results → n_simulations=1, drawdown=0."""
        from optimizer import Optimizer
        rm = Optimizer.compute_deterministic_risk([], score=0)
        self.assertEqual(rm.n_simulations, 1)
        self.assertEqual(rm.max_drawdown, 0.0)
    
    def test_grid_optimizer_populates_risk_measures(self):
        """DP#29: GridOptimizer now populates risk_measures in RankedScenario."""
        opt = GridOptimizer(_make_config())
        results = opt.optimize(strategies=[STRATEGY_BALANCED], income_overrides=[None])
        for r in results:
            # n_simulations should be 1 (deterministic), not 0 (not measured)
            self.assertEqual(r.risk_measures.n_simulations, 1,
                             f"Expected n_simulations=1 for deterministic, got {r.risk_measures.n_simulations}")
            self.assertTrue(r.risk_measures.measures_available,
                           f"Risk measures should be available for {r.scenario_name}")


class TestOptimizerBase(unittest.TestCase):
    def test_cannot_optimize_directly(self):
        opt = Optimizer(_make_config())
        with self.assertRaises(NotImplementedError):
            opt.optimize()


if __name__ == '__main__':
    unittest.main()