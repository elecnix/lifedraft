#!/usr/bin/env python3
"""
Pension Income Splitting Optimization — DP#22 pluggable objective.

Pension splitting allows up to 50% of eligible pension income to be
allocated to a spouse. The optimal split percentage depends on:
- Both spouses' marginal rates
- OAS clawback thresholds
- Pension income credit ($2,000 per spouse)
- Provincial rules (Quebec: 65+ only)

SCENARIO 12.1: The optimal split is NOT always 50%. Sometimes a
lower split keeps the higher-earning spouse just below the OAS
clawback threshold while still giving both spouses the pension credit.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Pension Splitting entry
    ITA s.60.03
    CRA T1032 guide: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/pension-income-splitting.html

Per DP#10: this module handles pension splitting optimization.
Per DP#22: the optimizer accepts the objective as data.
Per DP#26: pure functions over explicit state.

Usage:
    from countries.canada.pension_split_optimizer import optimize_pension_split, PensionSplitResult

    result = optimize_pension_split(
        spouse_a_income=62908,
        spouse_b_income=31908,
        eligible_pension=40000,
        province='quebec',
    )
    print(f"Optimal split: {result.optimal_split_pct:.0%}")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from copy import deepcopy

from countries.canada.retirement import (
    oas_clawback,
    get_oas_annual_max, get_oas_clawback_threshold,  # DP#20 year-versioned
    pension_splitting_available, rrif_minimum_withdrawal,
    PensionIncomeType,
)
from tax_calculator import (
    marginal_rate, tax_on_income,
)
from tax_data import default_tax_provider


# =============================================================================
# Pension Income Credit
# =============================================================================

PENSION_INCOME_CREDIT_MAX = 2000   # First $2,000 of eligible pension income
PENSION_CREDIT_RATE = 0.15         # 15% federal non-refundable credit

# Federal cap: at most 50% of the transferor's eligible pension may be
# allocated to the spouse on a T1032 election (ITA s.60.03).
MAX_PENSION_SPLIT_PCT = 0.50


def split_pension_amounts(
    primary_pension: float,
    spouse_pension: float,
    split_pct: float,
    primary_is_higher: bool,
) -> Tuple[float, float]:
    """Apply an ELECTED pension-income split (T1032) as a pure transfer.

    Pension income splitting lets the pension HOLDER allocate up to 50% of
    their eligible pension income to their spouse on the joint T1032 election
    (ITA s.60.03). This is the MECHANISM only — it moves the elected fraction
    of the higher-bracket spouse's eligible pension to the lower-bracket
    spouse and CONSERVES the household total. The tax consequence is priced
    downstream by the per-spouse progressive drawdown (each spouse's slice
    stacked on their OWN bracket set) — this function invents no tax model
    (DP#30: we model the consequence of the elected split, not whether to make
    it; DP#22: the split_pct is the election the optimizer sweeps).

    Per DP#3: pure function — same inputs → same outputs. Per DP#32: a
    ``split_pct`` of 0 is a real value ("no split elected"), returning the
    inputs unchanged; it is never coerced away.

    Args:
        primary_pension: primary spouse's eligible pension income.
        spouse_pension: spouse's eligible pension income.
        split_pct: fraction (0..0.5) of the HIGHER-bracket spouse's eligible
            pension to allocate to the lower-bracket spouse.
        primary_is_higher: True when the primary is the higher-bracket spouse
            (the transferor); False when the spouse is.

    Returns:
        ``(new_primary_pension, new_spouse_pension)`` — the household total is
        conserved (``new_primary + new_spouse == primary + spouse``).
    """
    if split_pct < 0 or split_pct > MAX_PENSION_SPLIT_PCT:
        raise ValueError(
            f"split_pct={split_pct} is outside the statutory [0, "
            f"{MAX_PENSION_SPLIT_PCT}] range for a T1032 election "
            "(ITA s.60.03)."
        )
    if primary_is_higher:
        transfer = split_pct * primary_pension
        return primary_pension - transfer, spouse_pension + transfer
    transfer = split_pct * spouse_pension
    return primary_pension + transfer, spouse_pension - transfer


def pension_income_credit(pension_income: float) -> float:
    """Calculate the pension income credit.

    Both spouses can claim this credit if each has at least $2,000
    of eligible pension income (possibly via splitting).

    Args:
        pension_income: Eligible pension income amount

    Returns:
        Credit amount in dollars
    """
    eligible = min(pension_income, PENSION_INCOME_CREDIT_MAX)
    return eligible * PENSION_CREDIT_RATE


def both_spouses_get_credit(
    spouse_a_pension: float,
    spouse_b_pension: float,
) -> bool:
    """Check if both spouses qualify for the pension income credit.

    Each spouse needs at least $2,000 of eligible pension income.
    """
    return spouse_a_pension >= PENSION_INCOME_CREDIT_MAX and \
           spouse_b_pension >= PENSION_INCOME_CREDIT_MAX


# =============================================================================
# Split Optimization
# =============================================================================

@dataclass
class PensionSplitResult:
    """Result of pension income splitting optimization.

    Attributes:
        optimal_split_pct: Optimal percentage of pension to split (0-50%)
        optimal_split_amount: Dollar amount to split
        total_tax_no_split: Total family tax without splitting
        total_tax_with_split: Total family tax with optimal split
        tax_savings: Net tax savings from optimal split
        oas_savings: OAS clawback savings from splitting
        credit_savings: Additional pension credit from splitting
        both_get_credit: Whether both spouses get the pension credit
        split_details: Year-by-year breakdown
    """
    optimal_split_pct: float = 0.0
    optimal_split_amount: float = 0.0
    total_tax_no_split: float = 0.0
    total_tax_with_split: float = 0.0
    tax_savings: float = 0.0
    oas_savings: float = 0.0
    credit_savings: float = 0.0
    both_get_credit: bool = False
    split_details: Dict = field(default_factory=dict)


def optimize_pension_split(
    spouse_a_income: float,
    spouse_b_income: float,
    eligible_pension: float,
    year: int = 2026,
    spouse_a_oas: float | None = None,
    spouse_b_oas: float | None = None,
    spouse_a_age: int = 68,
    spouse_b_age: int = 66,
    province: str = 'quebec',
    brackets: List[Dict] = None,
    split_resolution: int = 100,
    provider: 'TaxDataProvider' = None,
    income_type: 'PensionIncomeType' = None,
) -> PensionSplitResult:
    """Find the optimal pension income split percentage.

    SCENARIO 12.1: The optimal split is found by searching across
    split percentages (0% to 50% in increments determined by resolution).

    The search considers:
    1. Tax savings from income splitting (high-MTR→low-MTR transfer)
    2. OAS clawback savings (keeping higher earner below threshold)
    3. Pension credit optimization (both spouses need $2,000+)
    4. Provincial rules (Quebec: 65+ only for provincial splitting)

    DP#3: Pure function — same inputs always produce same outputs.
    DP#22: The objective (minimize total tax + clawback) is implicit;
    a future version could accept it as a parameter.

    Args:
        spouse_a_income: Spouse A's total non-pension-split income
        spouse_b_income: Spouse B's total non-pension-split income
        eligible_pension: Amount of pension income eligible for splitting
        year: Tax year for OAS amount and clawback threshold lookups (DP#20)
        spouse_a_oas: Spouse A's OAS entitlement (None → lookup from year)
        spouse_b_oas: Spouse B's OAS entitlement (None → lookup from year)
        spouse_a_age: Spouse A's age
        spouse_b_age: Spouse B's age
        province: Province code
        brackets: Tax brackets (default: load from tax_data)
        split_resolution: Number of split percentages to test (default 100)
        income_type: Type of the eligible pension income (PensionIncomeType).
            When provided, both age AND income-type eligibility are enforced
            for BOTH the federal and the provincial gate (DP#54). When None,
            the legacy behaviour applies (federal 55+ presence check only).

    Returns:
        PensionSplitResult with optimal split and savings breakdown
    """
    # Resolve year-versioned OAS amounts (DP#20)
    if spouse_a_oas is None:
        spouse_a_oas = get_oas_annual_max(year)
    if spouse_b_oas is None:
        spouse_b_oas = get_oas_annual_max(year)

    if brackets is None:
        if provider is None:
            provider = default_tax_provider()
        brackets = provider.get_combined_brackets()

    # Check if pension splitting is available for the pension-receiving spouse
    # (spouse A holds `eligible_pension` and elects to split it to spouse B).
    # Eligibility depends on the transferor's age, the income type, and the
    # province (DP#54).
    avail_a = pension_splitting_available(
        spouse_a_age, province, income_type=income_type)

    # Quebec provincial gate (TP-1 Schedule L / Loi sur les impôts a. 1029.8.116):
    # in Quebec, retirement income other than a life annuity (RRIF/LIF income)
    # is only splittable once the transferor reaches 65, even though the federal
    # rule allows splitting of LIF/RPP/annuity income from 55. Without this gate
    # a 55–64 Quebec resident would be granted a split the province disallows,
    # overstating the benefit (#351). Checked before the generic federal note so
    # the reason reflects the province-specific 65+ restriction.
    if province == 'quebec' and not avail_a.get('provincial_available', False):
        return PensionSplitResult(
            optimal_split_pct=0,
            optimal_split_amount=0,
            split_details={'note':
                'Pension splitting not available in Quebec '
                '(provincial 65+ requirement)'},
        )

    # Federal gate (age 55+ for qualifying income, ITA s.60.03).
    if not avail_a.get('federal_available', False):
        return PensionSplitResult(
            optimal_split_pct=0,
            optimal_split_amount=0,
            split_details={'note': 'Pension splitting not available (age requirement)'},
        )

    max_split_pct = 0.50

    # Baseline: no split
    baseline_result = _compute_split_outcome(
        spouse_a_income, spouse_b_income, eligible_pension,
        split_pct=0, spouse_a_oas=spouse_a_oas, spouse_b_oas=spouse_b_oas,
        brackets=brackets, province=province, year=year,
    )

    best_split_pct = 0
    best_total_cost = baseline_result['total_cost']
    best_result = baseline_result

    # Search over split percentages
    for i in range(split_resolution + 1):
        split_pct = max_split_pct * i / split_resolution

        result = _compute_split_outcome(
            spouse_a_income, spouse_b_income, eligible_pension,
            split_pct=split_pct,
            spouse_a_oas=spouse_a_oas, spouse_b_oas=spouse_b_oas,
            brackets=brackets, province=province, year=year,
        )

        if result['total_cost'] < best_total_cost:
            best_total_cost = result['total_cost']
            best_split_pct = split_pct
            best_result = result

    # Build result
    optimal_split_amount = eligible_pension * best_split_pct
    tax_savings = baseline_result['total_tax'] - best_result['total_tax']
    oas_savings = baseline_result['total_oas_clawback'] - best_result['total_oas_clawback']
    credit_savings = baseline_result['total_credits'] - best_result['total_credits']
    # Credit savings are actually added (more credits = less cost)
    credit_savings = best_result['total_credits'] - baseline_result['total_credits']

    both_get = best_result['both_get_credit']

    return PensionSplitResult(
        optimal_split_pct=best_split_pct,
        optimal_split_amount=optimal_split_amount,
        total_tax_no_split=baseline_result['total_tax'],
        total_tax_with_split=best_result['total_tax'],
        tax_savings=tax_savings,
        oas_savings=oas_savings,
        credit_savings=credit_savings,
        both_get_credit=both_get,
        split_details={
            'baseline': baseline_result,
            'optimal': best_result,
            'spouse_a_net_income_optimal': best_result['spouse_a_net_income'],
            'spouse_b_net_income_optimal': best_result['spouse_b_net_income'],
        },
    )


def _compute_split_outcome(
    spouse_a_income: float,
    spouse_b_income: float,
    eligible_pension: float,
    split_pct: float,
    spouse_a_oas: float,
    spouse_b_oas: float,
    brackets: List[Dict],
    province: str,
    year: int = 2026,
) -> Dict:
    """Compute the total family cost for a given split percentage.

    Cost = total tax on both spouses + total OAS clawback - total credits.
    Lower cost is better.
    """
    split_amount = eligible_pension * split_pct

    # Spouse A: receives pension, gives split to B
    a_pension_income = eligible_pension - split_amount
    a_total_income = spouse_a_income + a_pension_income

    # Spouse B: receives split pension
    b_pension_income = split_amount
    b_total_income = spouse_b_income + b_pension_income

    # Tax
    a_tax = tax_on_income(a_total_income, brackets)
    b_tax = tax_on_income(b_total_income, brackets)
    total_tax = a_tax + b_tax

    # OAS clawback (DP#20: pass year for year-versioned threshold lookup)
    a_clawback = oas_clawback(a_total_income, oas_amount=spouse_a_oas, year=year)
    b_clawback = oas_clawback(b_total_income, oas_amount=spouse_b_oas, year=year)
    total_oas_clawback = a_clawback['clawback_amount'] + b_clawback['clawback_amount']

    # Pension income credit
    a_credit = pension_income_credit(a_pension_income)
    b_credit = pension_income_credit(b_pension_income)
    total_credits = a_credit + b_credit

    # Both spouses get credit?
    both_get = both_spouses_get_credit(a_pension_income, b_pension_income)

    # Total cost (lower is better)
    total_cost = total_tax + total_oas_clawback - total_credits

    return {
        'split_pct': split_pct,
        'split_amount': split_amount,
        'spouse_a_income': a_total_income,
        'spouse_b_income': b_total_income,
        'spouse_a_pension_credit': a_credit,
        'spouse_b_pension_credit': b_credit,
        'both_get_credit': both_get,
        'total_tax': total_tax,
        'total_oas_clawback': total_oas_clawback,
        'total_credits': total_credits,
        'total_cost': total_cost,
        'spouse_a_net_income': a_total_income,
        'spouse_b_net_income': b_total_income,
        'spouse_a_oas_clawback': a_clawback['clawback_amount'],
        'spouse_b_oas_clawback': b_clawback['clawback_amount'],
    }


# =============================================================================
# Multi-Year Pension Split Projection — SCENARIO 12.1
# =============================================================================

def project_pension_split_retirement(
    spouse_a_age: int,
    spouse_b_age: int,
    spouse_a_rrif: float,
    spouse_b_rrif: float,
    spouse_a_tfsa: float,
    spouse_b_tfsa: float,
    spouse_a_cpp: float = 0,
    spouse_b_cpp: float = 0,
    year: int = 2026,
    spouse_a_oas: float | None = None,
    spouse_b_oas: float | None = None,
    annual_expenses: float = 60000,
    years: int = 25,
    province: str = 'quebec',
    investment_return: float | None = None,
    inflation: float = 0.025,
) -> List[Dict]:
    """Year-by-year retirement projection with pension split optimization.

    SCENARIO 5.1 + 12.1: Combined OAS clawback management and
    pension income splitting optimization over the retirement phase.

    Each year:
    1. Compute RRIF minimum withdrawals
    2. Optimize pension split percentage
    3. Determine TFSA bridge (draw from TFSA to keep income below clawback)
    4. Compute OAS clawback
    5. Compute total family tax

    Args:
        spouse_a_age: Spouse A age at retirement start
        spouse_b_age: Spouse B age at retirement start
        spouse_a_rrif: Spouse A RRIF balance
        spouse_b_rrif: Spouse B RRIF balance
        spouse_a_tfsa: Spouse A TFSA balance
        spouse_b_tfsa: Spouse B TFSA balance
        spouse_a_cpp: Spouse A CPP benefit
        spouse_b_cpp: Spouse B CPP benefit
        year: Tax year for OAS amount lookups (DP#20)
        spouse_a_oas: Spouse A OAS entitlement (None → lookup from year)
        spouse_b_oas: Spouse B OAS entitlement (None → lookup from year)
        annual_expenses: Annual living expenses
        years: Projection years
        province: Province code
        investment_return: Real investment return
        inflation: Inflation rate

    Returns:
        List of year-by-year results
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    # Resolve year-versioned OAS amounts (DP#20)
    if spouse_a_oas is None:
        spouse_a_oas = get_oas_annual_max(year)
    if spouse_b_oas is None:
        spouse_b_oas = get_oas_annual_max(year)
    results = []
    a_age = spouse_a_age
    b_age = spouse_b_age
    a_rrif = spouse_a_rrif
    b_rrif = spouse_b_rrif
    a_tfsa = spouse_a_tfsa
    b_tfsa = spouse_b_tfsa
    a_cpp = spouse_a_cpp
    b_cpp = spouse_b_cpp
    a_oas = spouse_a_oas
    b_oas = spouse_b_oas
    expenses = annual_expenses

    for yr in range(years):
        sim_year = year + yr

        # RRIF minimums
        a_rrif_min = rrif_minimum_withdrawal(a_rrif, a_age) if a_age >= 65 else 0
        b_rrif_min = rrif_minimum_withdrawal(b_rrif, b_age) if b_age >= 65 else 0

        # If not yet 71, may not have RRIF — skip minimums
        if a_age < 65:
            a_rrif_withdrawal = 0
        else:
            a_rrif_withdrawal = a_rrif_min

        if b_age < 65:
            b_rrif_withdrawal = 0
        else:
            b_rrif_withdrawal = b_rrif_min

        # CPP/OAS income
        a_current_cpp = a_cpp if a_age >= 60 else 0
        b_current_cpp = b_cpp if b_age >= 60 else 0
        a_current_oas = a_oas if a_age >= 65 else 0
        b_current_oas = b_oas if b_age >= 65 else 0

        # Base income (before pension split)
        a_base_income = a_current_cpp + a_current_oas + a_rrif_withdrawal
        b_base_income = b_current_cpp + b_current_oas + b_rrif_withdrawal

        # Optimize pension split on RRIF withdrawals
        eligible_pension = a_rrif_withdrawal + b_rrif_withdrawal
        if eligible_pension > 0 and a_age >= 65:
            split_result = optimize_pension_split(
                spouse_a_income=a_base_income - a_rrif_withdrawal,
                spouse_b_income=b_base_income - b_rrif_withdrawal,
                eligible_pension=eligible_pension,
                spouse_a_oas=a_current_oas,
                spouse_b_oas=b_current_oas,
                spouse_a_age=a_age,
                spouse_b_age=b_age,
                province=province,
            )
            optimal_split_pct = split_result.optimal_split_pct
            tax_savings = split_result.tax_savings
            oas_savings_from_split = split_result.oas_savings
        else:
            optimal_split_pct = 0
            tax_savings = 0
            oas_savings_from_split = 0

        # OAS clawback with optimal split
        split_amount = eligible_pension * optimal_split_pct
        a_total_income = a_base_income - split_amount + (b_rrif_withdrawal * optimal_split_pct if b_rrif_withdrawal > 0 else 0)
        b_total_income = b_base_income + split_amount - (b_rrif_withdrawal * optimal_split_pct if b_rrif_withdrawal > 0 else 0)

        # Simplified: redistribute based on total eligible pension split
        # Spouse A gets their RRIF - split, B gets their RRIF + split
        a_adjusted_income = a_base_income - (a_rrif_withdrawal * optimal_split_pct)
        b_adjusted_income = b_base_income + (a_rrif_withdrawal * optimal_split_pct)

        a_clawback = oas_clawback(a_adjusted_income, oas_amount=a_current_oas, year=sim_year)
        b_clawback = oas_clawback(b_adjusted_income, oas_amount=b_current_oas, year=sim_year)

        # TFSA bridge: draw from TFSA to cover expenses beyond income
        total_income = a_adjusted_income + b_adjusted_income
        income_after_clawback = total_income - a_clawback['clawback_amount'] - b_clawback['clawback_amount']
        shortfall = max(0, expenses - income_after_clawback)

        a_tfsa_draw = 0
        b_tfsa_draw = 0
        if shortfall > 0:
            # Draw from TFSA to cover shortfall (preserves low taxable income)
            tfsa_available = a_tfsa + b_tfsa
            tfsa_draw = min(shortfall, tfsa_available)
            # Proportional draw
            if tfsa_available > 0:
                a_tfsa_draw = tfsa_draw * (a_tfsa / tfsa_available) if tfsa_available > 0 else 0
                b_tfsa_draw = tfsa_draw * (b_tfsa / tfsa_available) if tfsa_available > 0 else 0

        # Update balances
        a_rrif = max(0, a_rrif - a_rrif_withdrawal) * (1 + investment_return)
        b_rrif = max(0, b_rrif - b_rrif_withdrawal) * (1 + investment_return)
        a_tfsa = max(0, a_tfsa - a_tfsa_draw) * (1 + investment_return)
        b_tfsa = max(0, b_tfsa - b_tfsa_draw) * (1 + investment_return)

        # Age and inflation
        a_age += 1
        b_age += 1
        expenses *= (1 + inflation)
        a_oas *= (1 + inflation)
        b_oas *= (1 + inflation)

        results.append({
            'year': sim_year,
            'spouse_a_age': a_age - 1,  # Age at start of year
            'spouse_b_age': b_age - 1,
            'spouse_a_income': a_adjusted_income,
            'spouse_b_income': b_adjusted_income,
            'spouse_a_rrif_withdrawal': a_rrif_withdrawal,
            'spouse_b_rrif_withdrawal': b_rrif_withdrawal,
            'optimal_split_pct': optimal_split_pct,
            'tax_savings_from_split': tax_savings,
            'oas_savings_from_split': oas_savings_from_split,
            'spouse_a_oas_clawback': a_clawback['clawback_amount'],
            'spouse_b_oas_clawback': b_clawback['clawback_amount'],
            'total_oas_clawback': a_clawback['clawback_amount'] + b_clawback['clawback_amount'],
            'tfsa_draw': a_tfsa_draw + b_tfsa_draw,
            'shortfall': shortfall,
            'spouse_a_rrif_balance': a_rrif,
            'spouse_b_rrif_balance': b_rrif,
            'spouse_a_tfsa_balance': a_tfsa,
            'spouse_b_tfsa_balance': b_tfsa,
        })

    return results