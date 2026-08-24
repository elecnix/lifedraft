#!/usr/bin/env python3
"""
Optimizer Framework - Pluggable optimizer modes (DP#22, DP#25, DP#26).

Per DP#8: optimizer mode is data, not inheritance. The OptimizerMode dataclass
and build_optimizer() factory provide the data+engine entry point.
Per DP#31: the search method and the objective are separate pluggable choices.

Optimizer modes:
- grid: Evaluate all candidates from a fixed grid
- monte_carlo: N stochastic runs per candidate (DP#29 risk measures)
- scipy: Continuous optimization via scipy.optimize (DP#26)
- dp: Sequential decision optimization via dynamic programming

Usage:
    from optimizer import build_optimizer, OptimizerMode
    from objective import MAX_NET_BENEFIT

    mode = OptimizerMode(type="grid")
    opt = build_optimizer(mode, base_config)
    results = opt.optimize(strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT)

Backward compatibility:
    GridOptimizer(config) -> build_optimizer(OptimizerMode(type="grid"), config)
"""

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple
from copy import deepcopy

from charge_limits import (
    ChargeLimitExceededError,
    MissingRefinanceAmortizationError,
    ReadvanceableWithoutPropertyError,
)
from scenario_overlay import apply_ltv_overlay
from simulation_config import SimulationConfig
from year_result import YearResult
# Issue #681: the run-path charge guard, asserted by BOTH folds (this one and
# FamilySimulation.run) -- an invariant wired into only one of them is exactly
# the half-enforcement #681 is about.
from trajectory_invariants import assert_run_invariants, InvariantBreachedError
from simulation_state import SimState, initial_state_for_run
# Issue #627: SimulationContext/simulate_year are the SAME pure year-step
# FamilySimulation folds (simulation.py). Every optimizer mode (Grid, Scipy,
# Monte Carlo via Optimizer._run_simulation; DP via its own call sites) now
# builds a SimulationContext and folds simulate_year over it -- there is no
# second, thinner implementation of "the year step" left to diverge from
# the one simulate.py uses. See Optimizer._build_context().
from simulation import SimulationContext, simulate_year
from return_model import ReturnModel, FixedReturn, build_return_model
from objective import ObjectiveFunction, MAX_NET_BENEFIT
from strategy import AllocationStrategy
from jurisdiction_providers import get_provider
from strategy import list_strategies as _list_strategies

# DP#25: Use provider pattern instead of direct imports from country packages
_RATE_MODEL_PROVIDER = get_provider('rate_model')
RatePath = _RATE_MODEL_PROVIDER['RatePath']
HELOCPath = _RATE_MODEL_PROVIDER['HELOCPath']
build_rate_path = _RATE_MODEL_PROVIDER['build_rate_path']
amortization_schedule = _RATE_MODEL_PROVIDER['amortization_schedule']
annual_summary = _RATE_MODEL_PROVIDER['annual_summary']
from tax_data import TaxDataProvider


# =============================================================================
# OptimizerMode - DP#8: compose through data, not inheritance
# =============================================================================

@dataclass
class OptimizerMode:
    """Optimizer mode configuration (DP#8: data, not inheritance).

    Per DP#31: the search method (grid, Monte Carlo, scipy, DP) is data
    the user provides, not code the optimizer hardcodes.

    Fields are grouped by mode type; unused fields for a given mode are ignored.
    """
    type: str = "grid"  # "grid", "monte_carlo", "scipy", "dp"

    # monte_carlo
    n_simulations: int = 100
    seed_base: int = 42

    # scipy
    optimize_vars: List[str] = field(default_factory=lambda: ["ltv"])
    scipy_method: str = "L-BFGS-B"

    # dp
    lookahead: int = 0

    def to_dict(self) -> dict:
        """Serialize to dict. DP#24: round-trip."""
        d = {"type": self.type}
        if self.type == "monte_carlo":
            d["n_simulations"] = self.n_simulations
            d["seed_base"] = self.seed_base
        elif self.type == "scipy":
            d["optimize_vars"] = self.optimize_vars
            d["method"] = self.scipy_method
        elif self.type == "dp":
            d["lookahead"] = self.lookahead
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'OptimizerMode':
        """Deserialize from dict. DP#24: round-trip."""
        return cls(
            type=data.get("type", "grid"),
            n_simulations=data.get("n_simulations", 100),
            seed_base=data.get("seed_base", 42),
            optimize_vars=data.get("optimize_vars", ["ltv"]),
            scipy_method=data.get("method", "L-BFGS-B"),
            lookahead=data.get("lookahead", 0),
        )


@dataclass
class RiskMeasures:
    """DP#29: Risk measures reported alongside expected value.

    Computed from Monte Carlo paths or deterministic simulation results.
    n_simulations=0 means no risk measurement at all (legacy default).
    n_simulations=1 means deterministic (single-path) risk measures.
    n_simulations>1 means stochastic (Monte Carlo) risk measures.
    """
    expected_value: float = 0.0
    probability_of_loss: float = 0.0  # P(net_benefit < 0)
    max_drawdown: float = 0.0  # Worst peak-to-trough decline in net worth
    worst_yoy_decline: float = 0.0  # Worst year-over-year net worth decline
    years_to_recovery: int = 0  # Years to recover from worst drawdown to peak (0 if never recovered)
    p10: float = 0.0  # 10th percentile net benefit
    p50: float = 0.0  # 50th percentile (median)
    p90: float = 0.0  # 90th percentile
    n_simulations: int = 0  # 0=not measured, 1=deterministic, >1=stochastic

    @property
    def measures_available(self) -> bool:
        """Whether risk measures were computed (vs deterministic = not measured).

        DP#29: When n_simulations=0, risk is not measured - this is different
        from measuring zero risk. Reports should show 'N/A', not '0%'.
        """
        return self.n_simulations > 0


@dataclass
class RankedScenario:
    """A scenario evaluated and ranked by the optimizer.

    DP#22: The optimizer produces a ranked list; the user makes the decision.
    """
    scenario_name: str
    score: float
    objective_name: str
    config_overrides: Dict = field(default_factory=dict)
    final_state: Optional[SimState] = None
    results: List[YearResult] = field(default_factory=list)
    risk_measures: RiskMeasures = field(default_factory=RiskMeasures)
    # Issue #681/#657: WHY this scenario scored -inf. A scenario the engine
    # refused (over the registered charge, no declared refinance
    # amortization, ...) is INFEASIBLE -- structurally impossible, not merely
    # unattractive -- and collapsing it to a bare ``-inf`` puts it at the
    # bottom of the ranked table looking exactly like a bad-but-legal choice
    # nobody would pick. That is a silent failure wearing an exception's
    # clothes. None means "feasible"; a string means the run was refused and
    # says so, in words the report prints (see ``is_infeasible``).
    infeasible_reason: Optional[str] = None

    @property
    def is_infeasible(self) -> bool:
        """Whether the engine REFUSED this scenario (vs. merely scoring it
        poorly). Issue #681/#657: callers that print a ranked table must
        separate these -- an infeasible scenario is not a ranked option."""
        return self.infeasible_reason is not None

    @property
    def is_deterministic(self) -> bool:
        """Whether this scenario uses deterministic (single-path) risk measures.

        DP#29: Deterministic scenarios have n_simulations=1 (single path),
        while Monte Carlo scenarios have n_simulations > 1 (multiple paths).
        The distinction matters for display: deterministic p10/p90 are estimates
        from a single path, not true percentile bounds.
        """
        return self.risk_measures.n_simulations <= 1

    @property
    def risk_summary(self) -> Dict:
        """Summary of risk measures, showing 'N/A' when not measured.

        DP#29: Distinguishes between 'risk not measured' (n=0),
        'deterministic risk measured' (n=1), and 'stochastic risk measured' (n>1).
        """
        rm = self.risk_measures
        if rm.measures_available:
            summary = {
                'probability_of_loss': f'{rm.probability_of_loss:.1%}',
                'max_drawdown': f'${rm.max_drawdown:,.0f}',
                'years_to_recovery': f'{rm.years_to_recovery}y' if rm.years_to_recovery > 0 else 'N/A',
                'p10': f'${rm.p10:,.0f}',
                'p50': f'${rm.p50:,.0f}',
                'p90': f'${rm.p90:,.0f}',
                'n_simulations': rm.n_simulations,
            }
            if rm.n_simulations == 1:
                summary['method'] = 'deterministic'
                summary['note'] = 'Single-path estimates; p10/p90 are approximations'
            return summary
        return {
            'probability_of_loss': 'N/A',
            'max_drawdown': 'N/A',
            'years_to_recovery': 'N/A',
            'p10': 'N/A',
            'p50': 'N/A',
            'p90': 'N/A',
            'n_simulations': 0,
            'note': 'Deterministic scenario - risk not measured',
        }


class Optimizer:
    """Base class for optimizer modes (DP#25: optimization layer).

    Folds ``simulate_year`` (simulation.py) over a ``SimulationContext`` it
    builds itself (``_build_context``) -- the same pure year-step
    ``FamilySimulation`` folds (issue #627).

    Per DP#25/DP#8: prefers explicit ``adapter`` injection; defaults to
    ``CanadaAdapter`` when none is given, exactly as ``FamilySimulation``
    does -- a deliberate act at construction time, not a hard core
    dependency on a jurisdiction package.

    Pre-computes amortization schedule from the rate path so that
    ``simulate_year`` receives proper mortgage data each year.
    """

    def __init__(self, base_config: SimulationConfig,
                 return_model: ReturnModel = None,
                 rate_path: RatePath = None,
                 *,
                 adapter=None):
        self.base_config = base_config
        # Issue #627 item 6: RESP CESG/QESI grants need a RESP calculator and
        # per-child RESP records -- the same ones FamilySimulation builds via
        # its JurisdictionAdapter (simulation.py's resp_calc/resp_children
        # lazy properties). Default to CanadaAdapter, exactly as
        # FamilySimulation.__init__ does, so both engines construct these
        # objects identically (DP#8/DP#25: a deliberate jurisdiction import,
        # not a core dependency).
        if adapter is None:
            from countries.canada.adapter import CanadaAdapter
            adapter = CanadaAdapter(base_config)
        self.adapter = adapter
        self.return_model = return_model or self._build_return_model(base_config)
        self.rate_path = rate_path or build_rate_path(
            "Default", base_config.mortgage_rate, base_config.projection_years,
            'variable', [base_config.mortgage_rate])
        # Issue #627 item 3: build the configured HELOC rate path instead of
        # letting simulate_year_pure fall back to its hardcoded heloc_rate=0.05
        # default.
        #
        # Issue #677: this used to construct HELOCPath directly with no
        # ``fixed_rate`` at all -- despite the comment above claiming it
        # "Mirrors CanadaAdapter.build_heloc_path() / FamilySimulation", it
        # never actually called that method, so it ALWAYS derived the HELOC
        # rate from the mortgage's rate path, 100% of the time, regardless
        # of whether base_config.heloc_rate (#654's canonical
        # property.heloc_rate) was declared. The Optimizer's own
        # ranking/screening path -- what actually prices every scenario the
        # optimizer ranks -- was charging Smith-Manoeuvre borrowing at a
        # mortgage-derived approximation for every household, HELOC-rate
        # declared or not. Delegating to the adapter makes the comment
        # true: the household's own declared HELOC rate wins outright when
        # present (adapter.build_heloc_path's docstring), and only falls
        # back to the mortgage-derived DP#13 placeholder when no HELOC rate
        # was ever declared at all.
        self.heloc_path = self.adapter.build_heloc_path(
            self.rate_path, heloc_rate=base_config.heloc_rate)
        self._tax_provider = TaxDataProvider()
        # Issue #627 item 8 (year-versioning): index future-year bracket
        # thresholds/limits by the configured inflation assumption, same as
        # FamilySimulation.tax_provider (simulation.py). Previously the
        # optimizer path always used TaxDataProvider's default indexation
        # regardless of config.inflation.
        if base_config.inflation is not None:
            self._tax_provider.indexation_rate = base_config.inflation
        # Pre-compute amortization schedule for the base config's mortgage
        self._precompute_amortization(base_config)

    @staticmethod
    def _build_return_model(config: 'SimulationConfig') -> 'ReturnModel':
        """DP#21: Build return model from return_model_data, falling back to investment_return."""
        from return_model import build_return_model_from_config, FixedReturn
        if config.return_model_data:
            return build_return_model_from_config(config.return_model_data)
        return FixedReturn(config.investment_return)

    def optimize(self, strategies: List[AllocationStrategy] = None,
                 objective: ObjectiveFunction = None) -> List[RankedScenario]:
        """Run optimization and return ranked results."""
        raise NotImplementedError

    @staticmethod
    def compute_deterministic_risk(results: List[YearResult],
                                    score: float) -> RiskMeasures:
        """Compute risk measures from deterministic simulation results (DP#29).

        For deterministic grid search, we can't compute probability-of-loss
        from a single path, but we CAN compute:
        - max_drawdown: worst year-over-year decline in net worth
        - worst_year_return: the single worst annual investment return
        - years_negative: how many years net benefit is negative

        Args:
            results: List of YearResult from a single simulation path
            score: The overall net benefit score for the scenario

        Returns:
            RiskMeasures with n_simulations=1 (deterministic single-path)
        """
        if not results:
            return RiskMeasures(
                expected_value=score,
                n_simulations=1,
                max_drawdown=0.0,
                years_to_recovery=0,
            )

        # Compute total net worth per year (assets - debt)
        net_worths = []
        for r in results:
            net_worth = r.total_assets - r.total_debt
            net_worths.append(net_worth)

        # Max drawdown: largest peak-to-trough decline
        max_drawdown = 0.0
        peak = net_worths[0] if net_worths else 0.0
        for nw in net_worths:
            if nw > peak:
                peak = nw
            drawdown = peak - nw
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # Probability of loss: for deterministic, it's 1 if score < 0, else 0
        prob_loss = 1.0 if score < 0 else 0.0

        # Worst year-over-year decline in net worth
        worst_yoy_decline = 0.0
        for i in range(1, len(net_worths)):
            decline = net_worths[i-1] - net_worths[i]
            if decline > worst_yoy_decline:
                worst_yoy_decline = decline

        # Years to recovery: years from trough (lowest after peak) back to peak level
        years_to_rec = 0
        if max_drawdown > 0 and len(net_worths) >= 2:
            # Find the global peak (highest net worth in path)
            peak_nw = max(net_worths)
            peak_idx = net_worths.index(peak_nw)

            # Find the trough (lowest point after the peak that caused max drawdown)
            # For max drawdown, we track when it occurred
            peak_for_dd = net_worths[0] if net_worths else 0.0
            trough_idx = 0
            for i in range(len(net_worths)):
                if net_worths[i] > peak_for_dd:
                    peak_for_dd = net_worths[i]
                if peak_for_dd - net_worths[i] == max_drawdown:
                    trough_idx = i
                    break

            # Compute recovery time from trough to peak_nw
            if trough_idx < len(net_worths) - 1:
                recovered = False
                for rec_idx in range(trough_idx + 1, len(net_worths)):
                    years_to_rec += 1
                    if net_worths[rec_idx] >= peak_nw:
                        recovered = True
                        break
                # If not recovered by end of simulation, the recovery is incomplete
                # years_to_rec represents years simulated after trough, not full recovery

        # Percentile approximation from single path
        # Use the score as expected_value, compute rough p10/p50/p90
        # For deterministic, p50 ≈ expected_value
        return RiskMeasures(
            expected_value=score,
            probability_of_loss=prob_loss,
            max_drawdown=max_drawdown,
            years_to_recovery=years_to_rec,
            p10=score - worst_yoy_decline,  # Conservative estimate
            p50=score,                       # Median ≈ single path
            p90=score,                       # Upper bound ≈ single path
            n_simulations=1,
            # Store worst YoY decline for diagnostic purposes
        )

    def _build_context(self, config: SimulationConfig, strategy: AllocationStrategy,
                        use_readvanceable: bool, deduct_later: bool,
                        lump_sum: float, state: SimState,
                        deployment_lag_months: int = 0,
                        deployment_idle_rate: float = 0.0,
                        deployment_schedule_years: int = 1) -> SimulationContext:
        """Build the SimulationContext simulate_year needs (issue #627).

        This is the ONE constructor for "the year step's static inputs" on
        the optimizer path, mirroring ``FamilySimulation._build_context()``
        (simulation.py) field-for-field. Every optimizer mode reaches
        ``simulate_year`` through here (Grid/Scipy/Monte-Carlo via
        ``_run_simulation`` below; DP via its own call sites in
        dp_optimizer.py) -- so a rule wired into ``simulate_year`` is wired
        into every optimizer mode simultaneously. There is no second,
        thinner allocation/marginal-rate/mortgage-data implementation left
        to silently omit retirement, RESP grants, FHSA, portfolio
        composition, cash flows or year-versioned limits (#625/#627 items
        1/2/5/6/7/8) -- those all live inside ``simulate_year`` itself, and
        this method's only job is to gather the per-call static context,
        exactly as ``FamilySimulation._build_context()`` gathers it from
        ``self``.

        ``state`` supplies ``has_fhsa`` (read once, at context-build time --
        matching ``FamilySimulation``, whose ``self._state`` is likewise
        only refreshed once per ``run()`` call, not per year).
        """
        primary = config.member_by_role('primary', {})  # #699 seam
        spouse = config.member_by_role('spouse', {})
        primary_income = primary.get('gross_income', 0)
        spouse_income = spouse.get('gross_income', 0)

        from simulation_state import adult_fhsa_active  # #700/#643: per-adult FHSA store
        canada = state.jurisdiction_state.get('canada', {})
        has_fhsa = adult_fhsa_active(canada)

        # Issue #627 item 2 (DP#27): build the configured portfolio
        # composition instead of never constructing it. Previously
        # non-reg/SM-investment growth on the optimizer path always fell
        # back to a flat rate because nothing ever built self._portfolio.
        portfolio = None
        if config.portfolio_data:
            try:
                from countries.canada.portfolio import PortfolioConfig
                portfolio = PortfolioConfig.from_dict(config.portfolio_data)
            except ImportError:
                pass

        # Issue #627 item 6: RESP CESG/QESI grants. Build the same
        # RESPCalculator/RESPChild objects FamilySimulation builds --
        # through self.adapter, exactly like FamilySimulation.resp_calc/
        # resp_children (simulation.py) -- not a provider-registry lookup.
        resp_calc = self.adapter.create_resp_calculator()
        resp_children = []
        for ch in config.children:
            child_birth_year = ch.get(
                'birth_year',
                config.start_year - ch.get('age', 0) if ch.get('age', 0) > 0 else 0)
            resp_children.append(self.adapter.create_resp_child(
                name=ch.get('name', 'Child'),
                birth_year=child_birth_year,
                province=ch.get('province', config.province),
                resp_balance=(config.resp_current_balance / len(config.children)
                              if config.children else 0),
            ))

        return SimulationContext(
            config=config,
            strategy=strategy,
            rate_path=self.rate_path,
            heloc_path=self.heloc_path,
            use_readvanceable=use_readvanceable,
            deduct_later=deduct_later,
            lump_sum=lump_sum,
            # Issue #914: this optimizer path threads a borrowed lump_sum but no
            # non-borrowed free cash -- RESP-collapse proceeds reach the engine
            # via simulate.evaluate_overlay's free_cash= kwarg (the FamilySimula-
            # tion path), which this grid/scipy optimizer does not use. 0.0 is
            # its exact prior behaviour (it never invested free cash), not a new
            # dead read (DP#32: explicit absence, no free cash to consume here).
            free_cash=0.0,
            return_model=self.return_model,
            tax_provider=self._tax_provider,
            amort_annual=self._amort_annual,
            amort=self._amort,
            resp_calc=resp_calc,
            resp_children=resp_children,
            has_fhsa=has_fhsa,
            frozen_brackets=config.frozen_brackets,
            brackets=self._tax_provider.get_combined_brackets(),
            start_year=config.start_year,
            primary_income=primary_income,
            deployment_lag_months=deployment_lag_months,
            deployment_idle_rate=deployment_idle_rate,
            deployment_schedule_years=deployment_schedule_years,
            spouse_income=spouse_income,
            portfolio=portfolio,
        )

    def _run_simulation(self, config: SimulationConfig,
                        strategy: AllocationStrategy,
                        use_readvanceable: bool = False,
                        deduct_later: bool = False,
                        lump_sum: float = 0.0,
                        deployment_lag_months: int = 0,
                        deployment_idle_rate: float = 0.0,
                        deployment_schedule_years: int = 1) -> Tuple[List[YearResult], SimState]:
        """Run a full simulation by folding ``simulate_year`` (DP#26/#627).

        Issue #627: this used to be a second, thinner implementation of "the
        year step" -- it re-derived allocations/marginal rates/mortgage data
        by hand and called ``simulate_year_pure`` with a much smaller
        argument set than ``FamilySimulation`` used, silently omitting
        retirement, RESP grants, FHSA, portfolio composition, cash flows,
        year-versioned limits and the configured HELOC rate. Now it builds
        the same ``SimulationContext`` and folds the same ``simulate_year``
        function ``FamilySimulation.run()`` folds -- there is exactly one
        implementation of "the year step" left, and both engines fold it
        the same way (only the traversal over candidate configs differs,
        per DP#26's own framing of what an "optimizer mode" is).

        If config differs from base_config (e.g. different mortgage_balance
        from LTV overlay), recompute amortization for the modified mortgage.
        """
        # Recompute amortization if mortgage balance changed (LTV overlay)
        if config.mortgage_balance != self.base_config.mortgage_balance:
            self._precompute_amortization(config)

        # Issue #583: the opening state (including any year-0 margin draw,
        # #577) is built by the single shared constructor
        # ``initial_state_for_run`` -- the same one FamilySimulation.__init__
        # and DPOptimizer use -- so this engine cannot drift from theirs on
        # what "the opening state" is.
        state = initial_state_for_run(config, lump_sum)
        ctx = self._build_context(config, strategy, use_readvanceable, deduct_later,
                                   lump_sum, state,
                                   deployment_lag_months=deployment_lag_months,
                                   deployment_idle_rate=deployment_idle_rate,
                                   deployment_schedule_years=deployment_schedule_years)

        results = []
        for year in range(config.projection_years):
            result, state = simulate_year(state, year, ctx)
            results.append(result)

        # Restore base amortization after modified-config runs
        if config.mortgage_balance != self.base_config.mortgage_balance:
            self._precompute_amortization(self.base_config)

        # Issue #681: the same charge guard FamilySimulation.run() applies.
        # Both engines fold the same year step (#627), so both must enforce
        # the same structural invariants on the trajectory that comes out --
        # otherwise the optimizer, the one path that actually RANKS
        # scenarios, is the one that skips the check.
        assert_run_invariants(results, config)

        return results, state

    def _precompute_amortization(self, config: SimulationConfig) -> None:
        """Pre-compute amortization schedule for the given config.

        Called on init and whenever LTV/mortgage changes (different config).
        Stores annual summaries keyed by (mortgage_balance, rate_path_name)
        so the optimizer can reuse schedules across similar configs.
        """
        amort = amortization_schedule(
            principal=config.mortgage_balance,
            rate_path=self.rate_path,
            amortization_years=config.amortization_years,
            projection_months=config.projection_years * 12,
            readvance_smith=False,  # readvance handled by simulate_year_pure
        )
        self._amort = amort
        self._amort_annual = annual_summary(amort)


class GridOptimizer(Optimizer):
    """Evaluate all candidates from a fixed grid.

    Extended grid dimensions:
    - LTV level (how much to refinance)
    - Income scenario (current job vs new job)
    - Strategy allocations (balanced, readvanceable priority, RRSP max, no readvance)
    - Readvanceable mortgage strategy on/off
    - Deduct-later on/off

    Each combination is evaluated and ranked by the objective function.

    DP#18: Scenarios compose from a base; LTV overlays modify mortgage_balance
       and margin_available, they don't replace the base config.
    """

    def optimize(self, strategies: List[AllocationStrategy] = None,
                 objective: ObjectiveFunction = None,
                 use_readvanceable_options: List[bool] = None,
                 deduct_later_options: List[bool] = None,
                 ltv_levels: List[float] = None,
                 income_overrides: List[Dict] = None,
                 search_space: Dict = None,
                 refinance_amortization_years: Optional[int] = None,
                 draw_fraction_options: List[float] = None,
                 deployment_lag_options: List[int] = None,
                 deployment_idle_rate: float = 0.0,
                 deployment_schedule_options: List[int] = None) -> List[RankedScenario]:
        """Evaluate a grid of scenario combinations and rank by objective.

        Args:
            strategies: List of allocation strategies to test
            objective: Objective function for ranking (default: MAX_NET_BENEFIT)
            use_readvanceable_options: readvanceable mortgage strategy on/off options
            deduct_later_options: Deduct-later on/off options
            ltv_levels: LTV ratios to explore (e.g. [0, 0.30, 0.50, 0.65, 0.80])
            draw_fraction_options: issue #735 -- what fraction of
                config.margin_available is drawn and invested at year 0, per
                scenario (e.g. [0.0, 0.25, 0.5, 1.0]). DP#13/DP#32: defaults
                to ``[0.0]`` -- undrawn -- NOT to a sweep and NOT to the old
                unconditional-full-draw behaviour; a caller that wants the
                trade-off explored must ask for it (via this parameter or
                ``search_space['draw_fraction_options']``), exactly like
                every other search dimension here. This keeps every existing
                caller that never asks for it on the DP#32-correct "declared
                facilities are not drawn unless something draws them"
                behaviour, rather than silently reintroducing the #735 bug
                for callers that opt into this new dimension without
                opting into the correctness fix along with it.
            income_overrides: List of dicts overriding primary/spouse gross_income
                e.g. [{'label': 'current', 'primary': 160000, 'spouse': 70000}, ...].
                REQUIRED, no silent default (DP#13/DP#32, issue #665): a
                caller that means "base income only" must pass ``[None]``
                explicitly. This parameter used to default to ``[None]`` via
                ``income_overrides or [None]`` -- every income scenario a
                household declared in ``decisions.income[]`` was silently
                discarded because the one production caller that should have
                populated this from the contract never did, and the default
                made the omission invisible (no error, no warning, a
                confidently-ranked table that answered a different question
                than the one asked). See ``optimize.py``'s
                ``run_income_scenario_exploration`` for the real caller.
            search_space: DP#31: Strategy search space from discover_anchors().
                When provided, sm_options and deduct_later_options are extracted
                from this dict instead of using hardcoded defaults.
            refinance_amortization_years: issue #655 -- the amortization any
                ltv_levels > current LTV refinance to. Passed through to
                apply_ltv_overlay for every ltv level; falls back to
                ``self.base_config.refinance_amortization_years`` when None
                (apply_ltv_overlay's own fallback). A household with neither
                declared has every LTV > 0 scenario reported infeasible
                (DP#32) rather than silently re-amortized over the incumbent
                mortgage's remaining term.
            deployment_schedule_options: issue #74 -- deploy-over-N-year
                schedules (e.g. [1, 2, 3, 4]) to rank for the year-0 lump's
                deployment, as SEARCH DIMENSION (DP#22/#31), same convention
                as deployment_lag_options: defaults to ``[1]`` (all at year
                0); a staggered ranking must be asked for.

        Returns:
            List of RankedScenario, sorted by score (best first)

        Raises:
            ValueError: if income_overrides is not provided. Absence must
                fail loudly (DP#32), never fall back to a plausible default.
        """
        if income_overrides is None:
            raise ValueError(
                "GridOptimizer.optimize() requires income_overrides to be "
                "passed explicitly (DP#13/DP#32, issue #665). Pass [None] "
                "to run the base config's income only (an explicit choice), "
                "or a list of income scenario override dicts (e.g. "
                "[{'label': 'current', 'primary': 160000}, "
                "{'label': 'job loss', 'primary': 0}]). A silent "
                "`income_overrides or [None]` default here previously "
                "discarded every income scenario a household declared "
                "without any warning."
            )
        objective = objective or MAX_NET_BENEFIT
        strategies = strategies or list(_list_strategies().values())
        # DP#31: Prefer search_space dimensions over hardcoded defaults
        if search_space is not None:
            use_readvanceable_options = use_readvanceable_options or search_space.get('sm_options', [True, False])
            deduct_later_options = deduct_later_options or search_space.get('deduct_later_options', [True, False])
            # Issue #735: [0.0] (undrawn), NOT [True, False]'s "sweep both
            # ways" pattern -- see this parameter's docstring for why the
            # default must be the single correct value, not a sweep.
            draw_fraction_options = draw_fraction_options or search_space.get('draw_fraction_options', [0.0])
            # Issue #137: deployment lag is another SEARCH DIMENSION — same
            # rationale as draw_fraction (DP#22/#31): how long borrowed money
            # sits uninvested is a decision the optimizer ranks by pricing the
            # idle-carry cost, not a fact the household declares. Default [0]
            # (deploy at year 0 — today's behavior; a lag must be asked for).
            deployment_lag_options = deployment_lag_options or [0,]
            # Issue #74: staggered deployment is the SPREAD sibling of the lag
            # dimension — deploy-over-N-years schedules the optimizer ranks
            # (DP#22/#31). Default [1]: all at year 0; a schedule must be asked
            # for, exactly like every other dimension here.
            deployment_schedule_options = deployment_schedule_options or [1]
        else:
            use_readvanceable_options = use_readvanceable_options or [True, False]  # DP#31: hardcoded default
            deduct_later_options = deduct_later_options or [True, False]  # DP#31: hardcoded default
            draw_fraction_options = draw_fraction_options or [0.0]  # Issue #735: undrawn by default (DP#32)
        # Issue #137: same convention absent/zero lag is the DP#32 default.
        deployment_lag_options = deployment_lag_options or [0,]
        # Issue #74: same convention for the schedule — absent means year-0.
        deployment_schedule_options = deployment_schedule_options or [1]
        ltv_levels = ltv_levels or [0.0]  # Default: no refinance exploration
        # Issue #137/#74: expand the draw-fraction × deployment-lag ×
        # deployment-schedule grid so the inner loop body needs no re-indent.
        _draw_lag_pairs = [
            (df, lag, sched)
            for df in draw_fraction_options
            for lag in deployment_lag_options
            for sched in deployment_schedule_options
        ]

        ranked = []

        for income_ov in income_overrides:
            # Apply income override if provided
            if income_ov is not None:
                base_for_income = self._apply_income_override(self.base_config, income_ov)
                income_label = income_ov.get('label', f"n{income_ov.get('primary', '?')}")
            else:
                base_for_income = self.base_config
                income_label = 'base'

            for ltv in ltv_levels:
                # DP#18/#619/#664/#655: compose from base, overlay LTV through
                # the one authoritative rule (scenario_overlay.apply_ltv_overlay)
                # -- margin_available is reduced by exactly the cash-out it
                # books (mortgage + HELOC share ONE registered charge, #664)
                # and the refinance re-amortizes over its own declared term
                # (#655). A target LTV this household's charge cannot support,
                # or that has no declared refinance amortization, raises
                # rather than silently overstating borrowing capacity; every
                # scenario at this LTV is then reported infeasible
                # (score=-inf, same as any other simulation failure) instead
                # of crashing the whole optimize() run.
                overlay_error = None
                try:
                    config = apply_ltv_overlay(
                        base_for_income, ltv,
                        refinance_amortization_years=refinance_amortization_years,
                    )
                    cash_out = config.cash_out
                except (ChargeLimitExceededError, MissingRefinanceAmortizationError) as exc:
                    config = base_for_income
                    cash_out = 0.0
                    overlay_error = exc

                for strategy in strategies:
                    for use_readvanceable in use_readvanceable_options:
                        for deduct_later in deduct_later_options:
                            for draw_fraction, deployment_lag, deployment_schedule in _draw_lag_pairs:
                                parts = [strategy.name, f"sm{int(use_readvanceable)}", f"dl{int(deduct_later)}"]
                                if ltv > 0:
                                    parts.append(f"ltv{ltv:.0%}")
                                # Issue #735: only clutters the name when the
                                # caller actually asked for more than one
                                # draw-fraction candidate -- the common
                                # (default [0.0], single-value) case reads
                                # exactly as it did before this dimension
                                # existed.
                                if len(draw_fraction_options) > 1:
                                    parts.append(f"df{draw_fraction:.0%}")
                                # Issue #137: only clutter the name for a lag
                                # when more than one candidate was swept.
                                if len(deployment_lag_options) > 1:
                                    parts.append(f"lag{deployment_lag}m")
                                # Issue #74: same rule for the schedule.
                                if len(deployment_schedule_options) > 1:
                                    parts.append(f"dca{deployment_schedule}y")
                                if income_ov is not None:
                                    parts.append(income_label)
                                name = "_".join(parts)

                                # Issue #681/#657: an INFEASIBLE scenario (one the
                                # engine DELIBERATELY refused -- over the registered
                                # charge, no declared refinance amortization, an
                                # invariant breach) is recorded with the reason IN
                                # WORDS, not collapsed to a bare -inf that reads like
                                # a merely-unattractive option at the bottom of the
                                # table. That catch is DELIBERATELY NARROW: only these
                                # typed, expected exceptions mean "infeasible".
                                #
                                # Issue #657: every OTHER exception -- a KeyError from
                                # a mis-mapped config, a ZeroDivisionError, an
                                # assertion in a rule -- is a BUG, not a data point,
                                # and MUST propagate (fail loud, DP#32). A bare
                                # ``except Exception: score = -inf`` here would rank a
                                # crashing strategy last, indistinguishable from one
                                # evaluated and found bad, so a bug that breaks the
                                # strategies that would otherwise WIN leaves the
                                # optimizer confidently recommending the runner-up
                                # with nothing in the output saying anything failed.
                                # We therefore do NOT catch it -- the crash surfaces.
                                infeasible_reason = None
                                try:
                                    if overlay_error is not None:
                                        raise overlay_error
                                    # Issue #735: margin_available is undrawn
                                    # room, not a balance -- only
                                    # draw_fraction of it is actually drawn
                                    # and invested at year 0. cash_out is a
                                    # SEPARATE, always-fully-realized mortgage
                                    # increase (#257) and is never scaled by
                                    # this fraction.
                                    results, final_state = self._run_simulation(
                                        config, strategy,
                                        use_readvanceable=use_readvanceable, deduct_later=deduct_later,
                                        lump_sum=config.margin_available * draw_fraction + config.cash_out,
                                        # Issues #137/#74: thread the deployment-
                                        # timing search dimensions (defaults =
                                        # today's year-0 behavior).
                                        deployment_lag_months=deployment_lag,
                                        deployment_idle_rate=deployment_idle_rate,
                                        deployment_schedule_years=deployment_schedule,
                                    )
                                    score = objective.evaluate(results)
                                    # DP#29: Compute risk measures from deterministic path
                                    risk_measures = self.compute_deterministic_risk(results, score)
                                except (ChargeLimitExceededError, MissingRefinanceAmortizationError,
                                        InvariantBreachedError,
                                        ReadvanceableWithoutPropertyError) as exc:
                                    score = float('-inf')
                                    results = []
                                    final_state = None
                                    risk_measures = RiskMeasures()
                                    infeasible_reason = f"{type(exc).__name__}: {exc}"

                                ranked.append(RankedScenario(
                                    scenario_name=name,
                                    score=score,
                                    objective_name=objective.name,
                                    infeasible_reason=infeasible_reason,
                                    config_overrides={
                                        'strategy': strategy.name,
                                        'use_readvanceable': use_readvanceable,
                                        'deduct_later': deduct_later,
                                        'ltv': ltv,
                                        'cash_out': cash_out,
                                        'draw_fraction': draw_fraction,
                                        'deployment_lag_months': deployment_lag,
                                        'deployment_schedule_years': deployment_schedule,
                                        'income_label': income_label,
                                    },
                                    final_state=final_state,
                                    results=results,
                                    risk_measures=risk_measures,
                                ))

        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked

    def _apply_income_override(self, config: SimulationConfig, override: Dict) -> SimulationConfig:
        """Apply an income override to family members (DP#18).

        override: {'primary': 220000, 'spouse': 70000, 'label': 'new-job'}
        """
        members = deepcopy(config.family_members)
        for m in members:
            if m.get('role') == 'primary' and 'primary' in override:
                m['gross_income'] = override['primary']
            if m.get('role') == 'spouse' and 'spouse' in override:
                m['gross_income'] = override['spouse']
        return replace(config, family_members=members)

# =============================================================================
# build_optimizer - DP#8: factory dispatch from mode data
# =============================================================================

def build_optimizer(mode: OptimizerMode = None,
                    base_config: SimulationConfig = None,
                    return_model: ReturnModel = None,
                    rate_path: RatePath = None) -> 'Optimizer':
    """Factory function to build an optimizer from mode data (DP#8).

    Per DP#31: the optimizer mode is data, not a class choice.
    This factory dispatches to the appropriate subclass based on mode.type.

    Args:
        mode: OptimizerMode data (type, n_simulations, etc.)
        base_config: Base simulation configuration
        return_model: Investment return model
        rate_path: Mortgage rate path

    Returns:
        Optimizer instance (GridOptimizer, MonteCarloOptimizer, etc.)
    """
    mode = mode or OptimizerMode()

    if mode.type == "grid":
        return GridOptimizer(base_config, return_model=return_model, rate_path=rate_path)
    elif mode.type == "monte_carlo":
        from monte_carlo_optimizer import MonteCarloOptimizer
        return MonteCarloOptimizer(
            base_config, return_model=return_model, rate_path=rate_path,
            n_simulations=mode.n_simulations, seed_base=mode.seed_base,
        )
    elif mode.type == "scipy":
        from scipy_optimizer import ScipyOptimizer
        return ScipyOptimizer(
            base_config, return_model=return_model, rate_path=rate_path,
            optimize_vars=mode.optimize_vars,
        )
    elif mode.type == "dp":
        from dp_optimizer import DPOptimizer
        return DPOptimizer(
            base_config, return_model=return_model, rate_path=rate_path,
            lookahead=mode.lookahead,
        )
    else:
        raise ValueError(f"Unknown optimizer mode: {mode.type!r}")
