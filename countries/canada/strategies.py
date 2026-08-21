#!/usr/bin/env python3
"""Canadian Allocation Strategies

Pre-defined strategy constants and discovery logic for Canadian
registered-account allocation (RRSP, TFSA, FHSA, RESP, non-reg).

These strategies encode common Canadian financial planning approaches:

- **Balanced**: Even split across account types
- **RRSP Max**: Prioritize RRSP deductions, spousal splitting, deduct-later
- **Readvance Priority**: Readvanceable mortgage + Smith manoeuvre
- **No Readvance**: Baseline without readvancing

Strategy names ``smith_priority`` and ``no_sm`` are deprecated aliases
for ``readvance_priority`` and ``no_readvance`` respectively.

Usage::

    from countries.canada.strategies import (
        STRATEGY_BALANCED, STRATEGY_RRSP_MAX,
        STRATEGY_READVANCE_PRIORITY, STRATEGY_NO_READVANCE,
        STRATEGIES, discover_strategies,
    )

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — RRSP/TFSA/FHSA entries
"""


from dataclasses import replace

from return_model import ReturnModel
from strategy import (
    AllocationStrategy,
    FamilyState,
    StrategyType,
)

# Issue #1000: FHSA allocation shares the optimizer's strategy search sweeps
# when the household has declared FHSA room. Each share is lifted off zero and
# rebalanced out of the strategy's non-reg residual (see discover_strategies),
# so the total allocation still sums to ~1.0. These are SEARCH points, not
# opinions: the optimizer ranks the variants alongside their fhsa_pct=0
# parents and the objective decides (DP#22). A household with no declared
# room never reaches this list (the gate in discover_strategies closes).
# 5% and 10% span "modest first-home tilt" to "aggressive FHSA priority" without
# exploding the grid (2 variants per discovered strategy).
_FHSA_SEARCH_PCTS = (0.05, 0.10)

# =============================================================================
# Pre-defined Strategies
# =============================================================================

STRATEGY_BALANCED = AllocationStrategy(
    name="Balanced",
    strategy_type=StrategyType.BALANCED,
    rrsp_pct=0.30, spousal_rrsp_pct=0.10, tfsa_pct=0.30,
    resp_pct=0.07, non_reg_pct=0.23,
    prioritize_readvanceable=False, deduct_later=False,
    spousal_splitting=True,
)

STRATEGY_RRSP_MAX = AllocationStrategy(
    name="Max RRSP + Spousal",
    strategy_type=StrategyType.RRSP_MAX,
    rrsp_pct=0.35, spousal_rrsp_pct=0.15, tfsa_pct=0.25,
    resp_pct=0.07, non_reg_pct=0.18,
    prioritize_readvanceable=False, deduct_later=True,
    spousal_splitting=True,
)

# Readvance priority: discovered when readvancing conditions hold
STRATEGY_READVANCE_PRIORITY = AllocationStrategy(
    name="Readvanceable Mortgage Priority",
    strategy_type=StrategyType.READVANCE_PRIORITY,
    rrsp_pct=0.15, spousal_rrsp_pct=0.05, tfsa_pct=0.20,
    resp_pct=0.05, non_reg_pct=0.55,
    prioritize_readvanceable=True, deduct_later=True,
    spousal_splitting=True,
)

# Baseline: no readvancing
STRATEGY_NO_READVANCE = AllocationStrategy(
    name="No Readvancing (Baseline)",
    strategy_type=StrategyType.NO_READVANCE,
    rrsp_pct=0.35, spousal_rrsp_pct=0.10, tfsa_pct=0.30,
    resp_pct=0.07, non_reg_pct=0.18,
    prioritize_readvanceable=False, deduct_later=False,
    spousal_splitting=True,
)



STRATEGIES = {
    'balanced': STRATEGY_BALANCED,
    'readvance_priority': STRATEGY_READVANCE_PRIORITY,
    'rrsp_max': STRATEGY_RRSP_MAX,
    'no_readvance': STRATEGY_NO_READVANCE,
    'smith_priority': STRATEGY_READVANCE_PRIORITY,
    'no_sm': STRATEGY_NO_READVANCE,
}


def discover_strategies(state: FamilyState,
                        mortgage_cfg: dict = None,
                        return_model: ReturnModel = None,
                        investment_return: float = None,
                        heloc_rate: float = None,
                        capital_gains_inclusion: float = 0.50,
                        custom_strategies: dict = None) -> dict:
    """Discover which strategies are applicable from rules, not names.

    DP#6: Strategies are discovered from conditions, not named by convention.
    The readvanceable mortgage priority strategy is discovered when
    three conditions hold:
    1. Mortgage is readvanceable (heloc_readvance=True in config)
    2. Investment interest on borrowed funds is tax-deductible (CRA §20(1)(c))
    3. After-tax investment return exceeds after-tax HELOC interest cost

    DP#2: Custom strategies can be provided via custom_strategies to
    override the default strategy allocations. Each custom strategy is
    an AllocationStrategy or a dict that will be passed to
    AllocationStrategy.from_dict().

    Financial assumptions (investment_return, heloc_rate) must be provided
    explicitly — either via ``return_model`` (which supplies
    investment_return from ``return_model.return_for_year(0)``) or as
    explicit float values. Hardcoded defaults (0.07 / 0.05) were removed
    per issue #23 to prevent embedding financial opinions in the API.

    Args:
        state: Current family financial state
        mortgage_cfg: Mortgage config dict with 'heloc_readvance' key
        return_model: ReturnModel to derive investment_return from;
            takes precedence over explicit investment_return when both given
        investment_return: Expected investment return rate (required if
            return_model not provided)
        heloc_rate: HELOC interest rate (required, no default)
        capital_gains_inclusion: CG inclusion rate (default 50%, a tax rule)
        custom_strategies: Dict of strategy_name → AllocationStrategy or dict
            for user-configured strategies (DP#2). Overrides default strategies.

    Returns:
        Dict of strategy_name → AllocationStrategy (only applicable ones)

    Raises:
        TypeError: if investment_return (or return_model) or heloc_rate
            is not provided
    """
    # Derive investment_return from return_model when available
    if return_model is not None:
        investment_return = return_model.return_for_year(0)
    if investment_return is None:
        raise TypeError(
            "discover_strategies() requires either 'return_model' or "
            "'investment_return' — hardcoded 7% default was removed (issue #23)"
        )
    if heloc_rate is None:
        raise TypeError(
            "discover_strategies() requires 'heloc_rate' — hardcoded 5% "
            "default was removed (issue #23)"
        )

    # DP#2: Start with custom strategies if provided, otherwise use defaults
    if custom_strategies:
        discovered = {}
        for name, strat in custom_strategies.items():
            if isinstance(strat, dict):
                discovered[name] = AllocationStrategy.from_dict(strat)
            else:
                discovered[name] = strat
    else:
        discovered = {'balanced': STRATEGY_BALANCED, 'rrsp_max': STRATEGY_RRSP_MAX}

    # Condition 1: Mortgage must be readvanceable
    is_readvanceable = False
    if mortgage_cfg is not None:
        is_readvanceable = mortgage_cfg.get('heloc_readvance', False)

    # Condition 2: Investment interest is deductible (CRA §20(1)(c))
    interest_is_deductible = True  # Non-reg investment interest is deductible

    # Condition 3: After-tax return > after-tax HELOC cost
    marginal = state.primary_marginal_rate
    after_tax_heloc_cost = heloc_rate * (1 - marginal)
    after_tax_return = investment_return * (1 - marginal * capital_gains_inclusion)
    is_profitable = after_tax_return > after_tax_heloc_cost

    # If all conditions hold, readvance_priority is available
    if is_readvanceable and interest_is_deductible and is_profitable:
        discovered['readvance_priority'] = STRATEGY_READVANCE_PRIORITY
        discovered['no_readvance'] = STRATEGY_NO_READVANCE
    else:
        discovered['no_readvance'] = STRATEGY_NO_READVANCE
        # #663/#687, and now #713: the three conditions above gate the BUILT-IN
        # readvance_priority -- but a strategy that asks for the readvance
        # mechanism is a strategy that asks for the readvance mechanism,
        # whoever wrote it. Once a household can author its own strategies
        # (#713), `use_smith: true` arrives through custom_strategies and would
        # otherwise sail straight past this gate: the engine would rank a
        # Smith Manoeuvre against a mortgage that is not readvanceable (or
        # whose after-tax spread does not clear the borrowing cost), i.e. score
        # a strategy the household structurally CANNOT execute. That is exactly
        # what #663 forbids, and the caller's own has_readvanceable_facility()
        # filter cannot catch it -- that predicate asks whether a revolving
        # line EXISTS, not whether it is readvanceable, so a split-charge
        # structure sails through it too (#687).
        #
        # Dropped from the SEARCH SPACE, never silently "run without the
        # mechanism": a Smith Manoeuvre with the readvance turned off is a
        # different strategy wearing the user's label, and reporting its number
        # under their name would be the silent substitution this codebase
        # exists to prevent (DP#32). optimize.py discloses the skip count.
        discovered = {
            name: strat for name, strat in discovered.items()
            if not strat.prioritize_readvanceable
        }

    # Issue #1000: a declared fhsa_room_accumulated activates the FHSA store
    # (SimState.initial builds it from fhsa_room_accumulated directly), but the
    # allocation gate is `s.fhsa_pct > 0` and every built-in strategy here
    # carries fhsa_pct=0.0 -- so the optimizer's strategy search never tried a
    # nonzero FHSA split, and declared room moved nothing (DP#14: data that
    # should trigger behaviour was inert). The fix is to make the SEARCH
    # exploit declared room: when the household has declared FHSA room AND
    # remaining lifetime headroom (DP#32 -- absent room => FHSA is NOT swept,
    # never silently defaulted), emit FHSA-enabled variants of each discovered
    # strategy. Each variant lifts fhsa_pct off zero and rebalances by drawing
    # the same share from non_reg_pct (the residual bucket) so the allocation
    # percentages still sum to ~1.0 -- no other account's target share moves.
    # The DEFAULT strategy (fhsa_pct=0.0) is untouched, so the golden fixture
    # -- which runs adapter.get_default_strategy(), NOT this search -- is
    # byte-exact unaffected. This gate reads state.fhsa_room, which the
    # optimizer populates from the primary's declared fhsa_room_accumulated
    # (and zeroes when fhsa_first_time_buyer_since is absent, mirroring
    # scenario_discovery._build_family_state); a household that declares no
    # room sees no FHSA variants and is unaffected.
    if state.fhsa_room > 0 and state.fhsa_lifetime_remaining > 0:
        fhsa_variants = {}
        for name, strat in discovered.items():
            for fhsa_share in _FHSA_SEARCH_PCTS:
                # DP#32: never invent room -- cap the FHSA share at what the
                # strategy's non-reg residual can yield so the total stays
                # ~1.0 and no other target share is silently cut.
                share = min(fhsa_share, strat.non_reg_pct)
                if share <= 0:
                    continue
                variant = replace(strat,
                                  fhsa_pct=share,
                                  non_reg_pct=strat.non_reg_pct - share)
                fhsa_variants[f"{name}_fhsa_{int(share * 100)}"] = variant
        discovered.update(fhsa_variants)

    return discovered


def register():
    """Register Canadian strategies with the core strategy engine."""
    from strategy import _STRATEGY_REGISTRY
    for name, strat in STRATEGIES.items():
        _STRATEGY_REGISTRY[name] = strat
