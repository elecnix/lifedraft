#!/usr/bin/env python3
"""
Debt Instruments — ITA §20(1)(c) Deductibility, Tracing, and Debt Swap

This module models the mechanics of debt instruments for tax purposes:
- DebtInstrument: tracks balance, rate, purpose, and deductibility
- HELOCTracing: CRA tracing requirements for deductible interest
- debt_swap_analysis: liquidate non-reg → pay mortgage → re-borrow
- prescribed_rate_loan: spousal loan at the CRA prescribed rate

Per DP#10: this module owns ITA §20(1)(c) — the rules that determine
whether interest on borrowed money is deductible.

Per DP#6: the SM is *discovered* when DebtInstrument.purpose is
"investment" and tracing supports deductibility, not named as a strategy.

References:
     countries/canada/docs/GOVERNMENT_REFERENCES.md — Interest Deductibility and HELOC Tracing entries
    ITA s.20(1)(c)
    ITA s.20(3) (borrowed money used to repay prior borrowing — replacement/refinancing):
        https://laws-lois.justice.gc.ca/eng/acts/I-3.3/page-20.html
    CRA Folio S3-F6-C1: https://www.canada.ca/en/revenue-agency/services/tax/technical-information/income-tax/folio-series/folio-s3/s3-f6-c1-interest-deductibility.html
    CRA Folio S3-F6-C1 para 1.45-1.48 (disappearing source & replacement property)
    CRA prescribed interest rates: https://www.canada.ca/en/revenue-agency/services/tax/prescribed-interest-rates.html
    ITA s.74.5(2) (prescribed-rate loan exception): https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-74.5.html
    Ludmer v. The Queen, 85 DTC 5506: https://canlii.ca/t/1tbb8

Usage:
    from countries.canada.debt import DebtInstrument, HELOCTracing, debt_swap_analysis
    from countries.canada.debt import PrescribedRateLoan

    # Trace HELOC advances for SM deductibility
    tracing = HELOCTracing()
    tracing.advance(50000, "2026-01", "XEQT ETF")
    deductible = tracing.deductible_interest(heloc_balance=100000, heloc_rate=0.05)  # DP#13: round-number placeholder
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum


# =============================================================================
# Enums
# =============================================================================

class DebtPurpose(Enum):
    """Purpose of the debt — determines deductibility under ITA §20(1)(c).

    Key rule: interest on money borrowed for the purpose of earning
    income from property or from a business is deductible. Money
    borrowed for personal purposes (including RRSP/TFSA contributions)
    is NOT deductible.
    """
    INVESTMENT = "investment"               # Non-reg investment → deductible
    RENTAL_EXPENSE = "rental_expense"        # Cash dam → deductible
    PERSONAL = "personal"                    # Mortgage, consumption → NOT deductible
    RRSP_CONTRIBUTION = "rrsp"              # Borrowed for RRSP → NOT deductible
    TFSA_CONTRIBUTION = "tfsa"              # Borrowed for TFSA → NOT deductible
    RESP_CONTRIBUTION = "resp"              # Borrowed for RESP → NOT deductible
    MIXED = "mixed"                          # Proportional tracing needed


class AdvanceRecord:
    """Record of a single HELOC advance for tracing purposes.

    CRA requires that each advance be traced to its use. If any
    advance is used for a non-qualifying purpose, it reduces the
    deductible proportion.

    Args:
        amount: Dollar amount of the advance
        date: Date string (YYYY-MM) for attribution tracking
        purpose: What the advance was used for
        investment_purchased: Name of investment (if investment purpose).
            After a reinvestment disposition, this field still references
            the original investment name since the advance tracks the
            original borrowing, not subsequent use of proceeds.
        tainted: Whether this advance has been tainted by Ludmer disposition (default False)
    """
    def __init__(self, amount: float, date: str,
                 purpose: DebtPurpose = DebtPurpose.INVESTMENT,
                 investment_purchased: str = "",
                 tainted: bool = False):
        self.amount = amount
        self.date = date
        self.purpose = purpose
        self.investment_purchased = investment_purchased
        self.tainted = tainted

    @property
    def is_deductible(self) -> bool:
        """Whether this advance supports interest deductibility.

        Under Ludmer, an investment advance is tainted when the
        investment is sold and proceeds are used for a non-qualifying
        purpose. Rental expense advances are not subject to tainting
        — their deductibility follows ITA s.20(1)(c) against rental income.
        """
        if self.tainted and self.purpose == DebtPurpose.INVESTMENT:
            return False
        return self.purpose in (DebtPurpose.INVESTMENT, DebtPurpose.RENTAL_EXPENSE)

    def __repr__(self):
        inv = f" → {self.investment_purchased}" if self.investment_purchased else ""
        taint = " (tainted)" if self.tainted else ""
        return f"Advance(${self.amount:,.0f} on {self.date}, {self.purpose.value}{inv}{taint})"


class DispositionRecord:
    """Record of a disposition of an investment purchased with HELOC money.

    Under Ludmer v. The Queen (1985 DTC 5506), when an investment purchased
    with borrowed money is sold and the proceeds are used for a
    non-qualifying purpose (not investment or rental), the original
    advance loses its deductibility ("tainted").
    If the proceeds are used to repay the HELOC, the advance is retired
    instead — no deductibility is lost.

    Args:
        amount: Dollar amount of the disposition
        date: Date string (YYYY-MM)
        investment_name: Name of investment sold
        proceeds_use: What the disposition proceeds were used for
        repaid_to_heloc: Amount of proceeds used to repay HELOC (if any)
    """
    def __init__(self, amount: float, date: str, investment_name: str,
                 proceeds_use: DebtPurpose = DebtPurpose.PERSONAL,
                 repaid_to_heloc: float = 0.0):
        if amount <= 0:
            raise ValueError("disposition amount must be positive")
        if repaid_to_heloc < 0:
            raise ValueError("repaid_to_heloc cannot be negative")
        if repaid_to_heloc > amount:
            raise ValueError(
                f"repaid_to_heloc ({repaid_to_heloc}) cannot exceed "
                f"disposition amount ({amount})")
        self.amount = amount
        self.date = date
        self.investment_name = investment_name
        self.proceeds_use = proceeds_use
        self.repaid_to_heloc = repaid_to_heloc

    @property
    def taints_advance(self) -> bool:
        """Whether this disposition taints any portion of the original advance.

        A disposition taints when some proceeds are used for a
        non-qualifying purpose (not investment or rental expense),
        net of any HELOC repayment.
        """
        qualifying = (DebtPurpose.INVESTMENT, DebtPurpose.RENTAL_EXPENSE)
        net_personal = self.amount - self.repaid_to_heloc
        return net_personal > 0 and self.proceeds_use not in qualifying

    def __repr__(self):
        return (f"Disposition(${self.amount:,.0f} of {self.investment_name} "
                f"on {self.date}, proceeds→{self.proceeds_use.value}, "
                f"repaid=${self.repaid_to_heloc:,.0f})")


# =============================================================================
# Debt Instrument
# =============================================================================

@dataclass
class DebtInstrument:
    """A debt instrument with balance, rate, and deductibility tracking.

    Models the mechanics of a debt (balance, interest, principal)
    plus the tax treatment of interest (deductible vs not) based
    on purpose and tracing.

    This is a data object (DP#8: compose through data). The engines
    in strategy.py and simulation.py make decisions based on this data.
    """
    balance: float = 0.0
    rate: float = 0.05  # Default: 5% (round number, DP#13)
    purpose: DebtPurpose = DebtPurpose.INVESTMENT
    name: str = "Debt"

    # Tracing records (for HELOC tracing rules)
    advances: List[AdvanceRecord] = field(default_factory=list)

    # Year-by-year tracking
    interest_paid: float = 0.0
    interest_deductible: float = 0.0
    principal_paid: float = 0.0

    @property
    def is_interest_deductible(self) -> bool:
        """Whether interest on this debt is deductible (computed from purpose + tracing)."""
        if self.purpose in (DebtPurpose.INVESTMENT, DebtPurpose.RENTAL_EXPENSE):
            return True
        if self.purpose == DebtPurpose.MIXED:
            # Check tracing: deductible if all advances were for qualifying purposes
            if not self.advances:
                return False
            return all(a.is_deductible for a in self.advances)
        return False

    @property
    def deductible_proportion(self) -> float:
        """Proportion of balance that supports deductible interest.

        For a mixed-purpose debt, this is the ratio of investment
        advances to total advances.
        """
        if self.purpose != DebtPurpose.MIXED or not self.advances:
            return 1.0 if self.is_interest_deductible else 0.0

        investment_total = sum(a.amount for a in self.advances if a.is_deductible)
        total = sum(a.amount for a in self.advances)
        return investment_total / total if total > 0 else 0.0

    def annual_interest(self) -> float:
        """Calculate annual interest on current balance."""
        return self.balance * self.rate

    def deductible_interest(self) -> float:
        """Calculate deductible portion of annual interest."""
        return self.annual_interest() * self.deductible_proportion


# =============================================================================
# HELOC Tracing — CRA §20(1)(c) Requirements
# =============================================================================

class HELOCTracing:
    """Tracks HELOC advances for CRA tracing requirements.

    The key rule under ITA §20(1)(c): interest on borrowed money is
    deductible only to the extent the borrowed money is used for the
    purpose of earning income from property or from a business.

    The CRA requires "tracing" — each dollar borrowed must be traced
    to its use. Personal draws "poison" the proportional deduction.

    This class is a mutable accumulator (ledger). The computation
    methods (deductible_interest, summary) are pure over the
    accumulated state.
    """

    def __init__(self, name: str = "HELOC"):
        self.name = name
        self.advances: List[AdvanceRecord] = []
        self.personal_draws: float = 0.0  # Initial personal draws (excludes tainted amounts)
        self.repayments: List[Tuple[float, str]] = []  # (amount, date)
        self.dispositions: List[DispositionRecord] = []

    def advance(self, amount: float, date: str,
                purpose: DebtPurpose = DebtPurpose.INVESTMENT,
                investment_purchased: str = "") -> None:
        """Record a HELOC advance for tracing.

        Args:
            amount: Dollar amount
            date: Date (YYYY-MM)
            purpose: Purpose of the advance
            investment_purchased: Name of investment purchased (if applicable)
        """
        record = AdvanceRecord(amount, date, purpose, investment_purchased)
        self.advances.append(record)
        if not record.is_deductible:
            self.personal_draws += amount

    def disposition(self, amount: float, date: str, investment_name: str,
                    proceeds_use: DebtPurpose = DebtPurpose.PERSONAL,
                    repaid_to_heloc: float = 0.0) -> None:
        """Record disposition of investment purchased with HELOC money.

        Under Ludmer v. The Queen (1985 DTC 5506), when an investment
        purchased with borrowed money is sold and the proceeds are used
        for a non-qualifying purpose, the original advance loses its
        deductibility ("tainted"). The new borrowing must be re-traced.

        Processing order:
        1. Retire repaid_to_heloc from matching advances (reduce/remove)
        2. Taint the non-qualifying portion from remaining advance balance

        If the non-qualifying portion exceeds remaining clean advances,
        only the available balance is tainted.

        Args:
            amount: Dollar amount of the disposition
            date: Date (YYYY-MM)
            investment_name: Name of investment sold (matches advance)
            proceeds_use: What the proceeds were used for
            repaid_to_heloc: Amount of proceeds used to repay HELOC

        Raises:
            ValueError: If no matching advance exists for investment_name
            ValueError: If repaid_to_heloc exceeds total matching advances
        """
        record = DispositionRecord(amount, date, investment_name,
                                     proceeds_use, repaid_to_heloc)

        # Pre-validate before any mutations
        matching = [i for i, a in enumerate(self.advances)
                    if a.investment_purchased == investment_name and not a.tainted]
        if not matching:
            raise ValueError(
                f"No unmatched advance for '{investment_name}' to dispose")

        matching_total = sum(self.advances[i].amount for i in matching)
        if repaid_to_heloc > matching_total:
            raise ValueError(
                f"repaid_to_heloc ({repaid_to_heloc}) exceeds total "
                f"matching advances for '{investment_name}'")

        # Phase 1: Retire repaid_to_heloc from matching advances
        to_retire = repaid_to_heloc
        indices_to_remove = []
        for idx in matching:
            if to_retire <= 0:
                break
            adv = self.advances[idx]
            if adv.amount <= to_retire:
                indices_to_remove.append(idx)
                to_retire -= adv.amount
            else:
                self.advances[idx] = AdvanceRecord(
                    adv.amount - to_retire, adv.date, adv.purpose,
                    adv.investment_purchased, adv.tainted
                )
                to_retire = 0

        # Remove fully-retired advances (reverse order to preserve indices)
        for idx in sorted(indices_to_remove, reverse=True):
            self.advances.pop(idx)

        # Phase 2: Taint non-qualifying portion from remaining matching advances
        qualifying = (DebtPurpose.INVESTMENT, DebtPurpose.RENTAL_EXPENSE)
        net_nonqualifying = amount - repaid_to_heloc
        if net_nonqualifying <= 0 or proceeds_use in qualifying:
            self.dispositions.append(record)
            return

        to_taint = net_nonqualifying
        new_records = []
        for idx, adv in enumerate(self.advances):
            if to_taint <= 0:
                break
            if adv.investment_purchased != investment_name or adv.tainted:
                continue
            if adv.amount <= to_taint:
                # Full taint: replace with tainted copy
                self.advances[idx] = AdvanceRecord(
                    adv.amount, adv.date, adv.purpose,
                    adv.investment_purchased, tainted=True
                )
                to_taint -= adv.amount
            else:
                # Partial taint: split into clean + tainted
                self.advances[idx] = AdvanceRecord(
                    adv.amount - to_taint, adv.date, adv.purpose,
                    adv.investment_purchased, tainted=False
                )
                new_records.append(
                    (idx + 1, AdvanceRecord(
                        to_taint, adv.date, adv.purpose,
                        adv.investment_purchased, tainted=True
                    ))
                )
                to_taint = 0

        # Insert new tainted records (reverse order to preserve indices)
        for insert_idx, rec in sorted(new_records, key=lambda x: x[0], reverse=True):
            self.advances.insert(insert_idx, rec)

        self.dispositions.append(record)

    def total_advanced(self) -> float:
        """Total dollars advanced from HELOC."""
        return sum(a.amount for a in self.advances)

    def investment_advanced(self) -> float:
        """Total dollars advanced for investment (deductible)."""
        return sum(a.amount for a in self.advances if a.is_deductible)

    def deductible_interest(self, heloc_balance: float,
                            heloc_rate: float) -> float:
        """Calculate deductible interest based on tracing.

        The deductible proportion = investment advances / total advances.
        This handles the "poisoning" from personal draws.

        Args:
            heloc_balance: Current HELOC balance
            heloc_rate: Current HELOC interest rate

        Returns:
            Deductible interest amount for the year
        """
        total = self.total_advanced()
        if total <= 0:
            return 0.0

        proportion = self.investment_advanced() / total
        annual_interest = heloc_balance * heloc_rate
        return annual_interest * proportion

    def non_deductible_interest(self, heloc_balance: float,
                                 heloc_rate: float) -> float:
        """Non-deductible portion of interest."""
        total_interest = heloc_balance * heloc_rate
        return total_interest - self.deductible_interest(heloc_balance, heloc_rate)

    def summary(self) -> Dict:
        """Return tracing summary for reporting."""
        total = self.total_advanced()
        invested = self.investment_advanced()
        tainted = sum(a.amount for a in self.advances if a.tainted)
        non_deductible = total - invested
        return {
            'name': self.name,
            'total_advanced': total,
            'investment_advanced': invested,
            'personal_draws': self.personal_draws,
            'non_deductible_amount': non_deductible,
            'deductible_proportion': invested / total if total > 0 else 0,
            'num_advances': len(self.advances),
            'num_dispositions': len(self.dispositions),
            'tainted_amount': tainted,
        }


# =============================================================================
# Debt Swap Analysis — SCENARIO_SEED 1.1
# =============================================================================

def debt_swap_analysis(
    non_reg_balance: float,
    adjusted_cost_base: float,
    marginal_rate: float,
    mortgage_balance: float,
    mortgage_rate: float,
    heloc_rate: float,
    capital_gains_inclusion: float = 0.50,
    years: int = 10,
) -> Dict:
    """Analyze the debt swap strategy: liquidate non-reg → pay mortgage → re-borrow.

    Steps:
    1. Sell non-reg investments → trigger capital gains tax on disposition
    2. Use after-tax proceeds to pay down mortgage
    3. Re-borrow via HELOC to repurchase investments
    4. New HELOC interest is deductible (traced to investment)

    The swap is beneficial when:
    - The after-tax cost of deductible HELOC < after-tax cost of non-deductible mortgage
    - i.e., heloc_rate × (1 - marginal_rate) < mortgage_rate

    Args:
        non_reg_balance: Current fair market value of non-reg holdings
        adjusted_cost_base: ACB of non-reg holdings (for capital gains tax)
        marginal_rate: Combined marginal tax rate
        mortgage_balance: Current mortgage balance
        mortgage_rate: Current mortgage rate
        heloc_rate: HELOC rate after re-borrowing
        capital_gains_inclusion: CG inclusion rate (default 50%)
        years: Projection period

    Returns:
        Dict with swap analysis results
    """
    # Step 1: Capital gains on disposition
    capital_gain = max(0, non_reg_balance - adjusted_cost_base)
    capital_gains_tax = capital_gain * capital_gains_inclusion * marginal_rate
    after_tax_proceeds = non_reg_balance - capital_gains_tax

    # Step 2: Pay down mortgage with after-tax proceeds
    new_mortgage_balance = max(0, mortgage_balance - after_tax_proceeds)

    # Step 3: Re-borrow via HELOC to repurchase investments
    # (We borrow back the full after-tax proceeds, traced to investment)
    new_heloc_balance = after_tax_proceeds

    # Step 4: Compare interest costs
    # Before swap: mortgage interest (not deductible)
    old_annual_interest = mortgage_balance * mortgage_rate
    old_after_tax_cost = old_annual_interest  # No tax deduction

    # After swap: reduced mortgage + new HELOC (deductible)
    new_mortgage_interest = new_mortgage_balance * mortgage_rate
    new_heloc_interest = new_heloc_balance * heloc_rate
    new_readvance_tax_savings = new_heloc_interest * marginal_rate  # Deductible!
    new_after_tax_cost = new_mortgage_interest + new_heloc_interest - new_readvance_tax_savings

    # Annual and cumulative benefit
    annual_benefit = old_after_tax_cost - new_after_tax_cost
    cumulative_benefit = annual_benefit * years

    # Net benefit accounting for the one-time tax cost
    net_benefit = cumulative_benefit - capital_gains_tax

    # Is the swap worth it?
    breakeven_years = capital_gains_tax / annual_benefit if annual_benefit > 0 else float('inf')
    swap_beneficial = net_benefit > 0 and breakeven_years < years

    return {
        'strategy': 'debt_swap',
        'non_reg_balance': non_reg_balance,
        'adjusted_cost_base': adjusted_cost_base,
        'capital_gain': capital_gain,
        'capital_gains_tax': capital_gains_tax,
        'after_tax_proceeds': after_tax_proceeds,
        'old_mortgage_balance': mortgage_balance,
        'new_mortgage_balance': new_mortgage_balance,
        'new_heloc_balance': new_heloc_balance,
        'old_annual_after_tax_cost': old_after_tax_cost,
        'new_annual_after_tax_cost': new_after_tax_cost,
        'annual_benefit': annual_benefit,
        'cumulative_benefit': cumulative_benefit,
        'one_time_tax_cost': capital_gains_tax,
        'net_benefit': net_benefit,
        'breakeven_years': breakeven_years,
        'swap_beneficial': swap_beneficial,
        'condition_met': heloc_rate * (1 - marginal_rate) < mortgage_rate,
    }


# =============================================================================
# Cash Dam Analysis — SCENARIO_SEED 1.5
# =============================================================================

def cash_dam_analysis(
    rental_income: float,
    rental_expenses: float,
    mortgage_balance: float,
    mortgage_rate: float,
    heloc_rate: float,
    marginal_rate: float,
    years: int = 10,
) -> Dict:
    """Analyze the cash damming strategy.

    Cash damming: pay rental expenses from HELOC (deductible),
    use rental income to pay down mortgage (non-deductible).

    This converts non-deductible mortgage interest into deductible
    HELOC interest, but only up to the amount of rental expenses.

    Args:
        rental_income: Annual gross rental income
        rental_expenses: Annual rental expenses (excluding interest)
        mortgage_balance: Current mortgage balance
        mortgage_rate: Current mortgage rate
        heloc_rate: HELOC rate
        marginal_rate: Combined marginal tax rate
        years: Projection period

    Returns:
        Dict with cash dam analysis results
    """
    # The amount we can shift is limited by rental expenses
    # (the HELOC advances must be traced to rental expenses)
    annual_shift = rental_expenses

    # Before cash dam: mortgage interest on full balance
    old_interest = mortgage_balance * mortgage_rate
    old_after_tax_cost = old_interest  # Not deductible

    # After cash dam: mortgage reduced by rental income each year
    # HELOC grows by rental expenses each year (traced to rental → deductible)
    cumulative_mortgage_reduction = 0
    cumulative_heloc_increase = 0
    annual_benefits = []

    mortgage = mortgage_balance
    heloc = 0

    for yr in range(years):
        # Pay rental expenses from HELOC (traced to rental → deductible)
        heloc += annual_shift
        # Use rental income to pay down mortgage
        mortgage = max(0, mortgage - rental_income)

        # Interest calculations
        mortgage_interest = mortgage * mortgage_rate
        heloc_interest = heloc * heloc_rate
        readvance_tax_savings = heloc_interest * marginal_rate

        new_after_tax = mortgage_interest + heloc_interest - readvance_tax_savings
        annual_benefit = old_after_tax_cost - new_after_tax
        annual_benefits.append(annual_benefit)

    total_benefit = sum(annual_benefits)

    return {
        'strategy': 'cash_dam',
        'annual_shift': annual_shift,
        'rental_income': rental_income,
        'rental_expenses': rental_expenses,
        'old_annual_after_tax_cost': old_after_tax_cost,
        'total_benefit': total_benefit,
        'annual_benefits': annual_benefits,
        'final_mortgage': mortgage,
        'final_heloc': heloc,
    }


# =============================================================================
# Prescribed Rate Loan — SCENARIO_SEED 14.1
# =============================================================================

class PrescribedRateLoan:
    """A loan at the CRA prescribed rate between family members.

    Used for income splitting: the higher-earner lends to the lower-earner
    at the prescribed rate. The lower-earner invests and earns returns.
    If the investment return exceeds the prescribed rate, the difference
    stays in the lower earner's hands (taxed at their lower rate).

    Key rules:
    - Interest must be paid by January 30 of the following year
    - If interest is not paid by Jan 30, attribution rules apply
    - The prescribed rate is set quarterly by CRA
    - The rate is locked in for the life of the loan

    Current prescribed rate (2026 Q1): 2% (round number for default)
    """

    def __init__(self, principal: float, rate: float = 0.02,
                 lender: str = "primary", borrower: str = "spouse",
                 start_year: int = 2026,
                 interest_paid_by_jan30: bool = True):
        self.principal = principal
        self.rate = rate
        self.lender = lender
        self.borrower = borrower
        self.start_year = start_year
        self.interest_paid_by_jan30 = interest_paid_by_jan30

        # Tracking
        self.annual_interest_payments: List[float] = []
        self.investment_returns: List[float] = []

    def annual_interest(self) -> float:
        """Annual interest payment due."""
        return self.principal * self.rate

    def interest_paid_on_time(self, year: int) -> bool:
        """Check if interest was paid by Jan 30 for a given year.

        If not paid on time, attribution rules apply (ITA s.74.1).
        """
        return self.interest_paid_by_jan30

    def net_income_splitting_benefit(
        self,
        investment_return: float,
        lender_marginal_rate: float,
        borrower_marginal_rate: float,
    ) -> float:
        """Calculate the net benefit of the prescribed rate loan.

        The benefit comes from the rate spread: the borrower invests
        at a higher return than the loan rate, and the difference is
        taxed at the borrower's lower rate.

        Args:
            investment_return: Expected investment return (e.g., 0.07)
            lender_marginal_rate: Lender's marginal tax rate
            borrower_marginal_rate: Borrower's marginal tax rate

        Returns:
            Net annual tax benefit in dollars
        """
        interest = self.annual_interest()
        investment_income = self.principal * investment_return

        # Lender: receives interest (taxed at their rate)
        # But also pays tax on interest income
        lender_net = interest * (1 - lender_marginal_rate)

        # Borrower: earns investment return, pays interest to lender
        # Net income = investment_income - interest
        borrower_net_income = investment_income - interest
        # Taxed at borrower's lower rate
        borrower_after_tax = borrower_net_income * (1 - borrower_marginal_rate)

        # Without the loan: lender would invest directly
        lender_direct_after_tax = investment_income * (1 - lender_marginal_rate)

        # With the loan: lender gets interest + borrower keeps the spread
        total_with_loan = lender_net + borrower_after_tax

        # Net benefit = with loan - without loan
        benefit = total_with_loan - lender_direct_after_tax

        # If attribution applies (interest not paid by Jan 30), no benefit
        if not self.interest_paid_by_jan30:
            return 0.0

        return max(0, benefit)

    def attribution_applies(self, year: int) -> bool:
        """Check if attribution rules apply.

        Attribution applies if interest is not paid by Jan 30.
        When attribution applies, the investment income is taxed
        in the lender's hands regardless.
        """
        return not self.interest_paid_by_jan30

    def summary(self) -> Dict:
        """Return loan summary for reporting."""
        return {
            'principal': self.principal,
            'rate': self.rate,
            'lender': self.lender,
            'borrower': self.borrower,
            'annual_interest': self.annual_interest(),
            'interest_paid_on_time': self.interest_paid_by_jan30,
            'attribution_risk': not self.interest_paid_by_jan30,
        }


# =============================================================================
# Interest Deductibility Check — ITA §20(1)(c)
# =============================================================================

def is_interest_deductible(
    purpose: DebtPurpose,
    tracing: Optional[HELOCTracing] = None,
    earns_income: bool = True,
    is_readvanceable: bool = True,
) -> Dict:
    """Pure function to determine if interest is deductible under ITA §20(1)(c).

    This is the rule engine that enables DP#6: strategies are discovered
    when the rules hold, not named by convention.

    Rules:
    1. Money must be borrowed for purpose of earning income (s.20(1)(c)(i))
    2. Income must be at least potentially taxable (no TFSA-like shelters)
    3. Tracing must show the money was used for qualifying investment
    4. Reasonable expectation of income (not just capital gains)

    Args:
        purpose: Purpose of the borrowing
        tracing: HELOC tracing records (if applicable)
        earns_income: Whether investment earns taxable income
        is_readvanceable: Whether mortgage allows readvancing

    Returns:
        Dict with deductible (bool), reason, and proportion
    """
    # Rule 1: Must be for purpose of earning income
    if purpose == DebtPurpose.PERSONAL:
        return {
            'deductible': False,
            'reason': 'Personal purpose: interest on personal debt is not deductible',
            'proportion': 0.0,
        }

    if purpose in (DebtPurpose.RRSP_CONTRIBUTION, DebtPurpose.TFSA_CONTRIBUTION,
                   DebtPurpose.RESP_CONTRIBUTION):
        return {
            'deductible': False,
            'reason': f'Borrowed for {purpose.value}: income in registered accounts is sheltered',
            'proportion': 0.0,
        }

    if purpose == DebtPurpose.RENTAL_EXPENSE:
        return {
            'deductible': True,
            'reason': 'Rental expense: interest deductible against rental income',
            'proportion': 1.0,
        }

    if purpose == DebtPurpose.INVESTMENT:
        if not earns_income:
            return {
                'deductible': False,
                'reason': 'Investment does not earn taxable income (no reasonable expectation)',
                'proportion': 0.0,
            }
        return {
            'deductible': True,
            'reason': 'Borrowed for investment earning taxable income: deductible under §20(1)(c)',
            'proportion': 1.0,
        }

    if purpose == DebtPurpose.MIXED:
        if tracing is None:
            return {
                'deductible': False,
                'reason': 'Mixed purpose without tracing: cannot determine deductibility',
                'proportion': 0.0,
            }
        proportion = tracing.investment_advanced() / max(1, tracing.total_advanced())
        return {
            'deductible': proportion > 0,
            'reason': f'Mixed purpose: {proportion*100:.0f}% traced to investment',
            'proportion': proportion,
        }

    return {
        'deductible': False,
        'reason': f'Unknown purpose: {purpose.value}',
        'proportion': 0.0,
    }


# =============================================================================
# Property Replacement / Refinancing — ITA s.20(3)
# =============================================================================

def replacement_property_deductibility(
    original_balance: float,
    disposition_proceeds: float,
    replacement_cost: float,
    original_purpose: DebtPurpose = DebtPurpose.INVESTMENT,
    replacement_earns_income: bool = True,
) -> Dict:
    """Determine deductibility of interest after an income-earning property is
    replaced by another, under ITA s.20(3) and the CRA's "disappearing source"
    and replacement-property administrative positions.

    ITA s.20(3) deems borrowed money that is used to repay money previously
    borrowed (refinancing) to have been used for the same purpose as the
    earlier borrowing. The CRA extends a parallel "flexible tracing" position
    (S3-F6-C1, para 1.45-1.48): when borrowed money was used to acquire an
    income-earning property and that property is replaced, the link between the
    borrowing and an income-earning use is preserved to the extent the proceeds
    of disposition are reinvested in a replacement income-earning property.

    Standard case modelled here (DP#10):
    - Interest on the original loan stays fully deductible up to the lesser of
      the original loan balance and the amount of disposition proceeds
      *reinvested* in a replacement income-earning property.
    - To the extent disposition proceeds are NOT reinvested (e.g. withdrawn for
      personal use), the corresponding share of the original loan loses its
      income-earning link and the interest becomes non-deductible
      (the "disappearing source" — but see s.20.1 for the partial relief that is
      out of scope here; we implement the standard full-trace case).
    - If the replacement property does not earn income, no portion qualifies.

    Assumption (documented per task): where proceeds exceed the original loan,
    deductibility is capped at the original loan balance (you cannot create
    deductible interest you never paid). Where the replacement costs less than
    the reinvested proceeds, the reinvested amount is capped at replacement cost.

    Args:
        original_balance: Outstanding balance of the original income-earning loan
        disposition_proceeds: Proceeds from selling the original property
        replacement_cost: Amount actually invested in the replacement property
        original_purpose: Purpose of the original borrowing (must be qualifying)
        replacement_earns_income: Whether the replacement property earns income

    Returns:
        Dict with deductible_balance, non_deductible_balance,
        deductible_proportion, reinvested_proceeds, and reason.
    """
    qualifying = (DebtPurpose.INVESTMENT, DebtPurpose.RENTAL_EXPENSE)

    if original_balance <= 0:
        return {
            'deductible_balance': 0.0,
            'non_deductible_balance': 0.0,
            'deductible_proportion': 0.0,
            'reinvested_proceeds': 0.0,
            'reason': 'No original loan balance to trace.',
        }

    if original_purpose not in qualifying:
        return {
            'deductible_balance': 0.0,
            'non_deductible_balance': original_balance,
            'deductible_proportion': 0.0,
            'reinvested_proceeds': 0.0,
            'reason': ('Original borrowing was not for an income-earning purpose; '
                       's.20(3) replacement relief does not apply.'),
        }

    if not replacement_earns_income:
        return {
            'deductible_balance': 0.0,
            'non_deductible_balance': original_balance,
            'deductible_proportion': 0.0,
            'reinvested_proceeds': 0.0,
            'reason': ('Replacement property does not earn income; the income '
                       'source has disappeared (S3-F6-C1).'),
        }

    # Proceeds actually reinvested in the replacement income-earning property.
    reinvested = max(0.0, min(disposition_proceeds, replacement_cost))

    # The original loan's deductible link is preserved only to the extent the
    # proceeds are reinvested, capped at the original loan balance.
    deductible_balance = min(original_balance, reinvested)
    non_deductible_balance = original_balance - deductible_balance
    proportion = deductible_balance / original_balance

    if proportion >= 1.0:
        reason = ('Full replacement under s.20(3): all proceeds reinvested in a '
                  'new income-earning property; interest stays deductible.')
    elif proportion <= 0.0:
        reason = ('No proceeds reinvested: income source disappeared; interest '
                  'on the original loan is no longer deductible.')
    else:
        reason = (f'Partial replacement: {proportion*100:.0f}% of the loan traced '
                  'to a new income-earning property remains deductible.')

    return {
        'deductible_balance': deductible_balance,
        'non_deductible_balance': non_deductible_balance,
        'deductible_proportion': proportion,
        'reinvested_proceeds': reinvested,
        'reason': reason,
    }