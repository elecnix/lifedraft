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

Callers that supply a contributory earnings series (explicit
earnings_history and/or incomes projected with salary_growth — issue #390)
get a grounded estimate; a Service Canada / Retraite Québec Statement
always replaces it on the contract path.

Algorithm:
    - Compute ratios over a contributory-period span (data span or 40 years).
    - Missing years *inside* the span count as zero (sparse careers get lower
      benefit). Issue #390: when the span must grow to the 40-year minimum,
      pad BEFORE the first data year — never zero-pad after the last known /
      projected year (callers extend through age 65 with projected income).
    - General dropout (17%, max 8 years) removes lowest ratio-years.
    - Average ratio × max_benefit_65 → base benefit at 65.
    - CPP2 tier computed in parallel for 2024+ earnings above YMPE.
    - Age factors: 0.6%/month penalty before 65, 0.7%/month bonus after 65.

Quebec (issue #390): ``plan="qpp"`` swaps the age-65 *max benefit* table for
Retraite Québec year-versioned maxima. YMPE / YAMPE history and the CPP2
tier remain CPP-table-centric (shared ceilings; boiling-ocean avoided).

References:
    https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-benefit.html
    https://www.canada.ca/en/services/benefits/publicpensions/cpp/cpp-benefit/amount.html
    CPP2: https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
"""

from dataclasses import dataclass
from typing import List, Mapping, Optional, Sequence

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

# ── QPP max retirement benefit at 65 (issue #390) ────────────────────────────
# Year-versioned Retraite Québec maxima. Used only when plan="qpp".
# Source: countries/canada/provinces/quebec/tax_data.py (DP#20 / DP#52).
# YMPE/YAMPE/CPP2 remain on the CPP tables above (shared ceilings).
_QPP_MAX_BENEFIT_65: dict = {
    2023: 15170,
    2024: 17334,
    2025: 17334,
    2026: 17334,
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


def _max_benefit_for_year(year: int, plan: str = "cpp") -> float:
    """Get max CPP/QPP retirement benefit at 65, with fallback.

    ``plan="qpp"`` (issue #390) uses Retraite Québec maxima when known;
    falls back to the latest QPP row, then to CPP if the QPP table is empty.
    """
    if plan == "qpp":
        if year in _QPP_MAX_BENEFIT_65:
            return _QPP_MAX_BENEFIT_65[year]
        if _QPP_MAX_BENEFIT_65:
            max_known = max(_QPP_MAX_BENEFIT_65.keys())
            if year > max_known:
                return _QPP_MAX_BENEFIT_65[max_known]
            min_known = min(_QPP_MAX_BENEFIT_65.keys())
            return _QPP_MAX_BENEFIT_65[min_known]
        # Empty QPP table: fall through to CPP rather than invent 0.
    if year in CPP_OAS_BY_YEAR:
        return CPP_OAS_BY_YEAR[year]["cpp_max_benefit_65"]
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
    plan: str = "cpp",
) -> CPPBenefitEstimate:
    """Compute CPP/QPP retirement benefit estimates from earnings history.

    Pure function (DP#3): same (earnings, start_age, plan) → same result.

    Algorithm:
    1. Filter valid years. Map year → (base_ratio, cpp2_ratio).
    2. Determine contributory span: from first_data_year to last_data_year.
       If shorter than _MIN_CONTRIBUTORY_SPAN, pad BEFORE first_data_year
       (issue #390 — never zero-pad after the last known/projected year).
    3. Fill missing years *inside* the span with zero ratios.
    4. Apply general dropout (17%, max 8) to the full-span ratio series.
    5. Average retained ratios.
    6. Multiply average by max_benefit_65 (year-versioned, DP#20; QPP table
       when plan="qpp").
    7. Apply age-adjustment factors for 60, 65, 70.

    CPP2 tier computed in parallel on YMPE→YAMPE band earnings (CPP tables
    even when plan="qpp" — shared ceilings; see module docstring).

    Args:
        earnings: List of EarningsEntry per contributory year.
        start_age: Age at which to start CPP/QPP (60-70).
        plan: ``"cpp"`` (default) or ``"qpp"`` for the age-65 max benefit table.

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
    # Issue #390: pad BEFORE first_year when the data span is short — never
    # invent zero contributory years after the last known / projected year
    # (callers extend through age 65 with incomes + salary_growth). For a
    # series that is still short of 40 years after that extension, the same
    # count of zero-ratio years is used either way; placing them before the
    # career keeps "after last year" free of fabricated zeros.
    first_year = min(all_entry_years)
    last_year = max(all_entry_years)
    data_span = last_year - first_year + 1
    if data_span >= _MIN_CONTRIBUTORY_SPAN:
        span_start = first_year
        span_end = last_year
    else:
        span_end = last_year
        span_start = span_end - _MIN_CONTRIBUTORY_SPAN + 1
    contrib_span = span_end - span_start + 1

    # Fill ratios over the full span (missing years inside → 0.0)
    base_ratios = [
        year_data.get(y, (0.0, 0.0))[0]
        for y in range(span_start, span_end + 1)
    ]
    cpp2_ratios = [
        year_data.get(y, (0.0, 0.0))[1]
        for y in range(span_start, span_end + 1)
    ]

    # ── Dropout ────────────────────────────────────────────────────────
    base_avg, dropout_count = _dropout_average(base_ratios)
    cpp2_avg, _ = _dropout_average(cpp2_ratios)

    # ── Max benefit reference ──────────────────────────────────────────
    max_benefit_65 = _max_benefit_for_year(last_year, plan=plan)
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

# ── Earnings series construction (issue #390) ────────────────────────────────

_PENSIONABLE_KINDS = frozenset({"employment", "self_employment"})


def build_earnings_for_estimate(
    *,
    earnings_history: Optional[Sequence[Mapping]] = None,
    incomes: Optional[Sequence[Mapping]] = None,
    salary_growth: float = 0.0,
    as_of_year: int,
    birth_year: int,
    end_age: int = 65,
) -> List[EarningsEntry]:
    """Build a contributory earnings series for ``compute_benefit_estimate``.

    Pure (DP#3). Issue #390:

    1. Seed from explicit ``earnings_history`` when present (wins year-by-year
       over income-derived amounts).
    2. Overlay dated ``incomes`` of kind employment / self_employment for
       years not already covered by history (declared amount on the interval;
       no reverse ``salary_growth`` into the past).
    3. Extend through ``birth_year + end_age - 1`` using active employment
       income at ``as_of_year``, grown forward with ``salary_growth`` —
       but never past a closed income's ``to`` date when every active
       income is closed (no open-ended job). Fallback when no active
       income at as_of: last known series year, grown the same way.
    4. Never invent zero years after the last known/projected year — that is
       the contract with ``compute_benefit_estimate``'s backward pad.

    Years after an income's ``to`` date are not projected from that income.
    ``self_employment`` uses gross ``amount`` (estimator takes employment
    income; T2125 netting is out of scope — issue #390).
    """
    by_year: dict[int, float] = {}

    for raw in earnings_history or ():
        year = raw.get("year")
        if year is None:
            continue
        income = raw.get("employment_income")
        if income is None:
            continue
        by_year[int(year)] = float(income)

    pensionable = [
        inc for inc in (incomes or ())
        if inc.get("kind") in _PENSIONABLE_KINDS
    ]
    for inc in pensionable:
        if not inc.get("from"):
            continue
        amount_raw = inc.get("amount")
        if amount_raw is None:
            continue
        start_s = str(inc["from"])
        if not start_s[:4].isdigit():
            continue
        amount = float(amount_raw)
        start = int(start_s[:4])
        end_raw = inc.get("to")
        # Inclusive calendar years overlapping [from, to). Null to = open
        # through as_of_year; future years are filled by the projection step.
        if end_raw is not None and not str(end_raw)[:4].isdigit():
            continue
        last = (int(str(end_raw)[:4]) - 1) if end_raw else as_of_year
        for y in range(start, min(last, as_of_year) + 1):
            by_year.setdefault(y, amount)

    end_year = birth_year + end_age - 1

    active_at_as_of = 0.0
    open_ended = False
    closed_future_ends: List[int] = []
    for inc in pensionable:
        if not inc.get("from"):
            continue
        start_s = str(inc["from"])
        if not start_s[:4].isdigit():
            continue
        if int(start_s[:4]) > as_of_year:
            continue
        if inc.get("amount") is None:
            continue
        end = inc.get("to")
        if end is not None and str(end)[:10] <= f"{as_of_year}-12-31":
            continue
        active_at_as_of += float(inc["amount"])
        if end is None:
            open_ended = True
        else:
            if not str(end)[:4].isdigit():
                continue
            closed_future_ends.append(int(str(end)[:4]) - 1)

    growth = float(salary_growth) if salary_growth else 0.0

    if active_at_as_of > 0:
        by_year.setdefault(as_of_year, active_at_as_of)
        project_until = end_year
        if not open_ended and closed_future_ends:
            project_until = min(end_year, max(closed_future_ends))
        base_amount = active_at_as_of
        growth_origin = as_of_year
        first_project = as_of_year + 1
    elif by_year:
        last_known = max(by_year)
        base_amount = by_year[last_known]
        growth_origin = last_known
        project_until = end_year
        first_project = last_known + 1
    else:
        return []

    for y in range(first_project, project_until + 1):
        if y in by_year:
            continue  # explicit history wins
        by_year[y] = base_amount * ((1.0 + growth) ** (y - growth_origin))

    return [
        EarningsEntry(year=y, employment_income=amt)
        for y, amt in sorted(by_year.items())
    ]
