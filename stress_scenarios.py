#!/usr/bin/env python3
"""
Stress Scenarios — Path-Based Market and Rate Stress Tests

Unlike sensitivity.py (which does Monte Carlo on random parameter
combinations), this module models specific historical-style stress
paths: market crashes, rate spikes, and combined scenarios.

Per DP#5: stress scenarios are overlays on anchor decisions, not
separate scenarios. You still compare "refinance vs not" under
each stress overlay.

Usage:
    from stress_scenarios import StressPath, STRESS_2008_CRASH, run_stress_test
    from stress_scenarios import stress_comparison_report

    result = run_stress_test(cfg, STRESS_2008_CRASH)
    print(f"Net benefit under 2008-style crash: ${result['net_benefit']:,.0f}")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from copy import deepcopy



# =============================================================================
# Stress Path Definitions — Historical and Hypothetical
# =============================================================================

@dataclass
class StressPath:
    """A path-based stress scenario with per-year investment returns and rates.

    Unlike flat sensitivity variables, path-based scenarios model the
    *shape* of the stress: what happens in year 1, year 2, etc.
    This captures sequence-of-returns risk and rate path dynamics.
    """
    name: str
    investment_return_path: List[float]  # Per-year returns (e.g., [-0.40, 0.15, ...])
    heloc_rate_path: List[float]  # Per-year HELOC rates
    description: str = ""

    @property
    def years(self) -> int:
        return max(len(self.investment_return_path), len(self.heloc_rate_path))

    def fill_returns(self, total_years: int, default_return: float = 0.07) -> List[float]:
        """Fill return path to total_years with default after the defined path."""
        path = list(self.investment_return_path)
        while len(path) < total_years:
            path.append(default_return)
        return path[:total_years]

    def fill_rates(self, total_years: int, default_rate: float = 0.05) -> List[float]:
        """Fill rate path to total_years with default after the defined path."""
        path = list(self.heloc_rate_path)
        while len(path) < total_years:
            path.append(default_rate)
        return path[:total_years]

    def average_return(self, total_years: int = 10) -> float:
        """Average annual return over the projection period."""
        path = self.fill_returns(total_years)
        return sum(path) / len(path) if path else 0

    def average_rate(self, total_years: int = 10) -> float:
        """Average HELOC rate over the projection period."""
        path = self.fill_rates(total_years)
        return sum(path) / len(path) if path else 0


# =============================================================================
# Pre-defined Stress Paths
# =============================================================================

STRESS_BASELINE = StressPath(
    name="Baseline (no stress)",
    investment_return_path=[0.07] * 10,
    heloc_rate_path=[0.0495] * 10,
    description="Normal market conditions: 7% return, 4.95% HELOC",
)

STRESS_2008_CRASH = StressPath(
    name="2008-style crash",
    investment_return_path=[-0.40, 0.15, 0.10, 0.12, 0.08] + [0.07] * 5,
    heloc_rate_path=[0.072, 0.035, 0.030, 0.035] + [0.0495] * 6,
    description="2008-style: -40% year 1, slow recovery over 5 years. Rates spike then drop.",
)

STRESS_RATE_SPIKE = StressPath(
    name="HELOC rate spike",
    investment_return_path=[0.07] * 10,
    heloc_rate_path=[0.0495, 0.060, 0.072, 0.065, 0.055] + [0.0495] * 5,
    description="HELOC rate spikes from 4.95% to 7.2% over 18 months, then normalizes over 3 years",
)

STRESS_STAGFLATION = StressPath(
    name="Stagflation",
    investment_return_path=[0.02, 0.01, 0.03, 0.02, 0.03] + [0.04] * 5,
    heloc_rate_path=[0.060, 0.065, 0.070, 0.068, 0.065] + [0.055] * 5,
    description="Low returns + high rates (stagflation). Returns 1-3%, rates 6-7%",
)

STRESS_COMBINED = StressPath(
    name="Crash + Rate Spike (worst realistic)",
    investment_return_path=[-0.30, -0.10, 0.05, 0.10, 0.08] + [0.07] * 5,
    heloc_rate_path=[0.072, 0.080, 0.065, 0.055, 0.050] + [0.0495] * 5,
    description="Market crash (year 1-2) combined with rate spike. Worst realistic case.",
)

STRESS_RATE_SHOCK_2022 = StressPath(
    name="2022-style rate shock",
    investment_return_path=[-0.10, -0.05, 0.10, 0.08, 0.07] + [0.07] * 5,
    heloc_rate_path=[0.030, 0.045, 0.055, 0.070, 0.065] + [0.055] * 5,
    description="2022-style: rates rise from 3% to 7% while markets drop. Bond-equity correlation shock.",
)

STRESS_LONG_RECOVERY = StressPath(
    name="Long slow recovery (Japan-style)",
    investment_return_path=[-0.20, 0.00, 0.02, 0.02, 0.03,
                            0.03, 0.04, 0.04, 0.05, 0.05],
    heloc_rate_path=[0.040, 0.035, 0.030, 0.025, 0.025] + [0.030] * 5,
    description="Japan-style lost decade: -20% crash followed by a decade of near-zero returns.",
)

ALL_STRESS_PATHS = [
    STRESS_BASELINE,
    STRESS_2008_CRASH,
    STRESS_RATE_SPIKE,
    STRESS_STAGFLATION,
    STRESS_COMBINED,
    STRESS_RATE_SHOCK_2022,
    STRESS_LONG_RECOVERY,
]


# =============================================================================
# HELOC Call Risk & Forced Liquidation (DP#29, DP#17)
# =============================================================================

@dataclass
class HELoCCallEvent:
    """A margin call / HELOC call event during a stress scenario.

    Per DP#6: this models the MECHANISM (LTV breach triggers call), not a
    branded product. Per DP#3: pure function, same inputs → same outputs.
    Per DP#7: we model readvanceable mortgage call rules, not Manulipe One.

    When LTV exceeds the lender's call threshold (typically 80% readvanceable),
    the lender may demand repayment of the excess. If the borrower cannot
    repay, they may be forced to liquidate investments at a loss.

    Key rules (CRA / lender policy):
    - Most readvanceable products trigger a call when LTV > 80% of property value
    - Some readvanceable products allow re-advancing up to 80% LTV; others cap at 65%
    - The call amount is the excess above the threshold, not the full HELOC balance
    - Forced liquidation triggers capital gains tax (inclusion rate × MTR)
    - Liquidation at a loss (market crash) may trigger capital losses for offset
    """
    year: int                              # Year in which the call happens
    ltv_before: float                      # LTV ratio just before the call
    call_threshold: float                  # LTV ratio that triggers a call (e.g. 0.80)
    heloc_balance: float                   # HELOC balance at time of call
    house_value_decline_pct: float         # % decline in house value (e.g., -0.15)
    portfolio_decline_pct: float           # % decline in investment portfolio
    call_amount: float = 0.0              # Computed: amount demanded by lender
    forced_liquidation: float = 0.0       # Computed: amount liquidated to cover call
    liquidation_tax: float = 0.0          # Computed: tax on liquidated gains
    net_loss_from_liquidation: float = 0.0 # Computed: total loss from forced sale

    def __post_init__(self):
        """Compute derived fields from the mechanism rules."""
        self.call_amount = max(0, self.heloc_balance - self.call_threshold * (
            self.heloc_balance / max(self.ltv_before, 0.01)
        ) * (1 + self.house_value_decline_pct)) if self.ltv_before > self.call_threshold else 0


def compute_heloc_call_risk(
    house_value: float,
    mortgage_balance: float,
    heloc_balance: float,
    ltv_threshold: float = 0.80,
    investment_portfolio: float = 0,
    portfolio_cost_basis: float = 0,
    marginal_rate: float = 0.0,  # DP#13: compute from TaxDataProvider
    capital_gains_inclusion: float = 0.50,
    house_decline_pct: float = 0.0,
    portfolio_decline_pct: float = 0.0,
) -> HELoCCallEvent:
    """Compute the HELOC call risk for current positions.

    Per DP#6: discovered from conditions, not named. When LTV > threshold,
    the lender may demand repayment.

    Per DP#17: two rule paths tested — call triggered vs. not triggered.

    Args:
        house_value: Current property value
        mortgage_balance: Current mortgage principal
        heloc_balance: Current HELOC balance (readvanceable portion)
        ltv_threshold: LTV ratio that triggers a call (default 80%)
        investment_portfolio: Total non-registered portfolio value
        portfolio_cost_basis: ACB of non-registered portfolio (DP#19)
        marginal_rate: Tax rate for capital gains computation
        capital_gains_inclusion: Capital gains inclusion rate (50% for 2026)
        house_decline_pct: House value decline (e.g., -0.15 for 15% drop)
        portfolio_decline_pct: Portfolio decline (e.g., -0.30 for 30% drop)

    Returns:
        HELoCCallEvent with computed call amount and liquidation impact
    """
    total_debt = mortgage_balance + heloc_balance
    adjusted_house = house_value * (1 + house_decline_pct)
    ltv_before = total_debt / adjusted_house if adjusted_house > 0 else 0

    # LTV after market decline
    ltv_after = total_debt / adjusted_house if adjusted_house > 0 else 0

    # Call amount: the excess above the threshold
    # Lender demands repayment of the amount that brings LTV back to threshold
    if ltv_after > ltv_threshold and adjusted_house > 0:
        max_debt_at_threshold = ltv_threshold * adjusted_house
        call_amount = max(0, total_debt - max_debt_at_threshold)
        # Call can only be for the HELOC portion (lender can't call mortgage)
        call_amount = min(call_amount, heloc_balance)
    else:
        call_amount = 0

    # Forced liquidation impact
    # If the borrower must sell investments to cover the call:
    # - Portfolio has declined by portfolio_decline_pct
    # - Cost basis stays the same, so gains/losses change
    portfolio_after_crash = investment_portfolio * (1 + portfolio_decline_pct)
    capital_gain = portfolio_after_crash - portfolio_cost_basis

    liquidation_amount = min(call_amount, portfolio_after_crash) if call_amount > 0 else 0

    if liquidation_amount > 0 and portfolio_after_crash > 0:
        # Proportion of portfolio liquidated
        liquidation_pct = liquidation_amount / portfolio_after_crash
        # Gain/loss on the liquidated portion
        liquidated_cost_basis = portfolio_cost_basis * liquidation_pct
        liquidated_proceeds = liquidation_amount
        gain_on_liquidation = liquidated_proceeds - liquidated_cost_basis

        if gain_on_liquidation > 0:
            # Capital gains tax on forced sale
            liquidation_tax = gain_on_liquidation * capital_gains_inclusion * marginal_rate
        else:
            # Capital loss — can offset gains, no tax due
            liquidation_tax = 0

        net_loss = call_amount  # Amount taken from portfolio
        if portfolio_decline_pct < 0:
            # Additional loss: forced to sell at crashed prices
            # What would this portfolio be worth at recovery?
            recovery_value = liquidation_amount / (1 + portfolio_decline_pct) if portfolio_decline_pct > -1 else 0
            opportunity_cost = recovery_value - liquidation_amount
            net_loss += liquidation_tax + opportunity_cost
        else:
            net_loss += liquidation_tax
    else:
        liquidation_tax = 0
        net_loss = 0

    return HELoCCallEvent(
        year=0,
        ltv_before=ltv_before if house_value > 0 else 0,
        call_threshold=ltv_threshold,
        heloc_balance=heloc_balance,
        house_value_decline_pct=house_decline_pct,
        portfolio_decline_pct=portfolio_decline_pct,
        call_amount=call_amount,
        forced_liquidation=liquidation_amount,
        liquidation_tax=liquidation_tax,
        net_loss_from_liquidation=net_loss,
    )


def ltv_triggered_stress_path(
    base_path: StressPath,
    house_decline_pct: float,
    portfolio_decline_pct: float,
    rate_increase: float = 0.0,
) -> StressPath:
    """Create a stress path that includes LTV-triggered events.

    Per DP#5: this is an overlay on the base stress path. It layers
    the LTV trigger on top of the market/rate stress.

    When house values decline, LTV increases, which may trigger
    margin calls. When combined with portfolio decline, this creates
    forced liquidation risk.

    Args:
        base_path: Base StressPath to layer on top of
        house_decline_pct: House value decline (e.g., -0.15)
        portfolio_decline_pct: Portfolio decline in year 1 (e.g., -0.30)
        rate_increase: Additional HELOC rate increase (e.g., 0.02)

    Returns:
        New StressPath with LTV-triggered rate adjustments
    """
    years = max(base_path.years, 10)
    adj_returns = list(base_path.fill_returns(years))
    adj_rates = list(base_path.fill_rates(years))

    # Layer the portfolio decline onto the first year
    if len(adj_returns) > 0:
        adj_returns[0] += portfolio_decline_pct
    # Layer the rate increase into the first years
    for i in range(min(3, len(adj_rates))):
        adj_rates[i] += rate_increase

    return StressPath(
        name=f"{base_path.name} + LTV trigger ({house_decline_pct:+.0%} house, {portfolio_decline_pct:+.0%} portfolio)",
        investment_return_path=adj_returns,
        heloc_rate_path=adj_rates,
        description=(f"{base_path.description} "
                     f"PLUS: House value {house_decline_pct:+.0%}, "
                     f"portfolio {portfolio_decline_pct:+.0%}, "
                     f"rate +{rate_increase:.2%}"),
    )


def forced_liquidation_impact(
    investment_portfolio: float,
    portfolio_cost_basis: float,
    liquidation_pct: float,
    marginal_rate: float = 0.0,  # DP#13: compute from TaxDataProvider
    capital_gains_inclusion: float = 0.50,
) -> Dict:
    """Compute the tax impact of forced liquidation.

    Per DP#19: track cost basis from day one, compute tax at withdrawal.
    Per DP#27: investment income has distinct tax treatments.

    When forced to liquidate a portion of investments to cover a margin call,
    the tax consequences depend on the gain/loss and the income type.
    This computes capital gains tax on the liquidated portion.

    Args:
        investment_portfolio: Current portfolio market value
        portfolio_cost_basis: ACB (Adjusted Cost Base) of the portfolio
        liquidation_pct: Percentage of portfolio to liquidate (0-1)
        marginal_rate: Tax rate for capital gains computation
        capital_gains_inclusion: Inclusion rate (50% for 2024+, proposed 66.67% >$250k)

    Returns:
        Dict with liquidation amounts and tax impact
    """
    if investment_portfolio <= 0 or liquidation_pct <= 0:
        return {
            'liquidation_amount': 0,
            'cost_basis_recovered': 0,
            'capital_gain': 0,
            'capital_loss': 0,
            'tax_on_gain': 0,
            'net_proceeds': 0,
            'portfolio_remaining': investment_portfolio,
            'cost_basis_remaining': portfolio_cost_basis,
        }

    liquidation_amount = investment_portfolio * liquidation_pct
    cost_basis_recovered = portfolio_cost_basis * liquidation_pct
    gain = liquidation_amount - cost_basis_recovered

    # Per DP#27: capital gains have 50% inclusion rate (default)
    if gain > 0:
        capital_gain = gain
        capital_loss = 0
        tax_on_gain = gain * capital_gains_inclusion * marginal_rate
    else:
        capital_gain = 0
        capital_loss = abs(gain)
        tax_on_gain = 0  # Losses can offset gains, no tax due

    net_proceeds = liquidation_amount - tax_on_gain
    portfolio_remaining = investment_portfolio - liquidation_amount
    cost_basis_remaining = portfolio_cost_basis - cost_basis_recovered

    return {
        'liquidation_amount': liquidation_amount,
        'cost_basis_recovered': cost_basis_recovered,
        'capital_gain': capital_gain,
        'capital_loss': capital_loss,
        'tax_on_gain': tax_on_gain,
        'net_proceeds': net_proceeds,
        'portfolio_remaining': portfolio_remaining,
        'cost_basis_remaining': cost_basis_remaining,
    }


# =============================================================================
# Stress Test Runner
# =============================================================================

def compute_heloc_call_series(
    house_value: float,
    mortgage_balance: float,
    heloc_balance: float,
    investment_portfolio: float,
    portfolio_cost_basis: float,
    stress_path: StressPath,
    ltv_threshold: float = 0.80,
    marginal_rate: float = 0.0,  # DP#13: compute from TaxDataProvider
    capital_gains_inclusion: float = 0.50,
    projection_years: int = 10,
) -> List[HELoCCallEvent]:
    """Project HELOC call risk year by year under a stress path.

    Per DP#26: the projection is a fold over years. Each step applies
    the stress path's return/rate for that year and checks LTV.

    Args:
        house_value: Initial property value
        mortgage_balance: Initial mortgage balance
        heloc_balance: Initial HELOC balance
        investment_portfolio: Initial non-reg portfolio value
        portfolio_cost_basis: ACB of non-reg portfolio
        stress_path: StressPath with per-year returns/rates
        ltv_threshold: LTV ratio that triggers a call (default 80%)
        marginal_rate: Marginal tax rate
        capital_gains_inclusion: Capital gains inclusion rate
        projection_years: Number of years to project

    Returns:
        List of HELoCCallEvent per year, showing call risk evolution
    """
    returns = stress_path.fill_returns(projection_years)
    events = []
    running_house = house_value
    running_portfolio = investment_portfolio
    running_mortgage = mortgage_balance
    running_heloc = heloc_balance

    for yr in range(projection_years):
        ret = returns[yr] if yr < len(returns) else 0.07

        # Apply market return to portfolio
        running_portfolio *= (1 + ret)
        # House value grows with a modest rate (2%/yr real + return component)
        house_appreciation = max(ret * 0.3, 0.02)  # House appreciates slower than equities
        running_house *= (1 + house_appreciation)

        # Compute current LTV
        total_debt = running_mortgage + running_heloc
        ltv = total_debt / running_house if running_house > 0 else 0

        # Check if LTV breach triggers a call
        if ltv > ltv_threshold:
            event = compute_heloc_call_risk(
                house_value=running_house,
                mortgage_balance=running_mortgage,
                heloc_balance=running_heloc,
                ltv_threshold=ltv_threshold,
                investment_portfolio=running_portfolio,
                portfolio_cost_basis=portfolio_cost_basis,
                marginal_rate=marginal_rate,
                capital_gains_inclusion=capital_gains_inclusion,
                house_decline_pct=0,  # Already reflected in running values
                portfolio_decline_pct=0,  # Already reflected
            )
            event = HELoCCallEvent(
                year=yr + 1,
                ltv_before=ltv,
                call_threshold=ltv_threshold,
                heloc_balance=running_heloc,
                house_value_decline_pct=0,
                portfolio_decline_pct=0,
                call_amount=event.call_amount,
                forced_liquidation=event.forced_liquidation,
                liquidation_tax=event.liquidation_tax,
                net_loss_from_liquidation=event.net_loss_from_liquidation,
            )
            events.append(event)

            # If forced liquidation, reduce portfolio and HELOC
            if event.forced_liquidation > 0:
                running_portfolio -= event.forced_liquidation
                running_heloc -= event.call_amount
                portfolio_cost_basis *= (1 - event.forced_liquidation / max(running_portfolio + event.forced_liquidation, 1))
        else:
            # No call — record the all-clear
            events.append(HELoCCallEvent(
                year=yr + 1,
                ltv_before=ltv,
                call_threshold=ltv_threshold,
                heloc_balance=running_heloc,
                house_value_decline_pct=0,
                portfolio_decline_pct=0,
                call_amount=0,
                forced_liquidation=0,
                liquidation_tax=0,
                net_loss_from_liquidation=0,
            ))

    return events


def apply_stress_overlay(cfg: Dict, stress_path: StressPath,
                              projection_years: int = None) -> Dict:
    """Return a deep-copied config with the stress path overlaid (DP#21/#727).

    The investment return path is routed through the engine's pluggable
    ``ReturnModel`` seam as a ``variable`` model whose per-year ``rates`` ARE
    the stress returns -- so ``ReturnEngine.return_for_year(model, year)``
    applies year N's stress return IN year N (a year-1 -40% crash hits year 1,
    not an averaged smear). The HELOC/mortgage rate path uses a separate seam
    (rate_model.py); the averaged rate is kept for now (#727 scope is the
    ReturnModel).

    Returned for testability so a test can drive ``run_optimization`` on the
    overlaid cfg and inspect ``year_by_year`` without duplicating the overlay.
    """
    mod_cfg = deepcopy(cfg)
    if projection_years is None:
        projection_years = cfg.get('assumptions', {}).get('projection_years', 10)
    return_path = stress_path.fill_returns(projection_years)
    mod_cfg['assumptions'] = dict(cfg.get('assumptions', {}))
    mod_cfg['return_model'] = {
        'type': 'variable',
        'rates': return_path,
        'fallback': return_path[-1] if return_path else 0.07,
    }
    avg_rate = stress_path.average_rate(projection_years)
    mod_cfg['property'] = dict(cfg.get('property', {}))
    mod_cfg['property']['mortgage_rate'] = avg_rate
    return mod_cfg


def run_stress_test(cfg: Dict, stress_path: StressPath) -> Dict:
    """Run a simulation under a specific stress path.

    DP#21 (#727): the stress path is a PER-YEAR return sequence, not a single
    averaged rate. Pre-#727 this collapsed the whole path to one average
    (`stress_path.average_return`) and set a flat `fixed` return_model at that
    average -- which averaged away the very shape a stress test exists to
    capture (a -40% year-1 crash then recovery was smeared across the whole
    horizon, so year 1 never bore the crash). Now the path is routed through
    the engine's pluggable ``ReturnModel`` seam as a `variable` model: each
    year's stress return is applied IN THAT YEAR via
    ``ReturnEngine.return_for_year(model, year)`` -- the same per-year seam
    `variable`/`stochastic` return models use elsewhere.

    The `StressPath` input shape (List[float] per-year) is preserved; only the
    routing changes (averaged scalar -> per-year ReturnModel).

    Per DP#5: this is an overlay, not a separate scenario. The anchor decision
    (refinance vs not) is still the primary comparison.

    Args:
        cfg: Input config dict (input.json format)
        stress_path: StressPath to overlay

    Returns:
        Dict with stress test results. `avg_return`/`avg_rate` are kept for the
        report's display columns; the SIMULATION uses the per-year path.
    """
    projection_years = cfg.get('assumptions', {}).get('projection_years', 10)
    mod_cfg = apply_stress_overlay(cfg, stress_path, projection_years)
    return_path = mod_cfg['return_model']['rates']
    avg_rate = mod_cfg['property']['mortgage_rate']

    # Run optimization with the per-year stress return model overlaid.
    from optimize import run_optimization
    results = run_optimization(mod_cfg, include_year_by_year=False)  # score-only caller — skip year_by_year serialization, #1058

    best = max(results, key=lambda r: r.get('net_benefit', 0)) if results else {}

    # avg_return is kept as a DISPLAY label for the report; it is NOT what the
    # simulation consumed (the per-year path is -- see 'return_path' below).
    avg_return = stress_path.average_return(projection_years)
    return {
        'stress_name': stress_path.name,
        'description': stress_path.description,
        'avg_return': avg_return,
        'avg_rate': avg_rate,
        'net_benefit': best.get('net_benefit', 0),
        'best_strategy': best.get('strategy', 'none'),
        'return_path': return_path,
        'rate_path': stress_path.fill_rates(projection_years),
    }


def stress_comparison_report(cfg: Dict) -> str:
    """Generate a comparison report across all stress scenarios.

    Shows how each anchor decision performs under each stress overlay.
    """
    projection_years = cfg.get('assumptions', {}).get('projection_years', 10)
    lines = [
        "=" * 120,
        "📉 STRESS TEST COMPARISON REPORT",
        "=" * 120,
        "",
        f"  Projection: {projection_years} years",
        "",
        f"  {'Stress Scenario':<35s} {'Avg Return':>10s} {'Avg Rate':>10s} "
        f"{'Net Benefit':>14s} {'Strategy':>25s}",
        f"  {'-'*95}",
    ]

    baseline_net = None
    results = []

    for stress in ALL_STRESS_PATHS:
        result = run_stress_test(cfg, stress)
        results.append(result)
        if baseline_net is None and stress.name == "Baseline (no stress)":
            baseline_net = result['net_benefit']

    for r in results:
        delta = ""
        if baseline_net is not None and r['stress_name'] != "Baseline (no stress)":
            diff = r['net_benefit'] - baseline_net
            pct = diff / baseline_net * 100 if baseline_net != 0 else 0
            delta = f" ({diff:+,.0f} / {pct:+.0f}%)"

        lines.append(
            f"  {r['stress_name']:<35s} {r['avg_return']*100:>9.1f}% {r['avg_rate']*100:>9.2f}% "
            f"${r['net_benefit']/1000:>12.0f}k {r['best_strategy']:>25s}"
            f"{delta}"
        )

    # Breakeven analysis
    lines.append("")
    lines.append("=" * 120)
    lines.append("  📊 BREAKEVEN ANALYSIS")
    lines.append("=" * 120)

    # Find worst-case net benefit
    worst = min(results, key=lambda r: r['net_benefit'])
    best = max(results, key=lambda r: r['net_benefit'])

    lines.append(f"\n  Best case:  {best['stress_name']} → ${best['net_benefit']:,.0f}")
    lines.append(f"  Worst case: {worst['stress_name']} → ${worst['net_benefit']:,.0f}")
    lines.append(f"  Range:      ${best['net_benefit'] - worst['net_benefit']:,.0f}")

    if worst['net_benefit'] < 0:
        lines.append(f"\n  ⚠️  WARNING: Strategy produces a LOSS under {worst['stress_name']}")
    elif worst['net_benefit'] > 0:
        lines.append(f"\n  ✅ Strategy remains profitable even under worst stress scenario")

    # Sequence-of-returns risk
    lines.append("")
    lines.append("=" * 120)
    lines.append("  ⚡ SEQUENCE-OF-RETURNS RISK")
    lines.append("=" * 120)
    lines.append("")
    lines.append("  Year-by-year return paths under each stress scenario:")
    lines.append("")

    # Table header
    header = f"  {'Year':>4s}"
    for stress in ALL_STRESS_PATHS:
        label = stress.name[:12]
        header += f" {label:>12s}"
    lines.append(header)
    lines.append(f"  {'-' * (6 + 13 * len(ALL_STRESS_PATHS))}")

    for yr in range(min(projection_years, 10)):
        row = f"  {yr+1:>4d}"
        for stress in ALL_STRESS_PATHS:
            path = stress.fill_returns(projection_years)
            ret = path[yr] if yr < len(path) else 0.07
            row += f" {ret*100:>11.1f}%"
        lines.append(row)

    lines.append("")
    return "\n".join(lines)