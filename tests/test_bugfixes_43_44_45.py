#!/usr/bin/env python3
"""Tests for bug fixes #43, #44, #45.

#43: ScipyOptimizer must produce same scores as GridOptimizer for same LTV.
#44: MonteCarloOptimizer must vary returns per path (StochasticReturn).
#45: deduct_later=True must use bracket-aware deduction, not leave all undeducted.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from dataclasses import replace

from simulation import SimulationConfig
from simulation_config import apply_ltv_overlay, apply_overlay, ScenarioOverlay
from simulation_state import SimState, simulate_year_pure
from scipy_optimizer import ScipyOptimizer
from optimizer import GridOptimizer
from monte_carlo_optimizer import MonteCarloOptimizer
from return_model import FixedReturn, StochasticReturn
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
        children=[],
        mortgage_balance=100000, mortgage_rate=0.05, house_value=400000,
        deduct_later_bracket_target=117045,  # DP#45: explicit bracket target for deduct_later tests
        refinance_amortization_years=25,  # #655: fabricated round new-loan term for LTV-overlay tests
    )


class TestBug43ScipyGridConsistency(unittest.TestCase):
    """ScipyOptimizer and GridOptimizer must produce consistent LTV results.

    Issue #619: both optimizers used to carry their own private
    ``_apply_ltv_overlay`` copy, and both inflated ``margin_available`` by
    ``cash_out`` -- the pre-#257 money-flow model. They agreed with each
    other (this test used to assert exactly that) but both disagreed with
    ``simulation_config.apply_overlay``, the #257-correct rule used by
    simulate.py. Fixed by deleting both copies (DP#9) and routing both
    optimizers through the single ``simulation_config.apply_ltv_overlay``.
    These tests now assert the *correct* invariant: the optimizer path and
    the simulate.py overlay path size the same overlay identically, and
    money is conserved (invested capital == new debt taken on).
    """

    def test_apply_ltv_overlay_matches_grid_optimizer(self):
        """The optimizer-path overlay and the simulate.py overlay path
        (apply_overlay/ScenarioOverlay) must agree on mortgage_balance,
        margin_available and cash_out for the same LTV -- and margin_available
        must shrink dollar-for-dollar by the cash-out booked (#664: mortgage
        and HELOC share ONE registered charge, not independent borrowing
        sources)."""
        cfg = _make_config()
        ltv = 0.65
        cash_out = ltv * cfg.house_value - cfg.mortgage_balance  # 160000

        # Optimizer path (GridOptimizer and ScipyOptimizer both call this).
        optimizer_cfg = apply_ltv_overlay(cfg, ltv)

        # simulate.py path: ScenarioOverlay + apply_overlay on the dict form.
        base_dict = cfg.to_dict()
        base_dict['property']['mortgage_rate'] = cfg.mortgage_rate
        overlaid_dict = apply_overlay(
            base_dict, ScenarioOverlay(label='test', cash_out=cash_out,
                                        mortgage_rate=cfg.mortgage_rate,
                                        refinance_amortization_years=cfg.refinance_amortization_years))
        simulate_cfg = SimulationConfig.from_dict(overlaid_dict)

        self.assertEqual(optimizer_cfg.mortgage_balance, simulate_cfg.mortgage_balance,
                         "Optimizer and simulate.py paths must agree on mortgage_balance")
        self.assertEqual(optimizer_cfg.margin_available, simulate_cfg.margin_available,
                         "Optimizer and simulate.py paths must agree on margin_available")
        self.assertEqual(optimizer_cfg.margin_available, cfg.margin_available - cash_out,
                         "margin_available must shrink by exactly the cash-out booked (#664)")
        self.assertEqual(optimizer_cfg.cash_out, simulate_cfg.cash_out)

        # Money conservation (DP#18): invested capital == new debt taken on.
        invested = optimizer_cfg.margin_available + optimizer_cfg.cash_out
        new_debt = (optimizer_cfg.mortgage_balance - cfg.mortgage_balance) + optimizer_cfg.margin_available
        self.assertEqual(invested, new_debt,
                         "Invested capital must equal new debt taken on, not new debt + a phantom cash_out")

    def test_ltv_zero_returns_base(self):
        """LTV=0 should return base config unchanged."""
        cfg = _make_config()
        modified = apply_ltv_overlay(cfg, 0.0)
        self.assertEqual(modified.mortgage_balance, cfg.mortgage_balance)
        self.assertEqual(modified.margin_available, cfg.margin_available)

    def test_ltv_positive_adds_cashout_to_mortgage_and_shrinks_margin(self):
        """LTV > 0 adds cash-out to mortgage_balance AND shrinks
        margin_available by the same amount (#664: was 'margin_available
        untouched' pre-fix -- that let the household draw the FULL
        pre-existing HELOC limit *in addition to* the refinanced mortgage,
        doubling apparent borrowing capacity against one registered charge)."""
        cfg = _make_config()  # mortgage=100000, house=400000, margin=200000
        ltv = 0.65  # keeps combined debt within the 80% charge limit (320000)
        expected_cashout = ltv * cfg.house_value - cfg.mortgage_balance  # 160000
        modified = apply_ltv_overlay(cfg, ltv)
        self.assertAlmostEqual(modified.mortgage_balance,
                               cfg.mortgage_balance + expected_cashout)
        self.assertAlmostEqual(modified.margin_available, cfg.margin_available - expected_cashout,
                               msg="margin_available must shrink dollar-for-dollar by the cash-out booked (#664)")
        self.assertAlmostEqual(modified.cash_out, expected_cashout)
        # The combined facility must still fit inside the charge (#664).
        total_secured = modified.mortgage_balance + modified.margin_available
        self.assertLessEqual(total_secured, cfg.house_value * cfg.charge_ltv_limit + 0.01)


class TestBug44MonteCarloVaryingReturns(unittest.TestCase):
    """MonteCarloOptimizer must use StochasticReturn per path."""
    
    def test_stochastic_returns_vary_per_seed(self):
        """Different seeds produce different return sequences."""
        r1 = StochasticReturn(mean=0.07, sigma=0.15, seed=42, n_years=10)
        r2 = StochasticReturn(mean=0.07, sigma=0.15, seed=43, n_years=10)
        # Different seeds must produce at least some different returns
        returns1 = [r1.return_for_year(i) for i in range(10)]
        returns2 = [r2.return_for_year(i) for i in range(10)]
        self.assertNotEqual(returns1, returns2,
                            "Different seeds must produce different return sequences")
    
    def test_stochastic_same_seed_reproducible(self):
        """Same seed produces same return sequence (DP#23)."""
        r1 = StochasticReturn(mean=0.07, sigma=0.15, seed=42, n_years=10)
        r2 = StochasticReturn(mean=0.07, sigma=0.15, seed=42, n_years=10)
        returns1 = [r1.return_for_year(i) for i in range(10)]
        returns2 = [r2.return_for_year(i) for i in range(10)]
        self.assertEqual(returns1, returns2,
                         "Same seed must produce identical sequences (reproducibility)")
    
    def test_monte_carlo_produces_risk_measures(self):
        """MC optimizer must produce varying risk measures (not P(loss)=0)."""
        cfg = _make_config()
        mc = MonteCarloOptimizer(cfg, n_simulations=10, seed_base=42)
        results = mc.optimize(strategies=[STRATEGY_BALANCED],
                              use_readvanceable_options=[False],
                              deduct_later_options=[False])
        self.assertGreater(len(results), 0)
        self.assertGreater(results[0].risk_measures.n_simulations, 0,
                           "MC must report n_simulations")
        # With stochastic returns and only 10 paths, P(loss) should be calculable
        self.assertIsNotNone(results[0].risk_measures.probability_of_loss)


class TestBug45DeductLaterBracketAware(unittest.TestCase):
    """deduct_later=True must use bracket-aware deduction, not just leave all undeducted."""
    
    def test_deduct_later_partial_deduction(self):
        """When deduct_later=True, only deduct enough to reach bracket target."""
        cfg = _make_config()
        # Primary income ~120000, bracket target default = 117045
        # With 50k RRSP contribution and deduct_later=True,
        # should deduct only (120000 - 117045) = 2955 at the highest bracket
        # leaving the rest undeducted for future years
        state = SimState.initial(cfg)
        state.jurisdiction_state['canada']['adult_rrsp']['primary']['own_room'] = 50000  # #700
        
        allocations = {
            'primary_rrsp': 15000,
            'spousal_rrsp': 0,
            'spouse_rrsp': 0,
            'primary_tfsa': 0,
            'spouse_tfsa': 0,
            'non_reg': 0,
            'resp': 0,
            '_primary_income': 120000,
            '_spouse_income': 50000,
            '_annual_savings': 15000,
        }
        
        result, new_state = simulate_year_pure(
            state, year=0, allocations=allocations, config=cfg,
            investment_return=0.07, mortgage_rate=0.05,
            use_readvanceable=False, deduct_later=True,
            primary_marginal_rate=0.4571,
            spouse_marginal_rate=0.3071,
        )
        
        # With deduct_later=True, the ledger should have undeducted contributions
        ledger = new_state.jurisdiction_state['canada']['rrsp_ledger']
        from simulation_state import ledger_undeducted_total, ledger_total_claimed
        total_undeducted = ledger_undeducted_total(ledger)
        total_deducted = ledger_total_claimed(ledger)
        
        # Some should be deducted (bracket-aware), rest carried forward
        self.assertGreater(total_deducted, 0,
                            "Some deductions should be claimed at the top bracket")
        self.assertGreater(total_undeducted, 0,
                            "Remaining should stay undeducted for future years")
        
        # Carry-forward field should be populated
        self.assertGreater(new_state.jurisdiction_state['canada']['rrsp_deduction_carry_forward'], 0,
                           "Carry-forward should be non-zero with deduct_later=True")
    
    def test_deduct_now_claims_all(self):
        """When deduct_later=False, all deductions should be claimed immediately."""
        cfg = _make_config()
        state = SimState.initial(cfg)
        state.jurisdiction_state['canada']['adult_rrsp']['primary']['own_room'] = 50000  # #700
        
        allocations = {
            'primary_rrsp': 15000,
            'spousal_rrsp': 0,
            'spouse_rrsp': 0,
            'primary_tfsa': 0,
            'spouse_tfsa': 0,
            'non_reg': 0,
            'resp': 0,
            '_primary_income': 120000,
            '_spouse_income': 50000,
            '_annual_savings': 15000,
        }
        
        result, new_state = simulate_year_pure(
            state, year=0, allocations=allocations, config=cfg,
            investment_return=0.07, mortgage_rate=0.05,
            use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.4571,
            spouse_marginal_rate=0.3071,
        )
        
        # All RRSP contributions should be deducted
        ledger = new_state.jurisdiction_state['canada']['rrsp_ledger']
        from simulation_state import ledger_undeducted_total, ledger_total_claimed
        total_undeducted = ledger_undeducted_total(ledger)
        total_deducted = ledger_total_claimed(ledger)
        
        self.assertAlmostEqual(total_undeducted, 0,
                               msg="All RRSP should be deducted when deduct_later=False")
        self.assertGreater(total_deducted, 0,
                           "Deductions should be claimed when deduct_later=False")
    
    def test_bracket_target_configurable(self):
        """deduct_later_bracket_target is configurable in SimulationConfig (DP#45)."""
        # Create a bare config without explicitly setting bracket target
        cfg_default = SimulationConfig()
        # DP#13: default is 0 (not set); the simulation auto-detects from tax brackets
        self.assertEqual(cfg_default.deduct_later_bracket_target, 0,
                         "Default bracket target should be 0 (auto-detect from tax brackets)")
        
        cfg_custom = SimulationConfig(deduct_later_bracket_target=117045)
        self.assertEqual(cfg_custom.deduct_later_bracket_target, 117045,
                         "Custom bracket target should be configurable")


if __name__ == '__main__':
    unittest.main()