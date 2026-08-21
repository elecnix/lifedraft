#!/usr/bin/env python3
"""
Tax Calculator — Generic Core Module

All functions are pure (same input = same output) and can be used independently.
Brackets come from tax_data module (year + jurisdiction as parameters),
not hardcoded constants.

This module contains **generic, jurisdiction-agnostic** tax functions that
take brackets as data parameters.  Canadian-specific convenience functions
(federal_tax, quebec_tax, RRSP helpers, dividend gross-up/DTC, etc.) have
been moved to ``countries.canada.tax_calc`` for locality.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Federal/Provincial Tax Brackets entries

Supports:
- Any province (via TaxDataProvider)
- Marginal rate calculation
- Total tax on income
- Capital gains effective rate
- Investment income tax by type
- Effective tax rate

Usage:
    from tax_calculator import marginal_rate, tax_on_income
    from tax_data import default_tax_provider

    # Default (Quebec 2026):
    brackets = default_tax_provider().get_combined_brackets()

    # Explicit year/province:
    brackets = default_tax_provider().get_combined_brackets(year=2026, province='ontario')
    rate = marginal_rate(130000, brackets)

    # Canadian-specific helpers (moved to countries.canada.tax_calc):
    from countries.canada.tax_calc import federal_tax, quebec_tax, QC_ABATEMENT
"""

from typing import Dict, List, Optional, Tuple

from tax_data import TaxDataProvider, default_tax_provider

# No module-level singleton — each caller should either pass an explicit
# TaxDataProvider or reuse the process-wide cached default provider (default
# indexation, read-only) rather than build a fresh one on every call (#839).


def _compute_legacy_brackets(provider: TaxDataProvider = None):
    """Compute QUEBEC_TAX_BRACKETS_2026 from tax_data."""
    if provider is None:
        provider = default_tax_provider()
    combined = provider.get_brackets(2026, 'canada', 'quebec')

    brackets = []
    for b in combined:
        brackets.append((b.min_income, b.max_income, 0.0, 0.0, b.rate))
    return brackets


# Issue #117: Removed _QUEBEC_TAX_BRACKETS_2026_CACHE module-level mutable
# singleton. Each call now computes fresh brackets from tax_data instead
# of caching in a global variable that persists across test runs.

def _QUEBEC_TAX_BRACKETS_2026():
    """Compute brackets via _BracketProxy for module-level QUEBEC_TAX_BRACKETS_2026."""
    return _compute_legacy_brackets()


class _BracketProxy:
    """Proxy that computes brackets from tax_data on each access.

    Issue #117: No global cache — fresh computation per call prevents
    stale brackets across test runs.
    """
    def __iter__(self):
        return iter(self._get())

    def __getitem__(self, index):
        return self._get()[index]

    def __len__(self):
        return len(self._get())

    def _get(self):
        return _compute_legacy_brackets()


QUEBEC_TAX_BRACKETS_2026 = _BracketProxy()


# =============================================================================
# Pure Functions — Generic, Jurisdiction-Agnostic
# =============================================================================

def marginal_rate(income: float, brackets: List[Dict] = None,
                   year: int = 2026, province: str = "quebec") -> float:
    """Get the marginal tax rate for a given income level.

    Args:
        income: Taxable income
        brackets: Combined bracket list (if None, loads from tax_data)
        year: Tax year (used if brackets is None)
        province: Province code (used if brackets is None)

    Returns:
        Marginal rate as decimal (e.g. 0.4571 for 45.71%)
    """
    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets(year, province)
    for b in brackets:
        upper = b['max'] if b['max'] else float('inf')
        if income <= upper:
            return b['rate']
    return brackets[-1]['rate']


def bracket_ceiling(income: float, brackets: List[Dict] = None,
                     year: int = 2026, province: str = "quebec") -> float:
    """Top of the tax bracket containing ``income`` (DP#20: year-versioned).

    Used by the retirement bracket-filling drawdown order (issue #618) to
    auto-detect how much MORE taxable income can be recognized this year
    before crossing into the next bracket, when the household has not
    configured an explicit target. Returns ``income`` itself (zero headroom)
    when ``income`` already sits in the unbounded top bracket — there is no
    higher bracket to avoid crossing into.

    Args:
        income: Taxable income already recognized this year.
        brackets: Combined bracket list (if None, loads from tax_data).
        year: Tax year (used if brackets is None).
        province: Province code (used if brackets is None).

    Returns:
        Dollar ceiling of the bracket containing ``income``.
    """
    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets(year, province)
    for b in brackets:
        upper = b['max'] if b['max'] else 0
        if upper <= 0:
            return income  # top (unbounded) bracket: no headroom to seek
        if income < upper:
            return upper
    return income


def tax_on_income(income: float, brackets: List[Dict] = None,
                   year: int = 2026, province: str = "quebec") -> float:
    """Calculate total combined tax on income.

    Args:
        income: Taxable income
        brackets: Combined bracket list (if None, loads from tax_data)
        year: Tax year (used if brackets is None)
        province: Province code (used if brackets is None)

    Returns:
        Total tax in dollars
    """
    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets(year, province)
    tax = 0.0
    for b in brackets:
        upper = b['max'] if b['max'] else float('inf')
        if income <= b['min']:
            break
        taxable = min(income, upper) - b['min']
        tax += max(0, taxable) * b['rate']
        if income <= upper:
            break
    return tax


def deduction_value(income: float, amount: float,
                    brackets: List[Dict] = None,
                    year: int = 2026, province: str = "quebec") -> float:
    """Tax saved by deducting ``amount`` from taxable ``income`` (bracket-fill).

    The deduction is valued at the actual marginal rates of the income slice it
    removes — the top bracket first, then progressively lower brackets as income
    falls. This is the tax difference, not ``amount * top_rate``: a deduction
    that crosses a bracket boundary is worth less than the top marginal rate
    applied flat (DP#19, DP#27).

    Returns 0 for a non-positive ``amount``.
    """
    if amount <= 0:
        return 0.0
    if brackets is None:
        brackets = default_tax_provider().get_combined_brackets(year, province)
    lower = max(0.0, income - amount)
    return tax_on_income(income, brackets) - tax_on_income(lower, brackets)


def effective_tax_rate(income: float, brackets: List[Dict] = None) -> float:
    """Calculate effective (average) tax rate.

    Args:
        income: Taxable income
        brackets: Combined bracket list

    Returns:
        Effective rate as decimal (e.g. 0.25)
    """
    if income <= 0:
        return 0.0
    total_tax = tax_on_income(income, brackets)
    return total_tax / income


def capital_gains_rate(income: float, brackets: List[Dict] = None,
                       inclusion_rate: float = 0.50) -> float:
    """Effective capital gains tax rate at a given income level.

    Args:
        income: Taxable income (before capital gains)
        brackets: Combined bracket list
        inclusion_rate: Capital gains inclusion rate (default 50% for 2024+)

    Returns:
        Effective CG rate as decimal
    """
    marginal = marginal_rate(income, brackets)
    return marginal * inclusion_rate


# Type alias for readability
TaxBracket = Dict[str, float]


# =============================================================================
# Investment Income Types — Per-Type Tax Treatment
# =============================================================================

from enum import Enum


class InvestmentIncomeType(Enum):
    """Types of investment income with different tax treatment.

    Each type has a different effective tax rate because of how
    the Income Tax Act treats them:
    - Eligible dividends: gross-up + dividend tax credit (DTC)
    - Capital gains: partial inclusion (50% for 2024+)
    - Interest/bond income: fully taxable at marginal rate
    - Foreign dividends: fully taxable + withholding tax implications
    - Return of capital: tax-deferred (reduces ACB)
    """
    CANADIAN_ELIGIBLE_DIVIDEND = "eligible_dividend"
    CAPITAL_GAIN = "capital_gain"
    INTEREST = "interest"
    FOREIGN_DIVIDEND_US = "us_dividend"
    FOREIGN_DIVIDEND_NON_US = "non_us_dividend"
    RETURN_OF_CAPITAL = "roc"


def tax_on_investment_income(amount: float,
                               income_type: InvestmentIncomeType,
                               marginal_rate: float = 0.0,  # DP#13: compute from TaxDataProvider; Quebec ~$150k: 0.4571
                               brackets: List[Dict] = None,
                               province: str = "quebec",
                               year: int = 2026,
                               existing_income: float = 0,
                               acb: float = 0,
                               **kwargs) -> Dict:
    """Calculate tax on investment income by type.

    Generic dispatch function that routes to the appropriate calculation
    based on income_type.  Canadian-specific calculations (eligible
    dividends, WHT) delegate to ``countries.canada.tax_calc``.

    Pure function: same inputs always produce same output (DP#3).

    Args:
        amount: Investment income amount
        income_type: Type of investment income
        marginal_rate: Marginal tax rate (combined)
        brackets: Tax brackets (for income-type calculations)
        province: Province code
        year: Tax year
        existing_income: Existing taxable income (for bracket push)
        acb: Adjusted cost base (for ROC / capital gains calculations)
        **kwargs: Additional keyword arguments (forwarded)

    Returns:
        Dict with tax, effective_rate, details
    """
    mr = marginal_rate

    if amount <= 0:
        return {'tax': 0, 'effective_rate': 0, 'taxable_amount': 0, 'details': 'No income'}

    if income_type == InvestmentIncomeType.CANADIAN_ELIGIBLE_DIVIDEND:
        from countries.canada.tax_calc import tax_on_eligible_dividend, effective_dividend_rate
        tax = tax_on_eligible_dividend(amount, mr, province, year)
        eff_rate = effective_dividend_rate(mr, province)
        return {
            'tax': tax,
            'effective_rate': eff_rate,
            'taxable_amount': amount * 1.38,  # Grossed-up
            'details': f'Gross-up 38%, DTC applied, effective {eff_rate*100:.1f}%',
        }

    elif income_type == InvestmentIncomeType.CAPITAL_GAIN:
        # DP#27, DP#20: Use year-versioned tiered inclusion rate
        from countries.canada.income_type import capital_gains_inclusion_rate
        inclusion = capital_gains_inclusion_rate(amount, year)
        taxable_gain = amount * inclusion
        tax = taxable_gain * mr
        eff_rate = tax / amount if amount > 0 else 0
        return {
            'tax': tax,
            'effective_rate': eff_rate,
            'taxable_amount': taxable_gain,
            'details': f'50% inclusion, taxed at {mr*100:.1f}% MTR',
        }

    elif income_type == InvestmentIncomeType.INTEREST:
        tax = amount * mr
        return {
            'tax': tax,
            'effective_rate': mr,
            'taxable_amount': amount,
            'details': f'Fully taxable at {mr*100:.1f}% MTR',
        }

    elif income_type == InvestmentIncomeType.FOREIGN_DIVIDEND_US:
        from countries.canada.tax_calc import withholding_tax_drag
        WHT_RATE = 0.15
        wht_amount = amount * WHT_RATE
        net_received = amount - wht_amount
        canadian_tax = amount * mr
        ftc = wht_amount
        tax_in_nonreg = max(0, canadian_tax - ftc)
        tax_in_rrsp = 0
        tax_in_tfsa = wht_amount
        eff_rate_nonreg = tax_in_nonreg / amount if amount > 0 else 0

        return {
            'tax': tax_in_nonreg,
            'effective_rate': eff_rate_nonreg,
            'taxable_amount': amount,
            'wht': wht_amount,
            'foreign_tax_credit': ftc,
            'tax_in_rrsp': tax_in_rrsp,
            'tax_in_tfsa': tax_in_tfsa,
            'details': (
                f'15% WHT (${wht_amount:.0f}), FTC covers WHT in non-reg. '
                f'RRSP: 0% WHT (treaty). TFSA: 15% unrecoverable drag.'
            ),
        }

    elif income_type == InvestmentIncomeType.FOREIGN_DIVIDEND_NON_US:
        WHT_RATE = 0.25
        wht_amount = amount * WHT_RATE
        canadian_tax = amount * mr
        ftc = min(wht_amount, canadian_tax)
        net_tax = max(0, canadian_tax - ftc)
        total_tax = net_tax + wht_amount
        eff_rate = total_tax / amount if amount > 0 else 0

        return {
            'tax': total_tax,
            'effective_rate': eff_rate,
            'taxable_amount': amount,
            'wht': wht_amount,
            'foreign_tax_credit': ftc,
            'details': (
                f'{WHT_RATE*100:.0f}% WHT (${wht_amount:.0f}), FTC limited. '
                f'Total rate: {eff_rate*100:.1f}%'
            ),
        }

    elif income_type == InvestmentIncomeType.RETURN_OF_CAPITAL:
        new_acb = acb - amount
        if new_acb < 0:
            capital_gain = -new_acb
            # DP#27, DP#20: Use year-versioned inclusion rate
            from countries.canada.income_type import capital_gains_inclusion_rate
            cg_inclusion = capital_gains_inclusion_rate(capital_gain, year)
            cg_tax = capital_gain * cg_inclusion * mr
            new_acb = 0
            return {
                'tax': cg_tax,
                'effective_rate': cg_tax / amount if amount > 0 else 0,
                'taxable_amount': capital_gain,
                'new_acb': new_acb,
                'details': f'ACB hit $0, ${capital_gain:.0f} treated as capital gain',
            }
        else:
            return {
                'tax': 0,
                'effective_rate': 0,
                'taxable_amount': 0,
                'new_acb': new_acb,
                'details': f'ACB reduced from ${acb:.0f} to ${new_acb:.0f}, tax deferred',
            }

    else:
        return {'tax': 0, 'effective_rate': 0, 'taxable_amount': 0,
                'details': f'Unknown income type: {income_type}'}


