#!/usr/bin/env python3
"""
Tests for DP#23: Randomness must be reproducible.

Verifies that monte_carlo() and run_monte_carlo() accept a seed parameter,
use isolated RNG generators, and produce reproducible results without
affecting global RNG state.

Per DP#23: "Any function that uses randomness must accept a seed parameter
and use it consistently."
"""

import unittest
import random
import numpy as np


class TestSensitivityMonteCarloSeed(unittest.TestCase):
    """Test sensitivity.monte_carlo() uses isolated per-call RNG (DP#23)."""

    def test_monte_carlo_accepts_seed_parameter(self):
        """monte_carlo() must accept a seed parameter."""
        from sensitivity import monte_carlo
        # Should not raise TypeError for seed kwarg
        import inspect
        sig = inspect.signature(monte_carlo)
        self.assertIn('seed', sig.parameters,
                      "monte_carlo() must accept a 'seed' parameter per DP#23")

    def test_monte_carlo_reproducible_with_same_seed(self):
        """monte_carlo(cfg, seed=42) produces the same results on every call."""
        from sensitivity import monte_carlo
        cfg = _make_minimal_sensitivity_cfg()
        result1 = monte_carlo(cfg, n_simulations=10, seed=42)
        result2 = monte_carlo(cfg, n_simulations=10, seed=42)
        # Returns columns should match exactly
        np.testing.assert_array_equal(
            result1['investment_return'].values,
            result2['investment_return'].values,
            err_msg="Same seed should produce identical results (DP#23)"
        )

    def test_monte_carlo_different_seeds_different_results(self):
        """monte_carlo(cfg, seed=1) != monte_carlo(cfg, seed=2)."""
        from sensitivity import monte_carlo
        cfg = _make_minimal_sensitivity_cfg()
        result1 = monte_carlo(cfg, n_simulations=10, seed=1)
        result2 = monte_carlo(cfg, n_simulations=10, seed=2)
        # At least one value should differ
        self.assertFalse(
            np.array_equal(result1['investment_return'].values,
                          result2['investment_return'].values),
            "Different seeds should produce different results"
        )

    def test_monte_carlo_does_not_affect_global_numpy_rng(self):
        """monte_carlo() must not set global np.random.seed (DP#3, DP#23)."""
        from sensitivity import monte_carlo
        cfg = _make_minimal_sensitivity_cfg()
        # Set a known global state
        np.random.seed(12345)
        state_before = np.random.get_state()[1][:5].copy()
        # Call monte_carlo with a different seed
        monte_carlo(cfg, n_simulations=10, seed=42)
        # Global state should be unchanged
        state_after = np.random.get_state()[1][:5].copy()
        np.testing.assert_array_equal(
            state_before, state_after,
            err_msg="monte_carlo() must not affect global numpy RNG state"
        )

    def test_monte_carlo_default_seed_is_reproducible(self):
        """monte_carlo() with default seed=42 is reproducible (DP#13)."""
        from sensitivity import monte_carlo
        cfg = _make_minimal_sensitivity_cfg()
        result1 = monte_carlo(cfg, n_simulations=10)
        result2 = monte_carlo(cfg, n_simulations=10)
        np.testing.assert_array_equal(
            result1['investment_return'].values,
            result2['investment_return'].values,
            err_msg="Default seed=42 should produce reproducible results (DP#23)"
        )


    def test_monte_carlo_accepts_return_model_parameter(self):
        """monte_carlo() must accept a return_model parameter (DP#21)."""
        from sensitivity import monte_carlo
        import inspect
        sig = inspect.signature(monte_carlo)
        self.assertIn('return_model', sig.parameters,
                      "monte_carlo() must accept a 'return_model' parameter per DP#21")

    def test_monte_carlo_with_stochastic_return_model(self):
        """monte_carlo() with StochasticReturn produces results (DP#21)."""
        from sensitivity import monte_carlo
        from return_model import StochasticReturn
        cfg = _make_minimal_sensitivity_cfg()
        model = StochasticReturn(mean=0.07, sigma=0.18, seed=42, n_years=10)
        result = monte_carlo(cfg, n_simulations=10, seed=42, return_model=model)
        self.assertEqual(len(result), 10)

    def test_monte_carlo_with_fixed_return_model(self):
        """monte_carlo() with FixedReturn adds noise around fixed rate (DP#21)."""
        from sensitivity import monte_carlo
        from return_model import FixedReturn
        cfg = _make_minimal_sensitivity_cfg()
        model = FixedReturn(rate=0.07)
        result = monte_carlo(cfg, n_simulations=10, seed=42, return_model=model)
        self.assertEqual(len(result), 10)


class TestSimulateRunMonteCarloSeed(unittest.TestCase):
    """Test simulate.run_monte_carlo() uses isolated per-call RNG (DP#23)."""

    def test_run_monte_carlo_accepts_seed_parameter(self):
        """run_monte_carlo() must accept a seed parameter."""
        from simulate import run_monte_carlo
        import inspect
        sig = inspect.signature(run_monte_carlo)
        self.assertIn('seed', sig.parameters,
                      "run_monte_carlo() must accept a 'seed' parameter per DP#23")

    def test_run_monte_carlo_does_not_affect_global_python_rng(self):
        """run_monte_carlo() must not set global random.seed (DP#3, DP#23)."""
        from simulate import run_monte_carlo
        # Set a known global state
        random.seed(12345)
        state_before = random.getstate()
        # Run with a different seed
        # We need a minimal results/anchors to avoid early return
        results = [{'net_benefit': 1000, 'resp_cash_out': 0,
                    'use_readvanceable': False, 'deduct_later': False,
                    'label': 'test', 'cash_out': 0, 'primary_income': 100000,
                    'spouse_income': 50000, 'mortgage_rate': 0.05,
                    'ltv': 0.5, 'strategy_id': 'test'}]
        anchors = {'strategy': [{'id': 'test', 'label': 'Test',
                                 'rrsp_pct': 0.35, 'spousal_rrsp_pct': 0.10,
                                 'tfsa_pct': 0.30, 'fhsa_pct': 0.0,
                                 'resp_pct': 0.07, 'non_reg_pct': 0.18}]}
        base_cfg = _make_minimal_simulate_cfg()
        # Call with seed different from global
        run_monte_carlo(base_cfg, results, anchors, n_paths=3, seed=99)
        # Global state should be unchanged
        state_after = random.getstate()
        self.assertEqual(state_before, state_after,
                         "run_monte_carlo() must not affect global random state")

    def test_run_monte_carlo_reproducible_with_same_seed(self):
        """run_monte_carlo(base_cfg, results, anchors, seed=42) is reproducible."""
        from simulate import run_monte_carlo
        results = [{'net_benefit': 1000, 'resp_cash_out': 0,
                    'use_readvanceable': False, 'deduct_later': False,
                    'label': 'test', 'cash_out': 0, 'primary_income': 100000,
                    'spouse_income': 50000, 'mortgage_rate': 0.05,
                    'ltv': 0.5, 'strategy_id': 'test'}]
        anchors = {'strategy': [{'id': 'test', 'label': 'Test',
                                 'rrsp_pct': 0.35, 'spousal_rrsp_pct': 0.10,
                                 'tfsa_pct': 0.30, 'fhsa_pct': 0.0,
                                 'resp_pct': 0.07, 'non_reg_pct': 0.18}]}
        base_cfg = _make_minimal_simulate_cfg()
        summary1 = run_monte_carlo(base_cfg, results, anchors, n_paths=3, seed=42)
        summary2 = run_monte_carlo(base_cfg, results, anchors, n_paths=3, seed=42)
        # The summaries should match
        self.assertEqual(len(summary1), len(summary2))
        for s1, s2 in zip(summary1, summary2):
            self.assertAlmostEqual(s1['p10'], s2['p10'],
                                 msg="Same seed should give same P10 (DP#23)")
            self.assertAlmostEqual(s1['p50'], s2['p50'],
                                 msg="Same seed should give same P50 (DP#23)")
            self.assertAlmostEqual(s1['p90'], s2['p90'],
                                 msg="Same seed should give same P90 (DP#23)")

    def test_run_monte_carlo_different_seeds_different_results(self):
        """run_monte_carlo with seed=1 != seed=2."""
        from simulate import run_monte_carlo
        results = [{'net_benefit': 1000, 'resp_cash_out': 0,
                    'use_readvanceable': False, 'deduct_later': False,
                    'label': 'test', 'cash_out': 0, 'primary_income': 100000,
                    'spouse_income': 50000, 'mortgage_rate': 0.05,
                    'ltv': 0.5, 'strategy_id': 'test'}]
        anchors = {'strategy': [{'id': 'test', 'label': 'Test',
                                 'rrsp_pct': 0.35, 'spousal_rrsp_pct': 0.10,
                                 'tfsa_pct': 0.30, 'fhsa_pct': 0.0,
                                 'resp_pct': 0.07, 'non_reg_pct': 0.18}]}
        base_cfg = _make_minimal_simulate_cfg()
        summary1 = run_monte_carlo(base_cfg, results, anchors, n_paths=10, seed=1)
        summary2 = run_monte_carlo(base_cfg, results, anchors, n_paths=10, seed=2)
        # At least one metric should differ
        differs = False
        for s1, s2 in zip(summary1, summary2):
            if s1['p10'] != s2['p10'] or s1['p50'] != s2['p50'] or s1['p90'] != s2['p90']:
                differs = True
                break
        self.assertTrue(differs, "Different seeds should produce different results")


class TestRiskAllocationSeed(unittest.TestCase):
    """Test risk_allocation._mc_risk_metrics / recommend_allocation thread an
    explicit, reproducible seed (DP#23)."""

    def test_mc_risk_metrics_accepts_seed_parameter(self):
        """_mc_risk_metrics() must accept a seed parameter."""
        import inspect
        from risk_allocation import _mc_risk_metrics
        sig = inspect.signature(_mc_risk_metrics)
        self.assertIn('seed', sig.parameters,
                      "_mc_risk_metrics() must accept a 'seed' parameter per DP#23")

    def test_recommend_allocation_accepts_seed_parameter(self):
        """recommend_allocation() must accept a seed parameter."""
        import inspect
        from risk_allocation import recommend_allocation
        sig = inspect.signature(recommend_allocation)
        self.assertIn('seed', sig.parameters,
                      "recommend_allocation() must accept a 'seed' parameter per DP#23")

    def test_recommend_allocation_reproducible_with_same_seed(self):
        """recommend_allocation(cfg, seed=474) gives identical risk_metrics twice."""
        from risk_allocation import recommend_allocation
        cfg = _make_minimal_risk_cfg()
        m1 = recommend_allocation(cfg, seed=474)["risk_metrics"]
        m2 = recommend_allocation(cfg, seed=474)["risk_metrics"]
        # Deep-equal the whole metrics dict (p10/p50/p90, P(loss), blended stats).
        self.assertEqual(m1, m2,
                         msg="Same seed should produce identical risk_metrics (DP#23)")

    def test_recommend_allocation_different_seeds_different_results(self):
        """recommend_allocation(cfg, seed=474) != recommend_allocation(cfg, seed=999)."""
        from risk_allocation import recommend_allocation
        cfg = _make_minimal_risk_cfg()
        m1 = recommend_allocation(cfg, seed=474)["risk_metrics"]
        m2 = recommend_allocation(cfg, seed=999)["risk_metrics"]
        self.assertNotEqual(m1, m2,
                            msg="Different seeds should produce different risk_metrics")

    def test_recommend_allocation_default_preserves_behaviour(self):
        """The default seed reproduces the explicit seed=474 (behaviour-preserving)."""
        from risk_allocation import recommend_allocation
        cfg = _make_minimal_risk_cfg()
        m_default = recommend_allocation(cfg)["risk_metrics"]
        m_474 = recommend_allocation(cfg, seed=474)["risk_metrics"]
        self.assertEqual(m_default, m_474,
                         msg="Default seed must equal seed=474 (additive, behaviour-preserving)")

    def test_mc_risk_metrics_seed_zero_is_honoured(self):
        """seed=0 (falsy but valid, DP#32) is reproducible and distinct from 474."""
        from risk_allocation import _mc_risk_metrics
        mix = {"equity_pct": 0.6, "fixed_income_pct": 0.4}
        m0a = _mc_risk_metrics(mix, years_to_horizon=10, seed=0)
        m0b = _mc_risk_metrics(mix, years_to_horizon=10, seed=0)
        self.assertEqual(m0a, m0b,
                         msg="seed=0 must be reproducible (DP#32: 0 is a value, not a fallback)")
        m474 = _mc_risk_metrics(mix, years_to_horizon=10, seed=474)
        self.assertNotEqual(m0a, m474,
                            msg="seed=0 must differ from seed=474 (proves 0 is not coerced to default)")

    def test_mc_risk_metrics_does_not_affect_global_numpy_rng(self):
        """_mc_risk_metrics() must not set global np.random.seed (DP#3, DP#23)."""
        from risk_allocation import _mc_risk_metrics
        np.random.seed(12345)
        state_before = np.random.get_state()[1][:5].copy()
        mix = {"equity_pct": 0.6, "fixed_income_pct": 0.4}
        _mc_risk_metrics(mix, years_to_horizon=10, seed=42)
        state_after = np.random.get_state()[1][:5].copy()
        np.testing.assert_array_equal(
            state_before, state_after,
            err_msg="_mc_risk_metrics() must not affect global numpy RNG state")


class TestMonteCarloOptimizerSeed(unittest.TestCase):
    """Test MonteCarloOptimizer.optimize() is same-seed reproducible,
    different-seed divergent, and leaves global np.random untouched (DP#23)."""

    def test_optimize_reproducible_with_same_seed(self):
        """Two optimizers with the same seed_base produce identical ranked output."""
        from monte_carlo_optimizer import MonteCarloOptimizer
        from countries.canada.strategies import STRATEGY_BALANCED
        cfg = _make_minimal_monte_carlo_cfg()
        opt1 = MonteCarloOptimizer(cfg, n_simulations=8, seed_base=42)
        opt2 = MonteCarloOptimizer(cfg, n_simulations=8, seed_base=42)
        r1 = opt1.optimize(strategies=[STRATEGY_BALANCED])
        r2 = opt2.optimize(strategies=[STRATEGY_BALANCED])
        self.assertEqual(len(r1), len(r2))
        for a, b in zip(r1, r2):
            # Deep-equal the deterministic, seed-driven fields: name, score,
            # objective, and every numeric risk measure. (scenario_name encodes
            # the sm/dl switches, which are seed-independent but identical here.)
            self.assertEqual(a.scenario_name, b.scenario_name)
            self.assertEqual(a.objective_name, b.objective_name)
            self.assertEqual(a.score, b.score,
                             msg="Same seed_base should give identical score (DP#23)")
            self.assertEqual(a.risk_measures, b.risk_measures,
                             msg="Same seed_base should give identical risk_measures (DP#23)")

    def test_optimize_different_seeds_different_results(self):
        """seed_base=42 != seed_base=999 produces a different score/risk profile."""
        from monte_carlo_optimizer import MonteCarloOptimizer
        from countries.canada.strategies import STRATEGY_BALANCED
        cfg = _make_minimal_monte_carlo_cfg()
        r1 = MonteCarloOptimizer(cfg, n_simulations=16, seed_base=42).optimize(
            strategies=[STRATEGY_BALANCED])
        r2 = MonteCarloOptimizer(cfg, n_simulations=16, seed_base=999).optimize(
            strategies=[STRATEGY_BALANCED])
        differs = (r1[0].score != r2[0].score
                   or r1[0].risk_measures != r2[0].risk_measures)
        self.assertTrue(differs,
                        msg="Different seed_base should produce different results")

    def test_optimize_does_not_affect_global_numpy_rng(self):
        """MonteCarloOptimizer.optimize() must not set global np.random.seed."""
        from monte_carlo_optimizer import MonteCarloOptimizer
        from countries.canada.strategies import STRATEGY_BALANCED
        cfg = _make_minimal_monte_carlo_cfg()
        np.random.seed(12345)
        state_before = np.random.get_state()[1][:5].copy()
        MonteCarloOptimizer(cfg, n_simulations=8, seed_base=42).optimize(
            strategies=[STRATEGY_BALANCED])
        state_after = np.random.get_state()[1][:5].copy()
        np.testing.assert_array_equal(
            state_before, state_after,
            err_msg="MonteCarloOptimizer.optimize() must not affect global numpy RNG state")


class TestRiskEnsembleScoresSeed(unittest.TestCase):
    """Test optimize._risk_ensemble_scores() threads an explicit, reproducible
    seed (DP#23)."""

    STOCH = {'type': 'stochastic', 'mean': 0.07, 'sigma': 0.15}

    def test_risk_ensemble_scores_accepts_seed_parameter(self):
        """_risk_ensemble_scores() must accept a seed parameter."""
        import inspect
        from optimize import _risk_ensemble_scores
        sig = inspect.signature(_risk_ensemble_scores)
        self.assertIn('seed', sig.parameters,
                      "_risk_ensemble_scores() must accept a 'seed' parameter per DP#23")

    def _scores(self, optimize_mod, seed, paths=20):
        """Run the ensemble over a small, fast path count (isolated via the
        module global, restored on exit -- the file's existing convention)."""
        from test_issue_937_risk_objectives import (
            _make_config, _strategy, _rate_path,
        )
        from countries.canada.adapter import CanadaAdapter
        from objective import MAX_CVAR_TERMINAL
        saved = optimize_mod.RISK_ENSEMBLE_PATHS
        optimize_mod.RISK_ENSEMBLE_PATHS = paths
        try:
            cfg = _make_config(self.STOCH)
            return optimize_mod._risk_ensemble_scores(
                cfg, CanadaAdapter(cfg), _strategy(), _rate_path(),
                True, False, 0.0, 0.0, MAX_CVAR_TERMINAL, {}, seed=seed)
        finally:
            optimize_mod.RISK_ENSEMBLE_PATHS = saved

    def test_reproducible_with_same_seed(self):
        """seed=42 produces identical score lists on every call."""
        import optimize
        s1 = self._scores(optimize, seed=42)
        s2 = self._scores(optimize, seed=42)
        self.assertIsNotNone(s1)
        self.assertEqual(s1, s2,
                         msg="Same seed should produce identical ensemble scores (DP#23)")

    def test_different_seeds_different_results(self):
        """seed=42 != seed=999 produces a different score list."""
        import optimize
        s1 = self._scores(optimize, seed=42)
        s2 = self._scores(optimize, seed=999)
        self.assertNotEqual(s1, s2,
                           msg="Different seeds should produce different ensemble scores")

    def test_default_seed_preserves_behaviour(self):
        """The default seed reproduces the explicit seed=42 (behaviour-preserving)."""
        import optimize
        from test_issue_937_risk_objectives import (
            _make_config, _strategy, _rate_path,
        )
        from countries.canada.adapter import CanadaAdapter
        from objective import MAX_CVAR_TERMINAL
        saved = optimize.RISK_ENSEMBLE_PATHS
        optimize.RISK_ENSEMBLE_PATHS = 20
        try:
            cfg = _make_config(self.STOCH)
            args = (cfg, CanadaAdapter(cfg), _strategy(), _rate_path(),
                    True, False, 0.0, 0.0, MAX_CVAR_TERMINAL, {})
            s_default = optimize._risk_ensemble_scores(*args)
            s_42 = optimize._risk_ensemble_scores(*args, seed=42)
            self.assertEqual(s_default, s_42,
                             msg="Default seed must equal seed=42 (additive, behaviour-preserving)")
        finally:
            optimize.RISK_ENSEMBLE_PATHS = saved

    def test_seed_zero_is_honoured(self):
        """seed=0 (falsy but valid, DP#32) is reproducible and distinct from 42."""
        import optimize
        s0a = self._scores(optimize, seed=0)
        s0b = self._scores(optimize, seed=0)
        self.assertEqual(s0a, s0b,
                         msg="seed=0 must be reproducible (DP#32: 0 is a value, not a fallback)")
        s42 = self._scores(optimize, seed=42)
        self.assertNotEqual(s0a, s42,
                            msg="seed=0 must differ from seed=42 (proves 0 is not coerced to default)")


# -- Helpers: minimal configs that avoid needing input.json --

def _make_minimal_sensitivity_cfg():
    """Minimal config dict for sensitivity.monte_carlo.

    Uses fake round numbers per DP#15 (no personal data).
    monte_carlo only uses the cfg to call run_scenario, which needs
    enough structure to avoid crashing. We mock run_scenario instead.
    """
    return {
        'assumptions': {'investment_return': 0.07},
        'property': {'house_value': 500000, 'mortgage_balance': 200000,
                     'mortgage_rate': 0.05, 'ltv_max': 0.80, 'margin_available': 50000},
        'family': {'members': [
            {'role': 'primary', 'gross_income': 100000},
            {'role': 'spouse', 'gross_income': 50000},
        ]},
    }


def _make_minimal_simulate_cfg():
    """Minimal config dict for simulate.run_monte_carlo.

    Issue #735: this fixture used to declare a `margin_available` and NO
    savings rate, and its only invested capital came from the engine drawing
    that entire facility at year 0 and investing it -- the exact bug #735
    fixes. With the draw correctly defaulting to zero, a household that
    saves nothing invests nothing, and a stochastic RETURN applied to $0 of
    risky capital produces $0 of variance: P10 == P50 == P90, for every
    seed. That is arithmetically right, and it made this file's DP#23
    "different seeds must differ" assertion vacuous rather than wrong.

    So the fixture now declares a real `savings.rate` -- an honest source of
    invested capital that does not depend on borrowing. The DP#23 property
    under test is unchanged; it is simply asserted against a household that
    can actually exhibit it.
    """
    return {
        'assumptions': {'investment_return': 0.07},
        'savings': {'rate': 0.20},
        'property': {'house_value': 500000, 'mortgage_balance': 200000,
                     'mortgage_rate': 0.05, 'ltv_max': 0.80, 'margin_available': 50000},
        'family': {'members': [
            {'role': 'primary', 'gross_income': 100000},
            {'role': 'spouse', 'gross_income': 50000},
        ]},
    }


def _make_minimal_risk_cfg():
    """Minimal config dict for risk_allocation.recommend_allocation.

    Declares a ``portfolio.risk_tolerance`` (so the recommendation runs rather
    than no-op-ing) plus the start_year/birth_year/retirement_age the glide's
    horizon needs (``_horizon`` returns None without all three). Fabricated
    round numbers per DP#15.
    """
    return {
        'assumptions': {'investment_return': 0.07, 'start_year': 2026,
                        'horizon_age': 90},
        'property': {'house_value': 500000, 'mortgage_balance': 200000,
                     'mortgage_rate': 0.05},
        'portfolio': {'risk_tolerance': 'balanced'},
        'family': {'members': [
            {'role': 'primary', 'gross_income': 100000,
             'birth_year': 1980, 'retirement_age': 65},
            {'role': 'spouse', 'gross_income': 50000},
        ]},
    }


def _make_minimal_monte_carlo_cfg():
    """Minimal SimulationConfig for MonteCarloOptimizer.optimize().

    Mirrors tests/test_monte_carlo_optimizer.py::_make_config: a fabricated
    household with enough structure (income, rooms, a coherent property so a
    readvanceable line has a charge to advance against) that the optimizer's
    per-path simulations return real, seed-sensitive scores.
    """
    from simulation import SimulationConfig
    return SimulationConfig(
        projection_years=3, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
        ],
        mortgage_balance=100000, mortgage_rate=0.05,
        house_value=800000,
    )


if __name__ == '__main__':
    unittest.main()
