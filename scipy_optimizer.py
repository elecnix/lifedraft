#!/usr/bin/env python3
"""
Scipy Optimizer — Continuous optimization via scipy.optimize (DP#26).

Finds optimal continuous decision variables (LTV %, allocation weights,
pension split %) using scipy.optimize.minimize.

The objective function maps a numpy vector → modified SimulationConfig →
fold simulate_year_pure → scalar score.

Boundary constraints enforce legal limits:
- LTV ∈ [0, 0.80]
- Allocation weights ∈ [0, 1]
- Pension split ∈ [0, 0.50]

Usage:
    from scipy_optimizer import ScipyOptimizer
    from objective import MAX_NET_BENEFIT
    
    opt = ScipyOptimizer(base_config)
    result = opt.optimize(objective=MAX_NET_BENEFIT)
    print(f"Optimal LTV: {result.optimal_params['ltv']:.1%}")
"""

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
import math

from optimizer import Optimizer, RankedScenario, RiskMeasures
from simulation import SimulationConfig, YearResult
from charge_limits import ChargeLimitExceededError, MissingRefinanceAmortizationError
from scenario_overlay import apply_ltv_overlay
from simulation_state import SimState, simulate_year_pure
from return_model import ReturnModel, FixedReturn
from objective import ObjectiveFunction, MAX_NET_BENEFIT
from strategy import AllocationStrategy, StrategyEngine
from strategy import list_strategies
_STRATEGIES = list_strategies()
STRATEGY_BALANCED = _STRATEGIES.get('balanced')


def _load_minimize():
    """Load ``scipy.optimize.minimize`` lazily; None when scipy is absent.

    scipy is a dev-only dependency (#765/#1080): the ImportError fallback in
    optimize() is a supported mode, not dead code. Issue #1074: this loader is
    the seam that makes BOTH branches deterministically coverable -- tests
    patch _load_minimize instead of setting ``sys.modules['scipy.optimize'] =
    None`` around engine calls. The sys.modules dance is process-global state
    whose finally-restore an interrupted xdist worker can skip, leaving the
    worker poisoned: every later optimize() call silently takes the fallback
    and the coverage gate flaps on lines 142-145 (0 vs 3 uncovered).
    """
    try:
        from scipy.optimize import minimize
    except ImportError:
        return None
    return minimize


@dataclass
class ScipyResult(RankedScenario):
    """Extended RankedScenario with continuous optimization results."""
    optimal_params: Dict[str, float] = field(default_factory=dict)
    convergence: bool = False
    n_evaluations: int = 0


class ScipyOptimizer(Optimizer):
    """Continuous optimization of financial decision variables.
    
    DP#26: Wraps simulate_year_pure inside f(x) → float for scipy.
    Uses FixedReturn for deterministic convergence (not stochastic).
    
    Decision variables (configurable):
    - ltv: Loan-to-value ratio for refinance [0, 0.80]
    - rrsp_weight: Fraction of savings to RRSP [0, 1]
    - tfsa_weight: Fraction of savings to TFSA [0, 1]
    - pension_split_pct: Fraction of pension to split [0, 0.50]
    """
    
    def __init__(self, base_config: SimulationConfig,
                 return_model: ReturnModel = None,
                 rate_path=None,
                 optimize_vars: List[str] = None):
        super().__init__(base_config, return_model or FixedReturn(base_config.investment_return), rate_path)
        self.optimize_vars = optimize_vars or ['ltv']
    
    def optimize(self, strategies: List[AllocationStrategy] = None,
                 objective: ObjectiveFunction = None,
                 use_readvanceable: bool = False,
                 deduct_later: bool = False,
                 method: str = 'L-BFGS-B',
                 refinance_amortization_years: Optional[int] = None) -> List[RankedScenario]:
        """Find optimal continuous decision variables.

        Args:
            strategies: Strategy for allocation (usually one)
            objective: Objective function
            use_readvanceable: Whether SM is active
            deduct_later: Whether deduct-later is active
            method: scipy.optimize method name
            refinance_amortization_years: issue #655 -- amortization for any
                ltv > current LTV the search explores. Falls back to
                ``base_config.refinance_amortization_years``; with neither
                declared, every ltv > 0 candidate scores -inf (DP#32) rather
                than silently re-amortizing over the incumbent mortgage's
                remaining term.

        Returns:
            List with one ScipyResult containing optimal parameters
        """
        objective = objective or MAX_NET_BENEFIT
        strategy = (strategies[0] if strategies else STRATEGY_BALANCED)

        # Decision variable bounds
        bounds, x0, var_names = self._setup_variables()

        n_evals = [0]

        def neg_objective(x):
            """Negative objective for scipy (we minimize, so negate)."""
            n_evals[0] += 1
            params = dict(zip(var_names, x))
            # DP#18/#619/#664/#655: apply_ltv_overlay is the one authoritative
            # overlay implementation, shared with GridOptimizer --
            # margin_available shrinks by exactly the cash-out booked (#664,
            # mortgage + HELOC share one charge) and the refinance
            # re-amortizes over its own term (#655). An infeasible ltv (over
            # the charge, or with no declared refinance amortization) scores
            # -inf, same as any other simulation failure -- scipy naturally
            # steers away from it rather than the whole search crashing.
            ltv = params.get('ltv', 0)
            try:
                config = apply_ltv_overlay(
                    self.base_config, ltv,
                    refinance_amortization_years=refinance_amortization_years,
                )
                # issue #735: an undrawn facility is the default, not a full
                # year-0 draw (DP#32) -- see GridOptimizer.optimize()'s
                # draw_fraction_options docstring for the same fix, applied
                # there as a swept dimension. This continuous search does
                # not yet expose draw_fraction as its own decision variable
                # (a real, separate follow-up, not silently claimed here);
                # it fixes the same bug at 0.0, the correct default.
                lump_sum = config.margin_available * 0.0 + config.cash_out
                results, _ = self._run_simulation(
                    config, strategy,
                    use_readvanceable=use_readvanceable, deduct_later=deduct_later,
                    lump_sum=lump_sum,
                )
                score = objective.evaluate(results)
            except Exception:
                score = float('-inf')

            return -score  # Negate for minimization
        
        minimize = _load_minimize()
        if minimize is not None:
            result = minimize(neg_objective, x0, method=method, bounds=bounds,
                            options={'maxiter': 50, 'ftol': 1e-6})
            optimal_x = result.x
            convergence = result.success
        else:
            # Fallback: grid search if scipy not available
            optimal_x = x0
            convergence = False
            # Try a few points
            best_score = float('inf')
            for ltv_frac in [0.0, 0.2, 0.4, 0.6, 0.8]:
                trial_x = [ltv_frac]
                score = neg_objective(trial_x)
                if score < best_score:
                    best_score = score
                    optimal_x = trial_x
        
        params = dict(zip(var_names, optimal_x))
        final_score = -neg_objective(optimal_x)
        
        return [ScipyResult(
            scenario_name=f"scipy_opt_{'+'.join(var_names)}",
            score=final_score,
            objective_name=objective.name,
            optimal_params=params,
            convergence=convergence,
            n_evaluations=n_evals[0],
            risk_measures=RiskMeasures(),  # Deterministic
        )]
    
    def _setup_variables(self):
        """Set up bounds and initial values for decision variables."""
        bounds = []
        x0 = []
        names = []
        
        for var in self.optimize_vars:
            if var == 'ltv':
                bounds.append((0.0, 0.80))
                x0.append(0.30)
                names.append('ltv')
            elif var == 'rrsp_weight':
                bounds.append((0.0, 1.0))
                x0.append(0.5)
                names.append('rrsp_weight')
            elif var == 'tfsa_weight':
                bounds.append((0.0, 1.0))
                x0.append(0.3)
                names.append('tfsa_weight')
            elif var == 'pension_split_pct':
                bounds.append((0.0, 0.50))
                x0.append(0.25)
                names.append('pension_split_pct')
            else:
                raise ValueError(f"Unknown optimization variable: {var}")
        
        return bounds, x0, names