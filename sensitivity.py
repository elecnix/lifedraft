#!/usr/bin/env python3
"""
Sensitivity & Risk Analysis for Mortgage Refinance + Smith Manoeuvre

DP#14: Scripts read a common config schema; each script uses the parts it needs.
This module reads `sensitivity_overlays` from input.json and uses
SimulationConfig.from_json + ScenarioOverlay instead of constructing
config dicts manually.

DP#5: Overlays layer on top of anchor scenarios — they modify, not replace.
DP#18: Scenarios compose from a base; overlays modify, they don't replace.

Produces:
- Two-way sensitivity table (return × rate)
- Break-even analysis for each variable
- Monte Carlo simulation with probability-weighted outcomes
- Risk tornado diagram (which variables matter most)
"""

from tax_data import default_tax_provider
import json
import numpy as np
import pandas as pd
from typing import Dict, List, Optional
from pathlib import Path

from tax_calculator import (
    marginal_rate, tax_on_income,
)
from simulation import SimulationConfig
from simulation_config import ScenarioOverlay, apply_overlay
from optimize import run_optimization
from return_model import ReturnModel, ReturnEngine, StochasticReturn
import contract_schema


# ── DP#14: Config pipeline ──────────────────────────────────────────────────
# All functions accept SimulationConfig + ScenarioOverlay instead of raw
# dicts and individual float parameters. The common config schema is the
# single source of truth; overlays compose on top per DP#18.


def _ensure_config(cfg_or_config):
    """Convert dict to SimulationConfig if needed.

    Per DP#9 (no backward compat), raises TypeError for unsupported types
    instead of silently converting.
    """
    if isinstance(cfg_or_config, SimulationConfig):
        return cfg_or_config
    if isinstance(cfg_or_config, dict):
        return SimulationConfig.from_dict(cfg_or_config)
    raise TypeError(f"Expected SimulationConfig or dict, got {type(cfg_or_config).__name__}")


def run_scenario(config: SimulationConfig, overlay: ScenarioOverlay) -> Dict:
    """Run a single scenario with a sensitivity overlay (DP#14, DP#18).

    Per DP#14, all scenario scripts read the common config schema and use
    ScenarioOverlay for parameter variations. This replaces the old
    run_scenario(cfg, investment_return, heloc_rate, inflation) signature
    that bypassed the config pipeline.

    Per DP#5, overlays are uncertain parameters that layer on top of anchor
    decisions — they modify, not replace.

    When inflation is specified in the overlay, the model uses real returns:
    - Real return = nominal return - inflation (for investment compounding)
    - HELOC rate stays nominal (it's a contract rate)

    Args:
        config: Base SimulationConfig (loaded from input.json via from_json).
        overlay: ScenarioOverlay with delta values for this sensitivity run.

    Returns:
        Dict with net_benefit, strategy, and overlay parameters.
    """
    config = _ensure_config(config)

    # DP#18: apply overlay on top of base config (deep copy, modify)
    base_dict = config.to_dict()
    derived_dict = apply_overlay(base_dict, overlay)

    # Adjust for inflation: use real return for investment compounding
    # HELOC rate stays nominal (contract rate)
    # DP#14: When overlay specifies inflation, compute real return.
    # When overlay does not specify inflation, fall back to config.inflation.
    # #260: write the real return into return_model — the single source of truth the
    # engine actually reads. apply_overlay always materializes a fixed return_model
    # whenever overlay.investment_return is set, so the block is present here.
    inflation = overlay.inflation if overlay.inflation is not None else config.inflation
    if inflation > 0 and overlay.investment_return is not None:
        derived_dict['return_model']['rate'] = overlay.investment_return - inflation

    # Use the modular optimization engine
    results = run_optimization(derived_dict, include_year_by_year=False)  # score-only caller — skip year_by_year serialization, #1058
    investment_return = overlay.investment_return if overlay.investment_return is not None else config.investment_return
    heloc_rate = overlay.mortgage_rate

    if not results:
        return {
            'investment_return': investment_return,
            'heloc_rate': heloc_rate,
            'inflation': inflation,
            'real_return': investment_return - inflation,
            'net_benefit': 0,
            'strategy': 'none',
            'sm_savings': 0,
            'rrsp_savings': 0,
        }

    best = max(results, key=lambda r: r['net_benefit'])
    return {
        'investment_return': investment_return,
        'heloc_rate': heloc_rate,
        'inflation': inflation,
        'real_return': investment_return - inflation,
        'net_benefit': best['net_benefit'],
        'strategy': best['strategy'],
        'sm_savings': best.get('sm_10yr_savings', 0),
        'rrsp_savings': best.get('rrsp_total_savings', 0),
    }


def two_way_sensitivity(config: SimulationConfig,
                        returns: List[float] = None,
                        rates: List[float] = None,
                        inflation: Optional[float] = None) -> pd.DataFrame:
    """Generate a two-way sensitivity table (return × HELOC rate) (DP#14).

    Args:
        config: Base SimulationConfig.
        returns: List of investment return rates to sweep.
        rates: List of HELOC rates to sweep.
        inflation: Fixed inflation rate for real return adjustment.
                    Defaults to config.inflation per DP#13.
    """
    config = _ensure_config(config)
    if inflation is None:
        inflation = config.inflation
    if returns is None:
        returns = [0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10]
    if rates is None:
        rates = [0.03, 0.04, 0.05, 0.06, 0.07]

    results = []
    for ret in returns:
        for rate in rates:
            overlay = ScenarioOverlay(
                label=f"2way-{ret:.2f}-{rate:.3f}",
                investment_return=ret,
                mortgage_rate=rate,
                inflation=inflation,
            )
            r = run_scenario(config, overlay)
            results.append(r)

    return pd.DataFrame(results)


def break_even_analysis(config: SimulationConfig,
                        variable: str = 'investment_return',
                        base_investment_return: float = None,
                        base_heloc_rate: float = None,
                        base_inflation: float = None) -> Dict:
    """Find the break-even point for a given variable (DP#14).

    Per DP#13, base values come from the config, not from hardcoded defaults.

    Args:
        config: Base SimulationConfig (also accepts dict for backward compat).
        variable: Which variable to sweep ('investment_return' or 'heloc_rate').
        base_investment_return: Override base investment return (default: from config).
        base_heloc_rate: Override base HELOC rate (default: from config).
        base_inflation: Override base inflation (default: from config or 0.025).
    """
    config = _ensure_config(config)
    # DP#13: defaults from config, not hardcoded
    if base_investment_return is None:
        base_investment_return = config.investment_return
    if base_heloc_rate is None:
        base_heloc_rate = config.mortgage_rate
    if base_inflation is None:
        base_inflation = config.inflation

    base_overlay = ScenarioOverlay(
        label="break-even-base",
        investment_return=base_investment_return,
        mortgage_rate=base_heloc_rate,
        inflation=base_inflation,
    )
    base = run_scenario(config, base_overlay)
    base_net = base['net_benefit']

    results = {'variable': variable, 'base_value': None, 'base_net': base_net}

    if variable == 'investment_return':
        results['base_value'] = base_investment_return
        for threshold_pct in [0.50, 0.75, 1.00]:
            target = base_net * threshold_pct
            lo, hi = 0.0, 0.15
            for _ in range(50):
                mid = (lo + hi) / 2
                overlay = ScenarioOverlay(
                    label=f"be-ret-{mid:.3f}",
                    investment_return=mid,
                    mortgage_rate=base_heloc_rate,
                    inflation=base_inflation,
                )
                r = run_scenario(config, overlay)
                if r['net_benefit'] > target:
                    hi = mid
                else:
                    lo = mid
            results[f'break_even_{int(threshold_pct*100)}pct'] = (lo + hi) / 2

    elif variable == 'heloc_rate':
        results['base_value'] = base_heloc_rate
        for threshold_pct in [0.50, 0.75, 1.00]:
            target = base_net * threshold_pct
            lo, hi = 0.02, 0.10
            for _ in range(50):
                mid = (lo + hi) / 2
                overlay = ScenarioOverlay(
                    label=f"be-rate-{mid:.3f}",
                    investment_return=base_investment_return,
                    mortgage_rate=mid,
                    inflation=base_inflation,
                )
                r = run_scenario(config, overlay)
                if r['net_benefit'] > target:
                    lo = mid
                else:
                    hi = mid
            results[f'rate_for_{int(threshold_pct*100)}pct'] = (lo + hi) / 2

    return results


def monte_carlo(config: SimulationConfig, n_simulations: int = 5000,
                seed: int = 42, return_model: ReturnModel = None) -> pd.DataFrame:
    """Run Monte Carlo simulation with probability distributions (DP#14, DP#21, DP#23).

    DP#14: Accepts SimulationConfig instead of raw dict.
    DP#21: Uses ReturnModel for investment return generation instead of
    hardcoded distribution parameters.
    DP#23: Uses isolated RNG per call for reproducibility.

    Args:
        config: Base SimulationConfig (also accepts dict for backward compat).
        n_simulations: Number of Monte Carlo paths.
        seed: Random seed for reproducibility (DP#23). Default 42.
        return_model: Optional ReturnModel for generating investment returns.
                     If None, uses StochasticReturn(mean=0.07, sigma=0.18).
    """
    config = _ensure_config(config)
    rng = np.random.default_rng(seed)

    # DP#21: Use ReturnModel for investment returns
    if return_model is None:
        return_model = StochasticReturn(mean=0.07, sigma=0.18, seed=seed,
                                        n_years=n_simulations)

    # Generate investment returns from the return model
    if isinstance(return_model, ReturnModel) and return_model.type == "stochastic":
        returns_model = ReturnModel(type="stochastic", mean=return_model.mean, sigma=return_model.sigma,
                                    seed=seed, n_years=n_simulations)
        returns = np.array([ReturnEngine.return_for_year(returns_model, i) for i in range(n_simulations)])
    elif isinstance(return_model, ReturnModel) and return_model.type == "fixed":
        returns = rng.normal(loc=return_model.rate, scale=0.02, size=n_simulations)
        returns = np.clip(returns, -0.10, 0.25)
    else:
        returns = np.array([ReturnEngine.return_for_year(return_model, i % return_model.n_years
                                                        if hasattr(return_model, 'n_years')
                                                            else i)
                           for i in range(n_simulations)])

    returns = np.clip(returns, -0.10, 0.25)

    # DP#13: HELOC rate distribution uses round-number defaults
    rates = rng.triangular(left=0.03, mode=0.05, right=0.08, size=n_simulations)

    # DP#13: inflation defaults are round-number placeholders
    inflations = rng.normal(loc=0.025, scale=0.01, size=n_simulations)
    inflations = np.clip(inflations, 0.0, 0.06)

    results = []
    for i in range(n_simulations):
        overlay = ScenarioOverlay(
            label=f"mc-{i}",
            investment_return=float(returns[i]),
            mortgage_rate=float(rates[i]),
            inflation=float(inflations[i]),
        )
        r = run_scenario(config, overlay)
        r['simulation'] = i
        results.append(r)

    return pd.DataFrame(results)


def tornado_data(config: SimulationConfig,
                  base_investment_return: float = None,
                  base_heloc_rate: float = None,
                  base_inflation: float = None) -> pd.DataFrame:
    """Calculate sensitivity for a tornado diagram (one-at-a-time) (DP#14).

    Per DP#13, base values come from the config, not hardcoded defaults.

    Args:
        config: Base SimulationConfig (also accepts dict for backward compat).
        base_investment_return: Override base investment return.
        base_heloc_rate: Override base HELOC rate.
        base_inflation: Override base inflation rate.
    """
    config = _ensure_config(config)
    # DP#13: defaults from config
    if base_investment_return is None:
        base_investment_return = config.investment_return
    if base_heloc_rate is None:
        base_heloc_rate = config.mortgage_rate
    if base_inflation is None:
        base_inflation = config.inflation

    base_overlay = ScenarioOverlay(
        label="tornado-base",
        investment_return=base_investment_return,
        mortgage_rate=base_heloc_rate,
        inflation=base_inflation,
    )
    base = run_scenario(config, base_overlay)
    base_net = base['net_benefit']

    variables = {
        'Investment Return': {
            'low': ScenarioOverlay(label="tornado-ret-low", investment_return=0.04, mortgage_rate=base_heloc_rate, inflation=base_inflation),
            'high': ScenarioOverlay(label="tornado-ret-high", investment_return=0.10, mortgage_rate=base_heloc_rate, inflation=base_inflation),
        },
        'HELOC Rate': {
            'low': ScenarioOverlay(label="tornado-rate-low", investment_return=base_investment_return, mortgage_rate=0.03, inflation=base_inflation),
            'high': ScenarioOverlay(label="tornado-rate-high", investment_return=base_investment_return, mortgage_rate=0.07, inflation=base_inflation),
        },
        'Inflation': {
            'low': ScenarioOverlay(label="tornado-inf-low", investment_return=base_investment_return, mortgage_rate=base_heloc_rate, inflation=0.01),
            'high': ScenarioOverlay(label="tornado-inf-high", investment_return=base_investment_return, mortgage_rate=base_heloc_rate, inflation=0.04),
        },
        'Investment + HELOC': {
            'low': ScenarioOverlay(label="tornado-combo-low", investment_return=0.04, mortgage_rate=0.07, inflation=base_inflation),
            'high': ScenarioOverlay(label="tornado-combo-high", investment_return=0.10, mortgage_rate=0.03, inflation=base_inflation),
        },
    }

    tornado = []
    for name, overlays in variables.items():
        low_r = run_scenario(config, overlays['low'])
        high_r = run_scenario(config, overlays['high'])
        low_overlay = overlays['low']
        high_overlay = overlays['high']
        # Determine which value varies per variable type
        if 'Return' in name:
            low_value = low_overlay.investment_return
            high_value = high_overlay.investment_return
        elif 'Rate' in name:
            low_value = low_overlay.mortgage_rate
            high_value = high_overlay.mortgage_rate
        elif 'Inflation' in name:
            low_value = low_overlay.inflation
            high_value = high_overlay.inflation
        else:
            # Combined: show investment return
            low_value = low_overlay.investment_return
            high_value = high_overlay.investment_return
        tornado.append({
            'variable': name,
            'low_value': low_value,
            'high_value': high_value,
            'low_net': low_r['net_benefit'],
            'high_net': high_r['net_benefit'],
            'base_net': base_net,
            'low_delta': low_r['net_benefit'] - base_net,
            'high_delta': high_r['net_benefit'] - base_net,
        })

    df = pd.DataFrame(tornado).sort_values('high_delta', ascending=True)
    return df


def print_sensitivity_report(config: SimulationConfig, cfg_dict: Dict = None):
    """Print a complete sensitivity and risk report (DP#14).

    Per DP#14, reads the common config schema. Sensitivity parameters come
    from the config's sensitivity_overlays section when available, falling
    back to reasonable defaults per DP#13.

    Args:
        config: Base SimulationConfig (from SimulationConfig.from_json).
        cfg_dict: Raw config dict for sensitivity_overlays access. If None,
                  derived from config.to_dict().
    """
    if cfg_dict is None:
        cfg_dict = config.to_dict()

    # DP#14: Read sensitivity_overlays from the common config schema
    sensitivity_overlays = cfg_dict.get('sensitivity_overlays', {})
    overlay_returns = sensitivity_overlays.get('investment_return', [0.04, 0.07, 0.10])
    overlay_rates_cfg = sensitivity_overlays.get('renewal_rates', {})
    # Flatten renewal rates into a single list of HELOC rates
    if overlay_rates_cfg:
        overlay_rates = sorted(set(rate for rates in overlay_rates_cfg.values() for rate in rates))
    else:
        overlay_rates = [0.03, 0.04, 0.05, 0.06, 0.07]

    # DP#13: base values from config, not hardcoded
    base_investment_return = config.investment_return
    base_heloc_rate = config.mortgage_rate
    # DP#14: inflation is a field on SimulationConfig
    base_inflation = config.inflation

    # Extract income from config (DP#13: personal data from config, not hardcoded)
    primary = config.member_by_role('primary', {})  # #699 seam
    income = primary.get('gross_income', 0)
    brackets = default_tax_provider().get_combined_brackets()
    rate = marginal_rate(income, brackets) if income > 0 else 0

    # Base overlay for reference scenario
    base_overlay = ScenarioOverlay(
        label="report-base",
        investment_return=base_investment_return,
        mortgage_rate=base_heloc_rate,
        inflation=base_inflation,
    )

    print("\n" + "=" * 120)
    print("📈 SENSITIVITY & RISK ANALYSIS")
    print("=" * 120)

    # 1. TWO-WAY SENSITIVITY TABLE
    print(f"\n  📊 TWO-WAY SENSITIVITY: Investment Return × HELOC Rate")
    print(f"     (Best strategy net benefit at each combination, $thousands)")

    # DP#14: use sensitivity_overlays ranges from config
    returns_list = overlay_returns
    rates_list = overlay_rates

    # Header
    print(f"\n     {'':>20s}", end="")
    for rate in rates_list:
        print(f" {'HELOC ' + f'{rate*100:.1f}%':>10s}", end="")
    print()
    print(f"     {'-'*80}")

    for ret in returns_list:
        label = f"Return {ret*100:.0f}%"
        print(f"     {label:>20s}", end="")
        for rate in rates_list:
            overlay = ScenarioOverlay(
                label=f"2way-{ret:.2f}-{rate:.3f}",
                investment_return=ret,
                mortgage_rate=rate,
                inflation=base_inflation,
            )
            r = run_scenario(config, overlay)
            net = r['net_benefit'] / 1000
            if net > 900:
                print(f" ${net:>8.0f}k", end="")
            elif net > 500:
                print(f"  {net:>8.0f}k", end="")
            elif net > 0:
                print(f"  {net:>7.0f}k ", end="")
            else:
                print(f"  ({net:>6.0f}k)", end="")
        print()

    # 2. TORNADO DIAGRAM
    print(f"\n{'=' * 120}")
    print(f"  🌪️  TORNADO DIAGRAM (Impact of Each Variable on Net Benefit)")
    print(f"{'=' * 120}")

    tornado = tornado_data(config, base_investment_return, base_heloc_rate, base_inflation)
    base_net = tornado.iloc[0]['base_net']

    print(f"\n  Base case ({base_investment_return*100:.0f}% return, {base_heloc_rate*100:.1f}% HELOC, {base_inflation*100:.1f}% inflation): ${base_net:,.0f}\n")
    print(f"  {'Variable':<25s} {'Low Impact':>15s} {'High Impact':>15s} {'Spread':>12s}")
    print(f"  {'-'*70}")

    for _, row in tornado.iterrows():
        print(f"  {row['variable']:<25s} "
              f"${row['low_delta']:>+14,.0f} ${row['high_delta']:>+14,.0f} "
              f"${row['high_delta'] - row['low_delta']:>11,.0f}")

    # 3. BREAK-EVEN ANALYSIS
    print(f"\n{'=' * 120}")
    print(f"  ⚖️  BREAK-EVEN ANALYSIS")
    print(f"{'=' * 120}")

    # Investment return break-even
    print(f"\n  Minimum investment return needed:")
    for target_pct, label in [(1.0, "100% of base"), (0.75, "75% of base"), (0.50, "50% of base")]:
        target = base_net * target_pct
        lo, hi = 0.0, 0.15
        for _ in range(50):
            mid = (lo + hi) / 2
            overlay = ScenarioOverlay(
                label=f"be-ret-{mid:.3f}",
                investment_return=mid,
                mortgage_rate=base_heloc_rate,
                inflation=base_inflation,
            )
            r = run_scenario(config, overlay)
            if r['net_benefit'] > target:
                hi = mid
            else:
                lo = mid
        be = (lo + hi) / 2
        print(f"     {label:>20s}: {be*100:.1f}% return")

    # Find break-even (net = 0)
    lo, hi = -0.05, 0.15
    for _ in range(60):
        mid = (lo + hi) / 2
        overlay = ScenarioOverlay(
            label=f"be-zero-{mid:.3f}",
            investment_return=mid,
            mortgage_rate=base_heloc_rate,
            inflation=base_inflation,
        )
        r = run_scenario(config, overlay)
        if r['net_benefit'] > 0:
            hi = mid
        else:
            lo = mid
    be_zero = (lo + hi) / 2
    print(f"     {'Break-even (net=0)':>20s}: {be_zero*100:.1f}% return")

    # HELOC rate break-even
    print(f"\n  Maximum HELOC rate before strategy loses money:")
    lo, hi = 0.03, 0.15
    for _ in range(60):
        mid = (lo + hi) / 2
        overlay = ScenarioOverlay(
            label=f"be-rate-{mid:.3f}",
            investment_return=base_investment_return,
            mortgage_rate=mid,
            inflation=base_inflation,
        )
        r = run_scenario(config, overlay)
        if r['net_benefit'] > 0:
            lo = mid
        else:
            hi = mid
    max_rate = (lo + hi) / 2
    print(f"     At {base_investment_return*100:.0f}% return: HELOC can go up to {max_rate*100:.1f}% before losing money")

    # 4. REAL RETURN ANALYSIS
    print(f"\n{'=' * 120}")
    print(f"  💵 REAL RETURN ANALYSIS (Nominal Return − Inflation)")
    print(f"{'=' * 120}")

    print(f"\n  {'Real Return':>12s} {'Nominal':>8s} {'Inflation':>10s} {'Net Benefit':>14s} {'SM Worth?':>10s}")
    print(f"  {'-'*60}")

    for real_ret in [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]:
        for inflation in [0.015, 0.025, 0.04]:
            nominal = real_ret + inflation
            overlay = ScenarioOverlay(
                label=f"real-{real_ret:.2f}-{inflation:.3f}",
                investment_return=nominal,
                mortgage_rate=base_heloc_rate,
                inflation=inflation,
            )
            r = run_scenario(config, overlay)
            sm_worth = "✓" if r.get('sm_savings', r.get('sm_10yr_savings', 0)) > 0 else "✗"
            print(f"  {real_ret*100:>11.1f}% {nominal*100:>7.1f}% {inflation*100:>9.1f}% "
                  f"${r['net_benefit']/1000:>12.0f}k {sm_worth:>10s}")

    # 5. MONTE CARLO
    print(f"\n{'=' * 120}")
    print(f"  🎲 MONTE CARLO SIMULATION (5,000 scenarios)")
    print(f"{'=' * 120}")

    mc = monte_carlo(config, n_simulations=5000)

    print(f"\n  Distribution of outcomes:")
    print(f"     5th percentile (pessimistic):  ${mc['net_benefit'].quantile(0.05):>12,.0f}")
    print(f"     25th percentile:               ${mc['net_benefit'].quantile(0.25):>12,.0f}")
    print(f"     50th percentile (median):        ${mc['net_benefit'].quantile(0.50):>12,.0f}")
    print(f"     75th percentile:               ${mc['net_benefit'].quantile(0.75):>12,.0f}")
    print(f"     95th percentile (optimistic):   ${mc['net_benefit'].quantile(0.95):>12,.0f}")
    print(f"     Mean:                           ${mc['net_benefit'].mean():>12,.0f}")
    print(f"     Std Dev:                         ${mc['net_benefit'].std():>12,.0f}")

    prob_positive = (mc['net_benefit'] > 0).mean() * 100
    prob_beat_base = (mc['net_benefit'] > base_net * 0.75).mean() * 100
    print(f"\n  Probability of positive outcome: {prob_positive:.1f}%")
    print(f"  Probability of 75%+ of base case: {prob_beat_base:.1f}%")

    # Probability by range
    print(f"\n  Outcome probability distribution:")
    bins = [-float('inf'), 0, 250000, 500000, 750000, 1000000, 1250000, float('inf')]
    labels = ['Loss', '%$0-250k', '$250-500k', '$500-750k', '$750k-1M', '$1-1.25M', '>$1.25M']
    mc['category'] = pd.cut(mc['net_benefit'], bins=bins, labels=labels)
    prob_dist = mc['category'].value_counts(normalize=True).sort_index() * 100
    for cat, pct in prob_dist.items():
        bar = '█' * int(pct / 2)
        print(f"     {cat:>12s}: {pct:>5.1f}% {bar}")

    # 6. KEY RISKS
    print(f"\n{'=' * 120}")
    print(f"  ⚠️  KEY RISKS (Ranked by Impact)")
    print(f"{'=' * 120}")

    # Calculate impact of each risk using ScenarioOverlay
    base = run_scenario(config, base_overlay)

    risk_overlays = [
        ("Market crash (−50% one year, then normal)",
         ScenarioOverlay(label="risk-crash", investment_return=0.02, mortgage_rate=base_heloc_rate, inflation=base_inflation),
         "Could wipe out years of gains"),
        ("HELOC rate rises to 7%",
         ScenarioOverlay(label="risk-rate-up", investment_return=base_investment_return, mortgage_rate=0.07, inflation=base_inflation),
         "After-tax cost rises from 2.7% to 3.8%"),
        ("Stagnant returns (4% for 10 years)",
         ScenarioOverlay(label="risk-stagnant", investment_return=0.04, mortgage_rate=base_heloc_rate, inflation=base_inflation),
         "Common in low-growth environments"),
        ("Inflation spikes to 5%",
         ScenarioOverlay(label="risk-inflation", investment_return=base_investment_return, mortgage_rate=base_heloc_rate, inflation=0.05),
         "Erodes real returns significantly"),
        ("Combined: Low returns + High rates",
         ScenarioOverlay(label="risk-combined", investment_return=0.04, mortgage_rate=0.07, inflation=0.04),
         "Worst realistic case"),
    ]

    risks = []
    for name, overlay, note in risk_overlays:
        r = run_scenario(config, overlay)
        risks.append((name, r['net_benefit'], note))

    print(f"\n  {'Risk':<40s} {'Net Benefit':>12s} {'Change':>12s} {'Note'}")
    print(f"  {'-'*100}")
    for name, net, note in risks:
        change = net - base['net_benefit']
        pct = change / base['net_benefit'] * 100 if base['net_benefit'] != 0 else 0
        print(f"  {name:<40s} ${net/1000:>10.0f}k {pct:>+10.0f}%   {note}")
    print(f"  {'Base case':<40s} ${base['net_benefit']/1000:>10.0f}k {'':>12s}")

    print("\n  💡 CONCLUSION:")
    worst_overlay = ScenarioOverlay(label="worst", investment_return=0.04, mortgage_rate=0.07, inflation=0.04)
    best_overlay = ScenarioOverlay(label="best", investment_return=0.10, mortgage_rate=0.035, inflation=0.02)
    worst = run_scenario(config, worst_overlay)
    best = run_scenario(config, best_overlay)
    print(f"     Worst realistic case: ${worst['net_benefit']/1000:.0f}k")
    print(f"     Best realistic case: ${best['net_benefit']/1000:.0f}k")
    print(f"     Base case: ${base['net_benefit']/1000:.0f}k")
    print(f"     Break-even return: {be_zero*100:.1f}%")
    print()

    # Save CSVs (DP#15: output files go to ~/.cache, not repo root)
    from output_paths import output_path
    mc_path = output_path("monte_carlo_results.csv")
    tornado_path = output_path("tornado_results.csv")
    mc.to_csv(mc_path, index=False)
    tornado.to_csv(tornado_path, index=False)
    print(f"  📁 Monte Carlo results saved to {mc_path}")
    print(f"  📁 Tornado results saved to {tornado_path}")


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Sensitivity & Risk Analysis")
    parser.add_argument("--input", default="input.json")
    parser.add_argument("--template", nargs='?', const='-',
                        help='Output config template (use filename to save)')
    args = parser.parse_args()

    if args.template is not None:
        # Epic #603 Track C Phase 2b: see optimize.py's identical comment --
        # the template is the input contract's own example, not the
        # deleted legacy input_schema.json.
        import shutil
        schema_path = contract_schema.EXAMPLE_PATH
        if args.template == '-':
            with open(schema_path) as f:
                print(f.read())
        else:
            shutil.copy2(schema_path, args.template)
            print(f'Template written to {args.template}')
        sys.exit(0)

    # DP#14: Use the standard config pipeline instead of raw json.load
    config = SimulationConfig.from_json(args.input)
    cfg_dict = config.to_dict()  # For sensitivity_overlays access
    print_sensitivity_report(config, cfg_dict)