#!/usr/bin/env python3
"""Tests for Issue #118: MonteCarloOptimizer accepts search_space from caller (DP#31).

Per DP#31, the optimizer mode is pluggable data; the search method and objective
are separate choices. The search space should come from the caller (e.g.,
discover_anchors()), not be hardcoded in the optimizer.

Tests verify:
- search_space parameter overrides hardcoded defaults
- search_space sm_options and deduct_later_options are used when provided
- Backward compatibility: no search_space falls back to [True, False]
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch, MagicMock
from monte_carlo_optimizer import MonteCarloOptimizer
from optimizer import GridOptimizer
from simulation import SimulationConfig
from return_model import FixedReturn
from objective import MAX_NET_BENEFIT
from countries.canada.strategies import STRATEGY_BALANCED


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
    )


class TestMonteCarloSearchSpace(unittest.TestCase):
    """Test that MonteCarloOptimizer accepts search_space from caller (DP#31)."""

    def test_search_space_overrides_defaults(self):
        """search_space parameter should override hardcoded defaults."""
        cfg = _make_config()
        opt = MonteCarloOptimizer(cfg, n_simulations=2, seed_base=42)
        
        search_space = {
            'sm_options': [True],  # Only SM enabled
            'deduct_later_options': [False],  # No deduct-later
        }
        
        results = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            search_space=search_space,
        )
        
        # Should only produce scenarios with sm=1 and dl=0
        for r in results:
            self.assertIn('_sm1_', r.scenario_name)
            self.assertIn('_dl0_', r.scenario_name)

    def test_search_space_sm_only_true(self):
        """search_space with sm_options=[True] should produce only SM-enabled scenarios."""
        cfg = _make_config()
        opt = MonteCarloOptimizer(cfg, n_simulations=2, seed_base=42)
        
        search_space = {
            'sm_options': [True],
            'deduct_later_options': [True, False],
        }
        
        results = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            search_space=search_space,
        )
        
        # All scenarios should have sm=1
        for r in results:
            self.assertIn('_sm1_', r.scenario_name)

    def test_backward_compat_no_search_space(self):
        """Without search_space, optimizer falls back to hardcoded defaults."""
        cfg = _make_config()
        opt = MonteCarloOptimizer(cfg, n_simulations=2, seed_base=42)
        
        # Should work without search_space (backward compat)
        results = opt.optimize(strategies=[STRATEGY_BALANCED])
        self.assertGreater(len(results), 0)

    def test_explicit_params_override_search_space(self):
        """Explicit use_readvanceable_options should override search_space."""
        cfg = _make_config()
        opt = MonteCarloOptimizer(cfg, n_simulations=2, seed_base=42)
        
        search_space = {
            'sm_options': [True, False],
            'deduct_later_options': [True, False],
        }
        
        # Explicit parameter should take precedence over search_space
        results = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            use_readvanceable_options=[False],  # Override: only SM disabled
            search_space=search_space,
        )
        
        # All scenarios should have sm=0 (explicit param overrides search_space)
        for r in results:
            self.assertIn('_sm0_', r.scenario_name)


class TestGridOptimizerSearchSpace(unittest.TestCase):
    """Test that GridOptimizer also accepts search_space from caller (DP#31)."""

    def test_grid_optimizer_search_space(self):
        """GridOptimizer.optimize() should accept search_space parameter."""
        cfg = _make_config()
        opt = GridOptimizer(cfg)
        
        search_space = {
            'sm_options': [True],
            'deduct_later_options': [False],
        }
        
        results = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            search_space=search_space,
            income_overrides=[None],  # DP#13/#665: base income only, explicit
        )

        # Should only produce scenarios with sm=1 and dl=0
        for r in results:
            self.assertIn('sm1', r.scenario_name)
            self.assertIn('dl0', r.scenario_name)

    def test_grid_backward_compat(self):
        """GridOptimizer without search_space falls back to hardcoded defaults."""
        cfg = _make_config()
        opt = GridOptimizer(cfg)

        # Should work without search_space (backward compat). income_overrides
        # is still required explicitly (#665: no silent default for THAT param).
        results = opt.optimize(strategies=[STRATEGY_BALANCED], income_overrides=[None])
        self.assertGreater(len(results), 0)


if __name__ == '__main__':
    unittest.main()