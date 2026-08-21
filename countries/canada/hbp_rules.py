#!/usr/bin/env python3
"""
Home Buyers' Plan (HBP) — RRSP withdrawal for first home purchase.

Per DP#10: this module owns HBP rules (ITA s.146.4).

The HBP allows first-time home buyers to withdraw up to $60,000
from their RRSP tax-free to purchase a qualifying home. The withdrawal
must be repaid over a maximum of 15 years (starting the 3rd year
after withdrawal). Failure to repay means the outstanding amount is
included in income and taxed.

HBP can be used simultaneously with FHSA (SCENARIO 10.1).
Each spouse can withdraw from their own RRSP ($120,000 combined).

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — HBP entry
    ITA s.146.4
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan.html
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan/participate-home-buyers-plan.html

Usage:
    from countries.canada.hbp_rules import HBPAccount, compare_first_home_strategies

    hbp = HBPAccount(withdrawal=35000)
    schedule = hbp.repayment_schedule()
    comparison = compare_first_home_strategies(income=75000, mtr=0.35, investment_return=0.07)
"""

from dataclasses import dataclass, field

# =============================================================================
# Constants — CRA HBP Rules (2026 defaults, DP#13)
# =============================================================================

HBP_MAX_WITHDRAWAL = 60000         # 2024+ increased from $35k to $60k
HBP_REPAYMENT_YEARS = 15           # Must repay over 15 years
HBP_REPAYMENT_START_DELAY = 2      # Start repaying 3rd year after withdrawal
HBP_ANNUAL_MIN_REPAYMENT_PCT = 1.0 / HBP_REPAYMENT_YEARS  # ~6.67% per year

# 89-day rule: RRSP contributions made within 89 days BEFORE an HBP
# withdrawal are not deductible (to the extent the RRSP balance would
# otherwise drop below the contribution). The withdrawal must leave the
# property in the RRSP for at least 90 days.
# Source: ITA s.146.01(2)(a)/(2.1); CRA "Participate in the Home Buyers' Plan",
# T1036 line "Excluded withdrawals" / "contributions you made ... less than 90 days".
# https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan/participate-home-buyers-plan.html
HBP_MIN_CONTRIBUTION_DAYS = 90  # property must be in the RRSP 90 days before withdrawal

# DP#20: Temporary repayment relief for the 2022-2025 federal budget measure.
# Withdrawals made between 2022-01-01 and 2025-12-31 get a 5-year grace
# period (repayment starts the 5th year after withdrawal) instead of the
# normal 3rd year. Modelled as a year-versioned start-delay table.
# Source: Budget 2024 / Department of Finance; CRA "Repay the funds...".
# https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan/repay-funds-withdrawn-rrsp-s-under-home-buyers-plan.html
HBP_RELIEF_START_DELAY = 4         # relief: 5th year after withdrawal
HBP_RELIEF_YEARS = range(2022, 2026)  # 2022, 2023, 2024, 2025 inclusive


def repayment_start_delay_for_year(withdrawal_year: int) -> int:
    """Return the repayment start-delay (years) for a given withdrawal year.

    DP#20: year-versioned data. Withdrawals in 2022-2025 receive the
    temporary 5-year relief (delay of 4); all other years use the
    standard 3rd-year start (delay of 2).

    Source: Budget 2024 temporary HBP repayment relief; ITA s.146.01.
    """
    if withdrawal_year in HBP_RELIEF_YEARS:
        return HBP_RELIEF_START_DELAY
    return HBP_REPAYMENT_START_DELAY


def deductible_contribution_before_hbp(
    contribution_year: int,
    contribution_day_of_year: int,
    withdrawal_year: int,
    withdrawal_day_of_year: int,
    contribution_amount: float,
    rrsp_balance_after_withdrawal: float,
) -> dict:
    """Apply the HBP 89-day (90-day) rule to an RRSP contribution.

    A contribution made less than 90 days before an HBP withdrawal is
    NOT deductible to the extent the post-withdrawal RRSP balance is
    lower than it would have been without that contribution. In practice
    CRA disallows the lesser of (the recent contribution) and (the amount
    by which the balance would fall below the contribution).

    DP#10: real CRA rule that affects deduction optimization.
    Source: ITA s.146.01(2)(a)/(2.1).

    Args:
        contribution_year/contribution_day_of_year: contribution date.
        withdrawal_year/withdrawal_day_of_year: HBP withdrawal date.
        contribution_amount: the recent RRSP contribution.
        rrsp_balance_after_withdrawal: RRSP fair-market value immediately
            after the HBP withdrawal (i.e. what remains).

    Returns:
        Dict with days_between, within_window, deductible_amount and
        non_deductible_amount.
    """
    days_between = (withdrawal_year - contribution_year) * 365 + (
        withdrawal_day_of_year - contribution_day_of_year
    )
    within_window = 0 <= days_between < HBP_MIN_CONTRIBUTION_DAYS

    if not within_window:
        non_deductible = 0.0
    else:
        # Disallow only to the extent the remaining balance is below the
        # recent contribution (CRA leaves the contribution deductible if
        # enough property remains in the plan after the withdrawal).
        shortfall = max(0.0, contribution_amount - rrsp_balance_after_withdrawal)
        non_deductible = min(contribution_amount, shortfall)

    return {
        'days_between': days_between,
        'within_window': within_window,
        'contribution_amount': contribution_amount,
        'non_deductible_amount': non_deductible,
        'deductible_amount': contribution_amount - non_deductible,
        'note': (
            'Contributions made less than 90 days before an HBP withdrawal '
            'are not deductible to the extent the RRSP balance falls below '
            'the contribution (ITA s.146.01(2)(a)).'
        ),
    }


# =============================================================================
# HBP Account Model
# =============================================================================

@dataclass
class HBPAccount:
    """Home Buyers' Plan account tracking.

    DP#8: The HBP is a data object with state that the simulation
    evolves year-by-year (repayment tracking, default detection).

    Attributes:
        withdrawal: Total amount withdrawn from RRSP under HBP
        withdrawal_year: Calendar year of withdrawal
        repaid: Total amount repaid so far
        repayment_schedule: Year-by-year repayment amounts
        is_first_home: Whether this is a first-time home purchase
    """
    withdrawal: float = 0.0
    withdrawal_year: int = 2026
    repaid: float = 0.0
    repayment_schedule: list[dict] = field(default_factory=list)
    is_first_home: bool = True

    # Computed
    _outstanding: float = 0.0

    def __post_init__(self):
        self._outstanding = self.withdrawal - self.repaid

    @property
    def outstanding(self) -> float:
        """Amount still owed to RRSP."""
        return max(0, self.withdrawal - self.repaid)

    def annual_min_repayment(self) -> float:
        """Minimum annual repayment amount."""
        return self.withdrawal / HBP_REPAYMENT_YEARS

    def repayment_start_year(self) -> int:
        """Year by which repayments must start.

        Normally repayment begins the 3rd calendar year after withdrawal.
        E.g., withdraw in 2026 → first repayment due for the 2028 tax year
        (start year 2029).

        DP#20: Withdrawals in 2022-2025 receive the temporary federal
        relief that defers the start to the 5th year after withdrawal.
        Source: Budget 2024 temporary HBP repayment relief; ITA s.146.01.
        """
        delay = repayment_start_delay_for_year(self.withdrawal_year)
        return self.withdrawal_year + delay + 1

    def generate_repayment_schedule(self) -> list[dict]:
        """Generate the full 15-year repayment schedule.

        Returns:
            List of dicts with year, min_payment, actual_payment, outstanding
        """
        schedule = []
        outstanding = self.withdrawal
        annual_min = self.annual_min_repayment()
        start_year = self.repayment_start_year()

        for yr in range(HBP_REPAYMENT_YEARS):
            payment_year = start_year + yr
            payment = min(annual_min, outstanding)
            outstanding -= payment

            schedule.append({
                'year': payment_year,
                'min_payment': annual_min,
                'actual_payment': payment,
                'outstanding_after': max(0, outstanding),
                'is_final': outstanding <= 0,
            })

            if outstanding <= 0:
                break

        self.repayment_schedule = schedule
        return schedule

    def make_repayment(self, amount: float, year: int) -> dict:
        """Make a repayment to the HBP.

        If less than the minimum, the shortfall is included in income.

        Args:
            amount: Amount repaid this year
            year: Calendar year of repayment

        Returns:
            Dict with payment details and any tax consequences
        """
        self.repaid += amount
        min_payment = self.annual_min_repayment()
        shortfall = max(0, min_payment - amount)

        result = {
            'year': year,
            'amount_repaid': amount,
            'minimum_required': min_payment,
            'shortfall': shortfall,
            'shortfall_tax_consequence': shortfall > 0,
            'outstanding': self.outstanding,
        }

        if shortfall > 0:
            result['note'] = (
                f'Shortfall of ${shortfall:,.0f} will be included in income '
                f'and taxed at your marginal rate'
            )

        return result

    def default_tax_impact(self, year: int, marginal_rate: float) -> dict:
        """Calculate tax impact of not repaying the HBP.

        If you don't repay, the outstanding balance is added to income
        over the remaining years. This is the "worst case" scenario.

        Args:
            year: Current year
            marginal_rate: Marginal tax rate

        Returns:
            Dict with total tax if entire balance is included in income
        """
        outstanding = self.outstanding
        tax = outstanding * marginal_rate

        return {
            'year': year,
            'outstanding_balance': outstanding,
            'tax_if_default': tax,
            'effective_cost_rate': marginal_rate,
            'note': 'HBP default: entire outstanding balance added to income',
        }

    def can_reparticipate(self, new_withdrawal_year: int) -> dict:
        """Check eligibility to participate in the HBP again.

        You may participate in the HBP a second time only if your HBP
        balance is zero on January 1 of the year of the new withdrawal
        (and you again meet the first-time-buyer/4-year rule).

        DP#28: eligibility is date-computed from the outstanding balance.
        Source: CRA "Participate in the Home Buyers' Plan" — re-participation.
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/rrsps-related-plans/what-home-buyers-plan/participate-home-buyers-plan.html

        Args:
            new_withdrawal_year: year of the prospective new withdrawal.

        Returns:
            Dict with eligible flag and the reason.
        """
        balance_zero = self.outstanding <= 0
        eligible = balance_zero
        return {
            'new_withdrawal_year': new_withdrawal_year,
            'outstanding_balance': self.outstanding,
            'balance_zero_on_jan1': balance_zero,
            'eligible': eligible,
            'note': (
                'Re-participation requires a zero HBP balance on January 1 '
                'of the withdrawal year, plus meeting the first-time-buyer '
                '(4-year) rule again.'
            ),
        }

    def cancel(self, cancellation_year: int, amount: float = None) -> dict:
        """Cancel an HBP participation (Form RC471).

        If the home is not bought/built by the deadline, you became a
        non-resident, or the withdrawal otherwise fails to qualify, you
        can cancel by repaying the funds to an RRSP. A timely cancellation
        repayment is NOT a deductible RRSP contribution and is not included
        in income; any portion not repaid by the deadline is included in
        income for the withdrawal year.

        DP#10: Form RC471 cancellation mechanism.
        Source: CRA Form RC471 "Home Buyers' Plan (HBP) Cancellation".

        Args:
            cancellation_year: year the cancellation repayment is made.
            amount: amount repaid to cancel (default: full outstanding).

        Returns:
            Dict with cancelled amount, any income inclusion, deductibility.
        """
        if amount is None:
            amount = self.outstanding
        cancelled = min(amount, self.outstanding)
        self.repaid += cancelled
        income_inclusion = self.outstanding  # any unrepaid balance is income

        return {
            'cancellation_year': cancellation_year,
            'amount_cancelled': cancelled,
            'income_inclusion': income_inclusion,
            'outstanding_after': self.outstanding,
            'deductible': False,
            'note': (
                'HBP cancellation (Form RC471): repayment is not deductible '
                'and not income; any balance not repaid by the deadline is '
                'included in income for the withdrawal year.'
            ),
        }

    def on_death(self, death_year: int, surviving_spouse_elects: bool,
                 marginal_rate: float = 0.40) -> dict:
        """Handle death of the HBP participant.

        On death, the outstanding HBP balance is normally included in the
        deceased's income for the year of death. However, the legal
        representative and the surviving spouse/common-law partner may
        jointly elect to have the surviving spouse assume the deceased's
        remaining HBP repayments (no income inclusion to the deceased).

        DP#10: death rules with surviving-spouse election.
        Source: CRA "Death of an HBP participant"; ITA s.146.01(7).

        Args:
            death_year: calendar year of death.
            surviving_spouse_elects: whether the spouse elects to assume
                the repayments.
            marginal_rate: MTR used to estimate the income-inclusion tax.

        Returns:
            Dict describing income inclusion vs. spouse assumption.
        """
        outstanding = self.outstanding
        if surviving_spouse_elects:
            return {
                'death_year': death_year,
                'spouse_assumes_repayments': True,
                'income_inclusion': 0.0,
                'tax_on_death': 0.0,
                'assumed_balance': outstanding,
                'note': (
                    'Surviving spouse/common-law partner elected to assume '
                    'the remaining HBP repayments; no income inclusion to '
                    'the deceased (ITA s.146.01(7)).'
                ),
            }
        return {
            'death_year': death_year,
            'spouse_assumes_repayments': False,
            'income_inclusion': outstanding,
            'tax_on_death': outstanding * marginal_rate,
            'assumed_balance': 0.0,
            'note': (
                'No election: outstanding HBP balance included in the '
                "deceased's income for the year of death."
            ),
        }

    def summary(self) -> dict:
        """Return account summary."""
        return {
            'withdrawal': self.withdrawal,
            'withdrawal_year': self.withdrawal_year,
            'repaid': self.repaid,
            'outstanding': self.outstanding,
            'annual_min_repayment': self.annual_min_repayment(),
            'repayment_start_year': self.repayment_start_year(),
            'years_remaining': HBP_REPAYMENT_YEARS - len(self.repayment_schedule),
        }


# =========================================================================
# HBP-RRSP Integration Functions — DP#46
# =========================================================================

def hbp_repayment_to_rrsp_room(
    hbp_account: HBPAccount,
    repayment_year: int,
    repayment_amount: float = None,
) -> dict:
    """Calculate the RRSP room impact of an HBP repayment.

    DP#46: HBP repayments must be tracked for RRSP room purposes.

    When an HBP repayment is NOT made by the deadline, the shortfall
    is included in income (and thus taxed at marginal rate). This does
    NOT reduce RRSP contribution room — it's already been deducted
    when the original contribution was made.

    When a repayment IS made, it goes back into the RRSP but does NOT
    create new contribution room. The room was used when the original
    contribution was made.

    However, the repayment affects net RRSP room available:
    - The repayment adds back to the RRSP balance but NOT to contribution room
    - If repayment is missed, the shortfall is included in income

    Args:
        hbp_account: HBPAccount with withdrawal and repayment tracking
        repayment_year: Calendar year of repayment
        repayment_amount: Amount to repay (default: minimum annual)

    Returns:
        Dict with repayment details and RRSP room impact
    """
    if repayment_amount is None:
        repayment_amount = hbp_account.annual_min_repayment()

    result = hbp_account.make_repayment(repayment_amount, repayment_year)

    return {
        'repayment_year': repayment_year,
        'amount_repaid': repayment_amount,
        'minimum_required': result['minimum_required'],
        'shortfall': result['shortfall'],
        'shortfall_included_in_income': result['shortfall'] > 0,
        'outstanding_after': result['outstanding'],
        'note': (
            'HBP repayment goes back to RRSP balance but does NOT create '
            'new contribution room. Original deduction was already claimed.'
        ),
    }


def hbp_missed_repayment_tax_impact(
    hbp_account: HBPAccount,
    missed_years: int = 1,
    marginal_rate: float = 0.40,
) -> dict:
    """Calculate tax impact of missed HBP repayments.

    DP#46: When HBP repayments are missed, the shortfall is included in
    income and taxed at the marginal rate. This is separate from the
    RRSP deduction system.

    Args:
        hbp_account: HBPAccount tracking the withdrawal
        missed_years: Number of years of missed repayments
        marginal_rate: Marginal tax rate for shortfall tax

    Returns:
        Dict with tax impact details
    """
    annual_min = hbp_account.annual_min_repayment()
    total_shortfall = annual_min * missed_years
    tax_cost = total_shortfall * marginal_rate

    return {
        'annual_minimum': annual_min,
        'years_missed': missed_years,
        'total_shortfall': total_shortfall,
        'tax_cost': tax_cost,
        'effective_tax_rate': marginal_rate,
        'outstanding_balance': hbp_account.outstanding,
        'note': (
            f'Missing {missed_years} year(s) of HBP repayments adds '
            f'${total_shortfall:,.0f} to income, costing ${tax_cost:,.0f} in tax '
            f'at {marginal_rate:.0%} marginal rate.'
        ),
    }


# =========================================================================
# First Home Strategy Comparison — SCENARIO 10.1
# =========================================================================

def compare_first_home_strategies(
    income: float,
    marginal_rate: float,
    years_to_purchase: int = 4,
    annual_savings: float = None,
    investment_return: float | None = None,
    rrsp_existing_balance: float = 0,
    fhsa_contrib_per_year: float = 8000,
    hbp_withdrawal: float = 35000,
    max_hbp_withdrawal: float = HBP_MAX_WITHDRAWAL,
    province: str = 'quebec',
) -> dict:
    """Compare FHSA vs RRSP HBP vs TFSA for first home purchase.

    SCENARIO 10.1: Which account maximizes the down payment?

    Three strategies:
    1. FHSA: Contribute $8k/yr, tax-deductible, tax-free withdrawal
    2. RRSP HBP: Withdraw from RRSP tax-free, must repay over 15 years
    3. TFSA: After-tax contributions, tax-free withdrawal

    A fourth combined strategy uses FHSA + HBP together.

    Args:
        income: Annual gross income
        marginal_rate: Combined marginal tax rate
        years_to_purchase: Years until home purchase
        annual_savings: Total annual savings (default: 15% of income)
        investment_return: Expected investment return
        rrsp_existing_balance: Existing RRSP balance available for HBP
        fhsa_contrib_per_year: Annual FHSA contribution
        hbp_withdrawal: Amount to withdraw from RRSP via HBP
        max_hbp_withdrawal: Maximum HBP withdrawal ($60k in 2024+)
        province: Province code

    Returns:
        Dict ranking all strategies by net down payment
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    if annual_savings is None:
        annual_savings = income * 0.15

    # Cap HBP withdrawal
    hbp_withdrawal = min(hbp_withdrawal, max_hbp_withdrawal, rrsp_existing_balance)

    # ── Strategy 1: FHSA ──
    # Contribute $8k/yr, deduct from income, grow tax-free, withdraw tax-free
    fhsa_balance = 0
    fhsa_total_contributions = 0
    fhsa_total_tax_savings = 0

    for _yr in range(years_to_purchase):
        contrib = min(fhsa_contrib_per_year, 40000 - fhsa_total_contributions)
        fhsa_balance = (fhsa_balance + contrib) * (1 + investment_return)
        fhsa_total_contributions += contrib
        fhsa_total_tax_savings += contrib * marginal_rate

    # FHSA down payment = balance at withdrawal (tax-free)
    fhsa_down_payment = fhsa_balance

    # ── Strategy 2: RRSP HBP ──
    # Assume RRSP already has balance; withdraw via HBP (tax-free loan)
    # Must repay over 15 years; repayment is NOT a new deduction
    hbp_annual_repayment = hbp_withdrawal / HBP_REPAYMENT_YEARS
    # Cost of HBP: the "forced savings" from repayment isn't tax-deductible
    # It replaces the original RRSP deduction you already claimed
    # The original deduction was the benefit (cost is zero)

    # HBP also allows pre-existing RRSP contributions to count
    # Calculate existing RRSP if we contribute same annual amount
    rrsp_for_hbp = rrsp_existing_balance
    rrsp_contributions_total = 0
    rrsp_tax_savings_total = 0

    # After-tax savings that goes to RRSP for HBP
    rrsp_annual = min(annual_savings, income * 0.18)  # RRSP room limit
    for _yr in range(years_to_purchase):
        contrib = rrsp_annual
        rrsp_for_hbp = (rrsp_for_hbp + contrib) * (1 + investment_return)
        rrsp_contributions_total += contrib
        rrsp_tax_savings_total += contrib * marginal_rate

    # Can withdraw up to max_hbp from RRSP
    hbp_available = min(rrsp_for_hbp, max_hbp_withdrawal)
    hbp_down_payment = hbp_available

    # HBP opportunity cost: you lose the tax-sheltered growth on withdrawn
    # amount over the 15-year repayment period
    hbp_lost_growth = 0
    for yr in range(HBP_REPAYMENT_YEARS):
        # Each year, the amount still outside RRSP could have been growing
        remaining_outside = hbp_available * (1 - yr / HBP_REPAYMENT_YEARS)
        hbp_lost_growth += remaining_outside * investment_return

    # ── Strategy 3: TFSA ──
    # After-tax contributions, tax-free growth, tax-free withdrawal
    tfsa_annual = annual_savings * (1 - marginal_rate)  # After-tax savings
    tfsa_balance = 0
    for _yr in range(years_to_purchase):
        tfsa_balance = (tfsa_balance + tfsa_annual) * (1 + investment_return)

    tfsa_down_payment = tfsa_balance

    # ── Strategy 4: FHSA + HBP Combined ──
    combined_down_payment = fhsa_down_payment + hbp_down_payment
    combined_tax_savings = fhsa_total_tax_savings + rrsp_tax_savings_total

    # ── Strategy 5: Double Deduction (HBP → FHSA) ──
    # Per BLG / CRA: Withdraw from RRSP under HBP, contribute to FHSA
    # Gets tax deduction on original RRSP contribution AND FHSA deduction
    double_deduction_hbp = min(rrsp_existing_balance, max_hbp_withdrawal)
    double_deduction_fhsa = 0
    double_deduction_tax_savings = double_deduction_hbp * marginal_rate  # Original RRSP deduction

    for yr in range(years_to_purchase):
        fhsa_contrib = min(fhsa_contrib_per_year, 40000 - yr * fhsa_contrib_per_year)
        double_deduction_fhsa = (double_deduction_fhsa + fhsa_contrib) * (1 + investment_return)
        double_deduction_tax_savings += fhsa_contrib * marginal_rate  # FHSA deduction

    double_deduction_down_payment = double_deduction_fhsa

    # ── Rank strategies ──
    strategies = [
        {
            'name': 'FHSA',
            'down_payment': fhsa_down_payment,
            'tax_savings': fhsa_total_tax_savings,
            'net_benefit': fhsa_down_payment + fhsa_total_tax_savings,
            'repayment_required': False,
            'pros': ['Tax-deductible contributions', 'Tax-free growth', 'Tax-free withdrawal', 'No repayment'],
            'cons': ['$40k lifetime limit', 'Must close after purchase'],
        },
        {
            'name': 'RRSP HBP',
            'down_payment': hbp_down_payment,
            'tax_savings': rrsp_tax_savings_total,
            'net_benefit': hbp_down_payment + rrsp_tax_savings_total,
            'repayment_required': True,
            'repayment_annual': hbp_annual_repayment,
            'lost_growth': hbp_lost_growth,
            'pros': ['Tax-free withdrawal', 'Up to $60k available'],
            'cons': ['Must repay over 15 years', 'Lost RRSP growth', 'No new deduction on repayment'],
        },
        {
            'name': 'TFSA',
            'down_payment': tfsa_down_payment,
            'tax_savings': 0,
            'net_benefit': tfsa_down_payment,
            'repayment_required': False,
            'pros': ['Tax-free growth', 'Tax-free withdrawal', 'Room regained next year', 'No repayment'],
            'cons': ['No tax deduction on contributions', 'Smaller contributions (after-tax)'],
        },
        {
            'name': 'FHSA + HBP Combined',
            'down_payment': combined_down_payment,
            'tax_savings': combined_tax_savings,
            'net_benefit': combined_down_payment + combined_tax_savings,
            'repayment_required': True,
            'repayment_annual': hbp_annual_repayment,
            'pros': ['Maximum down payment', 'Double tax deduction', 'CRA confirmed both can be used'],
            'cons': ['HBP repayment required', 'Two accounts to manage'],
        },
        {
            'name': 'Double Deduction (HBP→FHSA)',
            'down_payment': double_deduction_down_payment,
            'tax_savings': double_deduction_tax_savings,
            'net_benefit': double_deduction_down_payment + double_deduction_tax_savings,
            'repayment_required': True,
            'repayment_annual': double_deduction_hbp / HBP_REPAYMENT_YEARS,
            'pros': ['Double deduction on same money', 'CRA confirmed strategy'],
            'cons': ['HBP repayment required', 'Need existing RRSP balance', 'Complex'],
        },
    ]

    # Sort by net benefit (DP#22: optimizer ranks, user chooses)
    strategies.sort(key=lambda s: s['net_benefit'], reverse=True)

    return {
        'strategies': strategies,
        'best': strategies[0]['name'],
        'income': income,
        'marginal_rate': marginal_rate,
        'years_to_purchase': years_to_purchase,
        'investment_return': investment_return,
    }
