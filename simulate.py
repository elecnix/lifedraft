#!/usr/bin/env python3
"""
simulate.py — Single Entry Point (DP#16 → DP#14 → rank)

Wires discover_anchors → simulate → rank into one pipeline.
Replaces the 4 existing scripts with a unified entry point.

Flow:
1. Load input.json
2. Call discover_anchors(cfg) to auto-generate anchor scenarios
3. Compose all anchor × overlay combinations:
   income × mortgage × refinance × strategy × SM × DL × resp
4. For each combo, call _get_enumerate().evaluate_overlay(base_cfg, overlay)
5. Rank by net_benefit
6. Print report

Usage:
    python simulate.py
    python simulate.py --input input.json --top-n 20
    python simulate.py --mc 500 --sensitivity
    python simulate.py --csv --html report.html
"""

from tax_data import default_tax_provider
import argparse
import json
import math
import os
import sys

from dataclasses import replace

import numpy as np
from itertools import product
from typing import Dict, List, Optional

from scenario_discovery import discover_anchors
# DP#25 (#998): inject the simulation-layer callables scenario_discovery needs
# (marginal_rate, strategy types/engine, rate resolvers) so the scenario layer
# has ZERO runtime imports from tax_calculator / strategy / simulation_config.
# Importing simulation_deps configures the injection point at import time.
import simulation_deps  # noqa: F401  (import side-effect: configures scenario_discovery)
from simulation_config import (
    SimulationConfig, YearResult, ScenarioOverlay, build_overlay_config,
    has_readvanceable_facility, refinance_amortization_fallback,
)
from optimize import compute_net_benefit, evaluate_strategy_with_simulation
from countries.canada.resp_rules import RESPCalculator
from tax_calculator import marginal_rate


def load_inputs(path: str = "input.json") -> dict:
    """Load input configuration from JSON file."""
    with open(path) as f:
        return json.load(f)


def evaluate_overlay(base_cfg: dict, overlay: ScenarioOverlay, strategy_alloc: dict = None) -> Dict:
    """Evaluate a single overlay using the existing simulation modules.
    
    Uses the same pure functions the optimizer uses, but applied to
    one specific scenario at a time for independent verification.
    
    Args:
        base_cfg: Base configuration dict
        overlay: ScenarioOverlay defining the scenario parameters
        strategy_alloc: Dict with allocation percentages from discover_anchors['strategy'].
            If None, auto-discovers from base_cfg using discover_anchors.
    """
    cfg = build_overlay_config(base_cfg, overlay)
    config = SimulationConfig.from_dict(cfg)
    
    # Build rate path using the scenario's mortgage rate
    from countries.canada.rate_model import build_rate_path
    rate_path = build_rate_path(
        name=overlay.label[:40],
        initial_rate=overlay.mortgage_rate,
        term_years=config.projection_years,
        rate_type='variable',
        renewal_rates=[overlay.mortgage_rate],
    )
    
    # Strategy allocation from discover_anchors
    if strategy_alloc is None:
        anchors = discover_anchors(base_cfg)
        if anchors['strategy']:
            strategy_alloc = anchors['strategy'][0]
        else:
            strategy_alloc = {}
    
    # Strategy reflecting the overlay's SM and DL choices
    from strategy import AllocationStrategy
    strategy = AllocationStrategy(
        name=overlay.label[:30],
        rrsp_pct=strategy_alloc.get('rrsp_pct', 0.35),
        spousal_rrsp_pct=strategy_alloc.get('spousal_rrsp_pct', 0.10),
        tfsa_pct=strategy_alloc.get('tfsa_pct', 0.30),
        fhsa_pct=strategy_alloc.get('fhsa_pct', 0.0),
        resp_pct=strategy_alloc.get('resp_pct', 0.07),
        non_reg_pct=strategy_alloc.get('non_reg_pct', 0.18),
        prioritize_readvanceable=overlay.use_readvanceable,
        deduct_later=overlay.deduct_later,
    )
    
    # issue #257: invested lump sum = HELOC draw (pre-existing undrawn margin)
    # + cash-out proceeds (the mortgage refinance increase). margin_available is
    # NOT inflated by cash_out (see apply_overlay), so the cash-out is sourced
    # explicitly from the overlay here. This keeps invested capital == total new
    # debt (margin draw + cash_out), each counted once.
    #
    # issue #735: only overlay.draw_fraction of margin_available is actually
    # drawn and invested -- 0.0 by default (DP#32: an undrawn facility is
    # the normal state of a facility, not something this evaluator invents a
    # draw for). Sweep overlay.draw_fraction across several ScenarioOverlay
    # instances to see the trade-off; cash_out (a mortgage increase, #257) is
    # never scaled by it.
    lump_sum = config.margin_available * overlay.draw_fraction + overlay.cash_out
    free_cash = cfg.get('property', {}).get('free_cash', 0.0)
    
    result = evaluate_strategy_with_simulation(
        name=overlay.label[:50],
        strategy=strategy,
        config=config,
        rate_path=rate_path,
        use_readvanceable=overlay.use_readvanceable,
        deduct_later=overlay.deduct_later,
        lump_sum=lump_sum,
        free_cash=free_cash,
    )
    
    # Add overlay metadata
    result['label'] = overlay.label
    result['cash_out'] = overlay.cash_out
    result['resp_cash_out'] = overlay.resp_cash_out
    result['primary_income'] = overlay.primary_income

    result['spouse_income'] = overlay.spouse_income

    result['mortgage_rate'] = overlay.mortgage_rate
    result['use_readvanceable'] = overlay.use_readvanceable
    result['deduct_later'] = overlay.deduct_later
    result['ltv'] = overlay.ltv
    result['retirement_age'] = overlay.retirement_age

    return result


def print_results(results: List[Dict], title: str = "SCENARIO RESULTS", top_n: int = 20):
    """Print formatted comparison table."""
    print(f"\n{'=' * 140}")
    print(f"  📊 {title}")
    print(f"{'=' * 140}")
    
    sorted_results = sorted(results, key=lambda r: r.get('net_benefit', 0), reverse=True)
    
    header = (f"  {'#':<3} {'Scenario':<55} {'Loan-to-Value':>15} "
              f"{'Net Benefit':>13} {'Liquid NW':>12} {'Assets':>11} "
              f"{'Debt':>11} {'RRSP':>11} {'TFSA':>10} {'Non-Reg':>10}")
    print(header)
    print(f"  {'-' * 138}")
    
    for i, r in enumerate(sorted_results[:top_n]):
        label = r.get('label', '?')[:54]
        ltv = r.get('ltv', 0)
        nb = r.get('net_benefit', 0)
        lqw = r.get('future_value', 0) - r.get('total_debt', 0)
        assets = r.get('future_value', 0)
        debt = r.get('total_debt', 0)
        rrsp = r.get('RRSP', 0) + r.get('Spousal_RRSP', 0)
        tfsa = r.get('TFSA', 0)
        nonreg = r.get('Non_Reg', 0)
        
        print(f"  {i+1:<3} {label:<55} {ltv:>14.1%}  "
              f"${nb:>11,.0f}  ${lqw:>10,.0f}  ${assets:>9,.0f}  "
              f"${debt:>9,.0f}  ${rrsp:>9,.0f}  ${tfsa:>8,.0f}  ${nonreg:>8,.0f}")
    
    print()


def enumerate_overlays(base_cfg: dict, anchors: dict = None) -> List[ScenarioOverlay]:
    """Enumerate all meaningful decision combinations using discover_anchors (DP#16).
    
    Uses discover_anchors to auto-discover scenario dimensions from
    trigger rules, replacing hardcoded values with data-driven detection.
    
    Returns list of ScenarioOverlay objects, each representing one decision combo.
    """
    if anchors is None:
        anchors = discover_anchors(base_cfg)
    
    house_value = base_cfg['property']['house_value']
    orig_mortgage = base_cfg['property']['mortgage_balance']
    
    members = base_cfg['family']['members']
    primary = next(m for m in members if m['role'] == 'primary')
    
    # Refinance levels from discover_anchors
    _refi_abbrevs = {
        'no_refinance': 'No Refinance',
        'min_refi': 'Fill Registered Room',
        'max_refi': 'Maximum Refinance (80%)',
    }
    refi_levels = [
        (_refi_abbrevs.get(r['id'], r['label']), r['cash_out'])
        for r in anchors['refinance']
    ]
    
    # Income scenarios from discover_anchors
    income_scenarios = [
        (i['id'], i['label'], i['primary_income'], i['spouse_income'])
        for i in anchors['income']
    ]
    
    # Mortgage scenarios from discover_anchors
    mortgage_scenarios = [
        (m['id'], m['label'], m['rate'])
        for m in anchors['mortgage']
    ]
    
    # SM and DL options from discover_anchors
    sm_options = anchors['sm_options']
    deduct_later_options = anchors['deduct_later_options']
    # Expand SM options for comprehensive enumeration when property has equity
    if True not in sm_options and base_cfg['property'].get('margin_available', 0) > 0:
        sm_options = [True, False]
    # Expand DL options for comprehensive enumeration when bracket gap exists
    if True not in deduct_later_options:
        brackets = default_tax_provider().get_combined_brackets()
        members_list = base_cfg['family']['members']
        p_inc = next((m['gross_income'] for m in members_list if m['role'] == 'primary'), 0)
        s_inc = next((m['gross_income'] for m in members_list if m['role'] == 'spouse'), 0)
        if marginal_rate(p_inc, brackets) > marginal_rate(s_inc, brackets):
            deduct_later_options = [True, False]
    
    # Issue #936: the declared deposit products to sweep take-vs-leave.
    # `None` is the implicit "leave it" baseline (no product taken -- today's
    # behaviour); each declared product is a "take it" candidate the optimizer
    # ranks head-to-head against that baseline (DP#33: the declaration
    # annotates the sweep, it does not replace it). Empty when the household
    # declared none -> only the None baseline iterates, byte-identical to today
    # (DP#32).
    deposit_choices = [None] + list(anchors.get('deposit_products', []))

    overlays = []

    for refi_label, cash_out in refi_levels:
        for inc_id, inc_label, n_income, a_income in income_scenarios:
            for mort_id, mort_label, mort_rate in mortgage_scenarios:
                for use_readvanceable in sm_options:
                    for deduct_later in deduct_later_options:
                        for deposit_product in deposit_choices:
                            deposit_label = (
                                f" | +Deposit:{deposit_product.get('label', deposit_product['id'])}"
                                if deposit_product is not None else "")
                            label = f"{inc_label} | {mort_label} | {refi_label} | {'+SM' if use_readvanceable else 'no SM'} | {'+DL' if deduct_later else 'no DL'}{deposit_label}"

                            overlays.append(ScenarioOverlay(
                                label=label,
                                cash_out=cash_out,
                                primary_income=n_income,
                                spouse_income=a_income,
                                mortgage_rate=mort_rate,
                                use_readvanceable=use_readvanceable,
                                deduct_later=deduct_later,
                                ltv=(orig_mortgage + cash_out) / house_value if house_value > 0 else 0,
                                # issue #655: refi_levels are exploratory sweep
                                # points, not a specific declared refinance
                                # option -- DP#13 placeholder fallback.
                                refinance_amortization_years=refinance_amortization_fallback(base_cfg),
                                # issue #936: the taken product (None = leave it).
                                deposit_product=deposit_product,
                            ))
    
    # RESP cash-out overlays from discover_anchors
    calc = RESPCalculator()
    resp_collapse = calc.resp_collapse_proceeds(base_cfg, 
        marginal_rate(primary['gross_income'], default_tax_provider().get_combined_brackets()))
    resp_eap = calc.resp_eap_proceeds(base_cfg)
    
    resp_action_map = {
        'eap': ("RESP EAP (student)", resp_eap['net_proceeds']),
        'collapse': ("RESP Collapse (no student)", resp_collapse['net_proceeds']),
    }
    
    resp_overlay_types = []
    for action in anchors['resp_action']:
        if action in resp_action_map:
            resp_overlay_types.append(resp_action_map[action])
    
    for resp_label, resp_proceeds in resp_overlay_types:
        if resp_proceeds <= 0:
            continue
        for refi_label, cash_out in refi_levels:
            for inc_id, inc_label, n_income, a_income in income_scenarios:
                # Only do base mortgage rate for RESP scenarios
                for mort_id, mort_label, mort_rate in mortgage_scenarios[:1]:
                    for use_readvanceable in sm_options:
                        for deduct_later in deduct_later_options:
                            label = f"{inc_label} | {mort_label} | {refi_label} | {resp_label} | {'+SM' if use_readvanceable else 'no SM'} | {'+DL' if deduct_later else 'no DL'}"
                            overlays.append(ScenarioOverlay(
                                label=label,
                                cash_out=cash_out,
                                resp_cash_out=resp_proceeds,
                                primary_income=n_income,
                                spouse_income=a_income,
                                mortgage_rate=mort_rate,
                                use_readvanceable=use_readvanceable,
                                deduct_later=deduct_later,
                                ltv=(orig_mortgage + cash_out) / house_value if house_value > 0 else 0,
                                refinance_amortization_years=refinance_amortization_fallback(base_cfg),
                            ))
    
    return overlays


def build_all_overlays(base_cfg: dict, anchors: dict) -> List[Dict]:
    """Build all anchor × overlay combinations from discover_anchors output.

    Returns a list of dicts, each with keys:
        overlay: ScenarioOverlay
        strategy_alloc: dict with allocation percentages
    """
    house_value = base_cfg["property"]["house_value"]
    orig_mortgage = base_cfg["property"]["mortgage_balance"]

    combinations = []

    income_list = anchors.get("income", [])
    mortgage_list = anchors.get("mortgage", [])
    refinance_list = anchors.get("refinance", [])
    strategy_list = anchors.get("strategy", [])
    sm_options = anchors.get("sm_options", [False])
    dl_options = anchors.get("deduct_later_options", [False])
    resp_actions = anchors.get("resp_action", ["keep"])
    # Issue #303: retirement-age dimension. discover_anchors gates this to a
    # single baseline age on short horizons, so the loop adds no extra
    # combinations unless retirement is actually reachable (or forced).
    # DP#32 (#606): only a genuinely missing key falls back to the
    # single-None placeholder -- an explicit retirement_age=[] means "sweep
    # nothing" and legitimately produces zero combinations, not the default.
    retirement_ages = anchors.get("retirement_age")
    retirement_ages = [None] if retirement_ages is None else retirement_ages

    # Compute primary MTR for RESP collapse
    brackets = default_tax_provider().get_combined_brackets()
    members = base_cfg["family"]["members"]
    primary = next((m for m in members if m["role"] == "primary"), {})
    primary_mtr = marginal_rate(primary.get("gross_income", 0), brackets)

    # Pre-compute RESP proceeds for each action
    resp_proceeds_map = {"keep": 0.0}
    calc = RESPCalculator()
    for action in resp_actions:
        if action == "eap":
            resp_proceeds_map["eap"] = calc.resp_eap_proceeds(base_cfg)["net_proceeds"]
        elif action == "collapse":
            resp_proceeds_map["collapse"] = calc.resp_collapse_proceeds(
                base_cfg, primary_mtr
            )["net_proceeds"]

    # Only label/overlay the retirement age when the dimension actually varies
    # (more than one candidate). On the gated single-age case we leave it None so
    # labels and results are byte-identical to a run without this dimension.
    sweep_retirement = len(retirement_ages) > 1

    for inc in income_list:
        for mort in mortgage_list:
            for refi in refinance_list:
                for strat in strategy_list:
                    for use_sm in sm_options:
                        for deduct_later in dl_options:
                            for resp_action in resp_actions:
                              for ret_age in retirement_ages:
                                resp_cash = resp_proceeds_map.get(resp_action, 0.0)
                                overlay_ret_age = ret_age if sweep_retirement else None

                                # Build label
                                parts = [
                                    inc.get("label", "Income"),
                                    mort.get("label", "Mortgage"),
                                    refi.get("label", "Refi"),
                                    strat.get("label", "Strategy"),
                                    "SM: Yes" if use_sm else "SM: No",
                                    "Deduction: Staggered" if deduct_later else "Deduction: Immediate",
                                ]
                                if resp_action != "keep":
                                    parts.append(f"RESP {resp_action}")
                                if sweep_retirement:
                                    parts.append(f"Retire @ {ret_age}")
                                label = " | ".join(parts)

                                overlay = ScenarioOverlay(
                                    label=label,
                                    cash_out=refi.get("cash_out", 0.0),
                                    resp_cash_out=resp_cash,
                                    primary_income=inc.get("primary_income"),  # DP#18: None if not set → keeps base
                                    spouse_income=inc.get("spouse_income"),  # DP#18: None if not set → keeps base
                                    mortgage_rate=mort.get("rate", 0.05),  # DP#13: round-number placeholder
                                    use_readvanceable=use_sm,
                                    deduct_later=deduct_later,
                                    retirement_age=overlay_ret_age,
                                    ltv=(
                                        (orig_mortgage + refi.get("cash_out", 0.0))
                                        / house_value
                                    ),
                                    refinance_amortization_years=refinance_amortization_fallback(base_cfg),
                                )

                                strategy_alloc = {
                                    "id": strat.get("id", "unknown"),
                                    "label": strat.get("label", "Strategy"),
                                    "rrsp_pct": strat.get("rrsp_pct", 0.35),
                                    "spousal_rrsp_pct": strat.get("spousal_rrsp_pct", 0.10),
                                    "tfsa_pct": strat.get("tfsa_pct", 0.30),
                                    "fhsa_pct": strat.get("fhsa_pct", 0.0),
                                    "resp_pct": strat.get("resp_pct", 0.07),
                                    "non_reg_pct": strat.get("non_reg_pct", 0.18),
                                    "child_fhsa_pct": strat.get("child_fhsa_pct", 0.0),
                                    "child_tfsa_pct": strat.get("child_tfsa_pct", 0.0),
                                    "child_rrsp_pct": strat.get("child_rrsp_pct", 0.0),
                                }

                                combinations.append({
                                    "overlay": overlay,
                                    "strategy_alloc": strategy_alloc,
                                })

    return combinations


# ──────────────────────────────────────────────────────
# Sensitivity sweep
# ──────────────────────────────────────────────────────

def run_sensitivity(
    base_cfg: dict,
    results: List[Dict],
    anchors: dict,
    return_range: Optional[List[float]] = None,
) -> List[Dict]:
    """Run return-rate sensitivity sweep on top anchor combinations.

    Returns additional result dicts for each return-rate overlay.
    """
    if return_range is None:
        return_range = base_cfg.get("sensitivity_overlays", {}).get(
            "investment_return", [0.04, 0.055, 0.07, 0.085, 0.10]
        )

    sensitivity_results = []

    # Pick top-3 non-RESP scenarios as anchors for sensitivity
    baseline = [r for r in results if r.get("resp_cash_out", 0) == 0]
    top3 = sorted(baseline, key=lambda r: r.get("net_benefit", 0), reverse=True)[:3]

    for base_result in top3:
        for ret in return_range:
            overlay = ScenarioOverlay(
                label=f"r={ret:.0%} | {base_result.get('label', '?')[:60]}",
                cash_out=base_result.get("cash_out", 0),
                resp_cash_out=0.0,
                primary_income=base_result.get("primary_income"),  # DP#18: None if not set → keeps base
                spouse_income=base_result.get("spouse_income"),  # DP#18: None if not set → keeps base
                mortgage_rate=base_result.get("mortgage_rate", 0.05),  # DP#13: round-number placeholder
                use_readvanceable=base_result.get("use_readvanceable", False),
                deduct_later=base_result.get("deduct_later", False),
                investment_return=ret,
                ltv=base_result.get("ltv", 0.0),
                refinance_amortization_years=refinance_amortization_fallback(base_cfg),
            )

            # Find the matching strategy alloc from anchors
            strat_id = base_result.get("strategy_id", "")
            strat_match = next(
                (s for s in anchors.get("strategy", []) if s.get("id") == strat_id),
                anchors["strategy"][0] if anchors.get("strategy") else {},
            )
            strategy_alloc = {
                "id": strat_match.get("id", "unknown"),
                "label": strat_match.get("label", "Strategy"),
                "rrsp_pct": strat_match.get("rrsp_pct", 0.35),
                "spousal_rrsp_pct": strat_match.get("spousal_rrsp_pct", 0.10),
                "tfsa_pct": strat_match.get("tfsa_pct", 0.30),
                "fhsa_pct": strat_match.get("fhsa_pct", 0.0),
                "resp_pct": strat_match.get("resp_pct", 0.07),
                "non_reg_pct": strat_match.get("non_reg_pct", 0.18),
            }

            result = evaluate_overlay(base_cfg, overlay, strategy_alloc=strategy_alloc)
            result["sensitivity_return"] = ret
            sensitivity_results.append(result)

    # Print summary
    print(f"\n{'=' * 100}")
    print(f"  📈 SENSITIVITY ANALYSIS (return rate sweep)")
    print(f"{'=' * 100}")
    print(
        f"  {'Return':>6} {'Strategy':>12} {'Scenario':<55} {'Net Benefit':>13} {'Verdict':>12}"
    )
    print(f"  {'-' * 100}")

    # Group by scenario to show cross-over
    from collections import defaultdict
    by_scenario = defaultdict(list)
    for r in sensitivity_results:
        # Strip the return prefix from label for grouping
        base_label = r.get("label", "").split(" | ", 1)[-1] if " | " in r.get("label", "") else r.get("label", "")
        by_scenario[base_label].append(r)

    for scenario_label, group in by_scenario.items():
        group.sort(key=lambda r: r.get("sensitivity_return", 0))
        for r in group:
            ret = r.get("sensitivity_return", 0)
            nb = r.get("net_benefit", 0)
            sm_tag = "+SM" if r.get("use_readvanceable") else "no SM"
            dl_tag = "+DL" if r.get("deduct_later") else "no DL"
            verdict = "✅" if nb > 0 else "❌"
            print(
                f"  {ret:>5.0%}  {sm_tag:>5} {dl_tag:>4}  "
                f"{scenario_label[:54]:<55}  ${nb:>11,.0f}  {verdict:>12}"
            )
        print()

    return sensitivity_results


# ──────────────────────────────────────────────────────
# Mortgage-rate (variable / fixed) sensitivity — issue #548
# ──────────────────────────────────────────────────────
#
# Modeling note (issue #548): build_rate_path holds a variable rate FLAT at its
# initial value for the whole term (countries/canada/rate_model.py emits a single
# step at initial_rate for years 0..term, and evaluate_overlay passes
# renewal_rates=[mortgage_rate]). So sweeping the mortgage term rate here is
# exactly equivalent to sweeping the AVERAGE realized rate over the term — the
# output labels it as such.

def _mortgage_rate_range(
    base_cfg: dict, rate_range: Optional[List[float]] = None
) -> List[float]:
    """Resolve the mortgage-rate sweep range (DP#2: config, not code).

    Precedence: explicit ``rate_range`` argument > config
    ``sensitivity_overlays.mortgage_rates`` > round-number fallback (DP#13).
    """
    if rate_range is not None:
        return list(rate_range)
    configured = base_cfg.get("sensitivity_overlays", {}).get("mortgage_rates")
    if configured:
        return sorted(configured)
    # DP#13: round-number placeholders, used only when the config is silent.
    return [0.03, 0.04, 0.05, 0.06, 0.07]


def mortgage_rate_sweep(
    base_cfg: dict,
    anchor_overlay: ScenarioOverlay,
    strategy_alloc: Optional[dict] = None,
    rate_range: Optional[List[float]] = None,
) -> List[Dict]:
    """Sweep the mortgage TERM rate for one anchor scenario (DP#3, DP#18).

    For each rate, overlay it on the anchor (everything else held fixed) and
    evaluate. The swept rate is the average rate over the term (see module note),
    recorded as ``avg_rate_over_term`` on each row.

    Pure function: same inputs → same rows. Reuses ``evaluate_overlay`` rather
    than re-implementing the simulation.
    """
    rates = _mortgage_rate_range(base_cfg, rate_range)
    rows = []
    for rate in rates:
        overlay = replace(anchor_overlay, mortgage_rate=rate)
        result = evaluate_overlay(base_cfg, overlay, strategy_alloc=strategy_alloc)
        # The swept rate is held flat for the term, so it IS the average rate.
        result["avg_rate_over_term"] = rate
        rows.append(result)
    return rows


def break_even_mortgage_rate(
    base_cfg: dict,
    anchor_overlay: ScenarioOverlay,
    strategy_alloc: Optional[dict] = None,
    reference_net_benefit: float = 0.0,
    lo: float = 0.0001,
    hi: float = 0.20,
    iterations: int = 50,
) -> Optional[float]:
    """Find the average term rate at which the anchor's net benefit equals a
    reference (e.g. the best fixed option's net benefit) (DP#3).

    Net benefit is monotonically non-increasing in the mortgage rate for a
    leveraged strategy, so the crossing — if one exists in [lo, hi] — is
    bracketed by bisection.

    There is a break-even in the range only when the anchor sits ABOVE the
    reference at the low end and AT/BELOW it at the high end. If even the lowest
    rate cannot lift the anchor's net benefit up to the reference
    (``net_at(lo) <= reference``), the reference is unreachable and there is no
    break-even — return ``None`` rather than collapsing the search toward 0
    (which floating-point underflows the amortization annuity factor to 1.0 and
    raises ZeroDivisionError). If the anchor still beats the reference at the
    high end (``net_at(hi) > reference``), the crossing is above ``hi`` — also
    return ``None``.

    The search floor is clamped to a small non-zero rate so a 0% mortgage rate
    (a degenerate annuity) is never evaluated.

    Returns the break-even rate in [lo, hi], or ``None`` if no break-even exists
    within the bounds.
    """
    # Clamp the floor above zero so the annuity factor never underflows to 1.0.
    lo = max(lo, 0.0001)

    def net_at(rate: float) -> float:
        overlay = replace(anchor_overlay, mortgage_rate=rate)
        return evaluate_overlay(base_cfg, overlay, strategy_alloc=strategy_alloc)[
            "net_benefit"
        ]

    # net_at is non-increasing in rate. A break-even exists in [lo, hi] only if
    # the anchor is above the reference at lo and at/below it at hi.
    if net_at(lo) <= reference_net_benefit:
        # Even the cheapest mortgage can't reach the reference: no break-even.
        return None
    if net_at(hi) > reference_net_benefit:
        # Still beats the reference at the top of the range: crossing is above hi.
        return None

    for _ in range(iterations):
        mid = (lo + hi) / 2
        # Above the break-even rate the net benefit dips below the reference, so
        # search lower; otherwise search up.
        if net_at(mid) > reference_net_benefit:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def run_mortgage_sensitivity(
    base_cfg: dict,
    results: List[Dict],
    anchors: dict,
    rate_range: Optional[List[float]] = None,
) -> List[Dict]:
    """Print a mortgage-rate sweep table + break-even rate vs the best fixed
    option, for the top scenarios (issue #548).

    The CLI driver; the sweep/break-even math lives in the pure functions above.
    """
    rates = _mortgage_rate_range(base_cfg, rate_range)

    baseline = [r for r in results if r.get("resp_cash_out", 0) == 0]
    top3 = sorted(baseline, key=lambda r: r.get("net_benefit", 0), reverse=True)[:3]

    print(f"\n{'=' * 100}")
    print("  🏦 MORTGAGE-RATE SENSITIVITY (average rate over the term)")
    print(f"{'=' * 100}")
    print(
        "  Note: the model holds the term rate flat, so each swept rate equals\n"
        "  the AVERAGE realized rate over the term (fixed-term, or a variable\n"
        "  term whose average lands at this level)."
    )

    all_rows: List[Dict] = []
    for base_result in top3:
        label = base_result.get("label", "?")
        anchor_overlay = ScenarioOverlay(
            label=label,
            cash_out=base_result.get("cash_out", 0),
            resp_cash_out=0.0,
            primary_income=base_result.get("primary_income"),
            spouse_income=base_result.get("spouse_income"),
            mortgage_rate=base_result.get("mortgage_rate", 0.05),
            use_readvanceable=base_result.get("use_readvanceable", False),
            deduct_later=base_result.get("deduct_later", False),
            investment_return=base_result.get("investment_return"),
            ltv=base_result.get("ltv", 0.0),
            refinance_amortization_years=refinance_amortization_fallback(base_cfg),
        )

        strat_id = base_result.get("strategy_id", "")
        strat_match = next(
            (s for s in anchors.get("strategy", []) if s.get("id") == strat_id),
            anchors["strategy"][0] if anchors.get("strategy") else {},
        )

        rows = mortgage_rate_sweep(
            base_cfg, anchor_overlay, strategy_alloc=strat_match, rate_range=rates
        )
        for r in rows:
            r["sensitivity_label"] = label
        all_rows.extend(rows)

        print(f"\n  Scenario: {label[:80]}")
        print(
            f"    {'Avg rate / term':>16} {'Net Benefit':>14} {'Verdict':>9}"
        )
        print(f"    {'-' * 45}")
        for r in rows:
            nb = r.get("net_benefit", 0)
            verdict = "✅" if nb > 0 else "❌"
            print(
                f"    {r['avg_rate_over_term']:>15.2%}  ${nb:>12,.0f}  {verdict:>8}"
            )

        # Break-even vs the best fixed option = the best net benefit among
        # the other top scenarios (the fixed alternative to beat).
        others = [r for r in top3 if r is not base_result]
        if others:
            best_fixed = max(others, key=lambda r: r.get("net_benefit", 0))
            be = break_even_mortgage_rate(
                base_cfg,
                anchor_overlay,
                strategy_alloc=strat_match,
                reference_net_benefit=best_fixed.get("net_benefit", 0),
            )
            best_label = best_fixed.get("label", "?")[:30]
            if be is None:
                # No mortgage rate in the search range makes this anchor match the
                # best alternative (it never catches up, or always beats it).
                print(
                    f"    Break-even avg rate vs best alternative "
                    f"({best_label}): no break-even within range"
                )
            else:
                print(
                    f"    Break-even avg rate vs best alternative "
                    f"({best_label}): {be:.2%}"
                )

    return all_rows


# ──────────────────────────────────────────────────────
# Monte Carlo
# ──────────────────────────────────────────────────────

def run_monte_carlo(
    base_cfg: dict,
    results: List[Dict],
    anchors: dict,
    n_paths: int = 500,
    return_sigma: float = 0.15,
    seed: int = 42,
) -> List[Dict]:
    """Monte Carlo simulation for top scenarios.

    Runs stochastic return paths and reports P10/P50/P90 and P(loss).
    """
    print(f"\n{'=' * 100}")
    print(f"  🎲 MONTE CARLO: {n_paths} stochastic paths (top scenarios)")
    print(f"{'=' * 100}")

    baseline = [r for r in results if r.get("resp_cash_out", 0) == 0]
    top3 = sorted(baseline, key=lambda r: r.get("net_benefit", 0), reverse=True)[:3]

    if not top3:
        print("  No baseline scenarios found.")
        return []

    base_return = base_cfg.get("assumptions", {}).get("investment_return", 0.07)
    annual_vol = return_sigma
    rng = np.random.default_rng(seed)

    mc_summary = []

    for i, r in enumerate(top3):
        label = r.get("label", "?")[:50]
        sm = r.get("use_readvanceable", False)
        dl = r.get("deduct_later", False)

        # Find matching strategy alloc
        strat_id = r.get("strategy_id", "")
        strat_match = next(
            (s for s in anchors.get("strategy", []) if s.get("id") == strat_id),
            anchors["strategy"][0] if anchors.get("strategy") else {},
        )
        strategy_alloc = {
            "id": strat_match.get("id", "unknown"),
            "label": strat_match.get("label", "Strategy"),
            "rrsp_pct": strat_match.get("rrsp_pct", 0.35),
            "spousal_rrsp_pct": strat_match.get("spousal_rrsp_pct", 0.10),
            "tfsa_pct": strat_match.get("tfsa_pct", 0.30),
            "fhsa_pct": strat_match.get("fhsa_pct", 0.0),
            "resp_pct": strat_match.get("resp_pct", 0.07),
            "non_reg_pct": strat_match.get("non_reg_pct", 0.18),
        }

        path_results = []
        for _ in range(n_paths):
            mu = math.log(1 + base_return) - 0.5 * annual_vol ** 2
            annual_ret = math.exp(rng.normal(mu, annual_vol)) - 1

            overlay = ScenarioOverlay(
                label=f"MC-{i}",
                cash_out=r.get("cash_out", 0),
                resp_cash_out=0.0,
                primary_income=r.get("primary_income"),  # DP#18: None if not set → keeps base
                spouse_income=r.get("spouse_income"),  # DP#18: None if not set → keeps base
                mortgage_rate=r.get("mortgage_rate", 0.05),  # DP#13: round-number placeholder
                use_readvanceable=sm,
                deduct_later=dl,
                investment_return=annual_ret,
                ltv=r.get("ltv", 0.0),
                refinance_amortization_years=refinance_amortization_fallback(base_cfg),
            )

            sim_result = evaluate_overlay(base_cfg, overlay, strategy_alloc=strategy_alloc)
            path_results.append(sim_result["net_benefit"])

        path_results.sort()
        p10 = path_results[int(n_paths * 0.10)]
        p50 = path_results[int(n_paths * 0.50)]
        p90 = path_results[int(n_paths * 0.90)]
        p_loss = sum(1 for nb in path_results if nb < 0) / n_paths

        sm_label = "+SM" if sm else "no SM"
        dl_label = "+DL" if dl else "no DL"
        print(f"\n  Scenario {i + 1}: {sm_label} {dl_label} (base=${r['net_benefit']:,.0f})")
        print(f"    P10: $    {p10:>12,.0f}  (worst 10%)")
        print(f"    P50: $    {p50:>12,.0f}  (median)")
        print(f"    P90: $    {p90:>12,.0f}  (best 10%)")
        print(f"    P(loss):     {p_loss:>8.1%}  (negative outcome)")

        mc_summary.append({
            "scenario_index": i + 1,
            "label": label,
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "p_loss": p_loss,
        })

    print()
    return mc_summary


# ──────────────────────────────────────────────────────
# Output exporters
# ──────────────────────────────────────────────────────

def print_category_comparison(results: List[Dict]):
    """Print best result in each decision category."""
    print(f"\n{'=' * 100}")
    print(f"  🏆 BEST PER CATEGORY")
    print(f"{'=' * 100}")
    
    categories = [
        ("No Refinance, no Readvanceable, no Stagger", lambda r: r['cash_out'] == 0 and not r['use_readvanceable'] and not r['deduct_later']),
        ("No Refinance, no Readvanceable, Stagger", lambda r: r['cash_out'] == 0 and not r['use_readvanceable'] and r['deduct_later']),
        ("No Refinance, Readvanceable, no Stagger", lambda r: r['cash_out'] == 0 and r['use_readvanceable'] and not r['deduct_later']),
        ("No Refinance, Readvanceable, Stagger", lambda r: r['cash_out'] == 0 and r['use_readvanceable'] and r['deduct_later']),
        ("Fill Room, no Readvanceable, Stagger", lambda r: r['cash_out'] > 0 and r['cash_out'] < 400000 and not r['use_readvanceable'] and r['deduct_later']),
        ("Fill Room, Readvanceable, Stagger", lambda r: r['cash_out'] > 0 and r['cash_out'] < 400000 and r['use_readvanceable'] and r['deduct_later']),
        ("Maximum Refinance (80%), no Readvanceable, Stagger", lambda r: r['cash_out'] > 400000 and not r['use_readvanceable'] and r['deduct_later']),
        ("Maximum Refinance (80%), Readvanceable, Stagger", lambda r: r['cash_out'] > 400000 and r['use_readvanceable'] and r['deduct_later']),
    ]
    
    for cat, pred in categories:
        matches = [r for r in results if pred(r) and r.get('resp_cash_out', 0) == 0]
        if matches:
            best = max(matches, key=lambda r: r.get('net_benefit', 0))
            print(f"  {cat:<45s}: ${best.get('net_benefit', 0):>12,.0f}")
    
    print()


def print_optimal_refi_level(results: List[Dict]):
    """Find optimal refinance level per strategy combination."""
    print(f"\n{'=' * 100}")
    print(f"  🎯 OPTIMAL REFINANCE LEVEL")
    print(f"{'=' * 100}")
    
    # Group by deduct_later and use_readvanceable
    for dl, dl_label in [(True, "Yes (Stagger Years)"), (False, "No")]:
        for sm, sm_label in [(True, "Yes (Readvanceable)"), (False, "No")]:
            filtered = [r for r in results 
                       if r['deduct_later'] == dl and r['use_readvanceable'] == sm 
                       and r.get('resp_cash_out', 0) == 0]
            if not filtered:
                continue
            
            # Group by cash_out level
            no_refi = [r for r in filtered if r['cash_out'] == 0]
            min_refi = [r for r in filtered if 0 < r['cash_out'] < 400000]
            max_refi = [r for r in filtered if r['cash_out'] > 400000]
            
            best_no = max(no_refi, key=lambda r: r['net_benefit']) if no_refi else None
            best_min = max(min_refi, key=lambda r: r['net_benefit']) if min_refi else None
            best_max = max(max_refi, key=lambda r: r['net_benefit']) if max_refi else None
            
            scores = {}
            if best_no: scores['No Refinance'] = best_no['net_benefit']
            if best_min: scores['Fill Registered Room'] = best_min['net_benefit']
            if best_max: scores['Maximum Refinance (80%)'] = best_max['net_benefit']
            
            if scores:
                best_level = max(scores, key=scores.get)
                print(f"  {sm_label} {dl_label}: "
                      f"No Refinance ${scores.get('No Refinance', 0):>10,.0f} | "
                      f"Fill Room ${scores.get('Fill Registered Room', 0):>10,.0f} | "
                      f"Max (${scores.get('Maximum Refinance (80%)', 0):>10,.0f}) | "
                      f"→ {best_level}")
    
    print()


def print_retirement_age_comparison(results: List[Dict]):
    """Issue #303: rank retirement ages by best net benefit / terminal net worth.

    Only prints when the retirement-age dimension actually varied (more than one
    distinct, non-null retirement_age across results). For each candidate age it
    reports the best-performing scenario's net benefit and terminal net worth so
    the user sees the tradeoff of retiring earlier vs later.
    """
    ages = sorted({r.get('retirement_age') for r in results
                   if r.get('retirement_age') is not None})
    if len(ages) < 2:
        return

    print(f"\n{'=' * 100}")
    print(f"  🏖️  RETIREMENT-AGE COMPARISON (Issue #303)")
    print(f"{'=' * 100}")
    print(f"  {'Retire @':>9}  {'Best Net Benefit':>18}  {'Terminal Net Worth':>20}")
    print(f"  {'-' * 96}")

    rows = []
    for age in ages:
        subset = [r for r in results if r.get('retirement_age') == age]
        best = max(subset, key=lambda r: r.get('net_benefit', 0))
        terminal_nw = best.get('future_value', 0) - best.get('total_debt', 0)
        rows.append((age, best.get('net_benefit', 0), terminal_nw))

    for age, nb, nw in rows:
        print(f"  {age:>9}  ${nb:>16,.0f}  ${nw:>18,.0f}")

    # Tradeoff vs the latest age (most accumulation): show the cost of retiring
    # earlier relative to the latest candidate.
    latest_nb = rows[-1][1]
    print(f"  {'-' * 96}")
    for age, nb, _ in rows[:-1]:
        delta = nb - latest_nb
        print(f"  Retiring at {age} vs {rows[-1][0]}: "
              f"{'+' if delta >= 0 else '-'}${abs(delta):,.0f} net benefit")
    print()


def print_resp_cashout_analysis(results: List[Dict], base_cfg: dict, n_mtr: float):
    """RESP cash-out decision analysis using actual scenario results."""
    print(f"\n{'=' * 100}")
    print(f"  📚 RESP CASH-OUT ANALYSIS")
    print(f"{'=' * 100}")
    
    resp_balance = base_cfg['accounts'].get('resp_current_balance', 0)
    calc = RESPCalculator()
    
    # ── A. RESP Collapse (no student enrolled) ──
    collapse = calc.resp_collapse_proceeds(base_cfg, n_mtr)
    print(f"\n  ── RESP Collapse (no student enrolled) ── ITA s.146.1 + s.204.9")
    print(f"  Contributions returned:  ${collapse['contributions_returned']:>12,.2f}  (tax-free)")
    print(f"  CESG + QESI clawback:    ${collapse['grant_clawback']:>12,.2f}  (returned to government)")
    print(f"  Investment earnings:      ${collapse['earnings_taxed']:>12,.2f}")
    print(f"  AIP penalty tax:          ${collapse['tax_cost']:>12,.2f}  (MTR {n_mtr:.1%} + 20% penalty = {collapse['effective_tax_rate']:.1%})")
    print(f"  ──────────────────────────────────────────────")
    print(f"  Net collapse proceeds:    ${collapse['net_proceeds']:>12,.2f}")
    print(f"  Effective loss:           ${resp_balance - collapse['net_proceeds']:>12,.2f}  ({(resp_balance - collapse['net_proceeds'])/resp_balance:.1%} of balance)")
    
    # ── B. RESP EAP (student enrolled) ──
    eap = calc.resp_eap_proceeds(base_cfg)
    print(f"\n  ── RESP EAP Withdrawal (student enrolled) ──")
    print(f"  Contributions returned:  ${eap['contributions_returned']:>12,.2f}  (tax-free)")
    print(f"  EAP (grants + earnings):  ${eap['earnings_taxed']:>12,.2f}")
    print(f"  Student tax:              ${eap['tax_cost']:>12,.2f}  (at {eap['effective_tax_rate']:.0%} student MTR)")
    print(f"  ──────────────────────────────────────────────")
    print(f"  Net EAP proceeds:         ${eap['net_proceeds']:>12,.2f}")
    print(f"  Effective loss:           ${resp_balance - eap['net_proceeds']:>12,.2f}  ({(resp_balance - eap['net_proceeds'])/resp_balance:.1%} of balance)")
    
    # ── C. Scenario comparison ──
    print(f"\n  ── Scenario Comparison (Keep RESP vs Collapse vs EAP) ──")
    
    # Compare per refi level: keep vs EAP vs collapse
    for refi_label, cash_out in [("No Refi", 0), ("80% LTV", 500000)]:
        margin = 50000 if cash_out == 0 else 0  # No Refi: all cash-outs are 0
        keep_results = [r for r in results 
                       if abs(r.get('cash_out', 0) - cash_out) < margin
                       and r.get('resp_cash_out', 0) == 0]
        eap_results = [r for r in results 
                      if abs(r.get('cash_out', 0) - cash_out) < margin
                      and r.get('resp_cash_out', 0) > 0
                      and 'EAP' in r.get('label', '')]
        collapse_results = [r for r in results 
                          if abs(r.get('cash_out', 0) - cash_out) < margin
                          and r.get('resp_cash_out', 0) > 0
                          and 'Collapse' in r.get('label', '')]
        
        if keep_results:
            best_keep = max(keep_results, key=lambda r: r.get('net_benefit', 0))
            print(f"  {refi_label}:")
            print(f"    Keep RESP:      ${best_keep['net_benefit']:>10,.0f}")
            if eap_results:
                best_eap = max(eap_results, key=lambda r: r.get('net_benefit', 0))
                diff_eap = best_eap['net_benefit'] - best_keep['net_benefit']
                wins_eap = "EAP wins" if diff_eap > 0 else "Keep RESP"
                print(f"    RESP → EAP:     ${best_eap['net_benefit']:>10,.0f}  Δ ${diff_eap:>+10,.0f} → {wins_eap}")
            if collapse_results:
                best_col = max(collapse_results, key=lambda r: r.get('net_benefit', 0))
                diff_col = best_col['net_benefit'] - best_keep['net_benefit']
                wins_col = "Collapse wins" if diff_col > 0 else "Keep RESP"
                print(f"    RESP ↘ Collapse: ${best_col['net_benefit']:>10,.0f}  Δ ${diff_col:>+10,.0f} → {wins_col}")
    
    print()


def export_csv(results: List[Dict], path: str):
    """Export results to CSV."""
    import csv

    sorted_results = sorted(results, key=lambda r: r.get("net_benefit", 0), reverse=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "rank", "label", "strategy_id", "cash_out", "resp_cash_out",
            "use_readvanceable", "deduct_later", "ltv", "net_benefit",
            "future_value", "total_debt", "RRSP", "Spousal_RRSP", "TFSA",
            "Non_Reg", "RESP",
        ])
        for i, r in enumerate(sorted_results):
            writer.writerow([
                i + 1,
                r.get("label", ""),
                r.get("strategy_id", ""),
                r.get("cash_out", 0),
                r.get("resp_cash_out", 0),
                r.get("use_readvanceable", False),
                r.get("deduct_later", False),
                f"{r.get('ltv', 0):.3f}" if r.get("ltv") else "",
                r.get("net_benefit", 0),
                r.get("future_value", 0),
                r.get("total_debt", 0),
                r.get("RRSP", 0),
                r.get("Spousal_RRSP", 0),
                r.get("TFSA", 0),
                r.get("Non_Reg", 0),
                r.get("RESP", 0),
            ])
    print(f"  📁 CSV: {path}")

    # Year-by-year tidy long-format export (issue #248): one row per
    # (scenario, year) for the top scenario. Written to a sibling file so the
    # ranked-summary CSV keeps its existing schema.
    from output_plugins import write_year_by_year_csv
    if path.endswith(".csv"):
        yby_path = path[:-4] + "_year_by_year.csv"
    else:
        yby_path = path + "_year_by_year.csv"
    n_rows = write_year_by_year_csv(results, yby_path)
    if n_rows:
        print(f"  📁 CSV (year-by-year): {yby_path}")


def export_json(results: List[Dict], base_cfg: dict, path: str, **metadata):
    """Export results to JSON."""
    sorted_results = sorted(results, key=lambda r: r.get("net_benefit", 0), reverse=True)

    # Strip non-serializable entries
    clean = []
    for r in sorted_results:
        entry = {k: v for k, v in r.items() if isinstance(v, (int, float, str, bool, list, dict, type(None)))}
        clean.append(entry)

    # Convenience: surface the #1 scenario's per-year series at top level
    # so consumers don't have to re-sort (issue #248). Each scenario in
    # `results` already carries its own `year_by_year` array.
    top_year_by_year = clean[0].get("year_by_year", []) if clean else []

    output = {
        "title": "Refinance Scenario Analysis",
        "metadata": metadata,
        "count": len(clean),
        "year_by_year": top_year_by_year,
        "results": clean,
    }
    with open(path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  📋 JSON: {path}")


def export_html(results: List[Dict], base_cfg: dict, path: str):
    """Export results to standalone HTML report."""
    from output_plugins import HtmlReport

    sorted_results = sorted(results, key=lambda r: r.get("net_benefit", 0), reverse=True)
    report = HtmlReport(sorted_results, base_cfg, title="🏠 Refinance Scenario Analysis")
    report.write(path)
    print(f"  🌐 HTML: {path}")


# ──────────────────────────────────────────────────────
# Scipy optimizer integration (replacing decide_refinance.py)
# ──────────────────────────────────────────────────────

def run_scipy_optimization(base_cfg: dict, anchors: dict) -> tuple:
    """Run scipy continuous optimization for LTV and allocations.
    
    Returns (results, scipy_results) tuple.
    """
    from scipy_optimizer import ScipyOptimizer
    from monte_carlo_optimizer import MonteCarloOptimizer
    from return_model import FixedReturn, StochasticReturn
    from objective import MAX_NET_BENEFIT
    from countries.canada.rate_model import build_rate_path
    from strategy import AllocationStrategy, StrategyType
    from countries.canada.cashout_optimizer import compute_min_extraction
    import numpy as np
    
    config = SimulationConfig.from_dict(base_cfg)
    rp = build_rate_path(
        "Current variable", config.mortgage_rate,
        config.projection_years, 'variable', [config.mortgage_rate],
    )
    
    prop = base_cfg["property"]
    calc = RESPCalculator()
    
    print(f"\n  🔍 ScipyOptimizer: searching continuous LTV ∈ [0, 80%]...")
    
    best_scipy = None
    best_score = float('-inf')
    scipy_results = []
    
    for sd in anchors.get("strategy", []):
        strategy = AllocationStrategy(
            name=sd["label"],
            strategy_type=StrategyType.CUSTOM,
            rrsp_pct=sd["rrsp_pct"],
            spousal_rrsp_pct=sd["spousal_rrsp_pct"],
            tfsa_pct=sd["tfsa_pct"],
            fhsa_pct=sd["fhsa_pct"],
            resp_pct=sd["resp_pct"],
            non_reg_pct=sd["non_reg_pct"],
            prioritize_readvanceable=sd["prioritize_readvanceable"],
            deduct_later=sd["deduct_later"],
        )
        for use_readvanceable in anchors.get("sm_options", [False]):
            for deduct_later in anchors.get("deduct_later_options", [False]):
                scipy_opt = ScipyOptimizer(
                    config,
                    return_model=FixedReturn(config.investment_return),
                    rate_path=rp,
                    optimize_vars=['ltv'],
                )
                results = scipy_opt.optimize(
                    strategies=[strategy],
                    objective=MAX_NET_BENEFIT,
                    use_readvanceable=use_readvanceable,
                    deduct_later=deduct_later,
                )
                for sr in results:
                    sr.config_overrides['strategy'] = strategy.name
                    sr.config_overrides['use_readvanceable'] = use_readvanceable
                    sr.config_overrides['deduct_later'] = deduct_later
                    scipy_results.append(sr)
                    if sr.score > best_score:
                        best_score = sr.score
                        best_scipy = sr
    
    # Convert scipy results to same format as grid enumeration
    results = []
    for sr in scipy_results:
        co = sr.config_overrides
        ltv = sr.optimal_params.get('ltv', 0)
        cashout = max(0, ltv * prop['house_value'] - prop['mortgage_balance'])
        results.append({
            'label': f"{co.get('strategy','')} | SM={'on' if co.get('use_readvanceable') else 'off'} | DL={'on' if co.get('deduct_later') else 'off'}",
            'cash_out': cashout,
            'resp_cash_out': 0,
            'use_readvanceable': co.get('use_readvanceable', False),
            'deduct_later': co.get('deduct_later', False),
            'ltv': ltv,
            'net_benefit': sr.score,
            'future_value': sr.score,
            'total_debt': 0,
        })
    
    return results, scipy_results


def print_scipy_report(scipy_results: list, base_cfg: dict, n_mtr: float, n_income: float):
    """Print decision report from scipy optimization results."""
    import numpy as np
    from countries.canada.cashout_optimizer import compute_min_extraction
    
    if not scipy_results:
        return
    
    best = max(scipy_results, key=lambda r: r.score)
    co = best.config_overrides
    opt_ltv = best.optimal_params.get('ltv', 0)
    opt_cash = max(0, opt_ltv * base_cfg['property']['house_value'] - base_cfg['property']['mortgage_balance'])
    
    print(f"\n{'=' * 120}")
    print("🏠  OPTIMIZER RESULTS — Continuous loan-to-value search")
    print(f"{'=' * 120}")
    
    print(f"\n  🎯 OPTIMAL LOAN-TO-VALUE (found by scipy optimizer)")
    print(f"     {'─' * 90}")
    print(f"     Optimal loan-to-value:    {opt_ltv:>10.1%}")
    print(f"     Cash-out:                 ${opt_cash:>10,.0f}")
    print(f"     Score:                    ${best.score:>10,.0f}")
    print(f"     Convergence:              {best.convergence}")
    print(f"     Evaluations:              {best.n_evaluations}")
    
    if opt_ltv <= 0.001:
        print(f"\n     ⛔ OPTIMIZER SAYS: DO NOT REFINANCE")
        print(f"        The optimal loan-to-value is ~0% — keeping current mortgage is best.")
    elif opt_ltv >= 0.79:
        print(f"\n     ✅ OPTIMIZER SAYS: REFINANCE TO 80% LOAN-TO-VALUE")
        print(f"        Borrow ${opt_cash:,.0f} and invest the excess.")
    else:
        print(f"\n     ⚠️  OPTIMIZER SAYS: PARTIAL REFINANCE")
        print(f"        Refinance to {opt_ltv:.1%} loan-to-value, cash out ${opt_cash:,.0f}.")


# ──────────────────────────────────────────────────────
# Discovery report
# ──────────────────────────────────────────────────────

def print_discovery(anchors: dict, cfg: dict = None):
    """Print what discover_anchors found.

    ``cfg`` (issue #846) is the config the anchors were discovered FROM. When
    supplied, any dimension whose declared candidates replaced the auto ladder is
    named out loud (see scenario_discovery.discover_narrowings). Optional and
    None-defaulted so existing callers that only have the anchors keep working --
    DP#13: absence means "the caller cannot answer this", not "nothing was
    narrowed", so nothing is asserted rather than a reassuring silence printed.
    """
    print(f"\n  🔍 ANCHOR DISCOVERY RESULTS")
    print(f"  {'-' * 60}")

    for key, items in anchors.items():
        if isinstance(items, list):
            if items and isinstance(items[0], dict):
                labels = [it.get("label", it.get("id", str(it))) for it in items]
                print(f"  {key}: {len(items)} anchors → {', '.join(str(l) for l in labels[:6])}{'...' if len(labels) > 6 else ''}")
            else:
                print(f"  {key}: {items}")
        else:
            print(f"  {key}: {items}")

    total = 1
    for key in ["income", "mortgage", "refinance", "strategy"]:
        total *= max(1, len(anchors.get(key, [])))
    total *= len(anchors.get("sm_options", [False]))
    total *= len(anchors.get("deduct_later_options", [False]))
    total *= len(anchors.get("resp_action", ["keep"]))
    print(f"\n  Total combinations: ~{total:,}")
    print()

    # Issue #846: this grid is where a declared candidate list actually shrinks
    # the exploration -- declaring the two refinance_options in schema/
    # example.json takes it from 240 overlays to 48. The total above prints the
    # ALREADY-NARROWED count with no hint that it was narrowed; say so.
    if cfg is not None:
        from scenario_discovery import discover_narrowings, format_narrowings
        for line in format_narrowings(discover_narrowings(cfg)):
            print(line)


# ──────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="simulate.py — Single entry point: discover → simulate → rank"
    )
    parser.add_argument(
        "--input", default="input.json", help="Path to input JSON (default: input.json)"
    )
    parser.add_argument(
        "--mc", type=int, default=0, metavar="N",
        help="Monte Carlo paths (0=off, default: 0)",
    )
    parser.add_argument(
        "--optimize", action="store_true",
        help="Use scipy optimizer for continuous LTV/allocation search instead of grid enumeration",
    )
    parser.add_argument(
        "--sensitivity", action="store_true",
        help="Enable return-rate sensitivity sweep",
    )
    parser.add_argument(
        "--mortgage-sensitivity", action="store_true",
        help="Enable mortgage-rate (average rate over the term) sensitivity "
             "sweep + break-even rate vs the best alternative. Range comes from "
             "sensitivity_overlays.mortgage_rates in input.json (DP#2).",
    )
    parser.add_argument(
        "--csv", nargs="?", const="_default",
        help="Export CSV to file (default: ~/.cache/lifedraft/simulate_results.csv)",
    )
    parser.add_argument(
        "--html", nargs="?", const="simulate_report.html",
        help="Export HTML report (default: simulate_report.html)",
    )
    parser.add_argument(
        "--json", nargs="?", const="simulate_results.json",
        help="Export JSON (default: simulate_results.json)",
    )
    parser.add_argument(
        "--top-n", type=int, default=20, help="Number of top scenarios to display (default: 20)"
    )
    parser.add_argument(
        "--return-sigma", type=float, default=0.15,
        help="Annual return volatility for Monte Carlo (default: 0.15)",
    )
    parser.add_argument(
        "--retirement-ages", action="store_true",
        help="Issue #303: force enumeration & ranking of the configured "
             "retirement-age candidates (retirement.candidate_ages, default "
             "[60,62,65,67,70]) even when the horizon does not reach retirement. "
             "When the horizon already reaches retirement, the dimension is "
             "enumerated automatically.",
    )
    args = parser.parse_args()

    # ── 1. Load input ── Epic #603 Track C Phase 2b: args.input is a
    # contract document (the sole wire format, validated). input_contract.
    # load_and_map validates + maps it to the internal shape the rest of
    # this CLI's pipeline (discover_anchors, build_all_overlays, ...)
    # operates on -- unchanged internals, changed ingestion boundary.
    import input_contract
    base_cfg = input_contract.load_and_map(args.input)

    # ── 2. Discover anchors ──
    anchors = discover_anchors(base_cfg, force_retirement_ages=args.retirement_ages)

    print("\n🏠  SIMULATE — discover → simulate → rank")
    print(f"   Input: {args.input}")

    print_discovery(anchors, base_cfg)

    # ── 3. Report primary/spouse incomes and marginal rates ──
    brackets = default_tax_provider().get_combined_brackets()
    members = base_cfg["family"]["members"]
    primary = next(m for m in members if m["role"] == "primary")
    spouse = next(m for m in members if m["role"] == "spouse")
    n_income = primary["gross_income"]
    a_income = spouse["gross_income"]
    n_mtr = marginal_rate(n_income, brackets)
    a_mtr = marginal_rate(a_income, brackets)

    house = base_cfg["property"]
    house_value = house["house_value"]
    orig_mortgage = house["mortgage_balance"]
    # issue #663/DP#32: "no HELOC" is a first-class state, not a missing key.
    # A contract with no kind=heloc liability has no margin_available key at
    # all (input_contract.py deliberately omits it -- writing 0 would be the
    # silent fallback DP#32 forbids). has_readvanceable_facility() is the one
    # predicate every consumer must ask instead of indexing the key directly
    # or defaulting it to 0 (which would silently disable Smith Manoeuvre
    # rather than report it structurally unavailable).
    has_heloc = has_readvanceable_facility(base_cfg)
    orig_margin = house["margin_available"] if has_heloc else 0.0

    total_registered = (
        primary.get("rrsp_room_accumulated", 0)
        + spouse.get("rrsp_room_accumulated", 0)
        + primary.get("tfsa_room_accumulated", 0)
        + spouse.get("tfsa_room_accumulated", 0)
    )
    min_cashout = max(0, total_registered - orig_margin)
    min_ltv = (orig_mortgage + min_cashout) / house_value if house_value > 0 else 0

    print(f"  📋 SITUATION")
    print(f"     Primary:  ${n_income:,.0f} → {n_mtr:.1%} MTR")
    print(f"     Spouse:   ${a_income:,.0f} → {a_mtr:.1%} MTR")
    print(f"     Gap:      {(n_mtr - a_mtr) * 100:.1f}pp")
    print(f"     House:    ${house_value:,.0f} | Mortgage: ${orig_mortgage:,.0f} | LTV: {orig_mortgage / house_value:.1%}")
    if has_heloc:
        heloc_rate = base_cfg["property"]["mortgage_rate"]
        print(f"     Margin:   ${orig_margin:,.0f} | Registered Room: ${total_registered:,.0f}")
        print(f"     Room Gap: ${total_registered - orig_margin:,.0f} → Min LTV for full registered: {min_ltv:.1%}")
        print(f"     After-tax HELOC: {heloc_rate:.2%} × (1-{n_mtr:.1%}) = {heloc_rate * (1 - n_mtr):.2%}")
    else:
        print(f"     Registered Room: ${total_registered:,.0f}")
        print(f"     Smith Manoeuvre unavailable: no readvanceable line on this contract.")

    # ── 4. Scipy optimization OR grid enumeration ──
    if args.optimize:
        results, scipy_results = run_scipy_optimization(base_cfg, anchors)
        if scipy_results:
            print_scipy_report(scipy_results, base_cfg, n_mtr, n_income)
    else:
        combinations = build_all_overlays(base_cfg, anchors)
        print(f"\n  🔄 Evaluating {len(combinations)} scenario combinations ...")

        # ── 5. Evaluate each combination ──
        results = []
        for i, combo in enumerate(combinations):
            if (i + 1) % 25 == 0 or i + 1 == len(combinations):
                print(f"     ... {i + 1}/{len(combinations)} evaluated")
            overlay = combo["overlay"]
            strategy_alloc = combo["strategy_alloc"]
            result = evaluate_overlay(base_cfg, overlay, strategy_alloc=strategy_alloc)
            results.append(result)

    # ── 6. Rank and print ──
    print_results(results, title="SIMULATE — ALL COMBINATIONS", top_n=args.top_n)

    # ── 6b. Detailed analysis from enumerate_scenarios ──
    print_category_comparison(results)
    print_optimal_refi_level(results)
    print_retirement_age_comparison(results)
    if any(r.get('resp_cash_out', 0) > 0 for r in results):
        print_resp_cashout_analysis(results, base_cfg, n_mtr)

    # ── 7. Optional: sensitivity sweep ──
    if args.sensitivity:
        run_sensitivity(base_cfg, results, anchors)

    # ── 7b. Optional: mortgage-rate sensitivity sweep (issue #548) ──
    if args.mortgage_sensitivity:
        run_mortgage_sensitivity(base_cfg, results, anchors)

    # ── 8. Monte Carlo (explicit or automatic for SM recommendation) ──
    mc_paths = args.mc
    # Auto-run MC when top scenario involves Smith Manoeuvre (issue #366)
    sorted_results = sorted(results, key=lambda r: r.get('net_benefit', 0), reverse=True)
    top_has_sm = sorted_results and sorted_results[0].get('use_readvanceable', False) if mc_paths == 0 else False
    if top_has_sm:
        mc_paths = 500  # Default paths for auto-MC
        print(f"\n  ⚠️  Top recommendation uses Smith Manoeuvre — auto-running Monte Carlo")
        print(f"     to surface leverage risk (issue #366). Use --mc to customize.")
    if mc_paths > 0:
        mc_summary = run_monte_carlo(
            base_cfg, results, anchors,
            n_paths=mc_paths,
            return_sigma=args.return_sigma,
        )
        # Print risk summary when auto-run
        if top_has_sm and mc_summary:
            baseline_no_sm = [r for r in results if not r.get('use_readvanceable', False)]
            baseline_sorted = sorted(baseline_no_sm, key=lambda r: r.get('net_benefit', 0), reverse=True)
            best_no_sm = baseline_sorted[0]['net_benefit'] if baseline_sorted else 0
            print(f"\n  📋 LEVERAGE RISK SUMMARY (issue #366)")
            print(f"  {'-' * 60}")
            for ms in mc_summary:
                sm_label = ms['label']
                print(f"  Scenario: {sm_label}")
                print(f"    P10 (worst 10%):  ${ms['p10']:>12,.0f}")
                print(f"    P50 (median):     ${ms['p50']:>12,.0f}")
                print(f"    P90 (best 10%):   ${ms['p90']:>12,.0f}")
                print(f"    P(loss):          {ms['p_loss']:>8.1%}")
            print(f"\n  Comparison: Best no-SM scenario net benefit: ${best_no_sm:>10,.0f}")
            print(f"  If P10 < best no-SM, leverage worsens downside risk.")
            print()

    # ── 9. Optional: export outputs ── (DP#15: defaults go to ~/.cache)
    if args.csv:
        from output_paths import output_path
        csv_path = output_path(args.csv) if args.csv != "_default" else output_path("simulate_results.csv")
        export_csv(results, csv_path)
    if args.json:
        from output_paths import output_path
        json_path = output_path(args.json) if not os.path.isabs(args.json) else args.json
        export_json(results, base_cfg, json_path)
    if args.html:
        from output_paths import output_path
        html_path = output_path(args.html) if not os.path.isabs(args.html) else args.html
        export_html(results, base_cfg, html_path)

    print(f"\n  ✅ Done. {len(results)} scenarios evaluated.\n")


if __name__ == "__main__":
    main()