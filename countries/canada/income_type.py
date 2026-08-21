#!/usr/bin/env python3
"""
Income Type Tax Treatment — Per-type effective tax rates (DP#27).

Tax law treats five income types differently:
1. Interest — fully taxable at marginal rate
2. Canadian eligible dividends — 38% gross-up, federal + provincial DTC
3. Canadian non-eligible dividends — 15% gross-up, lower credits
4. Capital gains — tiered inclusion (50% below $250K, 66.67% above for 2024+)
5. Foreign income — fully taxable + withholding tax varies by account type
6. Return of capital — not taxed, reduces ACB

DP#30: This models tax treatment, not investment selection.
The user provides portfolio composition; the simulator applies taxes.

DP#20: DTC rates and capital gains inclusion are year-versioned data,
loaded from tax_data via the TaxDataProvider. No hardcoded constants.

DP#12: DTC rates come from tax_data.py, not from constants in this module.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Dividend Tax Credits, Capital Gains, Foreign Withholding entries
    ITA s.82 (dividend gross-up), s.121 (DTC), s.38-40 (capital gains)
    ITA s.7, s.110(1)(d)/(d.1) (employee stock option benefit and 50% deduction):
        https://laws-lois.justice.gc.ca/eng/acts/I-3.3/section-110.html
    ITA s.126 (foreign tax credit, line 40500):
        https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/about-your-tax-return/tax-return/completing-a-tax-return/deductions-credits-expenses/line-40500-federal-foreign-tax-credit.html
    Return of capital / ACB (ITA s.53(2)): reduces adjusted cost base, not taxed.
    CRA eligible dividends: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/eligible-dividends.html
    CRA capital gains: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/capital-gains.html
    Capital gains inclusion rate change: https://www.canada.ca/en/department-finance/news/2024/01/capital-gains-inclusion-rate.html

Usage:
    from countries.canada.income_type import IncomeType, effective_tax_rate
    
    rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec', year=2026)
    print(f"Effective rate on dividends at 45.7% MTR: {rate:.1%}")
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


class IncomeType(Enum):
    """Investment income types with distinct tax treatments.
    
    DP#27: A $10,000 eligible dividend is not the same as a $10,000
    capital gain. Each has its own gross-up, credit, inclusion rate.
    """
    INTEREST = "interest"
    ELIGIBLE_DIVIDEND = "eligible_dividend"
    NON_ELIGIBLE_DIVIDEND = "non_eligible_dividend"
    CAPITAL_GAIN = "capital_gain"
    FOREIGN_INCOME = "foreign_income"
    RETURN_OF_CAPITAL = "return_of_capital"
    STOCK_OPTION = "stock_option"  # Employee stock option benefit (ITA s.7)


# ── Backward-compatible constants (DP#20: use year-versioned lookup instead) ──
# These remain for backward compatibility but should not be used in new code.
# Use _get_federal_dtc_rates(year) and _get_provincial_dtc_rates(province, year) instead.
CAPITAL_GAINS_INCLUSION = 0.50  # DEPRECATED: use capital_gains_inclusion_rate(year, gain_amount)
FEDERAL_ELIGIBLE_GROSS_UP = 0.38      # DEPRECATED: use _get_federal_dtc_rates(year)
FEDERAL_ELIGIBLE_DTC_RATE = 0.150198  # DEPRECATED: use _get_federal_dtc_rates(year)
FEDERAL_NON_ELIGIBLE_GROSS_UP = 0.15  # DEPRECATED: use _get_federal_dtc_rates(year)
FEDERAL_NON_ELIGIBLE_DTC_RATE = 0.090301  # DEPRECATED: use _get_federal_dtc_rates(year)
QC_ELIGIBLE_DTC_RATE = 0.11510   # DEPRECATED: use _get_provincial_dtc_rates(province, year)
QC_NON_ELIGIBLE_DTC_RATE = 0.05575  # DEPRECATED: use _get_provincial_dtc_rates(province, year)

# Standard employee-stock-option deduction (ITA s.110(1)(d)/(d.1)): 50% of the
# benefit is deductible, so half is taxed. DP#13 round constant; non-standard
# cases (post-2021 $200k vesting cap denying the deduction) pass a rate of 0.
STOCK_OPTION_DEDUCTION_RATE = 0.50


# ── Foreign withholding tax by account type ────────────────────────────────

WHT_BY_ACCOUNT = {
    # (country, account_type) -> withholding rate
    ('us', 'rrsp'): 0.00,    # Treaty exemption for US equities in RRSP
    ('us', 'tfsa'): 0.15,   # No treaty exemption in TFSA
    ('us', 'non_reg'): 0.15, # Recoverable via foreign tax credit
    ('intl', 'rrsp'): 0.15, # One-level WHT (not treaty-exempt for non-US)
    ('intl', 'tfsa'): 0.15,  # Not recoverable
    ('intl', 'non_reg'): 0.15,  # Partially recoverable
}


# ── Year-versioned data lookup (DP#20, DP#12) ─────────────────────────────

def _get_tax_provider():
    """Return the cached default TaxDataProvider singleton.

    Issue #1057: this was the 4th read-only TaxDataProvider() construction
    site #837/#839 missed — it built a fresh provider per call (~409/run),
    each re-running register_all. #840 blocked reuse because the cache could
    be poisoned across tests; #1056 made default_tax_provider() poison-proof
    (a registry-generation counter rebuilds it when countries.default_registry
    changes), so read-only callers — including those querying the 'qc' postal-
    code alias — can now safely share the cached instance.
    """
    from tax_data import default_tax_provider
    return default_tax_provider()


def _get_federal_dtc_rates(year: int) -> Dict[str, float]:
    """Get federal DTC rates and gross-up rates for a given year.
    
    DP#20: DTC rates are year-versioned data from tax_data.
    DP#12: Data comes from tax_data.py, not hardcoded constants.
    
    Returns:
        Dict with keys: eligible_dtc_rate, non_eligible_dtc_rate,
        eligible_gross_up, non_eligible_gross_up
    """
    provider = _get_tax_provider()
    try:
        fed_data = provider._load_year(year, 'canada', 'federal')
        return {
            'eligible_dtc_rate': fed_data.federal_eligible_dtc_rate,
            'non_eligible_dtc_rate': fed_data.federal_non_eligible_dtc_rate,
            'eligible_gross_up': fed_data.federal_eligible_gross_up,
            'non_eligible_gross_up': fed_data.federal_non_eligible_gross_up,
        }
    except (ValueError, AttributeError):
        # Fallback to known defaults if data not found
        return {
            'eligible_dtc_rate': 0.150198,
            'non_eligible_dtc_rate': 0.090301,
            'eligible_gross_up': 0.38,
            'non_eligible_gross_up': 0.15,
        }


def _get_provincial_dtc_rates(province: str, year: int) -> Dict[str, float]:
    """Get provincial DTC rates for a given province and year.
    
    DP#20: Provincial DTC rates are year-versioned data from tax_data.
    DP#12: Data comes from tax_data.py, not hardcoded constants.
    
    Returns:
        Dict with keys: eligible_dtc_rate, non_eligible_dtc_rate
    """
    provider = _get_tax_provider()
    try:
        prov_data = provider._load_year(year, 'canada', province)
        return {
            'eligible_dtc_rate': prov_data.provincial_eligible_dtc_rate,
            'non_eligible_dtc_rate': prov_data.provincial_non_eligible_dtc_rate,
        }
    except (ValueError, AttributeError):
        # Fallback to known provincial defaults
        return _provincial_dtc_fallback(province)


def _provincial_dtc_fallback(province: str) -> Dict[str, float]:
    """Fallback provincial DTC rates when year-versioned data is unavailable."""
    rates = {
        'quebec': {'eligible_dtc_rate': 0.11510, 'non_eligible_dtc_rate': 0.05575},
        'ontario': {'eligible_dtc_rate': 0.1008, 'non_eligible_dtc_rate': 0.0455},
        'alberta': {'eligible_dtc_rate': 0.10, 'non_eligible_dtc_rate': 0.0367},
        'bc': {'eligible_dtc_rate': 0.12, 'non_eligible_dtc_rate': 0.0606},
    }
    return rates.get(province.lower(), {'eligible_dtc_rate': 0.10, 'non_eligible_dtc_rate': 0.05})


def capital_gains_inclusion_rate(gain_amount: float = 0, year: int = 2026) -> float:
    """Compute the effective capital gains inclusion rate.
    
    DP#27: For 2024+, the inclusion rate is tiered:
    - 50% for the first $250,000 of capital gains
    - 66.67% (2/3) for capital gains above $250,000
    
    For years before 2024: flat 50% inclusion rate.
    
    DP#20: The threshold and rates come from year-versioned tax_data.
    
    Args:
        gain_amount: Total capital gain amount in dollars
        year: Tax year
    
    Returns:
        Effective inclusion rate as decimal (0.0 to 1.0)
    """
    provider = _get_tax_provider()
    try:
        fed_data = provider._load_year(year, 'canada', 'federal')
        base_rate = fed_data.capital_gains_inclusion_rate
        upper_rate = fed_data.capital_gains_upper_inclusion_rate
        threshold = fed_data.capital_gains_threshold
    except (ValueError, AttributeError):
        # Fallback: flat 50% before 2024, tiered for 2024+
        if year >= 2024:
            base_rate = 0.50
            upper_rate = 2 / 3
            threshold = 250000
        else:
            return 0.50
    
    # No tier → flat rate
    if upper_rate <= 0 or threshold <= 0:
        return base_rate
    
    # Tiered: blended rate depends on gain amount
    if gain_amount <= 0:
        return base_rate  # No gain → base rate (no taxable amount anyway)
    if gain_amount <= threshold:
        return base_rate
    
    # Blend: first threshold at base_rate, remainder at upper_rate
    taxable = threshold * base_rate + (gain_amount - threshold) * upper_rate
    return taxable / gain_amount


def effective_tax_rate(
    income_type: IncomeType,
    marginal_rate: float,
    province: str = 'quebec',
    account_type: str = 'non_reg',
    foreign_country: str = 'us',
    wht_recoverable: bool = True,
    year: int = 2026,
    gain_amount: float = 0,
) -> float:
    """Compute the effective tax rate for an income type.
    
    Args:
        income_type: Type of investment income
        marginal_rate: Marginal tax rate (compute via tax_calculator.marginal_rate)
        province: Province for provincial DTC
        account_type: 'rrsp', 'tfsa', or 'non_reg'
        foreign_country: 'us' or 'intl' for foreign income
        wht_recoverable: Whether WHT is recoverable via foreign tax credit
        year: Tax year (for year-versioned DTC rates and CG inclusion)
        gain_amount: Capital gain amount (for tiered inclusion calculation)
    
    Returns:
        Effective tax rate (0.0 to 1.0)
    """
    if income_type == IncomeType.INTEREST:
        # Fully taxable at marginal rate
        return marginal_rate
    
    elif income_type == IncomeType.ELIGIBLE_DIVIDEND:
        # Correct formula (DP#27 fix):
        # Tax = grossed_up_amount × MTR - grossed_up_amount × fed_dtc - grossed_up_amount × prov_dtc
        # Effective = Tax / actual_dividend = gross_up_factor × (MTR - fed_dtc_rate - prov_dtc_rate)
        fed = _get_federal_dtc_rates(year)
        prov = _get_provincial_dtc_rates(province, year)
        gross_up_factor = 1 + fed['eligible_gross_up']
        fed_dtc_rate = fed['eligible_dtc_rate']
        prov_dtc_rate = prov['eligible_dtc_rate']
        effective = gross_up_factor * (marginal_rate - fed_dtc_rate - prov_dtc_rate)
        return max(0, effective)
    
    elif income_type == IncomeType.NON_ELIGIBLE_DIVIDEND:
        # Same corrected formula for non-eligible dividends
        fed = _get_federal_dtc_rates(year)
        prov = _get_provincial_dtc_rates(province, year)
        gross_up_factor = 1 + fed['non_eligible_gross_up']
        fed_dtc_rate = fed['non_eligible_dtc_rate']
        prov_dtc_rate = prov['non_eligible_dtc_rate']
        effective = gross_up_factor * (marginal_rate - fed_dtc_rate - prov_dtc_rate)
        return max(0, effective)
    
    elif income_type == IncomeType.CAPITAL_GAIN:
        # Tiered inclusion rate (DP#27, DP#20)
        inclusion = capital_gains_inclusion_rate(gain_amount, year)
        return marginal_rate * inclusion
    
    elif income_type == IncomeType.FOREIGN_INCOME:
        # Fully taxable at MTR + WHT drag
        base_rate = marginal_rate
        wht_key = (foreign_country, account_type)
        wht_rate = WHT_BY_ACCOUNT.get(wht_key, 0.15)
        net_wht = wht_rate if not wht_recoverable else 0.0
        return base_rate + net_wht  # Unrecoverable WHT adds to effective rate
    
    elif income_type == IncomeType.RETURN_OF_CAPITAL:
        # Not taxed when received (reduces ACB instead)
        return 0.0

    elif income_type == IncomeType.STOCK_OPTION:
        # Qualifying employee stock option benefit (ITA s.7) gets a 50%
        # deduction under s.110(1)(d)/(d.1), so only half the benefit is taxed
        # — the same effective rate as a capital gain at 50% inclusion.
        return marginal_rate * STOCK_OPTION_DEDUCTION_RATE

    return marginal_rate  # Default fallback


def _provincial_eligible_dtc(province: str, year: int = 2026) -> float:
    """Provincial DTC rate for eligible dividends (year-versioned, DP#20)."""
    rates = _get_provincial_dtc_rates(province, year)
    return rates['eligible_dtc_rate']


def _provincial_non_eligible_dtc(province: str, year: int = 2026) -> float:
    """Provincial DTC rate for non-eligible dividends (year-versioned, DP#20)."""
    rates = _get_provincial_dtc_rates(province, year)
    return rates['non_eligible_dtc_rate']


def wht_drag(account_type: str, foreign_country: str = 'us',
             wht_recoverable: bool = True, dividend_yield: float = 0.02) -> float:
    """Compute the annual WHT drag in basis points.
    
    DP#30: Models tax impact, not investment selection.
    User provides dividend yield; function computes the tax drag.
    
    Args:
        account_type: 'rrsp', 'tfsa', or 'non_reg'
        foreign_country: 'us' or 'intl'
        wht_recoverable: Whether WHT is recoverable via FTC
        dividend_yield: Expected dividend yield (e.g., 0.02 = 2%).
            DP#2/DP#13: This is a configurable parameter, not a hardcoded opinion.
    
    Returns:
        Annual drag in basis points (e.g., 22 = 22 bps)
    """
    wht_key = (foreign_country, account_type)
    wht_rate = WHT_BY_ACCOUNT.get(wht_key, 0.15)
    if wht_recoverable:
        return 0.0  # Recoverable WHT has no drag
    return wht_rate * dividend_yield * 10000  # Convert to bps


def after_tax_return(gross_return: float, income_type: IncomeType,
                     marginal_rate: float, province: str = 'quebec',
                     account_type: str = 'non_reg',
                     year: int = 2026,
                     gain_amount: float = 0) -> float:
    """Compute after-tax return for a given income type.
    
    Args:
        gross_return: Gross (pre-tax) return rate
        income_type: Type of income generated
        marginal_rate: Marginal tax rate
        province: Province for DTC calculation
        account_type: Account type for WHT
        year: Tax year for year-versioned rates
        gain_amount: Capital gain amount for tiered inclusion
    
    Returns:
        After-tax return rate
    """
    eff_rate = effective_tax_rate(income_type, marginal_rate, province,
                                   account_type, year=year, gain_amount=gain_amount)
    return gross_return * (1 - eff_rate)


# ── Return of capital — explicit ACB tracking (DP#19, issue #316) ───────────

def return_of_capital_treatment(distribution: float,
                                adjusted_cost_base: float) -> Dict[str, float]:
    """Model a return-of-capital (ROC) distribution (ITA s.53(2)).

    ROC is not income when received: it reduces the adjusted cost base (ACB) of
    the holding. Once ACB reaches zero, any further ROC is a capital gain
    realized immediately (DP#27). This makes the deferred-tax mechanics explicit
    rather than implicit in account_models.py.

    DP#3: pure function. DP#19: track cost basis from day one.

    Args:
        distribution: ROC distribution received.
        adjusted_cost_base: ACB before the distribution.

    Returns:
        Dict with new_acb, taxable_now (immediate capital gain on negative ACB),
        acb_reduction, and taxable_income (0 — ROC itself is never income).
    """
    if distribution <= 0:
        return {
            'new_acb': adjusted_cost_base,
            'acb_reduction': 0.0,
            'taxable_now': 0.0,
            'taxable_income': 0.0,
        }
    acb_reduction = min(distribution, max(0.0, adjusted_cost_base))
    new_acb = max(0.0, adjusted_cost_base - distribution)
    # ROC in excess of remaining ACB is an immediate capital gain (s.40(3)).
    taxable_now = max(0.0, distribution - acb_reduction)
    return {
        'new_acb': new_acb,
        'acb_reduction': acb_reduction,
        'taxable_now': taxable_now,
        'taxable_income': 0.0,  # ROC is never ordinary income
    }


# ── Employee stock option income (ITA s.7, s.110(1)(d)) ─────────────────────

def stock_option_benefit(fmv_at_exercise: float,
                         exercise_price: float,
                         shares: int = 1,
                         qualifies_for_deduction: bool = True,
                         deduction_rate: float = STOCK_OPTION_DEDUCTION_RATE) -> Dict[str, float]:
    """Compute the taxable employment benefit from exercising stock options.

    Benefit (ITA s.7) = (FMV at exercise - exercise price) × shares. For
    qualifying options the s.110(1)(d)/(d.1) deduction shields ``deduction_rate``
    of the benefit, so only the remainder is included in taxable income.

    Standard case (DP#10): qualifying public-company options with the 50%
    deduction. Set ``qualifies_for_deduction=False`` (or deduction_rate=0) for
    options denied the deduction (e.g. above the post-2021 $200k annual vesting
    cap), in which case the full benefit is taxed.

    DP#3: pure function.

    Args:
        fmv_at_exercise: Fair market value per share at exercise.
        exercise_price: Exercise (strike) price per share.
        shares: Number of shares exercised.
        qualifies_for_deduction: Whether the 50% deduction applies.
        deduction_rate: Deductible fraction when qualifying (default 50%).

    Returns:
        Dict with gross_benefit, deduction, taxable_benefit.
    """
    gross = max(0.0, fmv_at_exercise - exercise_price) * max(0, shares)
    rate = deduction_rate if qualifies_for_deduction else 0.0
    deduction = gross * rate
    return {
        'gross_benefit': gross,
        'deduction': deduction,
        'taxable_benefit': gross - deduction,
    }


# ── Foreign tax credit (ITA s.126, line 40500) ──────────────────────────────

def foreign_tax_credit(foreign_income: float,
                       foreign_tax_paid: float,
                       marginal_rate: float) -> Dict[str, float]:
    """Compute the non-business foreign tax credit (ITA s.126, line 40500).

    The FTC relieves double taxation on foreign income. It is the LESSER of:
    - foreign tax actually paid (e.g. withholding tax), and
    - the Canadian tax otherwise payable on that foreign income
      (approximated as foreign_income × marginal_rate).

    Foreign tax paid above the Canadian tax on the income is not creditable
    (the excess is the unrecoverable WHT drag modelled in ``wht_drag``); for
    non-business income that excess is generally lost (no carryforward).

    DP#3: pure function. This is the actual credit the prior code lacked —
    previously only the unrecoverable WHT drag was modelled.

    Args:
        foreign_income: Gross foreign income (before foreign tax).
        foreign_tax_paid: Foreign tax / withholding paid on that income.
        marginal_rate: Canadian combined marginal tax rate.

    Returns:
        Dict with credit (claimable FTC), canadian_tax_on_foreign_income,
        and unrecoverable (foreign tax that exceeds the Canadian tax).
    """
    foreign_income = max(0.0, foreign_income)
    foreign_tax_paid = max(0.0, foreign_tax_paid)
    canadian_tax = foreign_income * marginal_rate
    credit = min(foreign_tax_paid, canadian_tax)
    return {
        'credit': credit,
        'canadian_tax_on_foreign_income': canadian_tax,
        'unrecoverable': foreign_tax_paid - credit,
    }
