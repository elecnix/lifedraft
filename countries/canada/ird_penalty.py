#!/usr/bin/env python3
"""
IRD Penalty Model — Interest Rate Differential calculation for mortgage breakage.

When breaking a mortgage before term end, lenders charge a penalty:
- Variable rate: typically 3 months' interest
- Fixed rate: the GREATER of 3 months' interest OR Interest Rate Differential (IRD)

The IRD is the difference between your contract rate and the current
posted rate for the remaining term, multiplied by the remaining balance
and the remaining months.

Per DP#10: this module owns mortgage breakage penalty rules (CRA / lender).
Per DP#7: models the mechanism (IRD), not a branded product.
Per DP#12: posted rates come from data, not hardcoded.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — IRD entry
    FCAC mortgage breakup: https://www.canada.ca/en/financial-consumer-agency/services/mortgages/break-mortgage.html

Usage:
    from countries.canada.ird_penalty import compute_ird_penalty, compute_breakage_penalty

    penalty = compute_breakage_penalty(
        mortgage_balance=450000, contract_rate=0.04,
        remaining_months=36, rate_type='fixed',
        posted_rates={1: 0.04, 2: 0.04, 3: 0.042, 5: 0.045},
    )
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


# =============================================================================
# Constants — Default posted rates (DP#13: round numbers, fallbacks only)
# =============================================================================

DEFAULT_POSTED_RATES = {
    1: 0.05,   # 1-year posted
    2: 0.05,   # 2-year posted
    3: 0.05,   # 3-year posted
    4: 0.052,  # 4-year posted
    5: 0.055,  # 5-year posted
    7: 0.058,  # 7-year posted
    10: 0.060, # 10-year posted
}


# =============================================================================
# Pure Functions — IRD Penalty Calculation
# =============================================================================

def compute_three_months_interest(
    mortgage_balance: float,
    contract_rate: float,
) -> float:
    """Calculate 3 months' interest penalty.

    This is the standard penalty for variable-rate mortgages and the
    floor penalty for fixed-rate mortgages.

    Args:
        mortgage_balance: Remaining mortgage principal
        contract_rate: Current contract interest rate (annual)

    Returns:
        3 months' interest penalty amount
    """
    return mortgage_balance * contract_rate * (3 / 12)


def compute_ird_penalty(
    mortgage_balance: float,
    contract_rate: float,
    remaining_months: int,
    posted_rates: Dict[int, float] = None,
    use_discounted_rate: bool = True,
    discount_from_posted: float = 0.0,
) -> float:
    """Calculate Interest Rate Differential (IRD) penalty.

    The IRD represents the lender's loss from you breaking the mortgage:
    they would reinvest at the current (lower) rate for the remaining term.

    IRD = balance × (contract_rate - comparable_rate) × remaining_months / 12

    The "comparable rate" is the current posted rate for a term closest
    to the remaining months. Some lenders use the rate you actually
    got (discounted from posted), others use the full posted rate.

    DP#12: Posted rates come from the data parameter, not hardcoded.

    Args:
        mortgage_balance: Remaining mortgage principal
        contract_rate: Current contract interest rate (annual)
        remaining_months: Months remaining in the term
        posted_rates: Dict of {term_years: rate} for current posted rates
        use_discounted_rate: If True, compare to your discounted rate;
            if False, compare to the full posted rate (larger IRD)
        discount_from_posted: Your discount from posted rate when you signed

    Returns:
        IRD penalty amount (0 if negative — rates went up)
    """
    if posted_rates is None:
        posted_rates = DEFAULT_POSTED_RATES

    if remaining_months <= 0:
        return 0.0

    # Find comparable term: the posted rate for a term closest to
    # the remaining months
    remaining_years = remaining_months / 12
    comparable_term = _find_closest_term(remaining_years, posted_rates)
    comparable_rate = posted_rates.get(comparable_term, contract_rate)

    if use_discounted_rate:
        # Some lenders compute IRD using your original
        # discount from posted rate, making the penalty much larger
        # because they compare your discounted rate to the current
        # posted rate (not the current discounted rate).
        # rate_diff = contract_rate - (comparable_rate - discount)
        effective_comparable = comparable_rate - discount_from_posted
    else:
        # Other lenders compare to full posted rate
        effective_comparable = comparable_rate

    rate_diff = contract_rate - effective_comparable

    # If rates went up, IRD is 0 (lender can reinvest at higher rate)
    if rate_diff <= 0:
        return 0.0

    # IRD = balance × rate_diff × remaining_months / 12
    ird = mortgage_balance * rate_diff * (remaining_months / 12)
    return ird


def compute_breakage_penalty(
    mortgage_balance: float,
    contract_rate: float,
    remaining_months: int,
    rate_type: str = 'fixed',
    posted_rates: Dict[int, float] = None,
    use_discounted_rate: bool = True,
    discount_from_posted: float = 0.0,
    is_open: bool = False,
) -> Dict:
    """Calculate the total mortgage breakage penalty.

    Open term: NO prepayment penalty — it can be repaid or refinanced at any
        time at no cost. Openness is orthogonal to fixed/variable (issue #651),
        so this suppresses BOTH the IRD and the 3-months'-interest charge.
    Variable-rate (closed): 3 months' interest only.
    Fixed-rate (closed): GREATER of 3 months' interest or IRD.

    SCENARIO 3.1: Refinance cash-out — must factor penalty into benefit.
    SCENARIO 3.3: Breaking for readvanceable product — compare SM benefit
    vs penalty cost over remaining term.

    Args:
        mortgage_balance: Remaining mortgage principal
        contract_rate: Current contract interest rate (annual)
        remaining_months: Months remaining in the term
        rate_type: 'fixed' or 'variable'
        posted_rates: Dict of {term_years: rate} for current posted rates
        use_discounted_rate: Whether to use discounted rate in IRD calc
        discount_from_posted: Original discount from posted rate
        is_open: Whether the term is OPEN (no break penalty). Defaults to
            False (closed) so an undeclared term keeps the incumbent
            closed-term math unchanged — a term is closed unless it says
            otherwise (DP#32: the zero here is a DECLARED zero, never the
            silent by-product of an unset rate_type or a missing balance).

    Returns:
        Dict with penalty breakdown, total, and comparison
    """
    if is_open:
        # DP#32: a declared open term has a declared-zero break cost. Report
        # every component as an explicit 0.0 so a reader sees the penalty was
        # suppressed on purpose (openness), not that the IRD math happened to
        # net to zero this year.
        return {
            'rate_type': rate_type,
            'is_open': True,
            'three_months_interest': 0.0,
            'ird_penalty': 0.0,
            'total_penalty': 0.0,
            'method': 'Open term: no prepayment penalty',
            'remaining_months': remaining_months,
        }

    three_mo = compute_three_months_interest(mortgage_balance, contract_rate)

    if rate_type == 'variable':
        return {
            'rate_type': rate_type,
            'three_months_interest': three_mo,
            'ird_penalty': 0.0,
            'total_penalty': three_mo,
            'method': 'Variable: 3 months interest only',
            'remaining_months': remaining_months,
        }

    # Fixed rate: IRD vs 3 months' interest
    ird = compute_ird_penalty(
        mortgage_balance, contract_rate, remaining_months,
        posted_rates, use_discounted_rate, discount_from_posted,
    )

    total = max(three_mo, ird)
    method = 'IRD' if ird > three_mo else '3 months interest'

    return {
        'rate_type': rate_type,
        'three_months_interest': three_mo,
        'ird_penalty': ird,
        'total_penalty': total,
        'method': f'Fixed: {method} (greater of the two)',
        'remaining_months': remaining_months,
        'ird_exceeds_three_month': ird > three_mo,
    }


def refinance_with_penalty_analysis(
    mortgage_balance: float,
    contract_rate: float,
    remaining_months: int,
    rate_type: str = 'fixed',
    new_rate: float = 0.0,
    new_term_months: int = 60,
    cash_out: float = 0,
    investment_return: float | None = None,
    marginal_rate: float = 0.40,
    posted_rates: Dict[int, float] = None,
    use_discounted_rate: bool = True,
    discount_from_posted: float = 0.0,
) -> Dict:
    """Full analysis: should you break and refinance?

    SCENARIO 3.1: Does the after-tax benefit of cash-out refinance
    exceed the penalty and rate differential over the new term?

    Args:
        mortgage_balance: Current mortgage balance
        contract_rate: Current mortgage rate
        remaining_months: Months left on current term
        rate_type: 'fixed' or 'variable'
        new_rate: Rate on the new mortgage
        new_term_months: Term of the new mortgage
        cash_out: Extra cash extracted for investment
        investment_return: Expected return on invested cash-out
        marginal_rate: Tax rate (for after-tax calculations)
        posted_rates: Current posted rates for IRD calculation
        use_discounted_rate: IRD calculation method
        discount_from_posted: Original discount from posted rate

    Returns:
        Dict with penalty, benefit, break-even, and recommendation
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    penalty = compute_breakage_penalty(
        mortgage_balance, contract_rate, remaining_months,
        rate_type, posted_rates, use_discounted_rate, discount_from_posted,
    )

    total_penalty = penalty['total_penalty']

    # Rate differential benefit/cost over remaining months of old term
    # If new_rate < contract_rate: you save on interest
    # If new_rate > contract_rate: you pay more
    rate_diff = contract_rate - new_rate
    interest_savings_over_remaining = mortgage_balance * rate_diff * (remaining_months / 12)

    # Cash-out investment benefit (after tax)
    after_tax_return = investment_return * (1 - marginal_rate) if cash_out > 0 else 0
    annual_cash_out_benefit = cash_out * after_tax_return

    # HELOC cost for comparison (if keeping old mortgage + using HELOC)
    heloc_rate = new_rate + 0.005  # P+0.5% typical HELOC spread
    after_tax_heloc_cost = heloc_rate * (1 - marginal_rate) if cash_out > 0 else 0
    annual_heloc_cost = cash_out * after_tax_heloc_cost

    # Break-even: how many months until accumulated benefits exceed penalty
    net_annual_benefit = annual_cash_out_benefit + abs(interest_savings_over_remaining) / max(1, remaining_months / 12)
    if net_annual_benefit > 0:
        break_even_months = total_penalty / net_annual_benefit * 12
    else:
        break_even_months = float('inf')

    # Total benefit over new term (years)
    new_term_years = new_term_months / 12
    total_cash_out_benefit = annual_cash_out_benefit * new_term_years
    net_benefit = total_cash_out_benefit + interest_savings_over_remaining - total_penalty

    recommendation = 'refinance' if net_benefit > 0 else 'keep_current'

    return {
        'penalty': penalty,
        'total_penalty': total_penalty,
        'interest_savings_remaining': interest_savings_over_remaining,
        'annual_cash_out_benefit': annual_cash_out_benefit,
        'annual_heloc_cost': annual_heloc_cost,
        'net_annual_benefit': net_annual_benefit,
        'break_even_months': break_even_months,
        'total_cash_out_benefit': total_cash_out_benefit,
        'net_benefit_after_penalty': net_benefit,
        'recommendation': recommendation,
        'rate_savings_per_month': abs(interest_savings_over_remaining) / max(1, remaining_months),
        'new_term_years': new_term_years,
    }


def break_for_readvanceable_analysis(
    mortgage_balance: float,
    contract_rate: float,
    remaining_months: int,
    new_readvanceable_rate: float = 0.0,
    readvance_ratio: float = 1.0,
    house_value: float = 0,
    marginal_rate: float = 0.40,
    heloc_rate: float = 0.05,
    investment_return: float | None = None,
    rate_type: str = 'fixed',
    posted_rates: Dict[int, float] = None,
) -> Dict:
    """Should you break your mortgage for a readvanceable product?

    SCENARIO 3.3: Partial readvance (65% LTV cap) vs full readvance.
    The SM benefit lost by NOT having readvanceable may exceed
    the breakage penalty.

    Args:
        mortgage_balance: Current balance
        contract_rate: Current mortgage rate
        remaining_months: Months left on current term
        new_readvanceable_rate: Rate on new readvanceable mortgage
        readvance_ratio: How much of each dollar paid is readvanced
            (1.0 = full readvance, 0.65 = partial readvance)
        house_value: Property value (for LTV check)
        marginal_rate: Tax rate
        heloc_rate: HELOC interest rate
        investment_return: Expected investment return
        rate_type: 'fixed' or 'variable'
        posted_rates: Current posted rates for IRD

    Returns:
        Dict with SM benefit analysis and recommendation
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    penalty = compute_breakage_penalty(
        mortgage_balance, contract_rate, remaining_months,
        rate_type, posted_rates,
    )

    total_penalty = penalty['total_penalty']

    # Remaining years
    remaining_years = remaining_months / 12

    # Monthly mortgage payment (approximate: interest-only for simplicity)
    monthly_payment = mortgage_balance * contract_rate / 12
    annual_principal = monthly_payment * 12 - mortgage_balance * contract_rate
    # More accurate: assume ~60% of payment is principal in later years
    annual_principal_approx = max(0, mortgage_balance * 0.04)  # ~4% principal paydown/year

    # SM benefit per year of having readvanceable
    # Each dollar of principal readvanced → invested → interest deductible
    sm_annual_benefit = (annual_principal_approx * readvance_ratio *
                        heloc_rate * marginal_rate)

    # After-tax investment return on readvanced funds
    investment_benefit = (annual_principal_approx * readvance_ratio *
                         investment_return * (1 - marginal_rate))

    total_sm_benefit_per_year = sm_annual_benefit + investment_benefit

    # Total SM benefit over remaining term if we had readvanceable
    # (compounding: benefit grows as more principal is readvanced)
    total_sm_benefit = 0
    running_heloc = 0
    for yr in range(int(remaining_years)):
        running_heloc += annual_principal_approx * readvance_ratio
        year_interest = running_heloc * heloc_rate
        year_tax_saving = year_interest * marginal_rate
        year_investment = running_heloc * investment_return * (1 - marginal_rate)
        total_sm_benefit += year_tax_saving + year_investment

    # Net benefit: SM benefit - penalty
    net_benefit = total_sm_benefit - total_penalty

    recommendation = 'switch_to_readvanceable' if net_benefit > 0 else 'stay_current'

    return {
        'penalty': penalty,
        'total_penalty': total_penalty,
        'sm_annual_benefit': sm_annual_benefit,
        'investment_benefit_per_year': investment_benefit,
        'total_sm_benefit_per_year': total_sm_benefit_per_year,
        'total_sm_benefit_over_remaining_term': total_sm_benefit,
        'net_benefit_after_penalty': net_benefit,
        'recommendation': recommendation,
        'remaining_years': remaining_years,
        'break_even_years': (total_penalty / total_sm_benefit_per_year
                            if total_sm_benefit_per_year > 0 else float('inf')),
    }


# =============================================================================
# Helpers
# =============================================================================

def _find_closest_term(remaining_years: float,
                       posted_rates: Dict[int, float]) -> int:
    """Find the posted rate term closest to the remaining years.

    Lenders compare to the term that most closely matches the
    remaining months on your mortgage.
    """
    available_terms = sorted(posted_rates.keys())
    if not available_terms:
        return 5  # Default: 5-year term

    # Find closest term that is <= remaining years
    # If remaining years is between terms, use the shorter one
    closest = available_terms[0]
    for term in available_terms:
        if abs(term - remaining_years) < abs(closest - remaining_years):
            closest = term
    return closest