#!/usr/bin/env python3
"""
Retirement Phase — OAS/CPP/RRIF/Pension Splitting Rules

This module models the retirement drawdown phase, complementing
the accumulation phase in simulation.py.

Per DP#10: this module owns OAS (Old Age Security), CPP (Canada Pension Plan),
RRIF (Registered Rettlement Income Fund), and pension splitting rules.

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — OAS, CPP/QPP, RRIF, Pension Splitting entries
    OAS clawback: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/repayment.html
    OAS amounts: https://www.canada.ca/en/services/benefits/publicpensions/old-age-security/oas-amounts.html
    CPP sharing: https://www.canada.ca/en/services/benefits/publicpensions/cpp/share-cpp.html
    RRIF minimum withdrawal factors: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/registering-your-plan/minimum-withdrawal-factors-rrifs.html
    RRIF: https://www.canada.ca/en/revenue-agency/services/forms-publications/publications/t4040/rrsps-other-registered-plans-retirement.html
    CPP/QPP contribution rates: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/payroll-deductions-contributions/cpp-contributions.html
    CPP2 (second additional): https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
    Pension splitting (T1032): https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/pension-income-splitting.html

Usage:
    from countries.canada.retirement import RetirementState, oas_clawback, rrif_minimum_withdrawal
    from countries.canada.retirement import DrawdownOptimizer

    state = RetirementState(age=65, rrif_balance=500000, tfsa_balance=100000)
    clawback = oas_clawback(net_income=90000, threshold=86000)
    min_withdrawal = rrif_minimum_withdrawal(state.rrif_balance, state.age)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from enum import Enum

from tax_calculator import (
    marginal_rate, tax_on_income,
    effective_tax_rate,
)
from tax_data import default_tax_provider


# =============================================================================
# Constants — CRA / Service Canada rules (2026 defaults, DP#13)
# =============================================================================

OAS_ANNUAL_MAX = 8908          # 2026: $742.31/month × 12 for age 65-74 (source: Service Canada, 2026-03-31)
OAS_ANNUAL_MAX_75PLUS = 9800   # 2026: ~$816.67/month × 12 for age 75+ (10% enhancement since July 2022)
OAS_CLAWBACK_THRESHOLD = 95323  # 2026: indexed threshold for 2026 income year (source: CRA, 2026-03-31)

# DP#20 deprecation: Module-level constants are 2026-only values.
# Use get_oas_annual_max(year), get_oas_annual_max_75plus(year),
# and get_oas_clawback_threshold(year) for year-versioned data.
# These constants remain as fallbacks (DP#13) but should not be used in new code.
import warnings as _warnings


def _year_versioned_lookup(year: int, fallback_key: str,
                           provider_method: str, label: str) -> float:
    """Look up a year-versioned CRA/Service Canada value (DP#9, DP#20).

    Single parameterized implementation shared by every year-versioned
    retirement getter (issue #634). Resolution order:

    1. The CPP_OAS_BY_YEAR fallback table (DP#13), keyed by ``fallback_key``.
    2. The TaxDataProvider method named ``provider_method`` (returns > 0).
    3. Otherwise raise ValueError — year is required, no silent coercion.

    Args:
        year: Tax year.
        fallback_key: Key into a CPP_OAS_BY_YEAR year record.
        provider_method: Name of the TaxDataProvider getter for this value.
        label: Human-readable name used in the ValueError message.

    Returns:
        The year-versioned value.
    """
    if year in CPP_OAS_BY_YEAR:
        return CPP_OAS_BY_YEAR[year][fallback_key]
    try:
        from tax_data import default_tax_provider
        provider = default_tax_provider()
        val = getattr(provider, provider_method)(year)
        if val > 0:
            return val
    except Exception:
        pass
    raise ValueError(f"Unknown year {year} for {label}. Update CPP_OAS_BY_YEAR or TaxDataProvider.")


def get_oas_annual_max(year: int) -> float:
    """Get the OAS maximum annual amount for ages 65-74, year-versioned (DP#20).

    Uses TaxDataProvider for year-specific data with CPP_OAS_BY_YEAR
    dict as fallback. Raises ValueError if year is unknown.

    Per DP#9 and DP#20: year is required — no silent fallback to constants.
    Multi-year simulations must pass the correct year.

    Args:
        year: Tax year

    Returns:
        OAS annual maximum amount for ages 65-74
    """
    return _year_versioned_lookup(
        year, "oas_annual_max", "get_oas_annual_max", "OAS annual max (65-74)")


def get_oas_annual_max_75plus(year: int) -> float:
    """Get the OAS maximum annual amount for age 75+, year-versioned (DP#20).

    Since July 2022, OAS recipients aged 75+ receive a 10% enhancement.
    Uses TaxDataProvider for year-specific data with CPP_OAS_BY_YEAR
    dict as fallback. Raises ValueError if year is unknown.

    Per DP#9 and DP#20: year is required — no silent fallback to constants.

    Args:
        year: Tax year

    Returns:
        OAS annual maximum amount for ages 75+
    """
    return _year_versioned_lookup(
        year, "oas_annual_max_75plus", "get_oas_annual_max_75plus",
        "OAS annual max (75+)")


def get_oas_clawback_threshold(year: int) -> float:
    """Get the OAS clawback (recovery tax) threshold, year-versioned (DP#20).

    Uses TaxDataProvider for year-specific data with CPP_OAS_BY_YEAR
    dict as fallback. Raises ValueError if year is unknown.

    Per DP#9 and DP#20: year is required — no silent fallback to constants.

    Args:
        year: Tax year

    Returns:
        OAS clawback threshold for the given year
    """
    return _year_versioned_lookup(
        year, "oas_clawback_threshold", "get_oas_clawback_threshold",
        "OAS clawback threshold")
GIS_ANNUAL_MAX_SINGLE = 1726  # 2026: GIS maximum for single pensioner (source: Service Canada)
GIS_ANNUAL_MAX_COUPLED = 10384 # 2026: GIS maximum for coupled pensioner (source: Service Canada)
GIS_INCOME_EXEMPTION = 4000    # 2026: GIS income exemption (first $4K of other income ignored)

# DP#20 deprecation: Module-level GIS constants are 2026-only values.
# Use get_gis_max_single(year), get_gis_max_coupled(year),
# and get_gis_income_exemption(year) for year-versioned data.
# These constants remain as fallbacks (DP#13) but should not be used in new code.
import warnings as _gis_warnings


def get_gis_max_single(year: int) -> float:
    """Get the GIS annual maximum for a single pensioner, year-versioned (DP#20, issue #330).

    Uses TaxDataProvider for year-specific data with CPP_OAS_BY_YEAR
    dict as fallback. Raises ValueError if year is unknown.

    Per DP#9 and DP#20: year is required — no silent fallback to constants.

    Args:
        year: Tax year

    Returns:
        GIS annual maximum for a single pensioner.
    """
    return _year_versioned_lookup(
        year, "gis_max_single", "get_gis_max_single", "GIS max single")


def get_gis_max_coupled(year: int) -> float:
    """Get the GIS annual maximum for a coupled pensioner, year-versioned (DP#20, issue #330).

    Uses TaxDataProvider for year-specific data with CPP_OAS_BY_YEAR
    dict as fallback. Raises ValueError if year is unknown.

    Per DP#9 and DP#20: year is required — no silent fallback to constants.

    Args:
        year: Tax year

    Returns:
        GIS annual maximum for a coupled pensioner.
    """
    return _year_versioned_lookup(
        year, "gis_max_coupled", "get_gis_max_coupled", "GIS max coupled")


def get_cpp_max_pensionable(year: int) -> float:
    """Get the CPP YMPE (Year's Maximum Pensionable Earnings), year-versioned (DP#20, issue #330).

    Uses TaxDataProvider for year-specific data with CPP_OAS_BY_YEAR
    dict as fallback. Raises ValueError if year is unknown.

    Per DP#9 and DP#20: year is required — no silent fallback to constants.

    Args:
        year: Tax year

    Returns:
        CPP YMPE for the given year.
    """
    return _year_versioned_lookup(
        year, "cpp_max_pensionable", "get_cpp_max_pensionable",
        "CPP max pensionable")


def get_cpp_max_benefit_65(year: int) -> float:
    """Get the maximum CPP retirement benefit at age 65, year-versioned (DP#20, issue #330).

    Uses TaxDataProvider for year-specific data with CPP_OAS_BY_YEAR
    dict as fallback. Raises ValueError if year is unknown.

    Per DP#9 and DP#20: year is required — no silent fallback to constants.

    Args:
        year: Tax year

    Returns:
        Maximum CPP retirement benefit at age 65 for the given year.
    """
    return _year_versioned_lookup(
        year, "cpp_max_benefit_65", "get_cpp_max_benefit_65",
        "CPP max benefit at 65")


# DP#20: Year-versioned CPP/OAS defaults (fallback per DP#13)
CPP_OAS_BY_YEAR = {
    2023: {"cpp_max_pensionable": 66600, "cpp2_max_pensionable": 68500, "cpp_max_benefit_65": 14010, "cpp2_max_benefit": 188, "oas_annual_max": 8083, "oas_annual_max_75plus": 8888, "oas_clawback_threshold": 83917, "gis_max_single": 1572, "gis_max_coupled": 9476},
    2024: {"cpp_max_pensionable": 68500, "cpp2_max_pensionable": 73300, "cpp_max_benefit_65": 14448, "cpp2_max_benefit": 188, "oas_annual_max": 8291, "oas_annual_max_75plus": 9118, "oas_clawback_threshold": 87068, "gis_max_single": 1616, "gis_max_coupled": 9739},
    2025: {"cpp_max_pensionable": 71300, "cpp2_max_pensionable": 81900, "cpp_max_benefit_65": 14448, "cpp2_max_benefit": 188, "oas_annual_max": 8381, "oas_annual_max_75plus": 9218, "oas_clawback_threshold": 90997, "gis_max_single": 1657, "gis_max_coupled": 9987},
    2026: {"cpp_max_pensionable": 74600, "cpp2_max_pensionable": 81900, "cpp_max_benefit_65": 18092, "cpp2_max_benefit": 800, "oas_annual_max": 8908, "oas_annual_max_75plus": 9800, "oas_clawback_threshold": 95323, "gis_max_single": 1726, "gis_max_coupled": 10384},
}
OAS_CLAWBACK_RATE = 0.15        # 15% recovery tax on income above threshold

# DP#2/DP#12/DP#20: RRIF rates moved to TaxDataProvider. This module-level
# variable provides backward compatibility. Initialized at module load below.
# New code should use _get_rrif_rates() or rrif_minimum_withdrawal(year=...).
RRIF_MIN_WITHDRAWAL_RATES: Dict[int, float] = {}  # Initialized below after _get_rrif_rates is defined


def _get_rrif_rates(rates: Dict = None, year: int = 2026, provider=None) -> Dict[int, float]:
    """Get RRIF minimum withdrawal rates (DP#2/DP#12/DP#20, issue #86).

    Per DP#2, configuration belongs in input, not code.
    Per DP#12, real data is fetched from a data provider.
    Per DP#20, rates may change across tax years.

    Priority:
    1. Explicitly provided rates parameter (backward compat)
    2. Year-specific rates from TaxDataProvider
    3. Built-in CRA fallback rates
    """
    if rates is not None:
        return rates
    if provider is not None:
        return provider.get_rrif_min_withdrawal_rates(year)
    # Import lazily to avoid circular imports
    from tax_data import default_tax_provider
    return default_tax_provider().get_rrif_min_withdrawal_rates(year)


# CPP 2026 parameters
CPP_MAX_PENSIONABLE = 74600       # YMPE (Year's Maximum Pensionable Earnings)
CPP2_MAX_PENSIONABLE = 81900      # YMPE2 (second additional CPP, above YMPE)
CPP2_BENEFIT_RATE = 0.0025        # CPP2 accrual rate per year of contribution
CPP_MAX_BENEFIT_65 = 18092        # Maximum CPP retirement at 65: $1,507.65×12 (Service Canada, 2026-01)
CPP2_MAX_BENEFIT = 800            # Maximum CPP2 retirement benefit (2026)
CPP_EARLY_START_PENALTY = 0.006   # 0.6% per month before 65 (max 36% at 60)
CPP_LATE_START_BONUS = 0.007      # 0.7% per month after 65 (max 42% at 70)


# =============================================================================
# Drawdown Order
# =============================================================================

class DrawdownOrder(Enum):
    """Order of account drawdown in retirement.

    Different orders affect OAS clawback, tax efficiency, and estate value.
    """
    TAX_FIRST = "tax_first"          # Non-reg → TFSA → RRSP (minimize OAS clawback)
    DEFERRED_FIRST = "deferred_first"  # RRSP → non-reg → TFSA (preserve TFSA room)
    BALANCED = "balanced"              # Mix based on tax optimization


# =============================================================================
# Pure Functions — OAS, CPP, RRIF
# =============================================================================

def oas_clawback(net_income: float, threshold: float = None,
                 oas_amount: float = None,
                 recovery_rate: float = OAS_CLAWBACK_RATE,
                 year: int = 2026) -> Dict:
    """Calculate OAS clawback (recovery tax).

    Pure function (DP#3): same inputs → same output.

    DP#20: OAS amounts and thresholds are year-versioned.
    Callers should pass the correct year for multi-year simulations.

    If net income exceeds the threshold, OAS is reduced by 15%
    of the excess. OAS is fully clawed back when income reaches
    threshold + OAS / 0.15.

    Args:
        net_income: Net income before adjustments
        threshold: OAS clawback threshold (None → use year-versioned lookup)
        oas_amount: Annual OAS entitlement (None → use year-versioned lookup)
        recovery_rate: Recovery tax rate (15%)
        year: Tax year for year-versioned data (DP#20, default 2026)
        oas_amount: Annual OAS entitlement (None → use year or default)
        recovery_rate: Recovery tax rate (15%)
        year: Tax year for year-versioned data (DP#20)

    Returns:
        Dict with clawback_amount, net_oas, effective_oas
    """
    # DP#20: Look up year-specific values via year-versioned functions
    if oas_amount is None:
        oas_amount = get_oas_annual_max(year)
    if threshold is None:
        threshold = get_oas_clawback_threshold(year)

    if net_income <= threshold:
        return {
            'net_income': net_income,
            'threshold': threshold,
            'clawback_amount': 0,
            'net_oas': oas_amount,
            'clawback_rate_pct': 0,
            'full_clawback_threshold': threshold + oas_amount / recovery_rate,
        }

    excess = net_income - threshold
    clawback = min(oas_amount, excess * recovery_rate)
    net_oas = oas_amount - clawback
    clawback_pct = clawback / oas_amount * 100 if oas_amount > 0 else 0

    return {
        'net_income': net_income,
        'threshold': threshold,
        'clawback_amount': clawback,
        'net_oas': net_oas,
        'clawback_rate_pct': clawback_pct,
        'full_clawback_threshold': threshold + oas_amount / recovery_rate,
    }


OAS_DEFERRAL_RATE = 0.006  # 0.6% increase per month deferred after age 65


def oas_amount_for_age(age: int, year: int = None, defer_months: int = 0) -> float:
    """OAS annual amount based on recipient age and deferral (DP#28: date-computed).

    Since July 2022, OAS recipients aged 75+ receive a 10% enhancement.
    Age 65-74: standard OAS amount
    Age 75+: OAS amount x 1.10 (10% increase)

    OAS can be deferred up to 60 months (5 years) after age 65 for a
    0.6% increase per month (up to 36% at age 70).
    OAS is NOT available before age 65.

    DP#20: Amounts are year-versioned.
    Per DP#9/DP#20: year is required.

    Args:
        age: Recipient's age
        year: Tax year for year-versioned data (DP#20, required)
        defer_months: Number of months deferred after age 65 (0-60).
            Each month increases OAS by 0.6%. This is the OAS pension
            enhancement for delayed claiming.

    Returns:
        Annual OAS amount for the given age and deferral
    """
    if year is None:
        raise ValueError("year parameter is required for oas_amount_for_age (DP#9, DP#20: year-versioned data)")

    if year in CPP_OAS_BY_YEAR:
        year_data = CPP_OAS_BY_YEAR[year]
        base = year_data["oas_annual_max"]
        enhanced = year_data["oas_annual_max_75plus"]
    else:
        base = get_oas_annual_max(year)
        enhanced = get_oas_annual_max_75plus(year)

    if age >= 75:
        base_amount = enhanced
    else:
        base_amount = base

    # Apply OAS deferral increase (0.6%/month, DP#28)
    if defer_months > 0:
        defer_months = min(defer_months, 60)  # Cap at 60 months
        increase = 1 + (defer_months * OAS_DEFERRAL_RATE)
        return base_amount * increase

    return base_amount


def gis_benefit(net_income: float, is_coupled: bool = False,
                year: int = 2026) -> Dict:
    """Calculate GIS (Guaranteed Income Supplement) benefit.

    Pure function (DP#3): same inputs -> same output.

    GIS provides additional benefits to low-income OAS recipients.
    GIS is reduced by 50% of income above the exemption threshold
    ($4,000 per year, excluding OAS itself).

    DP#20: GIS amounts are year-versioned.
    Per DP#9/DP#20: year is required (default 2026 for backward compat).

    Args:
        net_income: Annual net income (excluding OAS, but including CPP, RRIF, etc.)
        is_coupled: Whether the recipient has a spouse/common-law partner
        year: Tax year for year-versioned data (DP#20, default 2026)

    Returns:
        Dict with gis_amount, eligible, and income_threshold
    """
    if year in CPP_OAS_BY_YEAR:
        year_data = CPP_OAS_BY_YEAR[year]
        max_gis = year_data["gis_max_coupled"] if is_coupled else year_data["gis_max_single"]
    else:
        max_gis = get_gis_max_coupled(year) if is_coupled else get_gis_max_single(year)

    # GIS is reduced by 50% of income above the exemption
    countable_income = max(0, net_income - GIS_INCOME_EXEMPTION)
    gis_reduction = countable_income * 0.50
    gis_amount = max(0, max_gis - gis_reduction)
    eligible = gis_amount > 0

    full_elimination = GIS_INCOME_EXEMPTION + max_gis / 0.50

    return {
        'gis_amount': gis_amount,
        'eligible': eligible,
        'max_gis': max_gis,
        'countable_income': countable_income,
        'gis_reduction': gis_reduction,
        'income_exemption': GIS_INCOME_EXEMPTION,
        'full_elimination_threshold': full_elimination,
        'is_coupled': is_coupled,
    }


def cpp2_benefit(earnings_above_ympe: float, years_contributing: int = 40,
                 start_age: int = 65,
                 max_benefit: float = None,
                 year: int = None) -> float:
    """Calculate CPP2 (second additional CPP) retirement benefit.

    CPP2 covers earnings between YMPE and YMPE2 (also called YAMPE).
    For earners above YMPE (like primary at $250k), CPP2 provides
    additional retirement benefits on top of regular CPP.

    The CPP2 benefit is calculated using the same age adjustment factors
    as CPP1 (0.6% penalty per month before 65, 0.7% bonus per month after).

    Pure function (DP#3): same inputs → same output.

    Args:
        earnings_above_ympe: Average earnings above YMPE (capped at YMPE2-YMPE)
        years_contributing: Number of years of CPP2 contributions
        start_age: Age to start CPP2 (60-70)
        max_benefit: Maximum CPP2 benefit at 65 (None → use year or default)
        year: Tax year for year-versioned data (DP#20, required)

    Returns:
        Estimated annual CPP2 benefit
    """
    if year is None:
        raise ValueError("year parameter is required for cpp2_benefit (DP#9, DP#20: year-versioned data)")

    # DP#20: Look up year-specific values
    if max_benefit is None:
        if year in CPP_OAS_BY_YEAR:
            max_benefit = CPP_OAS_BY_YEAR[year].get("cpp2_max_benefit", CPP2_MAX_BENEFIT)
        else:
            max_benefit = CPP2_MAX_BENEFIT

    # Cap earnings at the CPP2 pensionable range (year-versioned YMPE and YMPE2)
    if year in CPP_OAS_BY_YEAR:
        ympe = CPP_OAS_BY_YEAR[year]["cpp_max_pensionable"]
        ympe2 = CPP_OAS_BY_YEAR[year].get("cpp2_max_pensionable", CPP2_MAX_PENSIONABLE)
    else:
        ympe = get_cpp_max_pensionable(year)
        ympe2 = CPP2_MAX_PENSIONABLE  # Use default for unknown years
    ympe2_range = ympe2 - ympe
    capped_earnings = min(earnings_above_ympe, ympe2_range)

    # Pro-rate based on actual earnings vs max pensionable
    if ympe2_range > 0 and years_contributing > 0:
        benefit_ratio = capped_earnings / ympe2_range
    else:
        benefit_ratio = 0.0

    base_benefit = max_benefit * benefit_ratio

    # Apply age adjustment (same as CPP1)
    start_age = max(60, min(70, start_age))
    if start_age < 65:
        months_early = (65 - start_age) * 12
        penalty = 1 - months_early * CPP_EARLY_START_PENALTY
        return base_benefit * penalty
    elif start_age > 65:
        months_late = (start_age - 65) * 12
        bonus = 1 + months_late * CPP_LATE_START_BONUS
        return base_benefit * bonus
    else:
        return base_benefit


def cpp_benefit(start_age: int, average_contributions: float = None,
                max_benefit_at_65: float = None,
                year: int = None) -> float:
    """Calculate CPP retirement benefit based on start age.

    DP#20: Uses year-specific max benefit.
    Per DP#9/DP#20: year is required.

    Args:
        start_age: Age to start CPP (60-70)
        average_contributions: Average indexed earnings (if None, use max)
        max_benefit_at_65: Maximum CPP benefit at age 65 (None -> use year lookup)
        year: Tax year for year-versioned data (DP#20, required)

    Returns:
        Estimated annual CPP benefit
    """
    if year is None:
        raise ValueError("year parameter is required for cpp_benefit (DP#9, DP#20: year-versioned data)")

    # DP#20: Look up year-specific max benefit
    if max_benefit_at_65 is None:
        if year in CPP_OAS_BY_YEAR:
            max_benefit_at_65 = CPP_OAS_BY_YEAR[year]["cpp_max_benefit_65"]
        else:
            max_benefit_at_65 = get_cpp_max_benefit_65(year)

    start_age = max(60, min(70, start_age))

    if start_age < 65:
        months_early = (65 - start_age) * 12
        penalty = 1 - months_early * CPP_EARLY_START_PENALTY
        return max_benefit_at_65 * penalty
    elif start_age > 65:
        months_late = (start_age - 65) * 12
        bonus = 1 + months_late * CPP_LATE_START_BONUS
        return max_benefit_at_65 * bonus
    else:
        return max_benefit_at_65


def rrif_minimum_withdrawal(balance: float, age: int,
                             rates: Dict = None,
                             year: int = 2026,
                             provider=None) -> float:
    """Calculate RRIF minimum withdrawal for the year.

    Per DP#2/DP#12/DP#20, RRIF rates are data (not hardcoded).
    They come from the TaxDataProvider, indexed by year.

    Args:
        balance: RRIF balance at start of year
        age: Age at start of year
        rates: Custom rate table (overrides provider; backward compat)
        year: Taxation year (for year-versioned rates)
        provider: TaxDataProvider instance (for year-specific rates)

    Returns:
        Minimum withdrawal amount
    """
    actual_rates = _get_rrif_rates(rates, year, provider)
    rate = actual_rates.get(age, 0.20)  # Default 20% for ages above table
    return balance * rate


from enum import Enum
class PensionIncomeType(Enum):
    """Types of pension income for splitting eligibility (ITA s.60.03).
    
    Federal rules (ITA s.60.03):
    - Age 55-64: Can only split qualifying pension income from LIF, LIRA, RPP, 
      and life annuity income (NOT RRSP withdrawals)
    - Age 65+: Can also split RRIF income and annuity payments from RRSP
    
    Quebec rules (TP-1 Schedule L):
    - Age 65+: Can split life annuity, LIF, and RRIF income
    
    DP#28: Eligibility is a date-computed gate. The income type determines
    whether splitting is available, not just the amount.
    """
    LIFE_ANNUITY = "life_annuity"          # Annuity from RPP or purchased annuity (eligible at 55+)
    RPP_PENSION = "rpp_pension"            # Registered Pension Plan (eligible at 55+)
    LIF_INCOME = "lif_income"             # Life Income Fund (eligible at 55+)
    RRIF_INCOME = "rrif_income"           # RRIF income (eligible at 65+ only)
    RRSP_ANNUITY = "rrsp_annuity"          # Annuity from RRSP (eligible at 65+ only)
    RRSP_WITHDRAWAL = "rrsp_withdrawal"   # RRSP lump-sum withdrawal (NOT eligible for splitting)
    NON_QUALIFYING = "non_qualifying"     # Any other income type (NOT eligible)


def _is_income_type_eligible(income_type: PensionIncomeType, age: int, province: str = 'quebec') -> bool:
    """Check if a pension income type is eligible for splitting at a given age.
    
    DP#28: Eligibility is date-computed (from age/birth_year + income type).
    DP#27: Income type matters — not all pension income qualifies.
    
    Federal rules (ITA s.60.03):
    - Age 55-64: LIF, RPP, life annuity income qualifies
    - Age 65+: Also RRIF income and RRSP annuity income
    - RRSP lump-sum withdrawals NEVER qualify for splitting
    
    Quebec rules (TP-1 Schedule L):
    - Age 65+: Life annuity, LIF, RRIF income qualifies
    
    Args:
        income_type: Type of pension income
        age: Taxpayer's age
        province: Province code
    
    Returns:
        True if this income type qualifies for pension splitting
    """
    never_eligible = {PensionIncomeType.RRSP_WITHDRAWAL, PensionIncomeType.NON_QUALIFYING}
    if income_type in never_eligible:
        return False
    
    # Eligible at age 55+ (federal) or 65+ (Quebec): LIF, RPP, life annuity
    eligible_55_plus = {PensionIncomeType.LIFE_ANNUITY, PensionIncomeType.RPP_PENSION, PensionIncomeType.LIF_INCOME}
    # Eligible only at age 65+: RRIF, RRSP annuity
    eligible_65_plus = {PensionIncomeType.RRIF_INCOME, PensionIncomeType.RRSP_ANNUITY}
    
    if province == 'quebec':
        # Quebec: all qualifying types available at 65+
        if age >= 65:
            return income_type in (eligible_55_plus | eligible_65_plus)
        return False  # Quebec: no splitting before 65
    else:
        # Federal (other provinces): 55+ for LIF/RPP/annuity, 65+ for RRIF
        if age >= 65:
            return income_type in (eligible_55_plus | eligible_65_plus)
        elif age >= 55:
            return income_type in eligible_55_plus
        return False  # Under 55: no splitting


def pension_splitting_available(
    age: int,
    province: str = "quebec",
    has_qualifying_income: bool = True,
    income_type: PensionIncomeType = None,
) -> Dict:
    """Check if pension income splitting is available.

    Federal: available at age 55+ for qualifying pension income
    Quebec: available at age 65+ for life annuity/LIF/RRIF income

    DP#28: Eligibility depends on BOTH age AND income type.
    RRSP lump-sum withdrawals never qualify. RRIF income only qualifies at 65+.

    Args:
        age: Taxpayer's age
        province: Province code
        has_qualifying_income: Whether taxpayer has qualifying pension income
        income_type: Type of pension income (PensionIncomeType enum).
            If provided, both age AND income type are verified (DP#54).
            If None, only has_qualifying_income is checked.

    Returns:
        Dict with available, max_split, and rules
    """
    # DP#54: Verify income type eligibility when provided
    if income_type is not None:
        type_eligible = _is_income_type_eligible(income_type, age, province)
    else:
        type_eligible = has_qualifying_income  # Backward compat
    
    federal_available = age >= 55 and type_eligible
    max_split_pct = 0.50  # Can split up to 50% of qualifying income

    if province == "quebec":
        provincial_available = age >= 65 and type_eligible
        provincial_note = "Quebec: 65+ for RRIF/LIF/annuity income"
    else:
        provincial_available = federal_available
        provincial_note = "Same as federal rules"

    return {
        'federal_available': federal_available,
        'provincial_available': provincial_available,
        'max_split_pct': max_split_pct,
        'province': province,
        'federal_age_requirement': 55,
        'provincial_age_requirement': 65 if province == "quebec" else 55,
        'provincial_note': provincial_note,
    }


# =============================================================================
# Per-Member Retirement Data (DP#28)
# =============================================================================

@dataclass
class MemberRetirementData:
    """Per-member retirement income and pension data.
    
    DP#4: role-based names, not person names.
    DP#28: eligibility is date-computed from birth_year.
    DP#16: auto-include when any field is non-zero.
    """
    role: str = "primary"             # 'primary' or 'spouse'
    birth_year: int = 1979           # DP#1: store date, not age
    cpp_start_age: int = 65         # 60-70 range
    cpp_monthly_estimated: float = 0.0  # Estimated CPP benefit at start age
    oas_start_age: int = 65         # 65-70 (can defer 0-60 months)
    oas_defer_months: int = 0       # 0 = start at 65, 60 = start at 70
    pension_income_annual: float = 0.0  # Employer pension (DB or DC)
    employer_rrsp_match_pct: float = 0.0  # DP: employer RRSP match %
    employer_rrsp_match_max: float = 0.0  # Maximum employer match $
    rrif_conversion_age: int = 71   # Default; can convert at 65+ for splitting
    earnings_history: Optional[List] = None  # issue #365: optional contributory earnings

    @property
    def cpp_annual(self) -> float:
        """Annual CPP benefit based on start age."""
        if self.cpp_monthly_estimated <= 0:
            return 0.0
        return self.cpp_monthly_estimated * 12
    
    @property
    def oas_annual(self) -> float:
        """OAS annual amount, increased by deferral and 75+ enhancement (DP#28)."""
        # Base OAS depends on age (75+ gets 10% enhancement)
        # Use 2026 as default for property access without a specific year
        base = get_oas_annual_max(2026)
        if self.oas_defer_months > 0:
            # 0.6% increase per month deferred after age 65
            increase = 1 + (self.oas_defer_months * 0.006)
            return base * increase
        return base
    
    def oas_annual_for_year(self, year: int) -> float:
        """OAS annual amount for a specific year, accounting for age-based enhancement and deferral.
        
        DP#28: OAS amounts depend on recipient age (75+ gets 10% more).
        DP#20: Amounts are year-versioned.
        """
        age = year - self.birth_year
        return oas_amount_for_age(age, year=year, defer_months=self.oas_defer_months)
    
    @property
    def age_in(self) -> callable:
        """DP#1: Compute age from birth_year, not stored age."""
        def _age(year: int) -> int:
            return year - self.birth_year
        return _age
    
    def is_cpp_eligible(self, year: int) -> bool:
        """Check if CPP starts this year."""
        return (year - self.birth_year) >= self.cpp_start_age
    
    def is_oas_eligible(self, year: int) -> bool:
        """Check if OAS starts this year."""
        actual_start_age = 65 + self.oas_defer_months / 12
        return (year - self.birth_year) >= actual_start_age
    
    def is_pension_splitting_eligible(self, year: int, province: str = 'quebec') -> bool:
        """Check if pension splitting is available.
        
        Federal: 55+
        Quebec: 65+ (even for RPP)
        """
        age = year - self.birth_year
        if province == 'quebec':
            return age >= 65
        return age >= 55
    
    def employer_match(self, gross_income: float) -> float:
        """Calculate employer RRSP match contribution."""
        return min(gross_income * self.employer_rrsp_match_pct, self.employer_rrsp_match_max)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'MemberRetirementData':
        """Create from input.json family.members section.

        issue #365: when earnings_history is present and
        cpp_monthly_estimated is 0, a grounded estimate is computed
        from contributory earnings.
        """
        earnings_history_raw = data.get('earnings_history', None)
        cpp_monthly = data.get('cpp_monthly_estimated', 0)

        if earnings_history_raw and cpp_monthly == 0:
            from countries.canada.cpp_estimator import (
                EarningsEntry, compute_benefit_estimate,
            )
            entries = [
                EarningsEntry(year=e.get('year', 0),
                              employment_income=e.get('employment_income'))
                for e in earnings_history_raw
            ]
            start_age = data.get('cpp_start_age', 65)
            estimate = compute_benefit_estimate(entries, start_age=start_age)
            if start_age == 60:
                cpp_monthly = estimate.age_60_monthly
            elif start_age == 70:
                cpp_monthly = estimate.age_70_monthly
            else:
                cpp_monthly = estimate.age_65_monthly

        return cls(
            role=data.get('role', 'primary'),
            birth_year=data.get('birth_year', 1979),
            cpp_start_age=data.get('cpp_start_age', 65),
            cpp_monthly_estimated=cpp_monthly,
            oas_start_age=data.get('oas_start_age', 65),
            oas_defer_months=data.get('oas_defer_months', 0),
            pension_income_annual=data.get('pension_income_annual', 0),
            employer_rrsp_match_pct=data.get('employer_rrsp_match_pct', 0),
            employer_rrsp_match_max=data.get('employer_rrsp_match_max', 0),
            rrif_conversion_age=data.get('rrif_conversion_age', 71),
            earnings_history=earnings_history_raw,
        )

    def to_dict(self) -> dict:
        """Export to dict. DP#24: round-trip."""
        result = {
            'role': self.role,
            'birth_year': self.birth_year,
            'cpp_start_age': self.cpp_start_age,
            'cpp_monthly_estimated': self.cpp_monthly_estimated,
            'oas_start_age': self.oas_start_age,
            'oas_defer_months': self.oas_defer_months,
            'pension_income_annual': self.pension_income_annual,
            'employer_rrsp_match_pct': self.employer_rrsp_match_pct,
            'employer_rrsp_match_max': self.employer_rrsp_match_max,
            'rrif_conversion_age': self.rrif_conversion_age,
        }
        if self.earnings_history is not None:
            result['earnings_history'] = self.earnings_history
        return result


# =============================================================================
# Retirement State
# =============================================================================

@dataclass
class RetirementState:
    """Snapshot of retirement financial state.

    Stores account balances and key parameters for drawdown simulation.
    All values are in today's dollars (real, not nominal).

    DP#1: age is derived from birth_year and simulation year, not stored.
    When birth_year is provided, age_in(year) computes age correctly
    for any simulation year. The stored 'age' field remains for
    backward compatibility but should not be incremented manually.
    """
    birth_year: Optional[int] = None  # DP#1: compute age from birth_year, not stored age
    age: int = 65  # DP#1 deprecated: use birth_year + age_in(year) instead
    rrif_balance: float = 500000
    tfsa_balance: float = 100000
    non_reg_balance: float = 200000
    non_reg_acb: float = 100000  # Adjusted cost base

    # Income sources
    cpp_start_age: int = 65
    cpp_annual: float = 0.0
    oas_annual: float = 0  # DP#13: 0 means not set; use get_oas_annual_max(year) for year-versioned defaults

    # Living expenses
    annual_expenses: float = 50000

    # Province
    province: str = "quebec"

    # DP#28: Drawdown order as data (DP#8). User provides order; simulator computes outcome.
    drawdown_order: list = field(default_factory=lambda: ["tfsa", "non_reg", "rrsp"])
    rrif_conversion_age: int = 71  # Default; can convert at 65+ for splitting

    # Per-member retirement income (DP#28)
    members: List[MemberRetirementData] = field(default_factory=list)

    # LIF (Life Income Fund) — issue #230: CRI/LIRA converts to LIF at age 71
    lif_balance: float = 0.0
    lif_jurisdiction: str = 'federal'
    lif_birth_year: Optional[int] = None  # DP#1: compute age from birth_year

    # Computed
    year: int = 0

    def age_in(self, year: int) -> int:
        """DP#1: Compute age from birth_year for any simulation year.

        When birth_year is set, age is derived (not stored), preventing
        staleness bugs in multi-year simulations.
        """
        if self.birth_year is not None:
            return year - self.birth_year
        # Fallback: derive from stored age and year offset
        return self.age + (year - max(self.year, 0)) if self.year else self.age

    @property
    def total_assets(self) -> float:
        """Total retirement assets including LIF (issue #230)."""
        return self.rrif_balance + self.tfsa_balance + self.non_reg_balance + self.lif_balance

    def compute_cpp(self) -> float:
        """Compute CPP benefit based on start age.

        Uses self.year for year-versioned lookup (DP#20).
        Raises ValueError if self.year is 0 (unset).

        Returns:
            Estimated annual CPP benefit
        """
        if not self.year:
            raise ValueError("RetirementState.year must be set before calling compute_cpp() (DP#9, DP#20)")
        return cpp_benefit(self.cpp_start_age, year=self.year)

    def compute_taxable_income(self, rrif_withdrawal: float = 0) -> float:
        """Compute total taxable income including LIF withdrawals (issue #230)."""
        cpp = self.compute_cpp() if self.age >= self.cpp_start_age else 0
        oas = self.oas_annual if self.age >= 65 else 0
        rrif_income = rrif_withdrawal or rrif_minimum_withdrawal(
            self.rrif_balance, self.age)
        # LIF withdrawals are fully taxable as regular income (issue #230)
        lif_income = 0.0
        if self.lif_balance > 0 and self.lif_birth_year is not None:
            from countries.canada.locked_in_account import LIFFund
            lif_age = self.age if self.lif_birth_year is None else self.age_in(self.year) if self.year else self.age
            fund = LIFFund(
                balance=self.lif_balance,
                owner_birth_year=self.lif_birth_year or 0,
                jurisdiction=self.lif_jurisdiction,
            )
            if self.year > 0:
                lif_income = fund.minimum_withdrawal(self.year)
        return cpp + oas + rrif_income + lif_income


# =============================================================================
# Drawdown Optimizer
# =============================================================================

class DrawdownOptimizer:
    """Optimizes retirement drawdown order to minimize tax and maximize OAS.

    The optimizer discovers the best drawdown order from the rules
    (DP#6): it's not "use the TFSA first" as a rule, it's "withdraw
    from the account that minimizes the combined tax + OAS clawback cost."
    """

    def __init__(self, province: str = "quebec",
                 investment_return: float | None = None):
        if investment_return is None:
            raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
        self.province = province
        self.investment_return = investment_return
        self._tax_provider = default_tax_provider()
        self.brackets = self._tax_provider.get_combined_brackets()

    def optimize_year(self, state: RetirementState) -> Dict:
        """Optimize one year of drawdown.

        Returns the optimal withdrawal amounts from each account
        to meet expenses while minimizing tax + OAS clawback.
        
        DP#28: OAS deferral is integrated — if a member has deferred OAS,
        the optimizer accounts for the higher (deferred) OAS amount
        when it starts, and the gap years when OAS hasn't started yet.
        """
        needed = state.annual_expenses
        cpp = state.compute_cpp() if state.age >= state.cpp_start_age else 0
        
        # DP#28: Use age-based OAS amount (75+ enhancement) and deferral
        oas_for_age = oas_amount_for_age(state.age, year=state.year)
        # If members have retirement data with deferral, use that
        if state.members:
            primary = next((m for m in state.members if m.role == 'primary'), None)
            if primary:
                oas_for_age = primary.oas_annual_for_year(state.year)
        
        oas_info = oas_clawback(0, oas_amount=oas_for_age)  # Will be recalculated

        remaining_needed = needed - cpp
        if state.age >= 65:
            remaining_needed -= oas_for_age  # OAS covers some expenses

        # Try different drawdown strategies and pick the best
        strategies = {
            'tax_first': self._tax_first_strategy(state, remaining_needed),
            'deferred_first': self._deferred_first_strategy(state, remaining_needed),
            'balanced': self._balanced_strategy(state, remaining_needed),
        }

        # Pick the strategy with the lowest total cost (tax + clawback)
        best_name = None
        best_cost = float('inf')
        best_result = None

        for name, result in strategies.items():
            total_cost = result['total_tax'] + result['oas_clawback']
            if total_cost < best_cost:
                best_cost = total_cost
                best_name = name
                best_result = result

        best_result['strategy'] = best_name
        return best_result

    def _tax_first_strategy(self, state: RetirementState,
                             remaining: float) -> Dict:
        """Tax-first: non-reg → TFSA → RRSP/RRIF.

        Minimizes OAS clawback by keeping RRIF withdrawals low.
        """
        withdrawals = {'non_reg': 0, 'tfsa': 0, 'rrif': 0}
        remaining = max(0, remaining)

        # Non-reg first (capital gains, most tax-efficient)
        if state.non_reg_balance > 0 and remaining > 0:
            withdrawals['non_reg'] = min(remaining, state.non_reg_balance)
            remaining -= withdrawals['non_reg']

        # TFSA second (no tax, preserves OAS)
        if state.tfsa_balance > 0 and remaining > 0:
            withdrawals['tfsa'] = min(remaining, state.tfsa_balance)
            remaining -= withdrawals['tfsa']

        # RRIF last (taxable, may trigger clawback)
        rrif_min = rrif_minimum_withdrawal(state.rrif_balance, state.age)
        withdrawals['rrif'] = max(rrif_min, remaining) if state.rrif_balance > 0 else 0

        return self._compute_strategy_cost(state, withdrawals)

    def _deferred_first_strategy(self, state: RetirementState,
                                  remaining: float) -> Dict:
        """Deferred-first: RRSP/RRIF → non-reg → TFSA.

        Preserves TFSA room for later years.
        """
        withdrawals = {'non_reg': 0, 'tfsa': 0, 'rrif': 0}
        remaining = max(0, remaining)

        # RRIF first
        rrif_min = rrif_minimum_withdrawal(state.rrif_balance, state.age)
        withdrawals['rrif'] = max(rrif_min, remaining) if state.rrif_balance > 0 else 0
        rrif_cash = withdrawals['rrif'] - rrif_min
        remaining -= rrif_cash
        remaining += rrif_min  # Min withdrawal is forced income

        # Non-reg second
        if state.non_reg_balance > 0 and remaining > 0:
            withdrawals['non_reg'] = min(remaining, state.non_reg_balance)

        return self._compute_strategy_cost(state, withdrawals)

    def _balanced_strategy(self, state: RetirementState,
                           remaining: float) -> Dict:
        """Balanced: RRIF minimum + proportional from non-reg and TFSA.

        Fills each account proportionally to its share of total assets.
        """
        withdrawals = {'non_reg': 0, 'tfsa': 0, 'rrif': 0}

        # Always take RRIF minimum
        rrif_min = rrif_minimum_withdrawal(state.rrif_balance, state.age)
        withdrawals['rrif'] = rrif_min

        # Proportional from non-reg and TFSA
        rrif_excess_after_min = max(0, remaining - 0)  # After min RRIF income
        total_available = state.non_reg_balance + state.tfsa_balance
        if total_available > 0 and remaining > 0:
            non_reg_share = state.non_reg_balance / total_available
            tfsa_share = state.tfsa_balance / total_available
            withdrawals['non_reg'] = min(remaining * non_reg_share, state.non_reg_balance)
            withdrawals['tfsa'] = min(remaining * tfsa_share, state.tfsa_balance)

        return self._compute_strategy_cost(state, withdrawals)

    def _compute_strategy_cost(self, state: RetirementState,
                                withdrawals: Dict) -> Dict:
        """Compute tax and OAS clawback cost for a withdrawal strategy."""
        # Taxable income
        rrif_income = withdrawals.get('rrif', 0)
        cpp = state.compute_cpp() if state.age >= state.cpp_start_age else 0
        # DP#28: Use age-based OAS (75+ enhancement)
        oas = oas_amount_for_age(state.age, year=state.year) if state.age >= 65 else 0
        # If members have retirement data with deferral, use that
        if state.members:
            primary = next((m for m in state.members if m.role == 'primary'), None)
            if primary and state.age >= 65:
                oas = primary.oas_annual_for_year(state.year)

        # Non-reg: capital gains on disposition
        non_reg_withdrawal = withdrawals.get('non_reg', 0)
        non_reg_gains = max(0, non_reg_withdrawal - state.non_reg_acb *
                           (non_reg_withdrawal / max(1, state.non_reg_balance)))
        non_reg_taxable = non_reg_gains * 0.50  # 50% inclusion

        # Total net income for OAS clawback
        net_income = rrif_income + cpp + oas + non_reg_taxable

        # OAS clawback
        clawback_info = oas_clawback(net_income)

        # Tax on RRIF income
        rrif_tax = tax_on_income(net_income, self.brackets) if net_income > 0 else 0

        return {
            'withdrawals': withdrawals,
            'net_income': net_income,
            'rrif_income': rrif_income,
            'cpp_income': cpp,
            'non_reg_taxable': non_reg_taxable,
            'total_tax': rrif_tax,
            'oas_clawback': clawback_info['clawback_amount'],
            'net_oas': clawback_info['net_oas'],
        }


# =============================================================================
# Multi-Year Retirement Projection
# =============================================================================

def project_retirement(
    initial_state: RetirementState,
    years: int = 30,
    province: str = "quebec",
    investment_return: float | None = None,
    inflation: float = 0.025,
) -> List[Dict]:
    """Project retirement finances year by year.

    Args:
        initial_state: Starting retirement state
        years: Number of years to project
        province: Province code
        investment_return: Real investment return (after inflation)
        inflation: Inflation rate (for expense growth)

    Returns:
        List of year-by-year results
    """
    if investment_return is None:
        raise ValueError("investment_return must be specified explicitly (DP#13: no opinionated defaults)")
    optimizer = DrawdownOptimizer(province, investment_return)
    state = RetirementState(
        birth_year=initial_state.birth_year,
        age=initial_state.age,
        rrif_balance=initial_state.rrif_balance,
        tfsa_balance=initial_state.tfsa_balance,
        non_reg_balance=initial_state.non_reg_balance,
        non_reg_acb=initial_state.non_reg_acb,
        cpp_start_age=initial_state.cpp_start_age,
        cpp_annual=initial_state.cpp_annual,
        oas_annual=initial_state.oas_annual,
        annual_expenses=initial_state.annual_expenses,
        province=province,
    )

    results = []
    for yr in range(years):
        # DP#1: Compute age from birth_year when available
        if state.birth_year is not None:
            # Start year is initial retirement year; yr=0 is the first year
            sim_year = initial_state.year + yr if initial_state.year else 2026 + yr
            state.age = state.age_in(sim_year)
        else:
            sim_year = 2026 + yr
        state.year = sim_year  # Set for RetirementState.compute_cpp() and optimizer
        current_age = state.age

        if state.total_assets <= 0 and current_age < 65:
            break  # Can't continue

        year_result = optimizer.optimize_year(state)
        year_result['year'] = yr + 1
        year_result['age'] = current_age
        year_result['total_assets'] = state.total_assets
        results.append(year_result)

        # Update state for next year
        w = year_result['withdrawals']
        state.rrif_balance = max(0, state.rrif_balance - w.get('rrif', 0))
        state.tfsa_balance = max(0, state.tfsa_balance - w.get('tfsa', 0))
        state.non_reg_balance = max(0, state.non_reg_balance - w.get('non_reg', 0))

        # Grow remaining balances
        state.rrif_balance *= (1 + investment_return)
        state.tfsa_balance *= (1 + investment_return)
        state.non_reg_balance *= (1 + investment_return)

        # DP#1: When birth_year is set, age is derived; otherwise increment
        if state.birth_year is None:
            state.age += 1
        # else: age is recomputed at top of loop from age_in(sim_year)
        state.annual_expenses *= (1 + inflation)

        # OAS increases with inflation
        state.oas_annual *= (1 + inflation)

    return results


# DP#2/DP#12/DP#20: Initialize backward-compat RRIF rates from provider
RRIF_MIN_WITHDRAWAL_RATES = _get_rrif_rates()
