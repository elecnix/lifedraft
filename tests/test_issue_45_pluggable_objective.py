#!/usr/bin/env python3
"""Tests for Issue #45: Pluggable objective for optimizer (DP#22).

Per DP#22: the optimizer ranks, it doesn't choose. The user picks the objective;
the optimizer produces an ordered list. run_optimization() should accept
an objective parameter and use objective.evaluate() for scoring.

All test data uses round numbers per DP#13/DP#15.
"""

import unittest

from objective import (
    ObjectiveFunction, MAX_NET_BENEFIT, MAX_TERMINAL_WEALTH,
    MAX_TAX_SAVINGS, MIN_RETIREMENT_GAP,
)
from simulation_config import SimulationConfig


def _make_config():
    """Minimal config for testing, with round numbers per DP#13."""
    return SimulationConfig(
        projection_years=3,
        investment_return=0.07,
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


class TestObjectiveFunction(unittest.TestCase):
    """Test that ObjectiveFunction can be passed to run_optimization."""

    def test_default_objective_is_max_net_benefit(self):
        """When no objective is passed, MAX_NET_BENEFIT is used."""
        from optimize import run_optimization
        import json
        import tempfile
        import os

        cfg = {
            'family': {
                'members': [
                    {'role': 'primary', 'gross_income': 120000,
                     'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
                    {'role': 'spouse', 'gross_income': 50000,
                     'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
                ],
            },
            'property': {
                'house_value': 500000,
                'mortgage_balance': 100000,
                'mortgage_rate': 0.05,
                'margin_available': 50000,
            },
            'assumptions': {
                'projection_years': 3,
                'investment_return': 0.07,
                'salary_growth': 0.02,
            },
            'accounts': {
                'rrsp_annual_percent': 0.18,
                'tfsa_annual_room_per_person': 7000,
            },
        }

        results = run_optimization(cfg)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        # Each result should have objective_name field
        for r in results:
            self.assertIn('objective_name', r)
            self.assertEqual(r['objective_name'], 'max_net_benefit')

    def test_custom_objective_produces_different_ranking(self):
        """A custom objective should produce results with different scores."""
        from optimize import run_optimization

        cfg = {
            'family': {
                'members': [
                    {'role': 'primary', 'gross_income': 120000,
                     'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
                    {'role': 'spouse', 'gross_income': 50000,
                     'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
                ],
            },
            'property': {
                'house_value': 500000,
                'mortgage_balance': 100000,
                'mortgage_rate': 0.05,
                'margin_available': 50000,
            },
            'assumptions': {
                'projection_years': 3,
                'investment_return': 0.07,
                'salary_growth': 0.02,
            },
            'accounts': {
                'rrsp_annual_percent': 0.18,
                'tfsa_annual_room_per_person': 7000,
            },
        }

        # Run with MAX_NET_BENEFIT
        results_nb = run_optimization(cfg, objective=MAX_NET_BENEFIT)
        # Run with MAX_TERMINAL_WEALTH
        results_tw = run_optimization(cfg, objective=MAX_TERMINAL_WEALTH)

        # Both should produce results
        self.assertGreater(len(results_nb), 0)
        self.assertGreater(len(results_tw), 0)

        # Objective names should differ
        self.assertEqual(results_nb[0]['objective_name'], 'max_net_benefit')
        self.assertEqual(results_tw[0]['objective_name'], 'max_terminal_wealth')

    def test_evaluate_strategy_with_simulation_accepts_objective(self):
        """evaluate_strategy_with_simulation should accept objective parameter."""
        from optimize import evaluate_strategy_with_simulation
        from strategy import AllocationStrategy
        from countries.canada.rate_model import build_rate_path

        config = _make_config()
        strategy = AllocationStrategy(
            name='Test',
            rrsp_pct=0.35, spousal_rrsp_pct=0.10, tfsa_pct=0.30,
            resp_pct=0.07, prioritize_readvanceable=True, deduct_later=False,
        )
        rate_path = build_rate_path(
            name="Test", initial_rate=0.05, term_years=3,
            rate_type='variable', renewal_rates=[0.05],
        )

        # With default objective
        result_default = evaluate_strategy_with_simulation(
            name='Test Default',
            strategy=strategy,
            config=config,
            rate_path=rate_path,
        )
        self.assertEqual(result_default['objective_name'], 'max_net_benefit')

        # With MAX_TERMINAL_WEALTH
        result_tw = evaluate_strategy_with_simulation(
            name='Test Terminal Wealth',
            strategy=strategy,
            config=config,
            rate_path=rate_path,
            objective=MAX_TERMINAL_WEALTH,
        )
        self.assertEqual(result_tw['objective_name'], 'max_terminal_wealth')

    def test_objective_score_in_results(self):
        """Results should contain objective_score field."""
        from optimize import evaluate_strategy_with_simulation
        from strategy import AllocationStrategy
        from countries.canada.rate_model import build_rate_path

        config = _make_config()
        strategy = AllocationStrategy(
            name='Test',
            rrsp_pct=0.35, spousal_rrsp_pct=0.10, tfsa_pct=0.30,
            resp_pct=0.07, prioritize_readvanceable=True, deduct_later=False,
        )
        rate_path = build_rate_path(
            name="Test", initial_rate=0.05, term_years=3,
            rate_type='variable', renewal_rates=[0.05],
        )

        result = evaluate_strategy_with_simulation(
            name='Test',
            strategy=strategy,
            config=config,
            rate_path=rate_path,
        )
        self.assertIn('objective_score', result)
        self.assertIn('net_benefit', result)
        # objective_score should be numeric
        self.assertIsInstance(result['objective_score'], (int, float))
        self.assertIsInstance(result['net_benefit'], (int, float))


class TestObjectiveEvaluate(unittest.TestCase):
    """Test that ObjectiveFunction.evaluate() works correctly."""

    def test_custom_objective_function(self):
        """A custom ObjectiveFunction can be created and evaluated."""
        from simulation import YearResult

        # Create a simple objective: total assets
        total_assets = ObjectiveFunction(
            name="total_assets",
            fn=lambda results, cfg: results[-1].total_assets if results else 0,
            description="Total assets at end of projection",
        )

        self.assertEqual(total_assets.name, "total_assets")
        # evaluate should work with YearResult list
        self.assertEqual(total_assets.evaluate([], {}), 0)


if __name__ == '__main__':
    unittest.main()