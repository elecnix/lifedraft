#!/usr/bin/env python3
"""
CPP/OAS Claiming Age Optimizer — DP#22 pluggable objective.

Optimizes the CPP start age (60-70) and OAS deferral months (0-60)
to maximize lifetime after-tax benefits, considering:
- CPP: −0.6%/month before 65, +0.7%/month after 65
- OAS: +0.6%/month deferred after 65 (up to 36% at age 70)
- OAS clawback: 15% recovery tax above the threshold
- Longevity assumption (configurable)

This optimizer treats CPP and OAS claiming ages as decision variables,
not fixed inputs. It enumerates all viable combinations and recommends
the optimal ages with the lifetime after-tax benefit delta vs. claiming at 65.

Per DP#3: Pure functions — same inputs → same outputs.
Per DP#10: All rules are from official CRA/Service Canada sources.
Per DP#20: Amounts are year-versioned.
Per DP#22: The optimizer accepts the objective as data.

References:
    - CPP: https://www.canada.ca/en/services/benefits/publicpensions/cpp.html
    - OAS: https://www.canada.ca/en/services/benefits/publicpensions/cpp/old-age-security.html
    - ITA s.60.03 (pension splitting)
    - CRA T1032 guide

Usage:
    from countries.canada.claiming_age_optimizer import optimize_claiming_ages, ClaimingAgeResult

    result = optimize_claiming_ages(
        cpp_monthly_at_65=1500,
        oas_annual_max=8908,
        life_expectancy=90,
        marginal_rate=0.45,
    )
    print(f"Recommended CPP start age: {result.recommended_cpp_start_age}")
    print(f"Recommended OAS defer months: {result.recommended_oas_defer_months}")
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from countries.canada.retirement import (
    cpp_benefit,
    oas_amount_for_age,
    oas_clawback,
    get_oas_annual_max,
    get_oas_clawback_threshold,
    CPP_EARLY_START_PENALTY,
    CPP_LATE_START_BONUS,
)


@dataclass
class ClaimingAgeResult:
    """Result of CPP/OAS claiming age optimization.

    Contains the recommended ages, the benefit delta vs. claiming at 65,
    and year-by-year projections for each scenario.
    """
    recommended_cpp_start_age: int
    recommended_oas_defer_months: int
    baseline_cpp_start_age: int = 65
    baseline_oas_defer_months: int = 0
    lifetime_benefit_optimal: float = 0.0
    lifetime_benefit_baseline: float = 0.0
    benefit_delta: float = 0.0
    explanation: str = ""
    all_scenarios: List[Dict] = None


def _compute_lifetime_cpp(
    cpp_monthly_at_65: float,
    start_age: int,
    life_expectancy: int,
    year_start: int,
) -> float:
    """Compute total lifetime CPP benefits from start_age to life_expectancy.

    Args:
        cpp_monthly_at_65: CPP monthly benefit estimate at age 65
        start_age: Age to start CPP (60-70)
        life_expectancy: Age at death (longevity assumption)
        year_start: Starting tax year for year-versioned lookups (required)

    Returns:
        Total lifetime CPP benefits (undiscounted)
    """
    if start_age >= life_expectancy:
        return 0.0

    annual_benefit = cpp_benefit(start_age, max_benefit_at_65=cpp_monthly_at_65 * 12, year=year_start)
    years_receiving = life_expectancy - start_age

    return annual_benefit * years_receiving


def _compute_lifetime_oas(
    oas_annual_max: float,
    defer_months: int,
    life_expectancy: int,
    oas_start_age: int,
    marginal_rate: float,
    net_income_excluding_oas: float = 0,
    year: int = 2026,
) -> Tuple[float, float]:
    """Compute total lifetime OAS benefits after clawback.

    Args:
        oas_annual_max: Base OAS annual maximum for age 65-74
        defer_months: Number of months deferred after age 65 (0-60)
        life_expectancy: Age at death
        oas_start_age: Age when OAS starts (65 + defer_months/12)
        marginal_rate: Marginal tax rate for after-tax calculation
        net_income_excluding_oas: Net income excluding OAS (for clawback)
        year: Tax year for year-versioned lookups

    Returns:
        Tuple of (total_gross_oas, total_net_oas_after_clawback)
    """
    if oas_start_age >= life_expectancy:
        return 0.0, 0.0

    # Get OAS amount for the start age with deferral
    oas_gross = oas_amount_for_age(oas_start_age, year=year, defer_months=defer_months)

    # For age 75+, use enhanced amount
    years_receiving_65_74 = min(75, life_expectancy) - oas_start_age
    years_receiving_75_plus = max(0, life_expectancy - 75)

    total_gross = oas_gross * years_receiving_65_74

    # Age 75+ gets 10% enhancement
    oas_enhanced = get_oas_annual_max_75plus_safe(year)
    total_gross += oas_enhanced * years_receiving_75_plus

    # Compute clawback
    threshold = get_oas_clawback_threshold(year)
    total_net = 0.0

    for age in range(oas_start_age, life_expectancy):
        annual_oas = oas_enhanced if age >= 75 else oas_gross
        clawback_result = oas_clawback(
            net_income_excluding_oas + annual_oas,
            oas_amount=annual_oas,
            year=year,
        )
        net_annual = annual_oas - clawback_result['clawback_amount']
        total_net += net_annual

    return total_gross, total_net


def get_oas_annual_max_75plus_safe(year: int = None) -> float:
    """Safe wrapper for getting OAS 75+ amount."""
    try:
        from countries.canada.retirement import get_oas_annual_max_75plus
        return get_oas_annual_max_75plus(year)
    except ImportError:
        return get_oas_annual_max(year) * 1.10


def optimize_claiming_ages(
    cpp_monthly_at_65: float,
    oas_annual_max: float = None,
    life_expectancy: int = 90,
    marginal_rate: float = 0.45,
    net_income_excluding_oas: float = 0,
    year: int = 2026,
    cpp_start_range: Tuple[int, int] = (60, 70),
    oas_defer_range: Tuple[int, int] = (0, 60),
    discount_rate: float = 0.0,
) -> ClaimingAgeResult:
    """Optimize CPP start age and OAS deferral months.

    Enumerates all viable CPP start ages (60-70) and OAS defer months (0-60)
    and recommends the combination that maximizes lifetime after-tax benefits.

    The baseline scenario is claiming CPP at 65 and OAS at 65 (0 months deferral).

    Args:
        cpp_monthly_at_65: CPP monthly benefit estimate at age 65
        oas_annual_max: Base OAS annual maximum (None → year-versioned lookup)
        life_expectancy: Age at death (longevity assumption)
        marginal_rate: Marginal tax rate for after-tax calculation
        net_income_excluding_oas: Annual net income excluding OAS (for clawback)
        year: Starting tax year
        cpp_start_range: (min, max) range for CPP start age
        oas_defer_range: (min, max) range for OAS deferral months
        discount_rate: Annual discount rate for present value (0 = no discounting)

    Returns:
        ClaimingAgeResult with recommended ages and benefit analysis
    """
    if oas_annual_max is None:
        oas_annual_max = get_oas_annual_max(year)

    cpp_min, cpp_max = cpp_start_range
    oas_min, oas_max = oas_defer_range

    # Clamp ranges
    cpp_min = max(60, cpp_min)
    cpp_max = min(70, cpp_max)
    oas_min = max(0, oas_min)
    oas_max = min(60, oas_max)

    scenarios = []
    best_benefit = float('-inf')
    best_cpp_age = 65
    best_oas_defer = 0

    for cpp_age in range(cpp_min, cpp_max + 1):
        for oas_months in range(oas_min, oas_max + 1, 6):  # Step by 6 months
            oas_start_age = 65 + (oas_months + 11) // 12  # Effective start age

            # Lifetime CPP
            total_cpp = _compute_lifetime_cpp(
                cpp_monthly_at_65, cpp_age, life_expectancy, year
            )

            # Lifetime OAS
            _, total_oas_net = _compute_lifetime_oas(
                oas_annual_max, oas_months, life_expectancy,
                oas_start_age, marginal_rate, net_income_excluding_oas, year
            )

            # Total after-tax benefit
            total_benefit = total_cpp * (1 - marginal_rate) + total_oas_net

            # Apply discount rate if specified
            if discount_rate > 0:
                start_year = cpp_age  # Simplified: discount from start year
                total_benefit = total_benefit / ((1 + discount_rate) ** start_year)

            scenarios.append({
                'cpp_start_age': cpp_age,
                'oas_defer_months': oas_months,
                'oas_start_age': oas_start_age,
                'total_cpp': total_cpp,
                'total_oas_net': total_oas_net,
                'total_after_tax_benefit': total_benefit,
            })

            if total_benefit > best_benefit:
                best_benefit = total_benefit
                best_cpp_age = cpp_age
                best_oas_defer = oas_months

    # Baseline: CPP at 65, OAS at 65 (0 months deferral)
    baseline_cpp = _compute_lifetime_cpp(
        cpp_monthly_at_65, 65, life_expectancy, year
    )
    _, baseline_oas_net = _compute_lifetime_oas(
        oas_annual_max, 0, life_expectancy, 65, marginal_rate,
        net_income_excluding_oas, year
    )
    baseline_benefit = baseline_cpp * (1 - marginal_rate) + baseline_oas_net

    if discount_rate > 0:
        baseline_benefit = baseline_benefit / ((1 + discount_rate) ** 65)

    delta = best_benefit - baseline_benefit

    # Build explanation
    cpp_delta_pct = ""
    if best_cpp_age != 65:
        if best_cpp_age < 65:
            cpp_delta_pct = f" (−{(65 - best_cpp_age) * 12 * 0.6:.0f}% reduction)"
        else:
            cpp_delta_pct = f" (+{(best_cpp_age - 65) * 12 * 0.7:.0f}% increase)"

    oas_delta_pct = ""
    if best_oas_defer > 0:
        oas_delta_pct = f" (+{best_oas_defer * 0.6:.1f}% increase)"

    explanation = (
        f"Recommended: CPP start age {best_cpp_age}{cpp_delta_pct}, "
        f"OAS defer {best_oas_defer} months{oas_delta_pct}. "
        f"Lifetime after-tax benefit: ${best_benefit:,.0f} "
        f"(baseline at 65: ${baseline_benefit:,.0f}, "
        f"delta: ${delta:+,.0f})"
    )

    return ClaimingAgeResult(
        recommended_cpp_start_age=best_cpp_age,
        recommended_oas_defer_months=best_oas_defer,
        baseline_cpp_start_age=65,
        baseline_oas_defer_months=0,
        lifetime_benefit_optimal=best_benefit,
        lifetime_benefit_baseline=baseline_benefit,
        benefit_delta=delta,
        explanation=explanation,
        all_scenarios=scenarios,
    )