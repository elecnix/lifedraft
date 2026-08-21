#!/usr/bin/env python3
"""
CPP Retirement Benefit Estimator — issue #365.

Computes CPP retirement benefit estimates from a contributory earnings
history (YMPE-capped earnings per year), applying dropout provisions
and age-adjustment factors for start ages 60/65/70. Handles CPP2
(second additional) tier where applicable.

Design:
- DP#3: Pure functions — same inputs → same outputs. No globals.
- DP#10: One module per program — CPP estimator lives here.
- DP#20: Uses year-versioned getters from retirement.py for 2023+.
- DP#25: Imports from retirement.py (data layer) and core — never
  imports from simulation or optimization layers.
- DP#15: No personal data in defaults. All example data uses round numbers.

The estimator is optional: callers that provide earnings_history get a
grounded estimate; callers without it continue using the existing
cpp_monthly_estimated placeholder.

Algorithm:
    - Compute ratios over a contributory-period span (data span or 40 years).
    - Missing years in the span count as zero (sparse careers get lower benefit).
    - General dropout (17%, max 8 years) removes lowest ratio-years.
    - Average ratio × max_benefit_65 → base benefit at 65.
    - CPP2 tier computed in parallel for 2024+ earnings above YMPE.
    - Age factors: 0.6%/month penalty before 65, 0.7%/month bonus after 65.

References:
    https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-benefit.html
    https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-benefit/amount.html
    CPP2: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
"""

from dataclasses import dataclass
from typing import Optional, Sequence

from countries.canada.retirement import (
    CPP_EARLY_START_PENALTY,
    CPP_LATE_START_BONUS,
    CPP_OAS_BY_YEAR,
)

# ── Constants ────────────────────────────────────────────────────────────────

# General dropout: exclude the lowest 17% of contributory years.
# Floor at 1 year for careers > 5 years, capped at 8 (pre-2012 rule).
_GENERAL_DROPOUT_RATIO = 0.17
_MAX_DROPOUT_YEARS = 8

# Minimum contributory period span (for sparse careers).
# Without this, a single-year entry would appear as a full career at YMPE.
_MIN_CONTRIBUTORY_SPAN = 40

# CPP2 (second additional) introduced in 2024.
_CPP2_START_YEAR = 2024

# ── Historical YMPE table (1966–2022) ────────────────────────────────────────
# Fallback for years not covered by CPP_OAS_BY_YEAR (2023+).
# Source: Service Canada / CRA historical YMPE tables.
_HISTORICAL_YMPE: dict = {
    1966: 5000, 1967: 5000, 1968: 5100, 1969: 5200, 1970: 5300,
    1971: 5400, 1972: 5500, 1973: 5600, 1974: 6600, 1975: 7400,
    1976: 8300, 1977: 9300, 1978: 10400, 1979: 11700, 1980: 13100,
    1981: 14700, 1982: 16500, 1983: 18500, 1984: 20800, 1985: 23400,
    1986: 25800, 1987: 25900, 1988: 26500, 1989: 27700, 1990: 28900,
    1991: 30500, 1992: 32200, 1993: 33400, 1994: 34400, 1995: 34900,
    1996: 35400, 1997: 35800, 1998: 36900, 1999: 37400, 2000: 37600,
    2001: 38300, 2002: 39100, 2003: 39900, 2004: 40500, 2005: 41100,
    2006: 42100, 2007: 43700, 2008: 44900, 2009: 46300, 2010: 47200,
    2011: 48300, 2012: 50100, 2013: 51100, 2014: 52500, 2015: 53600,
    2016: 54900, 2017: 55300, 2018: 55900, 2019: 57400, 2020: 58700,
    2021: 61600, 2022: 64900,
}


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class EarningsEntry:
    """One year of contributory earnings for CPP estimation.

    DP#1: year is stored, not age.
    DP#3: pure data — no behavior attached.
    """
    year: int
    employment_income: Optional[float] = None


@dataclass
class CPPBenefitEstimate:
    """CPP benefit estimates at ages 60, 65, and 70.

    Values are monthly benefit estimates in the reference year's dollars.
    Zero when earnings history is insufficient or start_age is invalid.
    """
    age_60_monthly: float = 0.0
    age_65_monthly: float = 0.0
    age_70_monthly: float = 0.0
    cpp2_age_60_monthly: float = 0.0
    cpp2_age_65_monthly: float = 0.0
    cpp2_age_70_monthly: float = 0.0
    contributory_period_years: int = 0
    dropout_years: int = 0


# ── Year-level lookup helpers ────────────────────────────────────────────────

def _ympe_for_year(year: int) -> float:
    """Get YMPE for a given year."""
    if year in CPP_OAS_BY_YEAR:
        return CPP_OAS_BY_YEAR[year]["cpp_max_pensionable"]
    if year in _HISTORICAL_YMPE:
        return _HISTORICAL_YMPE[year]
    # Future year: extrapolate at 2%/yr from latest historical
    max_known = max(_HISTORICAL_YMPE.keys())
    return _HISTORICAL_YMPE[max_known] * (1.02 ** (year - max_known))


def _yampe_for_year(year: int) -> float:
    """Get YAMPE (CPP2 max pensionable) for a given year."""
    if year < _CPP2_START_YEAR:
        return _ympe_for_year(year)
    if year in CPP_OAS_BY_YEAR and "cpp2_max_pensionable" in CPP_OAS_BY_YEAR[year]:
        return CPP_OAS_BY_YEAR[year]["cpp2_max_pensionable"]
    return _ympe_for_year(year) * 1.14


def _max_benefit_for_year(year: int) -> float:
    """Get max CPP retirement benefit at 65, with fallback."""
    if year in CPP_OAS_BY_YEAR:
        return CPP_OAS_BY_YEAR[year]["cpp_max_benefit_65"]
    # Future/unknown year: use latest known
    max_known = max(CPP_OAS_BY_YEAR.keys())
    return CPP_OAS_BY_YEAR[max_known]["cpp_max_benefit_65"]


def _cpp2_max_benefit(year: int) -> float:
    """Get max CPP2 annual benefit at 65, with fallback."""
    if year < _CPP2_START_YEAR:
        return 0.0
    if year in CPP_OAS_BY_YEAR and "cpp2_max_benefit" in CPP_OAS_BY_YEAR[year]:
        return CPP_OAS_BY_YEAR[year]["cpp2_max_benefit"]
    # Future year: use latest known
    max_known = max(CPP_OAS_BY_YEAR.keys())
    return CPP_OAS_BY_YEAR[max_known].get("cpp2_max_benefit", 0.0)


# ── Age adjustment ───────────────────────────────────────────────────────────

def _age_factor(start_age: int) -> float:
    """Age adjustment factor for CPP take-up.

    - Before 65: 0.6% penalty per month (max 36% at 60).
    - After 65: 0.7% bonus per month (max 42% at 70).
    """
    if start_age < 65:
        months_early = (65 - start_age) * 12
        return max(0.0, 1.0 - months_early * CPP_EARLY_START_PENALTY)
    elif start_age > 65:
        months_late = (start_age - 65) * 12
        return 1.0 + months_late * CPP_LATE_START_BONUS
    return 1.0


# ── Core estimator ───────────────────────────────────────────────────────────

def compute_benefit_estimate(
    earnings: Sequence[EarningsEntry],
    start_age: int = 65,
) -> CPPBenefitEstimate:
    """Compute CPP retirement benefit estimates from earnings history.

    Pure function (DP#3): same (earnings, start_age) → same result.

    Algorithm:
    1. Filter valid years. Map year → (base_ratio, cpp2_ratio).
    2. Determine contributory span: from first_data_year to last_data_year,
       padded to _MIN_CONTRIBUTORY_SPAN years.
    3. Fill missing years with zero ratios.
    4. Apply general dropout (17%, max 8) to the full-span ratio series.
    5. Average retained ratios.
    6. Multiply average by max_benefit_65 (year-versioned, DP#20).
    7. Apply age-adjustment factors for 60, 65, 70.

    CPP2 tier computed in parallel on YMPE→YAMPE band earnings.

    Args:
        earnings: List of EarningsEntry per contributory year.
        start_age: Age at which to start CPP (60-70).

    Returns:
        CPPBenefitEstimate with monthly benefit values.
    """
    if not 60 <= start_age <= 70:
        return CPPBenefitEstimate()

    # Filter to valid entries: income not None, YMPE > 0
    year_data: dict[int, tuple[float, float]] = {}  # year → (base, cpp2)
    all_entry_years = set()
    for entry in earnings:
        all_entry_years.add(entry.year)
        if entry.employment_income is None:
            continue
        ympe = _ympe_for_year(entry.year)
        if ympe <= 0:
            continue

        income = entry.employment_income
        base_ratio = min(max(income, 0.0), ympe) / ympe

        yampe = _yampe_for_year(entry.year)
        cpp2_range = yampe - ympe
        if cpp2_range > 0:
            above_ympe = max(0.0, min(income - ympe, cpp2_range))
            cpp2_ratio = above_ympe / cpp2_range
        else:
            cpp2_ratio = 0.0

        year_data[entry.year] = (base_ratio, cpp2_ratio)

    if not all_entry_years:
        return CPPBenefitEstimate()

    # ── Determine contributory span ────────────────────────────────────
    first_year = min(all_entry_years)
    last_year = max(all_entry_years)
    data_span = last_year - first_year + 1
    contrib_span = max(data_span, _MIN_CONTRIBUTORY_SPAN)

    # Fill ratios over the full span (missing years → 0.0)
    base_ratios = [
        year_data.get(y, (0.0, 0.0))[0]
        for y in range(first_year, first_year + contrib_span)
    ]
    cpp2_ratios = [
        year_data.get(y, (0.0, 0.0))[1]
        for y in range(first_year, first_year + contrib_span)
    ]

    # ── Dropout ────────────────────────────────────────────────────────
    base_avg, dropout_count = _dropout_average(base_ratios)
    cpp2_avg, _ = _dropout_average(cpp2_ratios)

    # ── Max benefit reference ──────────────────────────────────────────
    max_benefit_65 = _max_benefit_for_year(last_year)
    cpp2_max = _cpp2_max_benefit(last_year)

    # ── Compute benefits ───────────────────────────────────────────────
    base_65_annual = base_avg * max_benefit_65
    cpp2_65_annual = cpp2_avg * cpp2_max

    return CPPBenefitEstimate(
        age_60_monthly=round(base_65_annual * _age_factor(60) / 12, 2),
        age_65_monthly=round(base_65_annual / 12, 2),
        age_70_monthly=round(base_65_annual * _age_factor(70) / 12, 2),
        cpp2_age_60_monthly=round(cpp2_65_annual * _age_factor(60) / 12, 2),
        cpp2_age_65_monthly=round(cpp2_65_annual / 12, 2),
        cpp2_age_70_monthly=round(cpp2_65_annual * _age_factor(70) / 12, 2),
        contributory_period_years=contrib_span,
        dropout_years=dropout_count,
    )


def _dropout_average(ratios: list) -> tuple:
    """Apply general dropout and compute average ratio.

    Returns (avg_ratio, dropout_count).
    """
    total = len(ratios)
    if total == 0:
        return 0.0, 0

    n_drop = int(total * _GENERAL_DROPOUT_RATIO)
    if n_drop == 0 and total >= 5:
        n_drop = 1
    n_drop = min(n_drop, _MAX_DROPOUT_YEARS)

    sorted_ratios = sorted(ratios)
    retained = sorted_ratios[n_drop:] if n_drop < total else []

    if not retained:
        return 0.0, n_drop

    return sum(retained) / len(retained), n_drop
