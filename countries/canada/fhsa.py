#!/usr/bin/env python3
"""
First Home Savings Account (FHSA) — Bill C-47 Rules

The FHSA is a registered plan that combines RRSP-like deductions
with TFSA-like tax-free withdrawals when used to purchase a first home.

Key rules (2023+):
- $8,000 annual contribution limit
- $40,000 lifetime contribution limit
- Tax-deductible contributions (like RRSP)
- Tax-free qualifying withdrawals (like TFSA / HBP)
- Must close by Dec 31 of year after first qualifying withdrawal,
  or 15th anniversary of opening, or age 71 (whichever comes first)
- Can transfer unused FHSA to RRSP/RRIF without using RRSP room
- HBP (Home Buyers' Plan) can be used simultaneously ($60k from RRSP)

Per DP#10: this module owns FHSA rules (Bill C-47).

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — FHSA entry
    Bill C-47, Division V of Part 1 (added s.146.6 to ITA)
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account.html
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/opening-your-fhsas.html
    https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/contributing-your-fhsa.html

Usage:
    from countries.canada.fhsa import FHSAAccount, fhsa_double_deduction_analysis

    fhsa = FHSAAccount()
    fhsa.contribute(8000)
    savings = fhsa.tax_savings(8000, marginal_rate=mtr)
    print(f"Tax savings: ${savings:.0f}")

    # Check first-home buyer eligibility
    fhsa.principal_residence_years = []  # No principal residence history
    result = fhsa.qualifying_withdrawal(2030)
    print(f"Eligible: {result['eligible']}")

    # Non-qualifying withdrawal: income tax (MTR) + withholding (pre-payment)
    fhsa2 = FHSAAccount()
    fhsa2.contribute(8000)
    result = fhsa2.non_qualifying_withdrawal(2028, marginal_rate=0.4571)
    print(f"Total income tax: ${result['income_tax']:.0f}, Withholding: ${result['withholding_tax']:.0f}")
"""

# Compute marginal rate from TaxDataProvider instead of hardcoding
from tax_data import default_tax_provider
from tax_calculator import marginal_rate as _compute_marginal_rate


def _default_marginal_rate(income: float = 150000, year: int = 2026, province: str = 'quebec') -> float:
    """Compute marginal rate from tax brackets (DP#2: no hardcoded rates)."""
    brackets = default_tax_provider().get_combined_brackets(year, province)
    return _compute_marginal_rate(income, brackets)


from dataclasses import dataclass, field
from typing import Dict, List, Optional


FHSA_ANNUAL_LIMIT = 8000
FHSA_LIFETIME_LIMIT = 40000
FHSA_CARRY_FORWARD_MAX = 8000  # Can only carry forward 1 year of unused room
FHSA_MAX_AGE = 71
FHSA_MAX_YEARS_OPEN = 15

# Excess FHSA contributions are subject to a 1% per-month tax on the highest
# excess amount in each month (same mechanic as the TFSA excess tax).
# Source: ITA s.207.021; CRA "Contributing to your FHSAs" — excess contributions.
# https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/contributing-your-fhsa.html
FHSA_EXCESS_TAX_RATE_MONTHLY = 0.01


def fhsa_contribution_deduction_year(contribution_year: int,
                                     contribution_in_first_60_days: bool) -> int:
    """Tax year an FHSA contribution can be deducted in.

    KEY DIFFERENCE FROM RRSP: an FHSA contribution is always attributed to
    the calendar year in which it is made. Unlike RRSP, contributions made
    in the first 60 days of a year CANNOT be deducted for the PRIOR year.

    DP#10/DP#28: contribution-timing rule that differs from RRSP.
    Source: ITA s.146.6(2); CRA "Contributing to your FHSAs" — the FHSA
    deduction is for the year the contribution is made (no 60-day rule).

    Args:
        contribution_year: calendar year the contribution was made.
        contribution_in_first_60_days: whether it fell in the first 60 days
            (relevant only to contrast with RRSP; for FHSA it has no effect).

    Returns:
        The tax year the contribution is deductible in (always the
        contribution year for FHSA).
    """
    # First-60-days contributions do NOT roll back to the prior year for FHSA.
    return contribution_year


def fhsa_designated_transfer_to_rrsp(amount: float) -> dict:
    """Module-level designated transfer (Form RC727) — no account state.

    Convenience wrapper documenting that a designated transfer of an FHSA
    excess to an RRSP/RRIF does not use RRSP room and is not deductible.
    Source: ITA s.146.6(8); CRA Form RC727.
    """
    return {
        'amount': max(0.0, amount),
        'uses_rrsp_room': False,
        'deductible': False,
        'restores_fhsa_room': False,
        'note': (
            'Designated transfer of FHSA excess to RRSP/RRIF (Form RC727): '
            'no RRSP room used, not deductible, FHSA room not restored.'
        ),
    }


def fhsa_excess_contribution_tax(highest_excess: float, months: int = 1) -> dict:
    """Compute the 1%/month excess FHSA contribution tax.

    CRA levies 1% per month on the HIGHEST excess FHSA amount in the month,
    for each month the excess remains in the account (ITA s.207.021).

    DP#10: explicit excess-tax computation (distinct from contribution room).

    Args:
        highest_excess: the highest excess amount during the period.
        months: number of months the excess persisted.

    Returns:
        Dict with the monthly and total excess tax.
    """
    highest_excess = max(0.0, highest_excess)
    monthly_tax = highest_excess * FHSA_EXCESS_TAX_RATE_MONTHLY
    return {
        'highest_excess': highest_excess,
        'months': months,
        'monthly_tax': monthly_tax,
        'total_tax': monthly_tax * months,
        'rate': FHSA_EXCESS_TAX_RATE_MONTHLY,
        'note': (
            '1% per month on the highest excess FHSA amount in the month '
            '(ITA s.207.021), for each month the excess remains.'
        ),
    }

# CRA flat-rate withholding brackets for non-qualifying withdrawals (DP#13)
# The rate is applied to the ENTIRE withdrawal amount based on which bracket it falls into.
# Source: canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account/withdrawing-your-fhsas.html
# Excluding Quebec (different rates apply: 19%/29%/34%)
# Note: These are flat rates on the total amount, NOT marginal/tiered rates.
# E.g., a $10k withdrawal falls in the $5,001-$15k bracket → 20% × $10k = $2,000
FHSA_WITHHOLDING_BRACKETS = [
    (5000, 0.10),    # ≤$5,000: 10% of total amount
    (15000, 0.20),   # $5,001–$15,000: 20% of total amount
    (float('inf'), 0.30),  # >$15,000: 30% of total amount
]

# Quebec-specific withholding brackets (DP#10, DP#13)
# Source: Revenu Québec Table TP-1015-TI; confirmed by Sun Life, WealthNorth
FHSA_WITHHOLDING_BRACKETS_QC = [
    (5000, 0.19),    # ≤$5,000: 19% of total amount
    (15000, 0.29),   # $5,001–$15,000: 29% of total amount
    (float('inf'), 0.34),  # >$15,000: 34% of total amount
]


def compute_withholding_tax(amount: float, quebec: bool = False) -> float:
    """Compute CRA withholding tax on non-qualifying FHSA withdrawal.

    CRA applies a FLAT withholding rate to the entire withdrawal amount
    based on which bracket the total falls into. This is NOT a marginal/
    tiered calculation — the rate applies to the full amount.

    Example: A $10,000 withdrawal falls in the $5,001-$15,000 bracket,
    so the withholding is 20% × $10,000 = $2,000 (not 10%×$5k + 20%×$5k).

    The withholding is a PRE-PAYMENT of income tax, not an additional tax.
    When you file your tax return, the withholding is credited against
    your total tax liability.

    Args:
        amount: Withdrawal amount
        quebec: Use Quebec-specific rates (default False)

    Returns:
        Total withholding tax amount
    """
    if amount <= 0:
        return 0.0

    brackets = FHSA_WITHHOLDING_BRACKETS_QC if quebec else FHSA_WITHHOLDING_BRACKETS

    for threshold, rate in brackets:
        if amount <= threshold:
            return amount * rate

    # Should not reach here since last bracket is inf
    return amount * brackets[-1][1]


# =============================================================================
# FHSA Account Model
# =============================================================================

@dataclass
class FHSAAccount:
    """FHSA account with contribution tracking and balance.

    Models the FHSA as a data object per DP#8 (compose through data).
    The simulation engine uses this data to make allocation decisions.

    Available contribution room is ``annual_room + carry_forward_room``;
    pass those fields explicitly at construction.
    """
    balance: float = 0.0
    lifetime_room: float = FHSA_LIFETIME_LIMIT
    lifetime_used: float = 0.0
    annual_room: float = FHSA_ANNUAL_LIMIT
    carry_forward_room: float = 0.0  # Unused room from prior year

    # Opening/closing
    open_year: int = 2026
    is_open: bool = True
    qualifying_withdrawal_made: bool = False
    withdrawal_year: Optional[int] = None

    # DP#28: First-home buyer eligibility — computed from principal residence history.
    # These are years when the account holder LIVED IN a home they owned
    # as their principal place of residence (not just owned — investment
    # properties you don't live in don't count per CRA rules).
    principal_residence_years: List[int] = field(default_factory=list)

    # DP#20: Carry-forward tracking — which year's room is carried forward
    carry_forward_year: Optional[int] = None  # Year the carry-forward room came from

    def contribute(self, amount: float) -> float:
        """Contribute to FHSA. Returns actual amount contributed.

        Respects both annual room and lifetime limit.

        Args:
            amount: Requested contribution amount

        Returns:
            Actual amount contributed (may be less if room is limited)
        """
        if not self.is_open:
            return 0.0

        total_room = self.annual_room + self.carry_forward_room
        remaining_lifetime = self.lifetime_room - self.lifetime_used

        max_contribution = min(total_room, remaining_lifetime)
        actual = min(amount, max_contribution)

        self.balance += actual
        self.lifetime_used += actual

        # Use carry-forward room first, then annual room
        from_carry = min(actual, self.carry_forward_room)
        self.carry_forward_room -= from_carry
        remaining = actual - from_carry
        self.annual_room -= remaining

        return actual

    def add_annual_room(self, annual_limit: float = None, current_year: int = None) -> None:
        """Add new annual contribution room (called at start of each year).

        DP#20: Year-specific FHSA limit. If annual_limit is provided,
        use it instead of FHSA_ANNUAL_LIMIT constant. The caller should
        look up the correct year's limit from TaxDataProvider.

        DP#20: Carry-forward tracking — records which year's room is
        carried forward, not just the amount.

        Args:
            annual_limit: Year-specific FHSA limit. If None, uses FHSA_ANNUAL_LIMIT.
            current_year: Current calendar year. Required for accurate carry_forward_year
                tracking. If provided, carry_forward_year is set to current_year - 1
                (the year the unused room actually came from).
        """
        if not self.is_open:
            return

        # Prior year's unused annual room becomes carry-forward, capped at FHSA_CARRY_FORWARD_MAX
        self.carry_forward_room = min(self.annual_room, FHSA_CARRY_FORWARD_MAX)
        # DP#20: Track the year the carry-forward came from
        if self.carry_forward_room > 0:
            if current_year is not None:
                self.carry_forward_year = current_year - 1
            # else: leave carry_forward_year unchanged (caller didn't provide year info)
        self.annual_room = annual_limit if annual_limit is not None else FHSA_ANNUAL_LIMIT

    def is_eligible_for_opening(self, current_year: int, spouse_residence_years: List[int] = None) -> bool:
        """Check eligibility to OPEN an FHSA account.

        Per CRA rules (s.146.6), to open an FHSA:
        - You must not have lived in a qualifying home as your principal
          residence in the current year or any of the 4 preceding calendar years
        - Your spouse/common-law partner must also not have lived in a
          qualifying home as their principal residence in that period

        Note: CRA specifies eligibility is checked 'at any time in the
        current calendar year before the account is opened'. In a year-level
        simulation, checking the entire current year is a conservative
        approximation that may incorrectly deny eligibility if the account
        holder moved out earlier in the same year.

        Args:
            current_year: Calendar year for eligibility check
            spouse_residence_years: Years when spouse lived in a home they
                owned as principal residence (optional)

        Returns:
            True if eligible to open an FHSA
        """
        lookback_years = range(current_year - 4, current_year + 1)

        for year in lookback_years:
            if year in self.principal_residence_years:
                return False
            if spouse_residence_years and year in spouse_residence_years:
                return False

        return True

    def is_first_home_buyer(self, current_year: int) -> bool:
        """Check first-home buyer eligibility for FHSA qualifying withdrawal.

        Per CRA rules (s.146.6), for a QUALIFYING WITHDRAWAL:
        - Only the ACCOUNT HOLDER's principal residence history matters
        - Spouse's ownership does NOT affect withdrawal eligibility
          (it only affects opening eligibility)

        DP#28: Eligibility is computed from dates, not a boolean flag.

        Note: CRA rules include a 30-day grace period — you may still qualify
        if you lived in a qualifying home within the 30 days immediately before
        the withdrawal. This year-level simulation does not model that
        day-level exception; the check covers the full calendar year.
        This is a conservative approximation (may incorrectly deny eligibility
        for withdrawals made within 30 days of moving).

        Args:
            current_year: Calendar year of the withdrawal

        Returns:
            True if eligible as a first-home buyer for qualifying withdrawal
        """
        lookback_years = range(current_year - 4, current_year + 1)  # 5 years

        for year in lookback_years:
            if year in self.principal_residence_years:
                return False

        return True

    def grow(self, return_rate: float) -> None:
        """Grow balance by investment return (tax-free inside FHSA)."""
        if self.balance > 0:
            self.balance *= (1 + return_rate)

    def qualifying_withdrawal(self, year: int, marginal_rate: float = 0.0, quebec: bool = False) -> Dict:
        """Make a qualifying withdrawal (tax-free for first home purchase).

        DP#28: Checks first-home buyer eligibility BEFORE modifying state.
        If eligible, performs the qualifying withdrawal (tax-free).
        If not eligible, does NOT modify account state and returns
        eligibility info with non-qualifying cost estimates.

        Args:
            year: Year of withdrawal
            marginal_rate: If not eligible, used to compute non-qualifying cost
            quebec: Use Quebec-specific withholding rates for cost estimate

        Returns:
            Dict with withdrawal amount, tax status, and eligibility info.
            If not eligible, account state is NOT modified.
        """
        amount = self.balance

        # DP#28: Check eligibility BEFORE modifying state
        eligible = self.is_first_home_buyer(year)

        if not eligible:
            # Do NOT modify account state — return eligibility info only
            withholding_tax = compute_withholding_tax(amount, quebec=quebec)
            income_tax = amount * marginal_rate if marginal_rate > 0 else 0
            net_owing = max(0, income_tax - withholding_tax)
            return {
                'amount': amount,
                'eligible': False,
                'tax_free': False,
                'withdrawal_year': year,
                'non_qualifying_cost': {
                    'withholding_tax': withholding_tax,
                    'income_tax': income_tax,
                    'net_tax_owing': net_owing,
                    'effective_tax_rate': marginal_rate if amount > 0 else 0,
                    'note': (
                        'Not eligible for qualifying withdrawal. '
                        'Withholding is a pre-payment of income tax, not additional. '
                        'Use non_qualifying_withdrawal() to proceed.'
                    ),
                },
            }

        # Eligible — perform the qualifying withdrawal
        self.balance = 0
        self.qualifying_withdrawal_made = True
        self.withdrawal_year = year

        return {
            'amount': amount,
            'eligible': True,
            'tax_free': True,
            'withdrawal_year': year,
        }

    def non_qualifying_withdrawal(self, year: int, marginal_rate: float = 0.5, quebec: bool = False) -> Dict:
        """Make a non-qualifying withdrawal (taxable).

        Non-qualifying withdrawals are included in income and taxed
        at the marginal rate. CRA requires a tiered withholding tax
        that varies by withdrawal amount (and province).

        IMPORTANT: The withholding tax is a PRE-PAYMENT of income tax,
        not an additional tax. When you file your tax return, the
        withholding is credited against your total tax liability.
        The actual tax burden is just your marginal tax rate (MTR).

        The net amount owing at tax time = MTR × amount - withholding.
        If withholding > MTR × amount, you get a refund.

        DP#50: Corrected from the original implementation which incorrectly
        added withholding on top of MTR.

        Args:
            year: Year of withdrawal
            marginal_rate: Marginal tax rate (DP#13: use 0.5 as clearly-round
                fallback, or provide jurisdiction-specific rate)
            quebec: Use Quebec-specific withholding rates (default False)

        Returns:
            Dict with withdrawal amount, tax implications, and net cost
        """
        amount = self.balance
        # Withholding is a pre-payment of income tax, not additional tax
        withholding_tax = compute_withholding_tax(amount, quebec=quebec)
        # Actual income tax burden (this is what you actually pay)
        income_tax = amount * marginal_rate
        # Net amount owing at tax time (income tax minus withholding pre-payment)
        net_tax_owing = max(0, income_tax - withholding_tax)
        self.balance = 0
        self.is_open = False

        return {
            'amount': amount,
            'withholding_tax': withholding_tax,
            'income_tax': income_tax,
            'net_tax_owing': net_tax_owing,
            'effective_tax_rate': marginal_rate if amount > 0 else 0,
            'taxable_income': amount,
            'note': (
                f'Non-qualifying withdrawal: taxable as income at {marginal_rate:.0%} MTR. '
                f'Withholding of ${withholding_tax:,.0f} is a pre-payment of income tax '
                f'(not additional). Net amount owing at tax time: ${net_tax_owing:,.0f}.'
            ),
        }

    def transfer_to_rrsp(self) -> float:
        """Transfer FHSA balance to RRSP without using RRSP room.

        This is a tax-free transfer that doesn't consume RRSP
        contribution room (per Bill C-47).

        Returns:
            Amount transferred
        """
        amount = self.balance
        self.balance = 0
        self.is_open = False
        return amount

    def must_close(self, current_year: int, owner_birth_year: int) -> bool:
        """Check if FHSA must be closed.

        Close by Dec 31 of the earliest of:
        1. Year after first qualifying withdrawal
        2. 15th anniversary of opening
        3. Year the owner turns 71

        DP#50: Also checks age 71 vs 15th anniversary (whichever comes first).

        Args:
            current_year: Current calendar year
            owner_birth_year: Birth year of FHSA holder

        Returns:
            True if FHSA must close
        """
        owner_age = current_year - owner_birth_year

        # After qualifying withdrawal + 1 year
        if self.qualifying_withdrawal_made and self.withdrawal_year:
            close_year = self.withdrawal_year + 1
            if current_year >= close_year:
                return True

        # 15th anniversary
        anniversary_year = self.open_year + FHSA_MAX_YEARS_OPEN

        # Age 71 (whichever comes first)
        age_71_year = owner_birth_year + FHSA_MAX_AGE

        must_close_by = min(anniversary_year, age_71_year)
        if current_year >= must_close_by:
            return True

        return False

    def tax_savings(self, contribution: float, marginal_rate: float) -> float:
        """Calculate tax savings from FHSA contribution.

        FHSA contributions are tax-deductible, just like RRSP.

        Args:
            contribution: Amount contributed
            marginal_rate: Marginal tax rate

        Returns:
            Tax savings in dollars
        """
        return contribution * marginal_rate

    def deductible_contribution(self, contribution: float) -> float:
        """Deductible portion of an FHSA contribution.

        Contributions made AFTER the first qualifying withdrawal are NOT
        deductible (the FHSA deduction is lost once the account is in the
        post-withdrawal phase). Before any qualifying withdrawal the full
        contribution is deductible.

        DP#10: post-qualifying-withdrawal contributions are non-deductible.
        Source: ITA s.146.6(2); CRA "Contributing to your FHSAs" —
        no deduction for contributions made after the first qualifying
        withdrawal.

        Args:
            contribution: amount contributed.

        Returns:
            Deductible amount (0 if a qualifying withdrawal was already made).
        """
        if self.qualifying_withdrawal_made:
            return 0.0
        return contribution

    def participation_end_year(self, owner_birth_year: int) -> int:
        """Last calendar year the FHSA can stay open (maximum participation period).

        The FHSA must be closed by Dec 31 of the EARLIEST of:
        - the 15th anniversary of first opening an FHSA,
        - the year after the first qualifying withdrawal,
        - the year the holder turns 71.

        DP#28: the deadline is date-computed.
        Source: ITA s.146.6; CRA FHSA "Definitions" — maximum participation period.

        Args:
            owner_birth_year: birth year of the holder.

        Returns:
            The latest year the account may remain open.
        """
        candidates = [
            self.open_year + FHSA_MAX_YEARS_OPEN,
            owner_birth_year + FHSA_MAX_AGE,
        ]
        if self.qualifying_withdrawal_made and self.withdrawal_year is not None:
            candidates.append(self.withdrawal_year + 1)
        return min(candidates)

    def designated_transfer_to_rrsp(self, amount: float = None) -> dict:
        """Designated transfer of FHSA funds to an RRSP/RRIF (Form RC727).

        Used to remove an excess FHSA amount (or to wind down the FHSA): a
        designated transfer to an RRSP/RRIF does NOT use RRSP contribution
        room and is not deductible (the deduction was claimed on the FHSA
        contribution). Removing an excess via a designated transfer reduces
        the excess but does NOT restore FHSA participation room (only a
        taxable withdrawal does — see ``resolve_excess_by_taxable_withdrawal``).

        DP#10: Form RC727 designated-transfer mechanism.
        Source: ITA s.146.6(2)/(8); CRA Form RC727 "Direct Transfer to an FHSA".

        Args:
            amount: amount to transfer (default: full balance).

        Returns:
            Dict describing the transfer.
        """
        if amount is None:
            amount = self.balance
        transferred = min(amount, self.balance)
        self.balance -= transferred
        return {
            'amount': transferred,
            'uses_rrsp_room': False,
            'deductible': False,
            'restores_fhsa_room': False,
            'balance_after': self.balance,
            'note': (
                'Designated transfer to RRSP/RRIF (Form RC727): does not use '
                'RRSP room, not deductible, and does NOT restore FHSA '
                'participation room.'
            ),
        }

    def resolve_excess_by_taxable_withdrawal(self, excess: float) -> dict:
        """Resolve an FHSA excess via a taxable (non-qualifying) withdrawal.

        When an excess is removed by a taxable withdrawal, the surplus
        amount RESTORES FHSA participation room (the withdrawal is added
        back to the holder's room), unlike a designated transfer which does
        not. The withdrawal itself is taxable income.

        DP#12: re-participation room restored only by taxable withdrawal.
        Source: ITA s.146.6 "FHSA carryforward"/"unused FHSA contribution
        amount" definitions; CRA FHSA excess-amount guidance.

        Args:
            excess: the excess amount withdrawn as taxable.

        Returns:
            Dict with restored room and balance.
        """
        withdrawn = min(max(0.0, excess), self.balance)
        self.balance -= withdrawn
        # Restore participation room (capped at remaining lifetime headroom).
        self.lifetime_used = max(0.0, self.lifetime_used - withdrawn)
        self.annual_room += withdrawn
        return {
            'amount_withdrawn': withdrawn,
            'restores_fhsa_room': True,
            'room_restored': withdrawn,
            'taxable': True,
            'balance_after': self.balance,
            'note': (
                'Taxable withdrawal of an FHSA excess restores participation '
                'room (unlike a designated transfer).'
            ),
        }

    def on_death(self, successor_is_eligible_spouse: bool,
                 marginal_rate: float = 0.40) -> dict:
        """Handle death of the FHSA holder (successor holder rules).

        If a spouse/common-law partner is named successor holder AND is
        themselves FHSA-eligible (first-time buyer, age <71), the FHSA can
        continue as the successor's own FHSA (without affecting their own
        room/limits) — no income inclusion. Otherwise the property must be
        transferred/withdrawn: amounts paid to a beneficiary are included in
        that beneficiary's income (a transfer to the survivor's own RRSP/RRIF
        or FHSA can avoid the inclusion).

        DP#10: FHSA death / successor-holder rules.
        Source: ITA s.146.6(13)-(17); CRA "After you die — FHSA".

        Args:
            successor_is_eligible_spouse: spouse named successor and eligible.
            marginal_rate: MTR used to estimate any income-inclusion tax.

        Returns:
            Dict describing the successor outcome.
        """
        amount = self.balance
        if successor_is_eligible_spouse:
            self.is_open = False  # continues as successor's FHSA
            return {
                'successor_continues_fhsa': True,
                'income_inclusion': 0.0,
                'tax_on_death': 0.0,
                'transferred_amount': amount,
                'note': (
                    'Eligible spouse successor holder: FHSA continues as the '
                    "successor's own FHSA with no income inclusion "
                    '(ITA s.146.6(13)).'
                ),
            }
        self.balance = 0
        self.is_open = False
        return {
            'successor_continues_fhsa': False,
            'income_inclusion': amount,
            'tax_on_death': amount * marginal_rate,
            'transferred_amount': 0.0,
            'note': (
                'No eligible spouse successor: amount paid to beneficiaries '
                "is included in the beneficiary's income unless transferred "
                'to their RRSP/RRIF/FHSA.'
            ),
        }

    def summary(self) -> Dict:
        """Return account summary."""
        return {
            'balance': self.balance,
            'annual_room': self.annual_room,
            'carry_forward_room': self.carry_forward_room,
            'carry_forward_year': self.carry_forward_year,
            'lifetime_used': self.lifetime_used,
            'lifetime_remaining': self.lifetime_room - self.lifetime_used,
            'is_open': self.is_open,
            'qualifying_withdrawal_made': self.qualifying_withdrawal_made,
            'principal_residence_years': self.principal_residence_years,
        }


# =============================================================================
# Double Deduction Analysis — FHSA + HBP
# =============================================================================

def fhsa_double_deduction_analysis(
    annual_income: float,
    marginal_rate: float,
    fhsa_contribution: float = FHSA_ANNUAL_LIMIT,
    hbp_withdrawal: float = 35000,
    years_to_repay_hbp: int = 15,
    years: int = 10,
    investment_return: float | None = None,
) -> Dict:
    """Analyze the FHSA + HBP double deduction strategy.

    CRA has confirmed that you can use BOTH the FHSA and the HBP
    simultaneously when buying a first home. This means:
    1. Contribute to FHSA each year ($8k) → tax deduction
    2. Withdraw from RRSP via HBP ($60k max) → tax-free loan
    3. Use both for down payment → maximize first-home savings

    The HBP must be repaid over 15 years (starting 3rd year after
    withdrawal). Failure to repay means the outstanding amount is
    included in income.

    Args:
        annual_income: Annual gross income
        marginal_rate: Combined marginal tax rate
        fhsa_contribution: Annual FHSA contribution (default $8k)
        hbp_withdrawal: RRSP withdrawal via HBP (default $35k, max $60k)
        years_to_repay_hbp: Repayment period for HBP
        years: Projection period
        investment_return: Expected investment return

    Returns:
        Dict with complete analysis
    """
    # FHSA: annual contributions, tax deduction, tax-free growth
    fhsa_tax_savings_per_year = fhsa_contribution * marginal_rate
    fhsa_total_contributions = fhsa_contribution * min(years, 5)  # $40k lifetime
    fhsa_total_tax_savings = fhsa_total_contributions * marginal_rate

    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")

    # FHSA balance at withdrawal
    fhsa_balance = 0
    for yr in range(min(years, 5)):
        fhsa_balance = (fhsa_balance + fhsa_contribution) * (1 + investment_return)

    # HBP: tax-free withdrawal from RRSP, must repay
    hbp_annual_repayment = hbp_withdrawal / years_to_repay_hbp
    # HBP repayment is NOT tax-deductible (it replaces the deduction
    # you originally claimed when contributing to RRSP)
    hbp_cost_of_not_repaying = hbp_annual_repayment * marginal_rate  # If you don't repay

    # Total down payment available
    total_down_payment = fhsa_balance + hbp_withdrawal

    # Net benefit calculation
    total_tax_savings = fhsa_total_tax_savings
    # HBP gives you the use of $35k now that was already in RRSP
    # The benefit is the time value of having the money for down payment

    return {
        'fhsa_annual_contribution': fhsa_contribution,
        'fhsa_lifetime_contributions': fhsa_total_contributions,
        'fhsa_tax_savings_total': fhsa_total_tax_savings,
        'fhsa_balance_at_withdrawal': fhsa_balance,
        'hbp_withdrawal': hbp_withdrawal,
        'hbp_annual_repayment': hbp_annual_repayment,
        'hbp_repayment_years': years_to_repay_hbp,
        'total_down_payment': total_down_payment,
        'total_tax_savings': total_tax_savings,
        'double_deduction_possible': True,
        'note': ('FHSA contribution deduction + HBP tax-free withdrawal '
                 'can be used simultaneously per CRA confirmation'),
    }