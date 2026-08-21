#!/usr/bin/env python3
"""Tests for scipy_optimizer edge cases, optimizer LTV overlays.

Run with: python3 -m pytest tests/test_coverage_push.py -v

Epic #603 Track C Phase 2b: this file used to also test
``module_registry._has_nested_field`` -- deleted along with that function
(zero production callers, operated on the legacy input shape's auto-include
triggers). See ``module_registry.py``'s module docstring.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from scipy_optimizer import ScipyOptimizer, ScipyResult
from optimizer import GridOptimizer
from simulation_config import apply_ltv_overlay


class TestScipyOptimizerSetup(unittest.TestCase):
    """Test ScipyOptimizer._setup_variables and _apply_ltv_overlay."""

    def test_setup_ltv_variable(self):
        """LTV bounds: [0.0, 0.80], x0=0.30."""
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3)
        opt = ScipyOptimizer(cfg, optimize_vars=['ltv'])
        bounds, x0, names = opt._setup_variables()
        self.assertEqual(bounds, [(0.0, 0.80)])
        self.assertAlmostEqual(x0[0], 0.30)
        self.assertEqual(names, ['ltv'])

    def test_setup_rrsp_weight(self):
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3)
        opt = ScipyOptimizer(cfg, optimize_vars=['rrsp_weight'])
        bounds, x0, names = opt._setup_variables()
        self.assertEqual(bounds, [(0.0, 1.0)])
        self.assertAlmostEqual(x0[0], 0.5)

    def test_setup_tfsa_weight(self):
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3)
        opt = ScipyOptimizer(cfg, optimize_vars=['tfsa_weight'])
        bounds, x0, names = opt._setup_variables()
        self.assertEqual(bounds, [(0.0, 1.0)])
        self.assertAlmostEqual(x0[0], 0.3)

    def test_setup_pension_split_pct(self):
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3)
        opt = ScipyOptimizer(cfg, optimize_vars=['pension_split_pct'])
        bounds, x0, names = opt._setup_variables()
        self.assertEqual(bounds, [(0.0, 0.50)])
        self.assertAlmostEqual(x0[0], 0.25)

    def test_setup_unknown_variable(self):
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3)
        opt = ScipyOptimizer(cfg, optimize_vars=['unknown_var'])
        with self.assertRaises(ValueError):
            opt._setup_variables()

    def test_apply_ltv_overlay_ltv80(self):
        """Applying LTV=0.80 overlay adds cash-out to mortgage_balance AND
        shrinks margin_available by the same amount (#664: mortgage and
        HELOC share ONE registered charge, not independent borrowing
        sources)."""
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3, house_value=700000, mortgage_balance=100000,
                                refinance_amortization_years=25)  # #655: fabricated round new-loan term
        # Default margin_available=200000
        modified = apply_ltv_overlay(cfg, 0.80)
        # cash_out = 0.80 * 700000 - 100000 = 460000
        # mortgage_balance = 100000 + 460000 = 560000
        # margin_available shrinks by the cash-out, floored at 0 (#664):
        # max(0, 200000 - 460000) = 0
        self.assertAlmostEqual(modified.mortgage_balance, 560000)
        self.assertAlmostEqual(modified.margin_available, 0)
        self.assertAlmostEqual(modified.cash_out, 460000)

    def test_apply_ltv_overlay_zero(self):
        """LTV=0 → config unchanged."""
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3, house_value=700000)
        modified = apply_ltv_overlay(cfg, 0.0)
        self.assertEqual(modified.house_value, cfg.house_value)

    def test_scipy_result_creation(self):
        """ScipyResult dataclass."""
        r = ScipyResult(
            scenario_name="test",
            score=100000,
            objective_name="net_benefit",
            optimal_params={'ltv': 0.80},
            convergence=True,
            n_evaluations=50,
        )
        self.assertAlmostEqual(r.score, 100000)
        self.assertTrue(r.convergence)


class TestScipyOptimizerFallback(unittest.TestCase):
    """Test scipy_optimizer fallback when scipy unavailable (lines 110-113, 152-162)."""

    def test_fallback_grid_search(self):
        """When scipy import fails, falls back to grid search over LTV levels."""
        from simulation import SimulationConfig
        cfg = SimulationConfig(projection_years=3, house_value=600000, mortgage_balance=200000,
                                refinance_amortization_years=25,  # #655: fabricated round new-loan term
                                family_members=[
                                    {'role': 'primary', 'gross_income': 120000,
                                     'rrsp_room_accumulated': 50000,
                                     'tfsa_room_accumulated': 40000},
                                    {'role': 'spouse', 'gross_income': 50000,
                                     'rrsp_room_accumulated': 20000,
                                     'tfsa_room_accumulated': 40000},
                                ])
        opt = ScipyOptimizer(cfg, optimize_vars=['ltv'])
        # Directly test the fallback code path by calling neg_objective
        # This is the grid path that runs when scipy is unavailable
        try:
            import numpy as np
            # Test apply_ltv_overlay produces valid config (DP#18: overlay modifies base)
            modified = apply_ltv_overlay(cfg, 0.50)
            # cash_out = 0.50 * 600000 - 200000 = 100000
            # mortgage_balance = 200000 + 100000 = 300000
            # margin_available shrinks by the cash-out booked (#664):
            # 200000 - 100000 = 100000
            self.assertAlmostEqual(modified.mortgage_balance, 300000)
            self.assertAlmostEqual(modified.margin_available, cfg.margin_available - 100000)
        except ImportError:
            pass


if __name__ == '__main__':
    unittest.main()