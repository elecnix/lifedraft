#!/usr/bin/env python3
"""
Monte Carlo Optimizer — Stochastic evaluation with risk measures (DP#29).

Extends GridOptimizer by running N simulations per candidate strategy
using StochasticReturn models with different seeds (DP#23).

Reports:
- Expected value (mean across N runs)
- Probability of loss (P(net_benefit < 0))
- Maximum drawdown across all paths
- Percentiles (P10, P50, P90)

Usage:
    from monte_carlo_optimizer import MonteCarloOptimizer
    from return_model import StochasticReturn
    from objective import MAX_NET_BENEFIT
    
    opt = MonteCarloOptimizer(
        base_config, 
        return_model=StochasticReturn(mean=0.07, sigma=0.15, seed=42),
        n_simulations=500,
        seed_base=42,
    )
    results = opt.optimize(strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT)
    for r in results:
        print(f"{r.scenario_name}: E={r.risk_measures.expected_value:.0f} "
              f"P(loss)={r.risk_measures.probability_of_loss:.1%}")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import statistics

from optimizer import Optimizer, RankedScenario, RiskMeasures
from simulation import SimulationConfig, YearResult
from simulation_state import SimState, simulate_year_pure
from return_model import ReturnModel, StochasticReturn, FixedReturn, build_return_model
from objective import ObjectiveFunction, MAX_NET_BENEFIT
from strategy import AllocationStrategy
from jurisdiction_providers import get_provider

# DP#25: Use provider pattern instead of direct imports from country packages
_RATE_MODEL_PROVIDER = get_provider('rate_model')
RatePath = _RATE_MODEL_PROVIDER['RatePath']
build_rate_path = _RATE_MODEL_PROVIDER['build_rate_path']

def _get_strategies() -> list:
    """Get strategies via provider registry (DP#25)."""
    from strategy import list_strategies
    return list(list_strategies().values())



class MonteCarloOptimizer(Optimizer):
    """Evaluate strategies using Monte Carlo simulation.
    
    DP#29: Reports risk measures alongside expected value.
    DP#23: All random seeds are reproducible.
    DP#30: Risk measures report tax impact of user-specified return
    distribution; they do not predict market scenarios.
    
    Each candidate strategy is evaluated N times with different
    StochasticReturn seeds. The result includes both the expected
    score and risk measures (P(loss), drawdown, percentiles).
    """
    
    def __init__(self, base_config: SimulationConfig,
                 return_model: ReturnModel = None,
                 rate_path: RatePath = None,
                 n_simulations: int = 100,
                 seed_base: int = 42):
        super().__init__(base_config, return_model, rate_path)
        self.n_simulations = n_simulations
        self.seed_base = seed_base
    
    def optimize(self, strategies: List[AllocationStrategy] = None,
                 objective: ObjectiveFunction = None,
                 use_readvanceable_options: List[bool] = None,
                 deduct_later_options: List[bool] = None,
                 search_space: Dict = None) -> List[RankedScenario]:
        """Run Monte Carlo optimization.
        
        For each strategy, run N simulations with different random seeds,
        compute expected score and risk measures, then rank.
        
        Args:
            strategies: List of allocation strategies to test
            objective: Objective function for ranking
            use_readvanceable_options: Smith Manoeuvre on/off options
            deduct_later_options: Deduct-later on/off options
            search_space: DP#31: Strategy search space from discover_anchors().
                When provided, sm_options and deduct_later_options are extracted
                from this dict instead of using hardcoded defaults.
        """
        objective = objective or MAX_NET_BENEFIT
        strategies = strategies or _get_strategies()
        # DP#31: Prefer search_space dimensions over hardcoded defaults
        if search_space is not None:
            use_readvanceable_options = use_readvanceable_options or search_space.get('sm_options', [True, False])
            deduct_later_options = deduct_later_options or search_space.get('deduct_later_options', [True, False])
        else:
            use_readvanceable_options = use_readvanceable_options or [True, False]  # DP#31: hardcoded default
            deduct_later_options = deduct_later_options or [True, False]  # DP#31: hardcoded default
        
        ranked = []
        
        for strategy in strategies:
            for use_readvanceable in use_readvanceable_options:
                for deduct_later in deduct_later_options:
                    name = f"{strategy.name}_sm{int(use_readvanceable)}_dl{int(deduct_later)}_mc"
                    
                    try:
                        scores = []
                        all_results = []
                        
                        for sim_idx in range(self.n_simulations):
                            # DP#23: each simulation gets a unique, reproducible seed
                            sim_seed = self.seed_base + sim_idx
                            sim_return = StochasticReturn(
                                mean=self.return_model.mean if hasattr(self.return_model, 'mean') else 0.07,
                                sigma=self.return_model.sigma if hasattr(self.return_model, 'sigma') else 0.15,
                                seed=sim_seed,
                                n_years=self.base_config.projection_years + 1,
                            )
                            
                            # Override the optimizer's return_model with this path's
                            # stochastic model so _run_simulation uses varying returns
                            # per path (DP#26: fold over simulate_year_pure with
                            # different return_model each path)
                            saved_return_model = self.return_model
                            saved_rate_path = self.rate_path
                            self.return_model = sim_return
                            self._precompute_amortization(self.base_config)
                            
                            try:
                                results, _ = self._run_simulation(
                                    self.base_config, strategy,
                                    use_readvanceable=use_readvanceable, deduct_later=deduct_later,
                                )
                            finally:
                                self.return_model = saved_return_model
                                self.rate_path = saved_rate_path
                                self._precompute_amortization(self.base_config)
                            
                            score = objective.evaluate(results)
                            scores.append(score)
                            all_results.append(results)
                        
                        risk = self._compute_risk_measures(scores, all_results)
                        # Issue #728 (DP#22/DP#29): rank by the objective's own
                        # aggregation of the per-path scores, not unconditionally
                        # by the mean. max_probability_success returns 1.0/0.0
                        # per path and aggregates to P(success); every other
                        # objective has rank_from_distribution=None and
                        # aggregate() falls back to mean (the historical
                        # expected-value ranking).
                        expected_score = objective.aggregate(scores)
                    except Exception:
                        expected_score = float('-inf')
                        risk = RiskMeasures()
                    
                    ranked.append(RankedScenario(
                        scenario_name=name,
                        score=expected_score,
                        objective_name=objective.name,
                        risk_measures=risk,
                    ))
        
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked
    
    def _compute_risk_measures(self, scores: List[float], all_results: List[List['YearResult']] = None) -> RiskMeasures:
        """Compute risk measures from a list of simulation scores.
        
        DP#29: Reports probability of loss, drawdown, and percentiles.
        years_to_recovery: computed from all_results paths (average recovery time
        from trough to peak across all Monte Carlo paths).
        """
        if not scores:
            return RiskMeasures()
        
        n = len(scores)
        sorted_scores = sorted(scores)
        
        # Expected value
        ev = statistics.mean(scores)
        
        # Probability of loss
        n_loss = sum(1 for s in scores if s < 0)
        p_loss = n_loss / n if n > 0 else 0
        
        # Percentiles
        p10_idx = max(0, int(0.1 * n) - 1)
        p50_idx = max(0, int(0.5 * n) - 1)
        p90_idx = max(0, int(0.9 * n) - 1)
        
        # Max drawdown (simplified: worst single-path score relative to best)
        max_dd = 0
        if sorted_scores[-1] > 0:
            worst = sorted_scores[0]
            max_dd = max(0, -worst)  # Only count if worst is negative
        
        # Years to recovery: average recovery time across all paths
        years_to_rec = 0
        if all_results and len(all_results) > 0:
            recovery_times = []
            for results in all_results:
                if len(results) < 2:
                    continue
                net_worths = [r.total_assets - r.total_debt for r in results]
                peak_nw = max(net_worths)
                trough_nw = min(net_worths)
                if trough_nw >= peak_nw:
                    continue
                # Find recovery time from trough to peak
                trough_idx = net_worths.index(trough_nw)
                for rec_idx in range(trough_idx + 1, len(net_worths)):
                    if net_worths[rec_idx] >= peak_nw:
                        recovery_times.append(rec_idx - trough_idx)
                        break
                else:
                    # No full recovery - use years simulated after trough
                    recovery_times.append(len(net_worths) - trough_idx - 1)
            if recovery_times:
                years_to_rec = int(statistics.mean(recovery_times))
        
        return RiskMeasures(
            expected_value=ev,
            probability_of_loss=p_loss,
            max_drawdown=max_dd,
            years_to_recovery=years_to_rec,
            p10=sorted_scores[p10_idx],
            p50=sorted_scores[p50_idx],
            p90=sorted_scores[p90_idx],
            n_simulations=n,
        )