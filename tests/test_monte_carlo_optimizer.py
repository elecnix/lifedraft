#!/usr/bin/env python3
"""Unit tests for monte_carlo_optimizer.py — MonteCarloOptimizer (DP#23, DP#29).

Tests verify:
- MonteCarloOptimizer returns ranked scenarios
- Risk measures are populated (n_simulations > 0)
- is_deterministic is False for MC results
- Reproducible seeds produce same results
- P(loss) is computed correctly
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from monte_carlo_optimizer import MonteCarloOptimizer
from simulation import SimulationConfig
from return_model import StochasticReturn, FixedReturn
from objective import MAX_NET_BENEFIT
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


class TestMonteCarloOptimizer(unittest.TestCase):
    def test_returns_ranked_list(self):
        opt = MonteCarloOptimizer(_make_config(), n_simulations=10)
        results = opt.optimize(strategies=[STRATEGY_BALANCED])
        self.assertGreater(len(results), 0)
        for r in results:
            self.assertFalse(r.is_deterministic)
    
    def test_risk_measures_populated(self):
        opt = MonteCarloOptimizer(_make_config(), n_simulations=10)
        results = opt.optimize(strategies=[STRATEGY_BALANCED])
        rm = results[0].risk_measures
        self.assertEqual(rm.n_simulations, 10)
        self.assertIsInstance(rm.expected_value, float)
        self.assertIsInstance(rm.probability_of_loss, float)
    
    def test_reproducible_seeds(self):
        """DP#23: same seed_base → same results."""
        cfg = _make_config()
        opt1 = MonteCarloOptimizer(cfg, n_simulations=10, seed_base=42)
        opt2 = MonteCarloOptimizer(cfg, n_simulations=10, seed_base=42)
        r1 = opt1.optimize(strategies=[STRATEGY_BALANCED])
        r2 = opt2.optimize(strategies=[STRATEGY_BALANCED])
        self.assertAlmostEqual(r1[0].score, r2[0].score)
    
    def test_p_loss_range(self):
        """P(loss) should be between 0 and 1."""
        opt = MonteCarloOptimizer(_make_config(), n_simulations=10)
        results = opt.optimize(strategies=[STRATEGY_BALANCED])
        for r in results:
            self.assertGreaterEqual(r.risk_measures.probability_of_loss, 0)
            self.assertLessEqual(r.risk_measures.probability_of_loss, 1)


if __name__ == '__main__':
    unittest.main()