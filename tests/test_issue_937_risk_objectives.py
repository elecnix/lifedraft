#!/usr/bin/env python3
"""Issue #937: pluggable risk objectives (CVaR@10% / P10 of terminal net worth).

A risk objective ranks a candidate by a tail statistic of its terminal-wealth
DISTRIBUTION over a stochastic-return ensemble, not by its single deterministic
outcome -- so the optimizer can prefer the strategy whose bad case is least bad
(sequence-of-returns-risk aversion), rather than the highest-expected-value one.

These tests lock four things:
  1. the tail math (`_cvar`, `_low_percentile`) including the single-path
     degenerate case;
  2. absence-safety -- a household with NO stochastic return model ranks by the
     single deterministic score, byte-identical to a per-path objective (DP#32);
  3. the ensemble genuinely runs and is reproducible (DP#23) when a stochastic
     return model IS declared, and the score equals the CVaR of the collected
     paths;
  4. the risk-aversion INVARIANT -- CVaR@10% never exceeds the ensemble mean, so
     the objective provably weights the downside.

Round numbers per DP#13/DP#15.
"""

import unittest

from objective import (
    _cvar, _low_percentile, get_objective, OBJECTIVES,
    MAX_CVAR_TERMINAL, MAX_P10_TERMINAL, MAX_TERMINAL_WEALTH,
)
from simulation_config import SimulationConfig
from strategy import AllocationStrategy
from countries.canada.rate_model import build_rate_path


def _make_config(return_model_data=None):
    """Minimal household. `return_model_data` declares the return distribution;
    None leaves the deterministic default (investment_return=0.07)."""
    return SimulationConfig(
        projection_years=5,
        investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
        ],
        mortgage_balance=100000, mortgage_rate=0.05,
        house_value=800000,
        return_model_data=return_model_data,
    )


def _strategy():
    return AllocationStrategy(
        name='Test', rrsp_pct=0.35, spousal_rrsp_pct=0.10, tfsa_pct=0.30,
        resp_pct=0.07, prioritize_readvanceable=True, deduct_later=False,
    )


def _rate_path():
    return build_rate_path(name="Test", initial_rate=0.05, term_years=5,
                           rate_type='variable', renewal_rates=[0.05])


class TestTailMath(unittest.TestCase):
    """The pure tail statistics -- the ranking correctness lives here."""

    def test_cvar_is_mean_of_worst_fraction(self):
        s = [1000, 900, 800, 700, 600, 500, 400, 300, 200, 100]  # unordered
        # worst 10% of 10 = worst 1 = 100
        self.assertEqual(_cvar(s, 0.10), 100)
        # worst 30% = worst 3 = (100+200+300)/3 = 200
        self.assertEqual(_cvar(s, 0.30), 200)

    def test_cvar_tail_floored_at_one_path(self):
        # ceil(0.10 * 3) = 1: never an empty tail even for a tiny ensemble
        self.assertEqual(_cvar([300, 200, 100], 0.10), 100)

    def test_low_percentile_is_nearest_rank(self):
        s = [1000, 900, 800, 700, 600, 500, 400, 300, 200, 100]
        # 10th pct nearest-rank: idx = ceil(0.10*10)-1 = 0 -> smallest = 100
        self.assertEqual(_low_percentile(s, 0.10), 100)
        self.assertEqual(_low_percentile(s, 0.50), 500)

    def test_single_path_degenerates_to_the_value(self):
        # no declared spread -> a one-point distribution -> the value itself
        self.assertEqual(_cvar([555.0], 0.10), 555.0)
        self.assertEqual(_low_percentile([555.0], 0.10), 555.0)

    def test_empty_is_zero_not_a_crash(self):
        self.assertEqual(_cvar([], 0.10), 0.0)
        self.assertEqual(_low_percentile([], 0.10), 0.0)

    def test_cvar_never_exceeds_the_mean(self):
        # The defining risk-aversion invariant: the mean of the worst tail is
        # <= the mean of the whole. Holds for any distribution.
        import statistics
        s = [100, 250, 400, 380, 900, 610, 220, 770, 480, 130]
        self.assertLessEqual(_cvar(s, 0.10), statistics.mean(s))
        self.assertLessEqual(_cvar(s, 0.30), statistics.mean(s))


class TestRegistration(unittest.TestCase):
    def test_both_objectives_registered_and_distributional(self):
        for name in ('max_cvar_terminal', 'max_p10_terminal'):
            obj = get_objective(name)
            self.assertIn(name, OBJECTIVES)
            self.assertIsNotNone(obj.rank_from_distribution,
                                 f"{name} must aggregate over a distribution")
            # per-path metric is terminal net worth (shared with terminal_wealth)
            self.assertIs(obj.fn, MAX_TERMINAL_WEALTH.fn)

    def test_aggregate_routes_through_rank_from_distribution(self):
        # objective.aggregate must use the tail statistic, not the mean
        self.assertEqual(MAX_CVAR_TERMINAL.aggregate([300, 200, 100]), 100)
        self.assertEqual(MAX_P10_TERMINAL.aggregate([300, 200, 100]), 100)


class TestAbsenceSafety(unittest.TestCase):
    """A household with no stochastic return model must not trigger the ensemble
    and must score identically to the plain per-path terminal-wealth objective."""

    def test_deterministic_config_skips_ensemble(self):
        from optimize import evaluate_strategy_with_simulation, _risk_ensemble_scores
        config = _make_config(return_model_data=None)  # fixed 0.07
        # the ensemble helper refuses a non-stochastic model
        self.assertIsNone(_risk_ensemble_scores(
            config, None, _strategy(), _rate_path(), True, False, 0.0, 0.0,
            MAX_CVAR_TERMINAL, {}))
        # ... so the risk objective's score equals the deterministic terminal
        # wealth (same single path max_terminal_wealth ranks by)
        r_cvar = evaluate_strategy_with_simulation(
            name='d', strategy=_strategy(), config=config, rate_path=_rate_path(),
            objective=MAX_CVAR_TERMINAL)
        r_tw = evaluate_strategy_with_simulation(
            name='d', strategy=_strategy(), config=config, rate_path=_rate_path(),
            objective=MAX_TERMINAL_WEALTH)
        self.assertEqual(r_cvar['objective_score'], r_tw['objective_score'])

    def test_declared_but_non_stochastic_model_skips_ensemble(self):
        # A return-model block that is present but FIXED (not a distribution)
        # still has no tail to take -- the helper refuses it, distinctly from
        # the no-block case above.
        from optimize import _risk_ensemble_scores
        config = _make_config(return_model_data={'type': 'fixed', 'mean': 0.07})
        self.assertIsNone(_risk_ensemble_scores(
            config, None, _strategy(), _rate_path(), True, False, 0.0, 0.0,
            MAX_CVAR_TERMINAL, {}))


class TestStochasticEnsemble(unittest.TestCase):
    """With a declared stochastic return model the ensemble runs, is reproducible
    (DP#23), and the score is the CVaR of the collected terminal net worths."""

    STOCH = {'type': 'stochastic', 'mean': 0.07, 'sigma': 0.15}

    def _score(self, objective, paths):
        import optimize
        saved = optimize.RISK_ENSEMBLE_PATHS
        optimize.RISK_ENSEMBLE_PATHS = paths
        try:
            return optimize.evaluate_strategy_with_simulation(
                name='s', strategy=_strategy(), config=_make_config(self.STOCH),
                rate_path=_rate_path(), objective=objective)['objective_score']
        finally:
            optimize.RISK_ENSEMBLE_PATHS = saved

    def test_ensemble_score_equals_cvar_of_collected_paths(self):
        from optimize import _risk_ensemble_scores
        scores = _risk_ensemble_scores(
            _make_config(self.STOCH),
            __import__('countries.canada.adapter', fromlist=['CanadaAdapter'])
                .CanadaAdapter(_make_config(self.STOCH)),
            _strategy(), _rate_path(), True, False, 0.0, 0.0,
            MAX_CVAR_TERMINAL, {})
        self.assertIsNotNone(scores)
        self.assertGreater(len(scores), 1)   # a real distribution
        self.assertEqual(self._score(MAX_CVAR_TERMINAL, len(scores)),
                         _cvar(scores, 0.10))

    def test_ensemble_is_reproducible(self):
        # DP#23: reproducible seeds -> identical score across runs
        self.assertEqual(self._score(MAX_CVAR_TERMINAL, 30),
                         self._score(MAX_CVAR_TERMINAL, 30))

    def test_cvar_score_below_expected_value(self):
        # The behavioural point of the objective: the worst-decile mean sits
        # below the expected terminal wealth, so ranking by CVaR genuinely
        # prices the downside differently from ranking by the mean.
        import statistics
        from optimize import _risk_ensemble_scores
        cfg = _make_config(self.STOCH)
        from countries.canada.adapter import CanadaAdapter
        scores = _risk_ensemble_scores(
            cfg, CanadaAdapter(cfg), _strategy(), _rate_path(), True, False,
            0.0, 0.0, MAX_CVAR_TERMINAL, {})
        self.assertLess(_cvar(scores, 0.10), statistics.mean(scores))


class TestRiskPathsEnv(unittest.TestCase):
    def test_env_resolution(self):
        from optimize import _risk_paths_from_env, _DEFAULT_RISK_ENSEMBLE_PATHS
        self.assertEqual(_risk_paths_from_env(None), _DEFAULT_RISK_ENSEMBLE_PATHS)
        self.assertEqual(_risk_paths_from_env('   '), _DEFAULT_RISK_ENSEMBLE_PATHS)
        self.assertEqual(_risk_paths_from_env('50'), 50)
        self.assertEqual(_risk_paths_from_env('0'), 1)   # floored at one path


class TestListObjectives(unittest.TestCase):
    def test_list_objectives_text_includes_risk_objectives(self):
        from optimize import _list_objectives_text
        text = '\n'.join(_list_objectives_text())
        self.assertIn('max_cvar_terminal', text)
        self.assertIn('max_p10_terminal', text)


if __name__ == '__main__':
    unittest.main()
