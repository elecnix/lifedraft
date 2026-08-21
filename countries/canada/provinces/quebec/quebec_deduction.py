#!/usr/bin/env python3
"""
Quebec Interest Deduction Limit — TP-1 Schedule L / ITA §20(1)(c)

Quebec limits the deduction for investment interest expenses to the
net investment income earned in the year. Federal has no such limit:
the full amount is deductible.

This is a critical difference for Quebec residents using a
readvanceable mortgage investment-loan strategy: if your HELOC
interest is $10,000 but you only
earned $3,000 in investment income (dividends + interest), Quebec
only allows a $3,000 deduction this year. The $7,000 excess carries
forward to future years.

Key insight for strategy optimization:
- Dividend income counts toward the Quebec deduction limit
- Capital gains only count when realized (and only the taxable portion)
- This makes dividend-heavy readvanceable-loan portfolios more efficient in Quebec
  because dividends are recurring annual income that supports
  the interest deduction

Per DP#10: this module owns the Quebec-specific deduction limit.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Quebec Interest Deduction entry
    ITA s.20(1)(c) (federal deductibility)
    Quebec TP-1 Schedule L (provincial limit)
    https://www.revenuquebec.ca/en/individuals/income-tax-return/tp-1-schedule-l/

Usage:
    from countries.canada.provinces.quebec.quebec_deduction import quebec_interest_deduction, QuebecDeductionTracker

    result = quebec_interest_deduction(
        heloc_interest=10000, investment_income=3000, carry_forward=0
    )
    print(f"Quebec deductible: ${result['deductible_this_year']}")
    print(f"Federal deductible: ${result['federal_deductible']}")
    print(f"Carry forward: ${result['new_carry_forward']}")
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# =============================================================================
# Pure Functions — Quebec Deduction Limit
# =============================================================================

def cap_qc_investment_interest(
    total_deductible: float,
    investment_income: float,
    opening_carry_forward: float,
) -> tuple:
    """Quebec TP-1 Schedule L investment-expense cap (TA s.336.0.1): investment
    interest is deductible only up to the investment income earned in the year,
    with an indefinite carry-forward of the excess.

    Returns ``(qc_deductible, new_carry_forward)`` -- the slice of
    ``total_deductible`` Quebec lets the household claim this year, and the
    unused excess that survives to next year (a deduction deferred, not denied).
    The carry-forward is released against a LATER year's investment income --
    including the year-of-death deemed disposition, where the caller threads it
    into ``compute_estate`` (issue #1035).

    This is the QUEBEC provincial limit ONLY -- the caller applies it only for
    Quebec households (#1035: an Ontario household is capped by no statute).
    The federal s.20(1)(c) deduction has NO investment-income limit -- the full
    ``total_deductible`` is deductible federally. The two are DISTINCT savings
    and must be valued separately (federal slice on ``total_deductible``,
    Quebec slice on ``qc_deductible``, each on its own bracket set), never as
    one capped amount at a blended combined rate.

    Pure function (DP#3): same inputs -> same output. The carry-forward sits on
    the AVAILABLE side (interest + prior carry-forward, capped by the year's
    investment income) -- the spelling ``apply_sm_interest`` has always used;
    ``quebec_interest_deduction`` below places it on the LIMIT side instead.

    Args:
        total_deductible: the s.20(1)(c)-qualifying, purpose-traced interest
            across EVERY borrowing the household has this year (the SM readvance
            line + the mortgage advance + the drawn revolving margin, #850).
        investment_income: the year's net investment income of the pots the
            traced borrowings bought -- the SCHEDULE L BASE (eligible dividends
            + non-eligible dividends + interest/other + 50% of realized capital
            gains + net rental), NOT a balance-times-yield product (#1035:
            the pre-fix base ``balance * yield_rate`` could not see the income
            breakdown and permanently under-used the cap for growth-tilted
            portfolios). Build it with
            ``countries.canada.portfolio.compute_investment_income`` and sum
            the type-specific components per Schedule L lines 2-6.
        opening_carry_forward: unused Quebec investment-interest from prior
            years (Schedule L carry-forward).
    """
    qc_available = total_deductible + opening_carry_forward
    qc_deductible = min(qc_available, investment_income)
    new_carry_forward = max(0.0, qc_available - qc_deductible)
    return qc_deductible, new_carry_forward


def quebec_interest_deduction(
    heloc_interest: float,
    investment_income: float,
    carry_forward_prior: float = 0,
    capital_gains_realized: float = 0,
    capital_gains_inclusion: float = 0.50,
    eligible_dividend_income: float = 0,
    non_eligible_dividend_income: float = 0,
    interest_income: float = 0,
    rental_income_net: float = 0,
) -> Dict:
    """Calculate Quebec interest deduction under the income limit.

    Quebec rule: deductible interest cannot exceed net investment income
    earned in the year, plus any carry-forward from prior years.

    Federal rule: full interest is deductible (no income limit).

    Schedule L line-by-line mapping:
    - Line 1: Interest on borrowed money (heloc_interest)
    - Line 2: Investment income (eligible dividends)
    - Line 3: Investment income (non-eligible dividends)
    - Line 4: Investment income (interest/other)
    - Line 5: Taxable capital gains (inclusion_rate × realized_gains)
    - Line 6: Net rental income
    - Line 7: Total investment income (sum of lines 2-6)
    - Line 8: Deductible interest (min of line 1 and line 7 + carry-forward)
    - Line 9: Carry-forward (line 1 - line 8, if positive)

    Pure function (DP#3): same inputs → same output.

    Args:
        heloc_interest: Total HELOC interest paid this year (Schedule L, line 1)
        investment_income: Net investment income for backward compat (dividends + interest)
            If detailed breakdown is provided, this is overridden.
        carry_forward_prior: Unused interest from prior years (Schedule L, carry-forward)
        capital_gains_realized: Capital gains realized this year (full amount)
        capital_gains_inclusion: CG inclusion rate (default 50%)
        eligible_dividend_income: Eligible dividend income (Schedule L, line 2)
        non_eligible_dividend_income: Non-eligible dividend income (Schedule L, line 3)
        interest_income: Interest and other investment income (Schedule L, line 4)
        rental_income_net: Net rental income after expenses (Schedule L, line 6)

    Returns:
        Dict with federal, Quebec, and carry-forward details, plus Schedule L lines
    """
    # Federal: full deduction (no limit)
    federal_deductible = heloc_interest

    # Schedule L line-by-line mapping
    # If detailed breakdown is provided, use it; otherwise use the aggregate
    has_detail = any(v > 0 for v in [eligible_dividend_income, non_eligible_dividend_income, interest_income, rental_income_net])
    
    if has_detail:
        line_2_eligible_dividends = eligible_dividend_income
        line_3_non_eligible_dividends = non_eligible_dividend_income
        line_4_interest = interest_income
        line_5_taxable_cg = capital_gains_realized * capital_gains_inclusion
        line_6_rental = rental_income_net
        total_qc_income = line_2_eligible_dividends + line_3_non_eligible_dividends + line_4_interest + line_5_taxable_cg + line_6_rental
    else:
        # Backward compat: aggregate investment_income includes dividends + interest
        line_2_eligible_dividends = 0  # Not separated in aggregate
        line_3_non_eligible_dividends = 0
        line_4_interest = investment_income  # Treated as interest/other income
        line_5_taxable_cg = capital_gains_realized * capital_gains_inclusion
        line_6_rental = 0
        taxable_cg = capital_gains_realized * capital_gains_inclusion
        total_qc_income = investment_income + taxable_cg

    qc_deduction_limit = total_qc_income + carry_forward_prior

    qc_deductible = min(heloc_interest, qc_deduction_limit)
    new_carry_forward = max(0, heloc_interest - qc_deductible)

    # Tax impact at a given marginal rate
    # (caller should compute with actual bracket-aware calculation)
    qc_shortfall = heloc_interest - qc_deductible

    # Strategy implication
    income_deficit = max(0, heloc_interest - total_qc_income)
    dividend_income_needed = income_deficit  # Dividends would cover this

    result = {
        'heloc_interest': heloc_interest,
        'investment_income': investment_income if not has_detail else total_qc_income,
        'capital_gains_realized': capital_gains_realized,
        'taxable_cg': line_5_taxable_cg,
        'total_qc_income': total_qc_income,
        'carry_forward_prior': carry_forward_prior,
        'federal_deductible': federal_deductible,
        'qc_deduction_limit': qc_deduction_limit,
        'qc_deductible': qc_deductible,
        'new_carry_forward': new_carry_forward,
        'qc_shortfall': qc_shortfall,
        'income_deficit': income_deficit,
        'dividend_income_needed': dividend_income_needed,
    }
    
    # Add Schedule L line items
    result['schedule_l'] = {
        'line_1_interest_on_borrowed_money': heloc_interest,
        'line_2_eligible_dividends': line_2_eligible_dividends,
        'line_3_non_eligible_dividends': line_3_non_eligible_dividends,
        'line_4_interest_and_other': line_4_interest,
        'line_5_taxable_capital_gains': line_5_taxable_cg,
        'line_6_net_rental_income': line_6_rental,
        'line_7_total_investment_income': total_qc_income,
        'line_8_deductible_interest': qc_deductible,
        'line_9_carry_forward': new_carry_forward,
    }
    
    return result


# =============================================================================
# Year-by-Year Tracker
# =============================================================================

@dataclass
class QuebecDeductionTracker:
    """Track Quebec interest deduction and carry-forward year by year.

    This is a stateful tracker that accumulates carry-forward.
    Use it in simulations where you process one year at a time.
    """
    carry_forward: float = 0.0
    annual_history: List[Dict] = field(default_factory=list)

    def process_year(
        self,
        year: int,
        heloc_interest: float,
        investment_income: float,
        capital_gains_realized: float = 0,
        capital_gains_inclusion: float = 0.50,
    ) -> Dict:
        """Process one year of Quebec deduction tracking.

        Args:
            year: Calendar year
            heloc_interest: HELOC interest paid
            investment_income: Dividends + interest income
            capital_gains_realized: Realized capital gains this year
            capital_gains_inclusion: CG inclusion rate

        Returns:
            Dict with year's deduction details
        """
        result = quebec_interest_deduction(
            heloc_interest=heloc_interest,
            investment_income=investment_income,
            carry_forward_prior=self.carry_forward,
            capital_gains_realized=capital_gains_realized,
            capital_gains_inclusion=capital_gains_inclusion,
        )
        result['year'] = year
        self.carry_forward = result['new_carry_forward']
        self.annual_history.append(result)
        return result

    def total_qc_deduction_shortfall(self) -> float:
        """Total Quebec shortfall across all years."""
        return sum(r.get('qc_shortfall', 0) for r in self.annual_history)

    def total_carry_forward(self) -> float:
        """Current unused carry-forward."""
        return self.carry_forward

    def summary(self) -> str:
        """Human-readable summary of Quebec deduction tracking."""
        lines = [
            "📊 QUEBEC INTEREST DEDUCTION TRACKER",
            f"  Current carry-forward: ${self.carry_forward:,.0f}",
            f"  Total QC shortfall: ${self.total_qc_deduction_shortfall():,.0f}",
            "",
            "  Year  | HELOC Int. | Inv. Inc. | QC Deduct. | Fed. Deduct. | Carry Fwd.",
            "  " + "-" * 70,
        ]
        for r in self.annual_history:
            lines.append(
                f"  {r['year']}  | ${r['heloc_interest']:>9,.0f} | "
                f"${r['investment_income']:>9,.0f} | "
                f"${r['qc_deductible']:>9,.0f} | "
                f"${r['federal_deductible']:>9,.0f} | "
                f"${r['new_carry_forward']:>9,.0f}"
            )
        return "\n".join(lines)


# =============================================================================
# Strategy Optimization for Quebec readvanceable mortgage strategy
# =============================================================================

def quebec_sm_portfolio_optimization(
    heloc_interest: float,
    current_dividend_income: float = 0,
    current_interest_income: float = 0,
    current_cg_realized: float = 0,
    target_deduction_pct: float = 1.0,
) -> Dict:
    """Optimize readvanceable-loan portfolio composition for Quebec interest deduction.

    In Quebec, having sufficient investment income is critical for
    claiming the readvanceable-loan interest deduction. If your portfolio is too
    growth-heavy (no dividends), you may not have enough investment
    income to fully deduct the HELOC interest.

    This function recommends the dividend yield needed to fully
    deduct the interest in Quebec.

    Args:
        heloc_interest: Annual HELOC interest
        current_dividend_income: Current annual dividend income
        current_interest_income: Current annual interest income
        current_cg_realized: Current realized capital gains
        target_deduction_pct: Target deduction coverage (1.0 = 100%)

    Returns:
        Dict with optimization recommendations
    """
    total_current_income = current_dividend_income + current_interest_income + current_cg_realized * 0.50
    target_income = heloc_interest * target_deduction_pct
    income_gap = max(0, target_income - total_current_income)

    # Strategy options to fill the gap
    options = []

    # Option 1: Add dividend ETFs (e.g., XEI ~5% yield)
    if income_gap > 0:
        div_yield = 0.05  # Typical Canadian dividend ETF yield
        dividend_capital_needed = income_gap / div_yield
        options.append({
            'strategy': 'add_dividend_etf',
            'description': f'Add ${dividend_capital_needed:,.0f} in Canadian dividend ETFs (5% yield)',
            'income_generated': income_gap,
            'capital_needed': dividend_capital_needed,
        })

    # Option 2: Increase realized gains (sell and rebuy)
    if income_gap > 0:
        options.append({
            'strategy': 'realize_capital_gains',
            'description': f'Realize ${income_gap / 0.50:,.0f} in capital gains',
            'income_generated': income_gap,
            'capital_needed': 0,
            'tax_cost_note': 'Triggering CG has tax cost — trade-off with deduction',
        })

    # Option 3: Current state is fine
    if income_gap <= 0:
        options.append({
            'strategy': 'no_change',
            'description': 'Current investment income covers HELOC interest deduction',
            'income_generated': 0,
            'capital_needed': 0,
        })

    return {
        'heloc_interest': heloc_interest,
        'current_income': total_current_income,
        'current_dividend': current_dividend_income,
        'current_interest': current_interest_income,
        'current_cg_taxable': current_cg_realized * 0.50,
        'target_income': target_income,
        'income_gap': income_gap,
        'qc_deduction_coverage': total_current_income / heloc_interest if heloc_interest > 0 else 0,
        'qc_deduction_fully_covered': total_current_income >= heloc_interest,
        'options': options,
    }


def compute_sm_qc_benefit(
    readvance_heloc_balance: float,
    heloc_rate: float,
    deductible_proportion: float,
    nonreg_balance: float,
    qc_carry_forward: float,
    marginal_rate: float,
    sim_year: int,
    non_reg_yield_rate: float = 0.02,
    portfolio_data: Dict = None,
) -> Dict:
    """Compute readvanceable mortgage strategy tax benefit including Quebec deduction limits.
    
    DP#10/DP#25: Business logic extracted from simulation layer into the
    Quebec deduction module. This is a pure function — the same inputs
    always produce the same outputs.
    
    Args:
        readvance_heloc_balance: Current readvanceable HELOC balance
        heloc_rate: HELOC interest rate for this year
        deductible_proportion: Proportion of HELOC interest that is deductible
                               (from HELOC tracing — investment-purpose advances / total)
        nonreg_balance: Non-registered account balance (for dividend yield estimation)
        qc_carry_forward: Quebec deduction carry-forward from prior years
        marginal_rate: Primary earner's marginal tax rate
        sim_year: Calendar year for this simulation step
    
    Returns:
        Dict with:
            - readvance_interest: Total readvanceable HELOC interest
            - deductible_interest: Tax-deductible portion (after HELOC tracing)
            - qc_deductible: Quebec-allowed deduction this year
            - qc_carry_forward: New carry-forward (unused amount)
            - readvance_tax_savings: Tax savings from the deduction
    """
    readvance_interest = readvance_heloc_balance * heloc_rate
    deductible_interest = readvance_interest * deductible_proportion
    
    # DP#2/DP#27: Use portfolio composition data when available,
    # otherwise fall back to configurable yield rate (not hardcoded 2%)
    from countries.canada.portfolio import compute_investment_income
    if portfolio_data:
        income_by_type = compute_investment_income(nonreg_balance, yield_data=portfolio_data)
    else:
        # DP#2: Pass configurable rate directly — no scaling against hardcoded 2%
        income_by_type = compute_investment_income(
            nonreg_balance, yield_data=None, default_yield_rate=non_reg_yield_rate
        )
    nonreg_investment_income = income_by_type['total_investment_income']
    
    qc_result = quebec_interest_deduction(
        heloc_interest=deductible_interest,
        investment_income=nonreg_investment_income,
        carry_forward_prior=qc_carry_forward,
        capital_gains_realized=0,
    )
    
    qc_deductible = qc_result['qc_deductible']
    new_carry_forward = qc_result['new_carry_forward']
    readvance_tax_savings = qc_deductible * marginal_rate
    
    return {
        'readvance_interest': readvance_interest,
        'deductible_interest': deductible_interest,
        'qc_deductible': qc_deductible,
        'qc_carry_forward': new_carry_forward,
        'readvance_tax_savings': readvance_tax_savings,
    }