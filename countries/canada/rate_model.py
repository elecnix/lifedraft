#!/usr/bin/env python3
"""
Rate Term Modeling for Canadian Mortgages

Handles:
- Fixed and variable rate terms with proper renewal modeling
- Month-by-month amortization that recalculates at rate changes
- HELOC rate tracking (prime + spread)
- Multiple renewal scenarios (best/expected/worst case)
- Rate stress testing (rate shock, rate path sensitivity)
- readvanceable mortgage readvanceable mortgage support
- IRD (Interest Rate Differential) penalty estimates for early break

All rates expressed as decimals (e.g., 0.0404 = 4.04%).
"""

import math
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from copy import deepcopy

# Thread 5: BoC data provider available for rate loading
from countries.canada.boc_data import BoCDataProvider

# DP#10 / issue #724: the IRD penalty *math* lives in ird_penalty.py (one
# module per program, one spelling of the rule). estimate_ird_penalty below
# delegates to those functions rather than restating the formula, so the two
# cannot drift.
from countries.canada.ird_penalty import (
    compute_ird_penalty, compute_three_months_interest,
)

# Issue #113: prepayment-privilege pricing lives in its own module (one
# spelling of the allowance split and the excess penalty, DP#10); the
# amortization loop calls it rather than restating it.
from countries.canada.prepayment_privileges import price_monthly_extra

# DP#10 / issue #723: ReadvanceableMortgage was moved here from account_models.py
# (it is a mortgage-rate / product concern, not a registered account). It shares
# the one "apply a rate of return to a balance" implementation with the account
# types; importing the helper keeps a single spelling of that math (no clone).
# No circular import: account_models imports only dataclasses/typing.
from countries.canada.account_models import _apply_growth

# Issue #113: tolerance for the "this prepayment repays the tranche in
# full" comparison against a float balance.
_BALANCE_EPSILON = 1e-6


# ──────────────────────────────────────────────────────
# Rate Path Modeling
# ──────────────────────────────────────────────────────

@dataclass
class RateStep:
    """A single rate period within a rate path.
    
    Attributes:
        rate: Annual interest rate (decimal, e.g. 0.0404)
        start_year: Year index when this rate takes effect (0-based)
        end_year: Year index when this rate ends (exclusive)
        label: Human-readable description
    """
    rate: float
    start_year: int
    end_year: int
    label: str = ""

    @property
    def duration_years(self) -> float:
        return self.end_year - self.start_year


@dataclass
class RatePath:
    """Year-by-year interest rate path for a mortgage or HELOC.
    
    Supports:
    - Fixed terms that renew at different rates
    - Variable rates with annual adjustments
    - Custom rate paths (e.g., stress scenarios)
    - Blended rates for amortization calculations
    """
    name: str
    steps: List[RateStep]
    rate_type: str = "mixed"  # "fixed", "variable", "mixed"
    
    def get_rate(self, year: int) -> float:
        """Get the rate for a given year index (0-based)."""
        for step in self.steps:
            if step.start_year <= year < step.end_year:
                return step.rate
        # Default to last known rate
        if self.steps:
            return self.steps[-1].rate
        return 0.05  # fallback
    
    def get_rate_month(self, month: int) -> float:
        """Get the rate for a given month index (0-based)."""
        year = month / 12.0
        return self.get_rate(int(year))
    
    @property
    def projection_years(self) -> int:
        if self.steps:
            return max(s.end_year for s in self.steps)
        return 10
    
    @property
    def average_rate(self) -> float:
        """Weighted-average rate over the entire path."""
        total_weight = 0
        weighted_sum = 0
        for step in self.steps:
            weight = step.duration_years
            weighted_sum += step.rate * weight
            total_weight += weight
        return weighted_sum / total_weight if total_weight > 0 else 0
    
    def description(self) -> str:
        lines = [f"  {self.name}"]
        for step in self.steps:
            duration = step.duration_years
            rate_pct = step.rate * 100
            if duration == 1:
                lines.append(f"    Year {step.start_year+1}: {rate_pct:.2f}%{f' — {step.label}' if step.label else ''}")
            else:
                lines.append(f"    Years {step.start_year+1}–{step.end_year}: {rate_pct:.2f}%{f' — {step.label}' if step.label else ''}")
        return "\n".join(lines)


def build_rate_path(name: str, initial_rate: float, term_years: int, 
                    rate_type: str, renewal_rates: List[float] = None,
                    projection_years: int = 10,
                    renewal_date: str = None,
                    contract_start_date: str = None) -> RatePath:
    """Build a rate path from term parameters.
    
    Args:
        name: Scenario name
        initial_rate: Rate during the initial term (decimal)
        term_years: Length of the initial term in years
        rate_type: "fixed" or "variable"
        renewal_rates: List of rates for each renewal period.
                       If None, uses [0.05] (5% assumption).
                       For variable, can include annual adjustments.
        projection_years: Total years to project
        renewal_date: Optional renewal date (YYYY-MM-DD). If provided,
                       term_years is computed from contract_start_date to renewal_date.
        contract_start_date: Optional contract start date (YYYY-MM-DD).
    
    Returns:
        RatePath with appropriate steps
    """
    steps = []
    
    if renewal_rates is None:
        renewal_rates = [0.05]  # default assumption
    
    # If renewal_date and contract_start_date are provided, compute term
    if renewal_date and contract_start_date:
        from datetime import date
        try:
            start = date.fromisoformat(contract_start_date)
            renewal = date.fromisoformat(renewal_date)
            computed_term = (renewal - start).days / 365.25
            if computed_term > 0:
                term_years = int(computed_term)
        except (ValueError, TypeError):
            pass  # Fall back to provided term_years
    
    # Initial term
    steps.append(RateStep(
        rate=initial_rate,
        start_year=0,
        end_year=min(term_years, projection_years),
        label=f"{rate_type.title()} term"
    ))
    
    # Renewal periods
    if term_years < projection_years:
        current_year = term_years
        rate_idx = 0
        while current_year < projection_years:
            rate = renewal_rates[min(rate_idx, len(renewal_rates) - 1)]
            next_renewal = min(current_year + 5, projection_years)  # 5-year renewals
            steps.append(RateStep(
                rate=rate,
                start_year=current_year,
                end_year=next_renewal,
                label=f"{'Renewal' if rate_type == 'fixed' else 'Adjustment'}"
            ))
            current_year = next_renewal
            rate_idx += 1
    
    return RatePath(name=name, steps=steps, rate_type=rate_type)


def build_broker_scenarios(current_rate: float = 0.05,
                           projection_years: int = 10,
                           broker_offers: List[Dict] = None,
                           renewal_overlays: Dict[str, List[float]] = None,
                           market_rates_provider = None,
                           ) -> Dict[str, RatePath]:
    """Build broker scenarios from input config or defaults.
    
    Args:
        current_rate: Current mortgage rate (baseline)
        projection_years: Number of years to project
        broker_offers: List of dicts from input.json, each with:
            - name: Offer label (e.g. "3yr fixed @ 4.04%")
            - rate: Initial rate (e.g. 0.0404)
            - term_years: Term length
            - type: "fixed" or "variable"
            - renewal_rate_assumption: Expected renewal rate (e.g. 0.05)
        renewal_overlays: Dict mapping scenario type to renewal rates
        market_rates_provider: Optional MarketRatesProvider for loading
            current rates. If provided, prime/overnight rates come from it.
    
    For each broker offer, creates 3 scenarios:
    - 'expected': renewal at moderate rate
    - 'best_case': renewal at favorable rate  
    - 'worst_case': renewal at high rate
    """
    if renewal_overlays is None:
        renewal_overlays = {
            'best_case': [0.035, 0.035],
            'worst_case': [0.065, 0.065],
        }
    
    # Thread 7: If market_rates_provider is given, use it for prime rate
    if market_rates_provider is not None and broker_offers is None:
        try:
            rates = market_rates_provider.get_current_rates()
            current_rate = rates.prime_rate
        except Exception:
            pass  # Fall back to provided current_rate
    
    # Thread 7: Load broker offers from market_rates_provider if none given
    if market_rates_provider is not None and broker_offers is None:
        try:
            market_rates = market_rates_provider.get_current_rates()
            if market_rates.mortgage_rates:
                broker_offers = [
                    {
                        'name': f"{q.term_years}yr {q.rate_type} @ {q.rate*100:.2f}%",
                        'rate': q.rate,
                        'term_years': q.term_years,
                        'type': q.rate_type,
                        'renewal_rate_assumption': rates.prime_rate,
                    }
                    for q in market_rates.mortgage_rates
                ]
        except Exception:
            pass  # Fall back to defaults
    
    scenarios = {}
    
    # Build from broker_offers if provided (from input.json)
    if broker_offers:
        for offer in broker_offers:
            name = offer.get('name', offer.get('label', 'unknown'))
            rate = offer.get('rate', 0.04)
            term = offer.get('term_years', 5)
            rtype = offer.get('type', 'fixed')
            expected_renewal = offer.get('renewal_rate_assumption', 0.05)
            
            # Expected
            scenarios[f"{name} — expected"] = build_rate_path(
                f"{name} → {expected_renewal*100:.1f}% expected",
                initial_rate=rate, term_years=term, rate_type=rtype,
                renewal_rates=[expected_renewal] * 2,
                projection_years=projection_years
            )
            # Best case
            best = renewal_overlays.get('best_case', [0.035, 0.035])
            scenarios[f"{name} — best case"] = build_rate_path(
                f"{name} → {best[0]*100:.1f}% best case",
                initial_rate=rate, term_years=term, rate_type=rtype,
                renewal_rates=best,
                projection_years=projection_years
            )
            # Worst case
            worst = renewal_overlays.get('worst_case', [0.065, 0.065])
            scenarios[f"{name} — worst case"] = build_rate_path(
                f"{name} → {worst[0]*100:.1f}% worst case",
                initial_rate=rate, term_years=term, rate_type=rtype,
                renewal_rates=worst,
                projection_years=projection_years
            )
    else:
        # Default demo scenarios with round numbers
        for name, rate, term, rtype, renewal in [
            ("3yr fixed", 0.04, 3, "fixed", 0.05),
            ("5yr variable", 0.0375, 5, "variable", 0.045),
        ]:
            scenarios[f"{name} — expected"] = build_rate_path(
                f"{name} @ {rate*100:.2f}% → {renewal*100:.1f}% expected",
                initial_rate=rate, term_years=term, rate_type=rtype,
                renewal_rates=[renewal] * 2,
                projection_years=projection_years
            )
            best = renewal_overlays.get('best_case', [0.035, 0.035])
            scenarios[f"{name} — best case"] = build_rate_path(
                f"{name} @ {rate*100:.2f}% → {best[0]*100:.1f}% best case",
                initial_rate=rate, term_years=term, rate_type=rtype,
                renewal_rates=best,
                projection_years=projection_years
            )
            worst = renewal_overlays.get('worst_case', [0.065, 0.065])
            scenarios[f"{name} — worst case"] = build_rate_path(
                f"{name} @ {rate*100:.2f}% → {worst[0]*100:.1f}% worst case",
                initial_rate=rate, term_years=term, rate_type=rtype,
                renewal_rates=worst,
                projection_years=projection_years
            )
    
    # Current rate baseline
    scenarios["current — no change"] = build_rate_path(
        "Current rate (no change)",
        initial_rate=current_rate, term_years=projection_years,
        rate_type="variable",
        renewal_rates=[current_rate],
        projection_years=projection_years
    )
    
    return scenarios


def build_variable_rate_path(name: str, start_rate: float, 
                             annual_adjustments: List[float],
                             term_years: int = 5,
                             renewal_rates: List[float] = None,
                             projection_years: int = 10) -> RatePath:
    """Build a variable rate path with annual rate adjustments during the term.
    
    For variable rates, the rate can change each year within the term.
    After the term, it renews at a new rate.
    
    Args:
        name: Scenario name
        start_rate: Initial rate
        annual_adjustments: Rate for each year during the term (year 1 onwards)
        term_years: Variable term length
        renewal_rates: Rates after term expiry
        projection_years: Total projection length
    """
    steps = []
    
    # Year 0 at start_rate
    current_rate = start_rate
    current_year = 0
    
    # Build year-by-year during term
    for yr in range(term_years):
        if yr > 0 and yr - 1 < len(annual_adjustments):
            current_rate = annual_adjustments[yr - 1]
        
        end_yr = yr + 1
        steps.append(RateStep(
            rate=current_rate,
            start_year=yr,
            end_year=min(end_yr, projection_years),
            label=f"Variable year {yr+1}"
        ))
        if end_yr >= projection_years:
            break
    
    # After term: renewal
    if term_years < projection_years:
        if renewal_rates is None:
            renewal_rates = [0.045]
        rate_idx = 0
        current_year = term_years
        while current_year < projection_years:
            rate = renewal_rates[min(rate_idx, len(renewal_rates) - 1)]
            next_yr = min(current_year + 1, projection_years)
            steps.append(RateStep(
                rate=rate,
                start_year=current_year,
                end_year=next_yr,
                label="Post-term renewal"
            ))
            current_year = next_yr
            rate_idx += 1
    
    return RatePath(name=name, steps=steps, rate_type="variable")


# ──────────────────────────────────────────────────────
# Amortization Calculator
# ──────────────────────────────────────────────────────

def monthly_payment(principal: float, annual_rate: float, 
                    amortization_years: int) -> float:
    """Standard mortgage monthly payment calculation.
    
    Args:
        principal: Outstanding balance
        annual_rate: Annual interest rate (decimal)
        amortization_years: Remaining amortization
    
    Returns:
        Monthly payment amount
    """
    if principal <= 0:
        return 0.0
    if annual_rate <= 0:
        return principal / (amortization_years * 12)
    
    monthly_rate = annual_rate / 12
    n = amortization_years * 12

    # P = L * r(1+r)^n / ((1+r)^n - 1)
    factor = (1 + monthly_rate) ** n
    # Defense in depth: for a rate so small that (1+r)^n underflows to exactly
    # 1.0 in floating point, factor - 1 == 0. The annuity then degenerates to
    # straight-line amortization (principal spread evenly), so return that
    # rather than dividing by zero.
    if factor - 1 == 0:
        return principal / n
    payment = principal * monthly_rate * factor / (factor - 1)
    return payment

def amortization_schedule(principal: float, rate_path: RatePath,
                          amortization_years: int = 25,
                          projection_months: int = 120,
                          extra_payment: float = 0.0,
                          readvance_smith: bool = False,
                          readvance_invest_monthly: bool = True,
                          prepayment_privileges=None) -> List[Dict]:
    """Month-by-month amortization with rate changes.
    
    Handles:
    - Rate changes at renewal (recalculates payment)
    - Extra payments to principal
    - Readvanceable mortgage: readvancing paid principal into HELOC
    - Prepayment privileges (issue #113): when ``prepayment_privileges``
      (a countries.canada.prepayment_privileges.PrepaymentPrivileges) is
      supplied alongside a nonzero ``extra_payment``, each month's extra is
      split into its FREE slices (the annual lump-sum allowance — a percent
      of ORIGINAL principal per year — plus the payment-increase allowance
      of ``multiplier x regular_payment`` at each payment) and the PENALIZED
      excess, charged ``penalty_months_interest`` months of interest at the
      lender's standard variable rate IN the month the excess fires. The
      entry carries 'prepayment_extra'/'prepayment_free'/
      'prepayment_excess'/'prepayment_penalty'; a prepayment that closes the
      loan entirely gets NO privilege protection and the whole amount is
      penalized. Without privileges the extra applies unlimited and free —
      the pre-#113 behaviour, unchanged (DP#32: no declared contract terms,
      no modeled limits).
    
    Args:
        principal: Initial mortgage balance
        rate_path: RatePath with year-by-year rates
        amortization_years: Initial amortization period
        projection_months: Months to project (default 120 = 10 years)
        extra_payment: Additional monthly principal payment
        readvance_smith: Whether to readvance paid principal for investment
        readvance_invest_monthly: Whether readvanceable investment is monthly
        prepayment_privileges: Optional PrepaymentPrivileges governing how
            much of ``extra_payment`` is penalty-free (issue #113). The
            annual allowance buckets reset at each schedule-year boundary
            (the schedule's own year index — the origination-anniversary
            convention, since the engine's schedule carries no calendar
            dates).

    Returns:
        List of dicts with monthly details
    """
    balance = principal
    schedule = []
    remaining_months = amortization_years * 12
    # Issue #113: cumulative free LUMP-SUM prepayments made so far in the
    # current schedule year, against that year's allowance. The
    # payment-increase privilege has no annual accumulator (it is capped per
    # payment, not per year), so it needs none here.
    lump_sum_used_ytd = 0.0
    schedule_year_seen = -1

    # Calculate initial payment
    current_rate = rate_path.get_rate_month(0)
    payment = monthly_payment(balance, current_rate, amortization_years)
    
    heloc_balance = 0.0
    total_interest = 0.0
    total_principal = 0.0
    total_readvanced = 0.0
    
    for month in range(projection_months):
        year_frac = month / 12.0
        year = int(year_frac)

        # Issue #113: the annual lump-sum allowance resets at each schedule-
        # year boundary (the origination-anniversary convention — the engine's
        # schedule carries no calendar dates to align a calendar-year window
        # against).
        if year != schedule_year_seen:
            lump_sum_used_ytd = 0.0
            schedule_year_seen = year
        
        # Check for rate change (recalculate payment at renewal)
        new_rate = rate_path.get_rate_month(month)
        if abs(new_rate - current_rate) > 0.0001:
            current_rate = new_rate
            # Recalculate payment based on remaining balance and amortization
            remaining_amort = max(1, remaining_months) / 12.0
            payment = monthly_payment(balance, current_rate, math.ceil(remaining_amort))
        
        # Monthly interest
        monthly_interest = balance * current_rate / 12
        
        # Principal portion of regular payment (negative = negative amortization)
        principal_portion = payment - monthly_interest
        if principal_portion > balance:
            principal_portion = balance
            payment = balance + monthly_interest

        # Total principal (including extra)
        total_principal_month = principal_portion + min(extra_payment, balance - principal_portion)
        total_principal_month = min(total_principal_month, balance)

        # Issue #113: price the extra against the declared prepayment
        # privileges — free within the allowances, penalized above them.
        # Absent privileges this block never runs and the schedule is
        # byte-identical to the pre-#113 one (DP#32).
        prepayment_extra = 0.0
        prepayment_free = 0.0
        prepayment_excess = 0.0
        prepayment_penalty = 0.0
        if prepayment_privileges is not None and extra_payment > 0:
            balance_after_regular = max(0.0, balance - principal_portion)
            applied_extra = min(extra_payment, balance_after_regular)
            # "Repaid entirely": the requested extra reaches the remaining
            # balance itself (NOT merely this month's applied cap).
            closes_loan = (
                balance_after_regular > _BALANCE_EPSILON
                and extra_payment >= balance_after_regular - _BALANCE_EPSILON
            )
            priced = price_monthly_extra(
                prepayment_privileges, applied_extra, payment,
                lump_sum_used_ytd, closes_loan=closes_loan)
            # Only the lump-sum slice consumed this year's allowance (the
            # per-payment increase privilege has no annual accumulator);
            # a full-repayment prepayment consumes none of it.
            lump_sum_used_ytd += priced['lump_sum']
            prepayment_extra = applied_extra
            prepayment_free = priced['free']
            prepayment_excess = priced['excess']
            prepayment_penalty = priced['penalty']

        # Update balance (negative total_principal_month → balance grows)
        balance -= total_principal_month
        remaining_months -= 1
        
        # Readvanceable mortgage: readvance principal to HELOC
        if readvance_smith and total_principal_month > 0:
            readvanced = total_principal_month if readvance_invest_monthly else 0
            heloc_balance += readvanced
            total_readvanced += readvanced
        
        total_interest += monthly_interest
        total_principal += total_principal_month
        
        schedule.append({
            'month': month + 1,
            'year': year + 1,
            'rate': current_rate,
            'payment': payment,
            'interest': monthly_interest,
            'principal': total_principal_month,
            'balance': balance,
            'heloc_balance': heloc_balance,
            'cumulative_interest': total_interest,
            'cumulative_principal': total_principal,
            'cumulative_readvanced': total_readvanced,
            **({
                # Issue #113: present only when privileges are declared, so
                # every legacy caller's schedule dict keeps its exact shape.
                'prepayment_extra': prepayment_extra,
                'prepayment_free': prepayment_free,
                'prepayment_excess': prepayment_excess,
                'prepayment_penalty': prepayment_penalty,
            } if prepayment_privileges is not None else {}),
        })
        
        if balance <= 0:
            break
    
    return schedule


def annual_summary(schedule: List[Dict]) -> List[Dict]:
    """Aggregate monthly schedule into annual summaries.

    Issue #113: a schedule that prices prepayment privileges carries
    'prepayment_extra'/'prepayment_free'/'prepayment_excess'/
    'prepayment_penalty' on its entries; ``total_payment`` — the figure
    ``apply_solvency`` folds as mortgage debt service — includes BOTH the
    applied extra principal AND the penalty, because the household pays
    both in cash (without them the extra paydown would conjure principal
    reduction out of no outflow). A legacy schedule carries none of those
    keys, every read defaults to 0.0, and the rows are unchanged.
    """
    if not schedule:
        return []
    
    years = {}
    prev_cum_readv = None  # Sentinel: None means we haven't seen any entry yet
    for entry in schedule:
        yr = entry['year']
        if yr not in years:
            start_cum_readv = 0 if prev_cum_readv is None else prev_cum_readv
            years[yr] = {
                'year': yr,
                'payments': 0,
                'total_payment': 0,
                'total_interest': 0,
                'total_principal': 0,
                'total_readvanced': 0,
                'start_balance': entry['balance'] + entry['principal'],
                'end_balance': entry['balance'],
                'heloc_end': entry['heloc_balance'],
                'rate_start': entry['rate'],
                'rate_end': entry['rate'],
                'start_cumulative_readvanced': start_cum_readv,
            }
        y = years[yr]
        y['payments'] += 1
        # Issue #113: the year's cash cost of the mortgage is the regular
        # payments PLUS any applied prepayment principal PLUS the excess-
        # prepayment penalty charged that month. Legacy schedules carry no
        # prepayment keys; these reads default to 0.0 and change nothing.
        y['total_payment'] += (
            entry['payment']
            + entry.get('prepayment_extra', 0.0)
            + entry.get('prepayment_penalty', 0.0))
        if 'prepayment_penalty' in entry:
            y['prepayment_penalty'] = (
                y.get('prepayment_penalty', 0.0)
                + entry['prepayment_penalty'])
        y['total_interest'] += entry['interest']
        y['total_principal'] += entry['principal']
        y['end_balance'] = entry['balance']
        y['heloc_end'] = entry['heloc_balance']
        y['rate_end'] = entry['rate']
        # Track the cumulative for the next year's start
        prev_cum_readv = entry.get('cumulative_readvanced', 0)
    
    # Compute per-year readvanced from cumulative deltas
    for y in years.values():
        yr_num = y['year']
        yr_entries = [e for e in schedule if e['year'] == yr_num]
        if yr_entries:
            end_cum = yr_entries[-1].get('cumulative_readvanced', 0)
            y['total_readvanced'] = max(0, end_cum - y['start_cumulative_readvanced'])
    
    return list(years.values())


# ──────────────────────────────────────────────────────
# HELOC Rate Modeling
# ──────────────────────────────────────────────────────

@dataclass
class HELOCPath:
    """HELOC rate path.

    Issue #654: a household's HELOC almost never shares its mortgage's rate
    -- the two are different credit products (a legacy fixed-rate mortgage
    alongside a prime-linked revolving HELOC is the single most common
    shape among the households this tool exists for). When the household
    HAS declared its own HELOC rate (``fixed_rate``), that value is the
    HELOC rate, full stop -- DP#32: declared data wins, it is never
    overridden by a value derived from a different liability.

    ``mortgage_rate_path``/``prime_spread``/``fixed_spread`` remain as the
    DP#13 placeholder used only when no HELOC rate was ever declared (e.g.
    a bare ``SimulationConfig()`` built directly by a test, never routed
    through a real contract) -- an approximation for absent input, not a
    fallback that overrides supplied input.
    """
    name: str
    prime_spread: float = 0.005  # HELOC = prime + spread
    mortgage_rate_path: RatePath = None
    fixed_spread: float = 0.005  # Extra spread for fixed mortgages
    # Issue #654: the household's own declared HELOC rate. When set, this
    # is returned unconditionally -- the mortgage-derived approximation
    # below is not consulted at all, for any year, regardless of
    # mortgage_type. None means "no HELOC rate was ever declared".
    fixed_rate: Optional[float] = None

    def get_heloc_rate(self, year: int, mortgage_type: str = "variable") -> float:
        """Get HELOC rate for a given year.

        If the household declared its own HELOC rate (``fixed_rate``), that
        rate wins outright (#654) -- ``mortgage_type`` is irrelevant to it.

        Otherwise (no HELOC rate ever declared), approximates one from the
        mortgage rate path as a DP#13 placeholder:
        For variable mortgages: HELOC rate ≈ mortgage rate + small spread
        For fixed mortgages: HELOC rate ≈ prime rate + spread
                           (prime ≈ BoC overnight + 2.2-2.8%)
        """
        if self.fixed_rate is not None:
            return self.fixed_rate

        if self.mortgage_rate_path is None:
            return 0.05 + self.prime_spread

        base = self.mortgage_rate_path.get_rate(year)
        if mortgage_type == "fixed":
            # Fixed: HELOC is on prime, which is different from mortgage rate
            # Prime ≈ mortgage rate - 1.5% for competitive rates, then + spread
            return base - 0.015 + self.prime_spread + self.fixed_spread
        else:
            # Variable: HELOC tracks close to mortgage rate
            return base + self.prime_spread
    
    def get_heloc_rate_month(self, month: int, mortgage_type: str = "variable") -> float:
        """Get HELOC rate for a given month."""
        return self.get_heloc_rate(int(month / 12), mortgage_type)


# ──────────────────────────────────────────────────────
# Readvanceable Mortgage (DP#10: mortgage-rate/product concern)
# ──────────────────────────────────────────────────────

@dataclass
class ReadvanceableMortgage:
    """Readvanceable mortgage: HELOC readvancing for investment.
    
    Tracks:
    - HELOC balance (grows as mortgage principal is paid)
    - Investment balance tied to SM
    - Interest deduction calculations
    """
    heloc_balance: float = 0.0
    investment_balance: float = 0.0
    investment_cost_basis: float = 0.0
    total_interest_paid: float = 0.0
    total_tax_saved: float = 0.0
    
    # Rates
    heloc_rate: float = 0.0  # DP#13: set from config; typical 2026: 0.0495
    
    def readvance(self, principal_paid: float) -> float:
        """Readvance mortgage principal into HELOC for investment.
        
        Called when mortgage principal is paid — that principal becomes
        available as new HELOC room (readvanceable mortgage).
        
        Args:
            principal_paid: Amount of mortgage principal paid this period
        
        Returns:
            Amount readvanced (equals principal_paid)
        """
        self.heloc_balance += principal_paid
        self.investment_balance += principal_paid
        self.investment_cost_basis += principal_paid
        return principal_paid
    
    def grow_investment(self, return_rate: float) -> float:
        """Apply investment growth to SM investment."""
        self.investment_balance, growth = _apply_growth(self.investment_balance, return_rate)
        return growth
    
    def calculate_interest(self, year: int = 0, rate_path=None) -> Tuple[float, float]:
        """Calculate HELOC interest and tax savings.
        
        Args:
            year: Year index (for rate path)
            rate_path: Optional RatePath for variable HELOC rates
        
        Returns:
            (interest, tax_savings)
        """
        rate = self.heloc_rate
        if rate_path:
            rate = rate_path.get_rate(year)
        
        interest = self.heloc_balance * rate
        return interest, 0.0  # tax_savings calculated separately with marginal rate
    
    def annual_summary(self) -> Dict:
        """Return current SM state as a dict."""
        return {
            'heloc_balance': self.heloc_balance,
            'investment_balance': self.investment_balance,
            'investment_cost_basis': self.investment_cost_basis,
            'unrealized_gains': self.investment_balance - self.investment_cost_basis,
            'total_interest_paid': self.total_interest_paid,
            'total_tax_saved': self.total_tax_saved,
            'after_tax_cost_rate': self.heloc_rate,  # Before tax savings
        }


# ──────────────────────────────────────────────────────
# Rate Stress Testing
# ──────────────────────────────────────────────────────

def build_stress_scenarios(base_path: RatePath, 
                           stresses: List[float] = None) -> Dict[str, RatePath]:
    """Generate rate stress scenarios by shifting all rates.
    
    Args:
        base_path: The base rate path
        stresses: List of rate shifts (e.g., [-0.02, -0.01, 0, +0.01, +0.02])
    
    Returns:
        Dict of scenario_name → RatePath
    """
    if stresses is None:
        stresses = [-0.02, -0.01, 0, 0.01, 0.02]
    
    scenarios = {}
    for shift in stresses:
        new_steps = []
        for step in base_path.steps:
            new_steps.append(RateStep(
                rate=max(0.001, step.rate + shift),  # floor at 0.1%
                start_year=step.start_year,
                end_year=step.end_year,
                label=step.label
            ))
        
        name = f"{'+' if shift >= 0 else ''}{shift*100:.1f}% shift"
        if shift == 0:
            name = "base case"
        scenarios[name] = RatePath(
            name=f"{base_path.name} ({name})",
            steps=new_steps,
            rate_type=base_path.rate_type
        )
    
    return scenarios


def build_renewal_stress(base_rate: float, term_years: int,
                         rate_type: str = "fixed",
                         renewal_shifts: List[float] = None,
                         projection_years: int = 10) -> Dict[str, RatePath]:
    """Generate scenarios where the renewal rate varies.
    
    Args:
        base_rate: Rate during the initial term
        term_years: Length of initial term
        rate_type: "fixed" or "variable"
        renewal_shifts: Shifts applied to expected renewal rate
        projection_years: Total years
    """
    if renewal_shifts is None:
        renewal_shifts = [-0.02, -0.01, 0, 0.01, 0.02]
    
    expected_renewal = 0.05 if rate_type == "fixed" else 0.045
    
    scenarios = {}
    for shift in renewal_shifts:
        renewal = expected_renewal + shift
        renewal = max(0.005, renewal)  # floor
        
        name = f"renewal {'+' if shift >= 0 else ''}{shift*100:.1f}%"
        if shift == 0:
            name = "expected renewal"
        
        path = build_rate_path(
            name=name,
            initial_rate=base_rate,
            term_years=term_years,
            rate_type=rate_type,
            renewal_rates=[renewal],
            projection_years=projection_years
        )
        scenarios[name] = path
    
    return scenarios


def build_boc_rate_path_scenario(start_prime: float = 0.07,
                                  boc_changes: List[Tuple[int, float]] = None,
                                  projection_years: int = 10,
                                  boc_provider=None) -> RatePath:
    """Build a rate path from Bank of Canada rate data.

    Args:
        start_prime: Starting prime rate
        boc_changes: List of (year, new_prime_rate) tuples.
            If None, attempts to load from boc_provider or uses flat rate.
        projection_years: Total years
        boc_provider: A BoCDataProvider instance. If provided and
            boc_changes is None, loads forecast from the provider.

    The Smith Manoeuvre is NOT hardcoded here — it is a strategy that
    the optimizer discovers when these conditions hold:
    1. Mortgage is readvanceable (heloc_readvance=True in config)
    2. Investment interest is tax-deductible (CRA §20(1)(c))
    3. After-tax investment return exceeds HELOC interest cost

    See test_rate_model.py::test_readvance_invest_discovered for verification.
    """
    if boc_changes is None and boc_provider is not None:
        # Load forecast from BoC data provider
        try:
            forecast = boc_provider.get_rate_forecast(projection_years)
            boc_changes = [(i, f.prime_rate) for i, f in enumerate(forecast)]
        except Exception:
            pass

    if boc_changes is None:
        # Flat rate: no prediction data available
        boc_changes = [(i, start_prime) for i in range(projection_years)]
    
    steps = []
    for i in range(len(boc_changes)):
        year, rate = boc_changes[i]
        next_year = boc_changes[i + 1][0] if i + 1 < len(boc_changes) else projection_years
        steps.append(RateStep(
            rate=rate,
            start_year=year,
            end_year=min(next_year, projection_years),
            label=f"Prime {rate*100:.2f}%"
        ))
    
    return RatePath(name="Prime Rate Path", steps=steps, rate_type="variable")


def estimate_ird_penalty(current_balance: float, current_rate: float,
                         remaining_months: int, posted_rate: float = None,
                         comparison_rate: float = None,
                         is_open: bool = False) -> Dict:
    """Estimate Interest Rate Differential penalty for breaking a mortgage.

    For most Canadian mortgages, the penalty is the greater of:
    - 3 months' interest, or
    - IRD = (posted_rate - comparison_rate) × balance × remaining_months / 12

    An OPEN term is the exception: it can be broken at any time with NO
    penalty (issue #651), so every component is a declared zero.

    Big 5 banks use posted rates (higher than negotiated).
    readvanceable mortgage may have different penalty rules.

    DP#10 / issue #724: the penalty *math* (3 months' interest and the IRD
    product) is owned by ``countries.canada.ird_penalty``. This wrapper keeps
    the scalar ``posted_rate``/``comparison_rate`` call surface callers expect
    and feeds those rates into the canonical functions, so there is a single
    spelling of each formula. ``posted_rate`` stands in for the contract rate
    the 3-month and IRD products are priced against; ``comparison_rate`` is
    passed as the sole posted-rate term so ``compute_ird_penalty``'s
    term-lookup resolves to it.

    Args:
        current_balance: Outstanding mortgage balance
        current_rate: Your negotiated rate (decimal)
        remaining_months: Months remaining in term
        posted_rate: Bank's posted rate (defaults to current + 1.5%)
        comparison_rate: Rate for remaining term (defaults to current - 0.5%)
        is_open: Whether the term is OPEN (no break penalty). Defaults to
            False (closed), so an undeclared term is unchanged — the zero is
            DECLARED, never accidental (DP#32/#651).

    Returns:
        Dict with penalty estimates
    """
    if is_open:
        # DP#32/#651: a declared open term has no break cost. Report the same
        # keys with explicit zeros so callers see openness suppressed the
        # charge, not that the rates happened to net out this year.
        return {
            'three_months_interest': 0.0,
            'ird': 0.0,
            'penalty': 0.0,
            'penalty_type': 'open term: no penalty',
            'posted_rate': posted_rate if posted_rate is not None else current_rate + 0.015,
            'comparison_rate': comparison_rate if comparison_rate is not None else current_rate - 0.005,
            'rate_differential': 0.0,
        }

    if posted_rate is None:
        posted_rate = current_rate + 0.015  # Typical posted rate markup
    if comparison_rate is None:
        comparison_rate = current_rate - 0.005

    # 3 months' interest — canonical formula from ird_penalty.py, priced on the
    # posted rate this wrapper was handed (preserving the prior input semantics).
    three_months_interest = compute_three_months_interest(
        current_balance, posted_rate)

    # IRD — canonical formula from ird_penalty.py. Hand the comparison rate as
    # the single posted-rate term and the posted rate as the contract rate, so
    # compute_ird_penalty computes (posted_rate - comparison_rate) × balance ×
    # remaining_months / 12, floored at zero — the same product, one source.
    remaining_years = max(1, int(round(remaining_months / 12)))
    ird = compute_ird_penalty(
        current_balance, posted_rate, remaining_months,
        posted_rates={remaining_years: comparison_rate},
        use_discounted_rate=False,
    )

    rate_diff = max(0.0, posted_rate - comparison_rate)
    penalty = max(three_months_interest, ird)

    return {
        'three_months_interest': three_months_interest,
        'ird': ird,
        'penalty': penalty,
        'penalty_type': 'IRD' if ird > three_months_interest else '3-months interest',
        'posted_rate': posted_rate,
        'comparison_rate': comparison_rate,
        'rate_differential': rate_diff,
    }


# ──────────────────────────────────────────────────────
# Comprehensive Scenario Generation
# ──────────────────────────────────────────────────────

def generate_all_mortgage_scenarios(cfg: Dict) -> Dict[str, Dict]:
    """Generate all mortgage scenarios from config.
    
    Creates:
    - Base broker scenarios (expected, best, worst)
    - Rate stress tests (±1%, ±2%)
    - Renewal stress tests
    - IRD penalty estimates
    - Amortization schedules for each
    
    Returns:
        Dict keyed by scenario name with full details
    """
    prop = cfg['property']
    projection_years = cfg['assumptions']['projection_years']
    current_rate = prop['mortgage_rate']
    balance = prop['mortgage_balance']
    amortization = prop.get('amortization_years', 25)
    
    # Build base scenarios from broker offers
    base_scenarios = build_broker_scenarios(current_rate, projection_years)
    
    all_scenarios = {}
    
    for name, rate_path in base_scenarios.items():
        # Determine mortgage type from the path
        mortgage_type = "fixed" if "fixed" in name else "variable"
        
        # HELOC path
        heloc_path = HELOCPath(
            name=f"HELOC for {name}",
            mortgage_rate_path=rate_path,
        )
        
        # Amortization schedule
        sched = amortization_schedule(
            principal=balance,
            rate_path=rate_path,
            amortization_years=amortization,
            projection_months=projection_years * 12,
            readvance_smith=prop.get('heloc_readvance', True),
        )
        
        annual = annual_summary(sched)
        
        all_scenarios[name] = {
            'rate_path': rate_path,
            'heloc_path': heloc_path,
            'amortization_schedule': sched,
            'annual_summary': annual,
            'mortgage_type': mortgage_type,
            'avg_mortgage_rate': rate_path.average_rate,
            'final_balance': sched[-1]['balance'] if sched else balance,
            'total_interest': sched[-1]['cumulative_interest'] if sched else 0,
            'total_readvanced': sched[-1]['cumulative_readvanced'] if sched else 0,
            'total_payments': sum(s['payment'] for s in sched),
        }
    
    # Renewal stress tests for each broker offer
    for refi in prop.get('refinance_options', []):
        refi_name = refi['name']
        stress = build_renewal_stress(
            base_rate=refi['rate'],
            term_years=refi['term_years'],
            rate_type=refi['type'],
            projection_years=projection_years
        )
        for stress_name, stress_path in stress.items():
            key = f"{refi_name} — {stress_name}"
            
            heloc_path = HELOCPath(
                name=f"HELOC for {key}",
                mortgage_rate_path=stress_path,
            )
            
            sched = amortization_schedule(
                principal=balance,
                rate_path=stress_path,
                amortization_years=amortization,
                projection_months=projection_years * 12,
                readvance_smith=prop.get('heloc_readance', True),
            )
            
            all_scenarios[key] = {
                'rate_path': stress_path,
                'heloc_path': heloc_path,
                'amortization_schedule': sched,
                'annual_summary': annual_summary(sched),
                'mortgage_type': refi['type'],
                'avg_mortgage_rate': stress_path.average_rate,
                'final_balance': sched[-1]['balance'] if sched else balance,
                'total_interest': sched[-1]['cumulative_interest'] if sched else 0,
                'total_readvanced': sched[-1]['cumulative_readvanced'] if sched else 0,
                'total_payments': sum(s['payment'] for s in sched),
            }
    
    # IRD penalty for current mortgage
    remaining_months = amortization * 12  # approximate
    ird = estimate_ird_penalty(balance, current_rate, remaining_months)
    all_scenarios['_ird_penalty'] = ird
    
    return all_scenarios


def print_amortization_tables(scenarios: Dict[str, Dict], 
                               max_scenarios: int = 6) -> None:
    """Print readable amortization comparison tables."""
    
    # Filter to just the main scenarios
    main_keys = [k for k in scenarios if not k.startswith('_') and 'stress' not in k.lower()][:max_scenarios]
    
    print(f"\n  💳 MORTGAGE AMORTIZATION COMPARISON")
    print(f"     {'─'*90}")
    
    for key in main_keys:
        sc = scenarios[key]
        annual = sc.get('annual_summary', [])
        if not annual:
            continue
        
        print(f"\n  📊 {key}")
        print(f"     {'Year':>4} {'Rate':>7} {'Payment':>9} {'Interest':>9} {'Principal':>9} {'Balance':>10} {'HELOC':>9} {'SM Cumul':>9}")
        print(f"     {'─'*70}")
        for yr in annual:
            print(f"     {yr['year']:>4} {yr.get('rate_start', 0)*100:>6.2f}% "
                  f"${yr['total_payment']/1000:>7.0f}k "
                  f"${yr['total_interest']/1000:>7.0f}k "
                  f"${yr['total_principal']/1000:>7.0f}k "
                  f"${yr['end_balance']/1000:>8.0f}k "
                  f"${yr.get('heloc_end', 0)/1000:>7.0f}k "
                  f"${yr.get('total_readvanced', 0)/1000:>7.0f}k")
        
        print(f"     {'─'*70}")
        print(f"     Total interest: ${sc['total_interest']:>12,.0f}")
        print(f"     Total SM readvanced: ${sc['total_readvanced']:>10,.0f}")
        print(f"     Final balance: ${sc['final_balance']:>14,.0f}")
    
    # Summary table
    print(f"\n  📈 SCENARIO SUMMARY")
    print(f"     {'Scenario':<40} {'Avg Rate':>8} {'Total Int':>10} {'Final Bal':>10} {'SM Readv':>10}")
    print(f"     {'─'*80}")
    for key in main_keys:
        sc = scenarios[key]
        print(f"     {key[:39]:<40} {sc['avg_mortgage_rate']*100:>7.2f}% "
              f"${sc['total_interest']/1000:>8.0f}k "
              f"${sc['final_balance']/1000:>8.0f}k "
              f"${sc['total_readvanced']/1000:>8.0f}k")
    
    # IRD
    if '_ird_penalty' in scenarios:
        ird = scenarios['_ird_penalty']
        print(f"\n  ⚠️  EARLY BREAK PENALTY ESTIMATE")
        print(f"     3 months' interest: ${ird['three_months_interest']:>12,.0f}")
        print(f"     IRD:                ${ird['ird']:>12,.0f}")
        print(f"     Estimated penalty: ${ird['penalty']:>12,.0f} ({ird['penalty_type']})")