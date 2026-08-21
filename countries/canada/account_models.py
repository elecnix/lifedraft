#!/usr/bin/env python3
"""
Account Models — Composable Canadian Investment Account Classes

Each account type is a dataclass with pure methods for:
- Contribution limits and room tracking
- Growth projection (simple or advanced)
- Tax treatment on withdrawal
- Government matching (RESP)

Supports both simple (flat rate) and advanced (rate paths, per-child rules) modes.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — RRSP, TFSA, RESP, FHSA entries
    RRSP: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4040/rrsps-other-registered-plans-retirement.html
    TFSA: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/tax-free-savings-account.html
    FHSA: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/first-home-savings-account.html
    RESP/CESG: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/registered-education-savings-plans-resps/canada-education-savings-programs-cesp/canada-education-savings-grant-cesg.html

Usage:
    from countries.canada.account_models import RRSPAccount, TSFAAccount, RESPAccount, NonRegAccount
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# =============================================================================
# Shared pure helpers (DP#3, DP#9)
# =============================================================================
# dupdelta (the repo's clone detector, .github/workflows/clone-detection.yml)
# flagged RRSPAccount.grow, TSFAccount.grow,
# RESPAccount.grow, and NonRegAccount.grow as byte-identical after blind-rename
# normalization (similarity 1.00), and
# RRSPAccount.contribute / TSFAccount.contribute likewise. These were five
# independently-copy-pasted implementations of the same two operations.
#
# Merging them is safe because the tax-treatment differences between account
# types never live in the growth/contribution *mechanics* — they live in
# (a) the return_rate handed to grow() (after-tax return, withholding drag,
# etc. are computed by the caller — see income_type.after_tax_return /
# income_type.wht_drag — before grow() ever sees the rate), and (b) the
# withdrawal/disposition-time tax functions (withdrawal_tax,
# capital_gains_tax, eap_tax), which remain distinct per account type.


def _apply_growth(balance: float, return_rate: float) -> Tuple[float, float]:
    """Pure: compound a balance by return_rate. Returns (new_balance, growth_amount).

    DP#9: the one implementation of "apply a rate of return to a balance",
    shared by every account type in this module (and by
    countries/canada/locked_in_account.py's LockedInAccount/LIFFund).
    """
    growth = balance * return_rate
    return balance + growth, growth


def _contribute_room_limited(balance: float, contribution_room: float,
                              amount: float) -> Tuple[float, float, float]:
    """Pure: contribute up to available room. Returns (new_balance, new_room, actual).

    DP#9: the one implementation of "contribute capped at remaining room",
    shared by RRSPAccount and TSFAccount (previously byte-identical
    copy-pasted logic in both classes).
    """
    actual = min(amount, contribution_room)
    return balance + actual, contribution_room - actual, actual


@dataclass
class RRSPAccount:
    """Registered Retirement Savings Plan.
    
    DP#2: Contribution limits and penalties come from CRA rules and
    year-versioned data, not hardcoded.
    
    Attributes:
        balance: Current account balance
        contribution_room: Available RRSP contribution room
        contributor: 'self' or 'spousal'
        deduction_room: Available RRSP deduction room (usually equals contribution room)
    """
    balance: float = 0.0
    contribution_room: float = 0.0
    annual_room_per_year: float = 0.0  # Based on 18% of earned income
    annual_room_cap: float = 0.0  # DP#12: set from TaxDataProvider.get_rrsp_limit(year)
    pension_adjustment: float = 0.0
    deduct_later: bool = False
    bracket_target: float = 0.0  # DP#12: set from tax brackets / input config
    
    # DP#2: CRA over-contribution rules — $2,000 grace, 1%/month penalty
    OVERCONTRIBUTION_GRACE: float = 2000.0  # CRA allows $2,000 excess without penalty
    PENALTY_RATE_MONTHLY: float = 0.01  # 1% per month on excess above grace
    
    def contribute(self, amount: float) -> Tuple[float, float]:
        """Contribute to RRSP. Returns (actual_contribution, remaining_room).
        
        Respects contribution room limits. Reduces room by amount contributed.
        """
        self.balance, self.contribution_room, actual = _contribute_room_limited(
            self.balance, self.contribution_room, amount)
        return actual, self.contribution_room
    
    def overcontribution_penalty(self, excess_amount: float, months: int = 1) -> float:
        """Calculate CRA over-contribution penalty.
        
        ITA s.204.1: RRSP over-contributions above the $2,000 grace amount
        are subject to a penalty of 1% per month on the excess.
        
        The $2,000 grace is cumulative — it's a lifetime allowance, not annual.
        
        Args:
            excess_amount: Amount over the available contribution room
            months: Number of months the excess was in the plan (default 1)
        
        Returns:
            Total penalty amount (0 if within grace amount)
        """
        penalty_base = max(0, excess_amount - self.OVERCONTRIBUTION_GRACE)
        return penalty_base * self.PENALTY_RATE_MONTHLY * months
    
    def grow(self, return_rate: float) -> float:
        """Apply investment growth. Returns growth amount."""
        self.balance, growth = _apply_growth(self.balance, return_rate)
        return growth

    def add_annual_room(self, earned_income: float, year: int = 2026,
                         annual_cap: float = None) -> float:
        """Add annual RRSP room based on earned income.
        
        Args:
            earned_income: Previous year's earned income
            year: Tax year (for cap lookup)
            annual_cap: Annual RRSP contribution cap. If None, uses
                self.annual_room_cap (set from TaxDataProvider by caller).
                If both are 0, falls back to 18% of income (uncapped).
        
        Returns:
            New room added
        """
        cap = annual_cap if annual_cap is not None else self.annual_room_cap
        # DP#13: if cap not configured, use uncapped 18% (clearly round fallback)
        if cap <= 0:
            new_room = earned_income * 0.18
        else:
            new_room = min(earned_income * 0.18, cap)
        new_room = max(0, new_room - self.pension_adjustment)
        self.contribution_room += new_room
        return new_room
    
    def withdrawal_tax(self, withdrawal: float, marginal_rate: float) -> float:
        """Tax on RRSP withdrawal at marginal rate."""
        return withdrawal * marginal_rate


@dataclass
class TSFAccount:
    """Tax-Free Savings Account.
    
    Contributions are after-tax; growth and withdrawals are tax-free.
    
    CRA rules (DP#10):
    - Withdrawals add to contribution room at the start of the NEXT calendar year.
    - Over-contribions are subject to a 1%/month penalty on the excess.
    """
    balance: float = 0.0
    contribution_room: float = 0.0
    annual_room: float = 0.0  # DP#20: set from TaxDataProvider; typical 2026: $7,000
    
    # DP#10: Withdrawals add to next year's contribution room.
    # Track pending recovery until the next annual room addition.
    withdrawals_pending_recovery: float = 0.0
    
    # CRA over-contribution penalty: 1% per month on excess
    PENALTY_RATE_MONTHLY: float = 0.01
    
    def contribute(self, amount: float) -> Tuple[float, float]:
        """Contribute to TFSA. Returns (actual, remaining_room)."""
        self.balance, self.contribution_room, actual = _contribute_room_limited(
            self.balance, self.contribution_room, amount)
        return actual, self.contribution_room

    def grow(self, return_rate: float) -> float:
        """Apply investment growth. Returns growth amount."""
        self.balance, growth = _apply_growth(self.balance, return_rate)
        return growth

    def add_annual_room(self, year: int = 2026,
                          annual_limit: float = None) -> float:
        """Add annual TFSA contribution room.
        
        DP#20: Year-specific TFSA limits. If annual_limit is provided,
        use it instead of self.annual_room. The caller (simulation engine)
        should look up the correct year's limit from TaxDataProvider.
        
        Also applies withdrawal room recovery: withdrawals from the previous
        year add to this year's contribution room (CRA TFSA rules, DP#10).
        
        Args:
            year: Tax year (for documentation; not used in calculation)
            annual_limit: Year-specific TFSA limit. If None, uses self.annual_room.
        
        Returns:
            Amount of room added (including withdrawal recovery)
        """
        limit = annual_limit if annual_limit is not None else self.annual_room
        recovery = self.withdrawals_pending_recovery
        self.contribution_room += limit + recovery
        # Reset pending recovery after applying
        self.withdrawals_pending_recovery = 0.0
        return limit + recovery
    
    def withdraw(self, amount: float) -> float:
        """Withdraw from TFSA (tax-free).
        
        DP#10: Withdrawals add to contribution room at the beginning of
        the next calendar year. The amount is tracked in
        withdrawals_pending_recovery and applied in the next call to
        add_annual_room().
        """
        actual = min(amount, self.balance)
        self.balance -= actual
        self.withdrawals_pending_recovery += actual
        return actual
    
    def overcontribution_penalty(self, excess_amount: float, months: int = 1) -> float:
        """Calculate CRA TFSA over-contribution penalty.
        
        ITA s.207.06: TFSA over-contributions are subject to a penalty
        of 1% per month on the full excess amount. Unlike RRSP, there
        is no grace amount for TFSA.
        
        Args:
            excess_amount: Amount over the available contribution room
            months: Number of months the excess was in the account (default 1)
        
        Returns:
            Total penalty amount (0 if no excess)
        """
        if excess_amount <= 0:
            return 0.0
        return excess_amount * self.PENALTY_RATE_MONTHLY * months


@dataclass
class RESPAccount:
    """Registered Education Savings Plan.
    
    Tracks per-child eligibility, CESG/QESI matching, and lifetime limits.
    """
    balance: float = 0.0
    contributions_total: float = 0.0
    cesg_received: float = 0.0
    qesi_received: float = 0.0
    
    # Per-child data
    children: List[Dict] = field(default_factory=list)
    
    # Government matching rates (depends on family income)
    cesg_basic_rate: float = 0.20    # 20% on first $2,500
    cesg_additional_rate: float = 0.0  # 0% at $189k income (set by income)
    qesi_basic_rate: float = 0.10    # 10% on first $5,000 (Quebec)
    
    # Limits
    cesg_lifetime_max: float = 7200.0
    qesi_lifetime_max: float = 3600.0
    contribution_lifetime_max: float = 50000.0
    annual_contribution_for_match: float = 2500.0
    
    def contribute(self, amount: float, child_name: str = None) -> Tuple[float, Dict]:
        """Contribute to RESP for a specific child.
        
        Returns (actual_contribution, grants_dict)
        """
        # Find child
        child = None
        if child_name:
            for c in self.children:
                if c['name'] == child_name:
                    child = c
                    break
        
        if child and not child.get('cesg_eligible', True):
            # Over 17: no matching, but can still contribute
            actual = min(amount, self.contribution_lifetime_max - self.contributions_total)
            self.contributions_total += actual
            self.balance += actual
            return actual, {'cesg': 0, 'qesi': 0, 'total': 0, 'note': 'Over 17 — no matching'}
        
        actual = min(amount, self.contribution_lifetime_max - self.contributions_total)
        
        # Calculate grants
        cesg = min(actual, self.annual_contribution_for_match) * self.cesg_basic_rate
        if self.cesg_additional_rate > 0:
            cesg += min(actual, 500) * self.cesg_additional_rate
        
        qesi = min(actual, 5000) * self.qesi_basic_rate  # QESI on first $5,000
        
        # Check lifetime limits
        cesg = min(cesg, self.cesg_lifetime_max - self.cesg_received)
        qesi = min(qesi, self.qesi_lifetime_max - self.qesi_received)
        
        self.cesg_received += cesg
        self.qesi_received += qesi
        self.contributions_total += actual
        self.balance += actual + cesg + qesi
        
        grants = {'cesg': cesg, 'qesi': qesi, 'total': cesg + qesi}
        return actual, grants
    
    def grow(self, return_rate: float) -> float:
        """Apply investment growth. Returns growth amount."""
        self.balance, growth = _apply_growth(self.balance, return_rate)
        return growth

    def eap_tax(self, marginal_rate: float = 0.15) -> float:
        """Tax on Educational Assistance Payment (EAP) withdrawal.
        
        40% is return of contributions (tax-free)
        60% is grants + earnings (taxable in student's hands)
        """
        taxable_portion = self.balance * 0.60
        return taxable_portion * marginal_rate


@dataclass
class NonRegAccount:
    """Non-Registered Investment Account.
    
    For Readvanceable mortgage: interest on borrowed funds is tax-deductible.
    Capital gains taxed at 50% inclusion rate.
    """
    balance: float = 0.0
    cost_basis: float = 0.0      # ACB (Adjusted Cost Base)
    interest_paid: float = 0.0    # Total interest paid on borrowed funds
    interest_deductible: float = 0.0  # Portion that's tax-deductible (SM)
    
    # Tax parameters
    capital_gains_inclusion: float = 0.50
    
    def contribute(self, amount: float, is_smith: bool = False) -> float:
        """Add funds. If readvanceable mortgage, mark as deductible investment."""
        self.balance += amount
        self.cost_basis += amount
        return amount
    
    def grow(self, return_rate: float) -> float:
        """Apply investment growth. Returns growth amount.

        Growth doesn't change cost basis (unrealized gains widen instead).
        """
        self.balance, growth = _apply_growth(self.balance, return_rate)
        return growth
    
    def add_smith_interest(self, interest: float, marginal_rate: float) -> Tuple[float, float]:
        """Record SM interest payment and calculate tax savings.
        
        Args:
            interest: Total interest paid on HELOC for investment
            marginal_rate: Contributor's marginal tax rate
        
        Returns:
            (interest_paid, tax_savings)
        """
        self.interest_paid += interest
        self.interest_deductible += interest  # All SM interest is deductible
        tax_savings = interest * marginal_rate
        return interest, tax_savings
    
    def capital_gains_tax(self, marginal_rate: float) -> float:
        """Calculate capital gains tax on unrealized gains."""
        gains = self.balance - self.cost_basis
        if gains <= 0:
            return 0.0
        taxable_income = gains * self.capital_gains_inclusion
        return taxable_income * marginal_rate
    
    @property
    def unrealized_gains(self) -> float:
        """Current unrealized capital gains."""
        return max(0, self.balance - self.cost_basis)


# -----------------------------------------------------------------------------
# DP#10 (issue #723): ReadvanceableMortgage lived here, but it is a mortgage-
# rate / product concern, not a registered account. It has been moved to
# countries/canada/rate_model.py, alongside HELOCPath and the RatePath types
# its calculate_interest() consumes. Import it from there, or from the
# package root (countries.canada) which re-exports it.
# =============================================================================
# Strategy definitions moved to strategy.py (DP#10: one module per concern)
# =============================================================================
# AllocationStrategy, STRATEGY_* constants, and STRATEGIES dict are
# now in strategy.py. Import from there:
#   from countries.canada.strategies import STRATEGIES
#   from strategy import AllocationStrategy