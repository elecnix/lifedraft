#!/usr/bin/env python3
"""
Mortgage Renewal Model — Term-end events for the simulation engine.

When a mortgage term ends, the borrower must renew at a new rate.
The new rate changes the monthly payment and the amortization schedule.

Per DP#10: this module models the renewal mechanism (not a branded product).
Per DP#7: model the mechanics, not the product name.
Per DP#20: the new rate comes from year-versioned data.

SCENARIO 3.2: Variable vs Fixed at refinance — comparing 10-year
outcomes under different rate path choices.

Usage:
    from countries.canada.renewal_model import RenewalEvent, MortgageTerm, simulate_renewal_path

    term = MortgageTerm(rate=0.0404, start_year=2026, term_years=3)
    renewal = RenewalEvent(year=2029, new_rate=0.05, new_term_years=5)
    path = simulate_renewal_path(terms=[term, renewal], balance=400000, amortization_years=25)
"""

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from copy import deepcopy


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class MortgageTerm:
    """A single mortgage term with a fixed rate and duration.

    DP#8: This is a data object, not a class hierarchy.
    Each term has a start year, a fixed rate, and a duration.

    Attributes:
        rate: Annual interest rate for this term (decimal)
        start_year: Calendar year the term starts
        term_years: Duration of the term in years
        lender: Lender identifier (optional, for DP#7 product comparison)
    """
    rate: float
    start_year: int
    term_years: int = 5
    lender: str = ""

    @property
    def end_year(self) -> int:
        return self.start_year + self.term_years

    @property
    def total_months(self) -> int:
        return self.term_years * 12

    def contains_year(self, year: int) -> bool:
        """Check if a calendar year falls within this term."""
        return self.start_year <= year < self.end_year

    def contains_month(self, month_index: int, start_year: int = 2026) -> bool:
        """Check if a month index falls within this term.

        Args:
            month_index: 0-based month index from simulation start
            start_year: Calendar year of simulation start
        """
        month_year = start_year + month_index // 12
        return self.start_year <= month_year < self.end_year


@dataclass
class RenewalEvent:
    """A mortgage renewal at term end.

    When a term ends, the borrower:
    1. Can renew with the same lender (no penalty)
    2. Can switch to a different lender (may have discharge fees)
    3. Can change the term length (e.g., 3yr fixed → 5yr variable)
    4. Can increase/borrow more (cash-out refinance)

    Attributes:
        year: Calendar year of renewal
        previous_term: The term that just ended
        new_term: The new term being entered
        discharge_fee: Fee to switch lenders (0 if staying)
        cash_out: Extra money borrowed at renewal (0 if no change)
    """
    year: int
    previous_term: MortgageTerm
    new_term: MortgageTerm
    discharge_fee: float = 0.0
    cash_out: float = 0.0

    @property
    def rate_change(self) -> float:
        """Difference in rate between old and new term."""
        return self.new_term.rate - self.previous_term.rate

    @property
    def is_rate_increase(self) -> bool:
        return self.rate_change > 0


@dataclass
class RenewalPathResult:
    """Result of simulating a mortgage through multiple renewals.

    Attributes:
        terms: List of all terms (original + renewals)
        annual_summaries: Year-by-year payment/interest/principal data
        total_interest_paid: Cumulative interest over all terms
        total_principal_paid: Cumulative principal over all terms
        final_balance: Mortgage balance at end of projection
        renewal_events: List of renewal events with cost implications
    """
    terms: List[MortgageTerm] = field(default_factory=list)
    annual_summaries: List[Dict] = field(default_factory=list)
    total_interest_paid: float = 0.0
    total_principal_paid: float = 0.0
    final_balance: float = 0.0
    renewal_events: List[RenewalEvent] = field(default_factory=list)


# =============================================================================
# Pure Functions — Renewal Calculations
# =============================================================================

def monthly_payment(balance: float, annual_rate: float,
                    amortization_years: int) -> float:
    """Standard mortgage monthly payment calculation.

    Pure function (DP#3).
    """
    if balance <= 0 or amortization_years <= 0:
        return 0.0

    if annual_rate == 0:
        return balance / (amortization_years * 12)

    monthly_rate = annual_rate / 12
    n_payments = amortization_years * 12

    # P = L × r(1+r)^n / ((1+r)^n - 1)
    if monthly_rate == 0:
        return balance / n_payments

    factor = (1 + monthly_rate) ** n_payments
    # Defense in depth: a rate so small that (1+r)^n underflows to exactly 1.0
    # makes factor - 1 == 0; the annuity degenerates to straight-line.
    if factor - 1 == 0:
        return balance / n_payments
    return balance * monthly_rate * factor / (factor - 1)


def simulate_renewal_path(
    initial_balance: float,
    terms: List[MortgageTerm],
    amortization_years: int = 25,
    projection_years: int = 10,
    extra_monthly: float = 0.0,
) -> RenewalPathResult:
    """Simulate a mortgage through multiple terms with renewal events.

    SCENARIO 3.2: Compare "3yr fixed at 4.04%, renewal at 5.0%"
    vs "5yr variable at 3.75%, renewal at 4.5%" over 10 years.

    The function simulates month-by-month amortization across terms,
    recalculating the payment at each renewal based on the new rate
    and remaining amortization.

    Pure function (DP#3): same inputs → same outputs.

    Args:
        initial_balance: Starting mortgage balance
        terms: List of MortgageTerm objects, in chronological order
        amortization_years: Total amortization period
        projection_years: Total years to project
        extra_monthly: Optional extra monthly payment

    Returns:
        RenewalPathResult with full projection
    """
    if not terms:
        return RenewalPathResult()

    # Sort terms by start year
    sorted_terms = sorted(terms, key=lambda t: t.start_year)
    start_year = sorted_terms[0].start_year

    balance = initial_balance
    total_amort_months = amortization_years * 12
    months_elapsed = 0

    # Track renewal events
    renewal_events = []

    # Month-by-month simulation
    annual_data = {}
    total_interest = 0.0
    total_principal = 0.0

    # Determine current rate from terms
    def get_rate_for_month(month_idx: int) -> float:
        sim_year = start_year + month_idx // 12
        for term in sorted_terms:
            if term.contains_year(sim_year):
                return term.rate
        return sorted_terms[-1].rate  # Fallback to last known

    for month in range(projection_years * 12):
        sim_year = start_year + month // 12

        # Current rate
        current_rate = get_rate_for_month(month)

        # Monthly interest
        monthly_interest = balance * current_rate / 12

        # Calculate payment (recalculate at renewal boundaries)
        remaining_amort = max(1, total_amort_months - months_elapsed)
        pmt = monthly_payment(balance, current_rate,
                              math.ceil(remaining_amort / 12))

        # Principal portion
        principal_portion = pmt - monthly_interest
        if principal_portion > balance:
            principal_portion = balance

        total_principal_month = principal_portion + min(extra_monthly, balance - principal_portion)
        total_principal_month = max(0, min(total_principal_month, balance))

        # Update balance
        balance -= total_principal_month
        months_elapsed += 1

        total_interest += monthly_interest
        total_principal += total_principal_month

        # Accumulate annual data
        if sim_year not in annual_data:
            annual_data[sim_year] = {
                'year': sim_year,
                'total_payment': 0,
                'total_interest': 0,
                'total_principal': 0,
                'end_balance': balance,
                'rate': current_rate,
            }
        annual_data[sim_year]['total_payment'] += pmt + extra_monthly
        annual_data[sim_year]['total_interest'] += monthly_interest
        annual_data[sim_year]['total_principal'] += total_principal_month
        annual_data[sim_year]['end_balance'] = balance
        annual_data[sim_year]['rate'] = current_rate

    # Detect renewal events
    for i in range(1, len(sorted_terms)):
        prev = sorted_terms[i - 1]
        curr = sorted_terms[i]
        renewal_events.append(RenewalEvent(
            year=curr.start_year,
            previous_term=prev,
            new_term=curr,
        ))

    # Compile annual summaries
    annual_summaries = sorted(annual_data.values(), key=lambda d: d['year'])

    return RenewalPathResult(
        terms=sorted_terms,
        annual_summaries=annual_summaries,
        total_interest_paid=total_interest,
        total_principal_paid=total_principal,
        final_balance=balance,
        renewal_events=renewal_events,
    )


def compare_rate_term_options(
    mortgage_balance: float,
    amortization_years: int,
    options: List[Dict],
    projection_years: int = 10,
    renewal_assumptions: Dict[str, float] = None,
) -> Dict:
    """Compare different rate/term options at mortgage renewal.

    SCENARIO 3.2: Broker offers 3yr fixed at 4.04% or 5yr variable at 3.75%.
    Which path is better over 10 years?

    Each option specifies a term with rate and duration. After the term ends,
    the renewal rate is assumed from renewal_assumptions.

    Args:
        mortgage_balance: Current mortgage balance
        amortization_years: Remaining amortization
        options: List of dicts, each with:
            - name: Option name
            - rate: Initial rate
            - term_years: Term duration
            - rate_type: 'fixed' or 'variable'
        projection_years: Total time horizon
        renewal_assumptions: Dict mapping rate_type to assumed renewal rate
            e.g., {'fixed': 0.05, 'variable': 0.045}

    Returns:
        Dict with comparison results and ranking
    """
    if renewal_assumptions is None:
        renewal_assumptions = {'fixed': 0.05, 'variable': 0.045}

    results = []
    start_year = 2026

    for opt in options:
        name = opt['name']
        initial_rate = opt['rate']
        term_years = opt['term_years']
        rate_type = opt.get('rate_type', 'fixed')

        # Build terms for this option
        terms = []
        remaining_projection = projection_years
        current_rate = initial_rate
        current_year = start_year

        while remaining_projection > 0:
            term_duration = min(term_years, remaining_projection)
            terms.append(MortgageTerm(
                rate=current_rate,
                start_year=current_year,
                term_years=term_duration,
                lender=name,
            ))
            remaining_projection -= term_duration
            current_year += term_duration

            # Renewal: assume rate moves to the assumed renewal rate
            renewal_rate = renewal_assumptions.get(rate_type, current_rate)
            current_rate = renewal_rate

        # Simulate this path
        path_result = simulate_renewal_path(
            initial_balance=mortgage_balance,
            terms=terms,
            amortization_years=amortization_years,
            projection_years=projection_years,
        )

        results.append({
            'name': name,
            'rate_type': rate_type,
            'initial_rate': initial_rate,
            'term_years': term_years,
            'total_interest': path_result.total_interest_paid,
            'total_principal': path_result.total_principal_paid,
            'final_balance': path_result.final_balance,
            'total_cost': path_result.total_interest_paid + path_result.final_balance,
            'num_renewals': len(path_result.renewal_events),
            'renewal_events': [
                {
                    'year': e.year,
                    'rate_change': e.rate_change,
                    'new_rate': e.new_term.rate,
                }
                for e in path_result.renewal_events
            ],
        })

    # Rank by total interest (lowest wins)
    results.sort(key=lambda r: r['total_interest'])

    return {
        'options': results,
        'best': results[0]['name'] if results else None,
        'total_cost_spread': (results[-1]['total_interest'] - results[0]['total_interest']
                             if len(results) > 1 else 0),
        'projection_years': projection_years,
        'mortgage_balance': mortgage_balance,
    }


def rate_sensitivity_analysis(
    mortgage_balance: float,
    amortization_years: int,
    base_rate: float,
    term_years: int = 5,
    projection_years: int = 10,
    renewal_rate_range: List[float] = None,
) -> Dict:
    """Sensitivity analysis on renewal rates.

    SCENARIO 3.2 extension: "What if renewal is 6%? 3.5%?"

    Shows how total interest paid varies with the assumed
    renewal rate, helping the user understand their risk exposure.

    Args:
        mortgage_balance: Current mortgage balance
        amortization_years: Remaining amortization
        base_rate: Current contract rate
        term_years: Current term length
        projection_years: Total time horizon
        renewal_rate_range: List of renewal rates to test

    Returns:
        Dict with sensitivity results
    """
    if renewal_rate_range is None:
        renewal_rate_range = [0.035, 0.04, 0.045, 0.05, 0.055, 0.06, 0.065]

    results = []
    for renewal_rate in renewal_rate_range:
        terms = [
            MortgageTerm(rate=base_rate, start_year=2026, term_years=term_years),
            MortgageTerm(rate=renewal_rate, start_year=2026 + term_years,
                         term_years=projection_years - term_years),
        ]

        path = simulate_renewal_path(
            initial_balance=mortgage_balance,
            terms=terms,
            amortization_years=amortization_years,
            projection_years=projection_years,
        )

        results.append({
            'renewal_rate': renewal_rate,
            'total_interest': path.total_interest_paid,
            'annual_payment_change': 0,  # Calculated below
        })

    # Calculate payment change at each renewal rate vs base
    base_pmt = monthly_payment(mortgage_balance, base_rate, amortization_years)
    for r in results:
        renewal_pmt = monthly_payment(
            mortgage_balance * 0.85,  # Approximate balance at renewal
            r['renewal_rate'],
            max(5, amortization_years - term_years),
        )
        r['annual_payment_change'] = (renewal_pmt - base_pmt) * 12

    return {
        'sensitivity_results': results,
        'base_rate': base_rate,
        'current_monthly_payment': base_pmt,
        'mortgage_balance': mortgage_balance,
        'projection_years': projection_years,
    }