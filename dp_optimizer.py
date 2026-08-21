#!/usr/bin/env python3
"""
Dynamic Programming Optimizer — Sequential Decision Optimization (DP#31).

Explores the state tree by forking SimState at each year, appropriate for
sequential decisions like deduct-later timing and drawdown order.

Unlike grid search (which evaluates fixed candidates) and continuous
optimization (which optimizes smooth parameters), DPOptimizer solves sequential
decision problems by:

1. At each year, enumerating the possible ACTIONS (not scenarios)
2. Forking SimState for each action
3. Evaluating each branch forward
4. Choosing the action that maximizes the cumulative objective

Per DP#26: consumes simulate_year_pure and ReturnModel — no changes to
the simulation engine. Per DP#31: the optimizer mode is pluggable data;
the search method and the objective are separate choices.

Per DP#25: no import from other optimizer modes. Per DP#8: strategies and
decisions are data objects, not code.

Example sequential decisions:
- Deduct-later timing: each year, decide whether to claim deferred RRSP
  deductions or carry them forward to a higher-bracket year
- Drawdown order: in retirement, which account to withdraw from first
- Rebalancing thresholds: at what deviation to rebalance
- Refinancing triggers: at what rate differential to break and refinance

Usage:
    from dp_optimizer import DPOptimizer, Decision
    from simulation import SimulationConfig

    config = SimulationConfig.from_json('input.json')
    opt = DPOptimizer(config)
    result = opt.optimize(
        decision_class=Decision(name="deduct_later", decision_type="deduct_later"),
    )
    for step in result[0].decision_path:
        print(f"Year {step.year}: {step.action_name} → score {step.cumulative_score:.0f}")
"""

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple
from copy import deepcopy

from simulation import SimulationConfig, YearResult, simulate_year
from simulation_state import SimState, initial_state_for_run
from return_model import ReturnModel, FixedReturn
from objective import ObjectiveFunction, MAX_NET_BENEFIT
from strategy import AllocationStrategy
from optimizer import Optimizer, RankedScenario, RiskMeasures
from jurisdiction_providers import get_provider

# DP#25: Access jurisdiction providers through registry
_rate_model = get_provider('rate_model')
RatePath = _rate_model['RatePath']
build_rate_path = _rate_model['build_rate_path']
amortization_schedule = _rate_model['amortization_schedule']
annual_summary = _rate_model['annual_summary']


# =============================================================================
# Decision — DP#8: compose through data, not inheritance
# =============================================================================
#
# Issue #717: Decision was an inheritance hierarchy (DeductLaterDecision /
# DrawdownOrderDecision subclasses) dispatched by isinstance. DP#8 says compose
# through data: one dataclass, a string discriminator field (decision_type),
# and the fields each variant needs. The optimizer branches on decision_type
# instead of isinstance(decision, <Subclass>).

# Discriminator values for Decision.decision_type (DP#8: a string, not subclasses).
DECISION_DEDUCT_LATER = "deduct_later"
DECISION_DRAWDOWN_ORDER = "drawdown_order"


@dataclass
class Decision:
    """A sequential decision point (DP#8: a data object, not a class hierarchy).

    decision_type is the discriminator the optimizer dispatches on; the
    remaining fields carry the options each variant needs. A field a given
    decision_type does not read is simply ignored, so the single dataclass
    holds every variant without an inheritance tree.

    Variants:
      - deduct_later: claim carried-forward RRSP deductions now or carry them
        forward. claim_fractions lists the fractions of the undeducted total
        to test (0 = carry all, 1 = claim all).
      - drawdown_order: which account to draw from in retirement. orderings
        lists the drawdown sequences to test. (Reserved: the drawdown handling
        lives in the retirement/decumulation layers this module does not own,
        so this variant runs the baseline year-step; the field is carried so a
        caller can declare it.)
    """
    name: str
    description: str = ""
    decision_type: str = DECISION_DEDUCT_LATER
    # deduct_later variant: fractions of undeducted total to test (0=carry, 1=claim)
    claim_fractions: List[float] = field(default_factory=lambda: [0.0, 0.25, 0.5, 0.75, 1.0])
    # drawdown_order variant: account drawdown sequences to test
    orderings: List[str] = field(default_factory=lambda: [
        "rrsp_first",       # Defer tax, spend RRSP while MTR is lower
        "tfsa_first",        # Tax-free withdrawals, let RRSP compound
        "nonreg_first",      # Trigger capital gains early at lower amounts
        "rrsp_tfsa_nonreg",  # Common sequence
        "tfsa_nonreg_rrsp",  # Preserve RRSP room
    ])


# =============================================================================
# Decision Step — result per year
# =============================================================================

@dataclass
class DecisionStep:
    """One year's decision in the optimal path.

    Records the action taken, the state before/after, and the score.
    """
    year: int
    action_name: str
    action_value: float  # e.g., fraction claimed, or drawdown amount
    score_contribution: float  # This year's contribution to the objective
    cumulative_score: float  # Running total up to this year
    state_before: Optional[SimState] = None
    state_after: Optional[SimState] = None
    result: Optional[YearResult] = None


@dataclass
class DPOptimizeResult:
    """Result of a dynamic programming optimization."""
    strategy_name: str
    decision_class: str
    objective_name: str
    total_score: float
    decision_path: List[DecisionStep] = field(default_factory=list)
    final_state: Optional[SimState] = None
    all_results: List[YearResult] = field(default_factory=list)
    risk_measures: RiskMeasures = field(default_factory=RiskMeasures)


# =============================================================================
# Dynamic Programming Optimizer
# =============================================================================

# DP#25: Default strategy helper using provider registry
def _get_strategies_default() -> AllocationStrategy:
    """Get the first available strategy for default DP optimization.

    Uses the list_strategies registry instead of direct STRATEGIES import.
    Returns the first registered strategy.

    Returns:
        The first registered AllocationStrategy
    """
    from strategy import list_strategies
    strategies = list(list_strategies().values())
    return strategies[0] if strategies else None


class DPOptimizer(Optimizer):
    """Sequential decision optimizer using dynamic programming.

    Per DP#31: this optimizer mode explores the state tree by forking
    SimState at each year. It is appropriate for sequential decisions
    like deduct-later timing or drawdown order.

    Per DP#26: each step calls simulate_year_pure — a pure function.
    Per DP#25: does not import from other optimizer modes.
    Per DP#3: same inputs → same outputs (deterministic for given seed).
    Per DP#23: randomness is controlled via seed on ReturnModel.

    The optimization method:
    1. At each year, enumerate possible actions for the decision class
    2. Fork SimState for each action option
    3. Evaluate each branch forward greedily (or with lookahead)
    4. Choose the action that maximizes the cumulative objective
    5. Record the decision path

    This is greedy optimization, not full Bellman DP. Full DP would
    require state discretization (exponential in state variables).
    Greedy DP with lookahead provides a practical approximation.
    """

    def __init__(self, base_config: SimulationConfig,
                 return_model: ReturnModel = None,
                 rate_path: RatePath = None,
                 lookahead: int = 0):
        """Initialize the DP optimizer.

        Args:
            base_config: Base simulation configuration
            return_model: Investment return model (DP#21: pluggable data)
            rate_path: Mortgage rate path
            lookahead: Number of years to look ahead (0 = greedy)
                       Higher lookahead gives better decisions but is slower.
                       Each lookahead year multiplies the branching factor.
        """
        super().__init__(base_config, return_model, rate_path)
        self.lookahead = lookahead

    def optimize(self, strategies: List[AllocationStrategy] = None,
                 objective: ObjectiveFunction = None,
                 use_readvanceable: bool = False,
                 decision_class: Decision = None) -> List[DPOptimizeResult]:
        """Run DP optimization for each strategy × decision class.

        For each strategy, explores the sequential decision path and
        returns the optimal action at each year.

        Args:
            strategies: Allocation strategies to test
            objective: Objective function (default: MAX_NET_BENEFIT)
            use_readvanceable: Whether to use Smith Manoeuvre
            decision_class: The sequential decision to optimize

        Returns:
            List of DPOptimizeResult, one per strategy
        """
        objective = objective or MAX_NET_BENEFIT
        strategies = strategies or [_get_strategies_default()]
        decision_class = decision_class or Decision(
            name="deduct_later", decision_type=DECISION_DEDUCT_LATER)

        results = []
        for strategy in strategies:
            result = self._optimize_strategy(
                strategy, objective, use_readvanceable, decision_class)
            results.append(result)

        results.sort(key=lambda r: r.total_score, reverse=True)
        return results

    def _optimize_strategy(
        self,
        strategy: AllocationStrategy,
        objective: ObjectiveFunction,
        use_readvanceable: bool,
        decision_class: Decision,
    ) -> DPOptimizeResult:
        """Optimize a single strategy with sequential decisions."""
        config = self.base_config
        # Issue #583: DPOptimizer does not (yet) accept a year-0 lump sum, but
        # it still must open from the SAME constructor FamilySimulation and
        # Optimizer._run_simulation use, so a lump-sum draw feature added here
        # later cannot silently diverge from how the other two engines book
        # it (the #577 bug's exact shape: three call sites, one rule).
        state = initial_state_for_run(config)
        decision_path = []
        all_results = []
        cumulative_score = 0.0

        for year in range(config.projection_years):
            best_step = self._find_best_action(
                state, year, strategy, objective, config,
                use_readvanceable, decision_class, cumulative_score,
            )

            decision_path.append(best_step)
            if best_step.result is not None:
                all_results.append(best_step.result)
            state = best_step.state_after or state
            cumulative_score = best_step.cumulative_score

        return DPOptimizeResult(
            strategy_name=strategy.name,
            decision_class=decision_class.name,
            objective_name=objective.name,
            total_score=cumulative_score,
            decision_path=decision_path,
            final_state=state,
            all_results=all_results,
            risk_measures=RiskMeasures(
                expected_value=cumulative_score,
                n_simulations=0,  # Deterministic
            ),
        )

    def _find_best_action(
        self,
        state: SimState,
        year: int,
        strategy: AllocationStrategy,
        objective: ObjectiveFunction,
        config: SimulationConfig,
        use_readvanceable: bool,
        decision_class: Decision,
        cumulative_score: float,
    ) -> DecisionStep:
        """Find the best action for this year given the current state.

        For decision_type == "deduct_later": tests different fractions of
        undeducted RRSP to claim this year.

        For greedy mode (lookahead=0): just picks the best immediate action.
        For lookahead: evaluates each action's future path.
        """
        if decision_class.decision_type == DECISION_DEDUCT_LATER:
            return self._optimize_deduct_later(
                state, year, strategy, objective, config,
                use_readvanceable, decision_class, cumulative_score,
            )
        else:
            # Default: run with deduct_later=True (baseline). Issue #627:
            # build the same SimulationContext GridOptimizer builds and fold
            # simulate_year -- the pure year-step FamilySimulation folds --
            # instead of a thinner, hand-rolled call to simulate_year_pure.
            ctx = self._build_context(config, strategy, use_readvanceable, True,
                                       0.0, state)
            result, new_state = simulate_year(state, year, ctx)
            score = objective.evaluate([result]) if result else 0
            return DecisionStep(
                year=year,
                action_name="baseline",
                action_value=1.0,
                score_contribution=score,
                cumulative_score=cumulative_score + score,
                state_before=state,
                state_after=new_state,
                result=result,
            )

    def _optimize_deduct_later(
        self,
        state: SimState,
        year: int,
        strategy: AllocationStrategy,
        objective: ObjectiveFunction,
        config: SimulationConfig,
        use_readvanceable: bool,
        decision: Decision,
        cumulative_score: float,
    ) -> DecisionStep:
        """Find the optimal deduct-later fraction for this year.

        Tests each claim fraction from decision.claim_fractions:
        - 0.0 = carry all forward (don't claim any deductions)
        - 1.0 = claim everything now

        Per DP#45: the bracket target in config controls the default;
        we override it by adjusting the deduction amount per fraction.
        """
        best_step = None
        best_score = float('-inf')

        for fraction in decision.claim_fractions:
            # Override the deduct_later_bracket_target to control how much
            # to claim. We simulate with deduct_later=True, then adjust
            # the claimed amount post-hoc, OR we simulate with a modified
            # config that claims `fraction` of undeducted.
            #
            # Simplest approach: test deduct_later=True with a custom
            # bracket target that claims fraction of undeducted.
            total_undeducted = state.rrsp_ledger.undeducted_total() if hasattr(state, 'rrsp_ledger') else 0

            if total_undeducted <= 0 or fraction == 1.0:
                # No undeducted or full claim: just use deduct_later=True
                # with a very low bracket target (claim everything)
                test_config = replace(config,
                    deduct_later_bracket_target=0.0) if fraction == 1.0 else config
                test_deduct_later = True
            elif fraction == 0.0:
                # Carry all forward: simulate without claiming deductions
                test_config = replace(config,
                    deduct_later_bracket_target=float('inf'))
                test_deduct_later = True
            else:
                # Partial claim: set bracket target to claim fraction of undeducted
                # The bracket_target logic claims enough to bring income down to target.
                # To claim fraction × undeducted, we simulate claiming that amount directly.
                # Use a very low bracket target to force immediate claim, then manually
                # limit the deduction to the fraction.
                # For simplicity, we adjust the bracket target to approximate:
                # If primary income is X, we want to deduct fraction × undeducted
                # A bracket target of (X - fraction × undeducted) approximates this
                primary = config.member_by_role('primary', {})  # #699 seam
                primary_income = primary.get('gross_income', 120000) * (1 + config.salary_growth) ** year
                target = primary_income - fraction * total_undeducted
                test_config = replace(config, deduct_later_bracket_target=max(0, target))
                test_deduct_later = True

            try:
                # Issue #627: same SimulationContext/simulate_year seam as
                # the "baseline" branch above and GridOptimizer -- ctx.config
                # is test_config so this candidate's deduct_later_bracket_target
                # override is honoured exactly as before.
                ctx = self._build_context(test_config, strategy, use_readvanceable,
                                           test_deduct_later, 0.0, state)
                result, new_state = simulate_year(state, year, ctx)

                if self.lookahead > 0 and year < config.projection_years - 1:
                    # Evaluate with lookahead: simulate remaining years with
                    # this action's resulting state
                    score = self._evaluate_with_lookahead(
                        new_state, year + 1, strategy, objective, test_config,
                        use_readvanceable, self.lookahead,
                    )
                else:
                    # Greedy: use this year's result
                    all_results = [result] if result else []
                    score = objective.evaluate(all_results) if all_results else 0

            except Exception:
                score = float('-inf')
                result = None
                new_state = state

            if score > best_score:
                best_score = score
                best_step = DecisionStep(
                    year=year,
                    action_name=f"claim_{fraction:.0%}",
                    action_value=fraction,
                    score_contribution=score,
                    cumulative_score=cumulative_score + score,
                    state_before=state,
                    state_after=new_state,
                    result=result,
                )

        return best_step or DecisionStep(
            year=year, action_name="no_action", action_value=0,
            score_contribution=0, cumulative_score=cumulative_score,
        )

    def _evaluate_with_lookahead(
        self,
        state: SimState,
        from_year: int,
        strategy: AllocationStrategy,
        objective: ObjectiveFunction,
        config: SimulationConfig,
        use_readvanceable: bool,
        remaining_lookahead: int,
    ) -> float:
        """Evaluate remaining years from a given state (greedy, no re-branching).

        Used for lookahead: after choosing an action, simulate forward
        to estimate the total future score. This is a greedy evaluation
        (no further branching) to keep computation tractable.
        """
        total_score = 0.0
        current_state = state

        for yr in range(from_year, min(from_year + remaining_lookahead, config.projection_years)):
            ctx = self._build_context(config, strategy, use_readvanceable, True,
                                       0.0, current_state)
            result, current_state = simulate_year(current_state, yr, ctx)
            if result:
                all_results = [result]
                total_score += objective.evaluate(all_results)

        return total_score



__all__ = [
    'DPOptimizer', 'Decision', 'DecisionStep', 'DPOptimizeResult',
]