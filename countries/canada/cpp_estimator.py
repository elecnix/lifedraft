#!/usr/bin/env python3
"""
CPP Retirement Benefit Estimator — issue #365.

Computes CPP retirement benefit estimates from a contributory earnings
history (YMPE-capped earnings per year), applying dropout provisions
and age-adjustment factors for start ages 60/65/70. Handles CPP2
(second additional) tier where applicable.

Design:
- DP#3: Pure functions — same inputs → same outputs. No globals.
- DP#10: One module per program — CPP estimator lives here; the year-versioned
  ceilings live in cpp_data.py, which this module reads and never restates.
- DP#20: Ceilings come from cpp_parameters(year, plan).
- DP#25: Imports from cpp_data.py / retirement.py (data layer) and core —
  never imports from simulation or optimization layers.
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

# Issue #415: the one spelling of what a dated [from, to) window means. Core
# and jurisdiction-neutral, so a Canada module importing it is the right
# direction of dependency (DP#25).
from income_window import covers_date, overlaps_year, parse_date, year_fraction

from countries.canada.cpp_data import cpp_parameters
from countries.canada.retirement import (
    CPP_EARLY_START_PENALTY,
    CPP_LATE_START_BONUS,
)

# ── Constants ────────────────────────────────────────────────────────────────

# General dropout: exclude the lowest 17% of contributory years.
# Floor at 1 year for careers > 5 years, capped at 8 (pre-2012 rule).
_GENERAL_DROPOUT_RATIO = 0.17
_MAX_DROPOUT_YEARS = 8

# Minimum contributory period span (for sparse careers).
# Without this, a single-year entry would appear as a full career at YMPE.
_MIN_CONTRIBUTORY_SPAN = 40

# ── Ceilings ─────────────────────────────────────────────────────────────────
# YMPE, YAMPE, the age-65 maximum and the CPP2 age-65 maximum are answered by
# countries/canada/cpp_data.py — one year-versioned interface, one documented
# rule for a year with no row. Nothing in this module holds a ceiling table or
# re-derives a fallback (DP#10, DP#12, DP#20).


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
        income = entry.employment_income
        params = cpp_parameters(entry.year, plan)
        if params.ympe <= 0:
            continue
        base_ratio = min(max(income, 0.0), params.ympe) / params.ympe

        cpp2_range = params.yampe - params.ympe
        if cpp2_range > 0:
            above_ympe = max(0.0, min(income - params.ympe, cpp2_range))
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
    reference = cpp_parameters(last_year, plan)
    max_benefit_65 = reference.max_benefit_65
    cpp2_max = reference.max_cpp2_benefit

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

# Issue #415, second finding: this set includes ``self_employment``, and
# ``contract_people._active_employment_income``'s ``kind != "employment"`` filter
# does not. A self-employment-only earner therefore gets a FOLD income base of
# $0 and a NON-ZERO CPP base -- same person, same day, measured. That is a
# question about which KINDS are pensionable, not about what a dated window
# means, so it is deliberately out of scope for income_window.py and is filed as
# a follow-up on #415 rather than quietly reconciled here. Documented at both
# sites so the disagreement is recorded, not accidental.


def build_earnings_for_estimate(
    *,
    earnings_history: Optional[Sequence[Mapping]] = None,
    incomes: Optional[Sequence[Mapping]] = None,
    salary_growth: float = 0.0,
    as_of_year: int,
    birth_year: int,
    end_age: int = 65,
    as_of_date: Optional[str] = None,
) -> List[EarningsEntry]:
    """Build a contributory earnings series for ``compute_benefit_estimate``.

    Pure (DP#3). Issue #390:

    1. Seed from explicit ``earnings_history`` when present (wins year-by-year
       over income-derived amounts).
    2. Overlay dated ``incomes`` of kind employment / self_employment for
       years not already covered by history (declared amount on calendar
       years that overlap half-open ``[from, to)``; no reverse
       ``salary_growth`` into the past).
    3. Extend through ``birth_year + end_age - 1`` using active employment
       income at ``as_of``, grown forward with ``salary_growth`` — but never
       past a closed income's ``to`` when every active income is closed.
       Fallback when no active income at as_of: last known series year,
       grown the same way.
    4. Never invent zero years after the last known/projected year — that is
       the contract with ``compute_benefit_estimate``'s backward pad.

    ``as_of_date`` (ISO ``YYYY-MM-DD``) is the snapshot for half-open
    ``[from, to)`` activity checks; defaults to Dec 31 of ``as_of_year``.
    ``self_employment`` uses gross ``amount`` (T2125 netting out of scope).
    """
    as_of = as_of_date or f"{as_of_year}-12-31"

    by_year: dict[int, float] = {}
    # Years the DECLARED history covers. History wins outright (documented
    # above); income-derived amounts never add to a year the history already
    # states, so a declared figure is never inflated.
    history_years: set[int] = set()

    for raw in earnings_history or ():
        year = raw.get("year")
        if year is None:
            continue
        income = raw.get("employment_income")
        if income is None:
            continue
        by_year[int(year)] = float(income)
        history_years.add(int(year))

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
        if end_raw is not None and not str(end_raw)[:4].isdigit():
            continue
        window_from = parse_date(str(inc["from"]))
        window_to = parse_date(str(end_raw)) if end_raw is not None else None
        # Calendar year Y overlaps [from, to) when
        # from < (Y+1)-01-01 and (to is null or to > Y-01-01).
        # Loop starts at start_year, so from is never after year_end_excl.
        for y in range(start, as_of_year + 1):
            if not overlaps_year(window_from, window_to, y):
                continue
            # Concurrent pensionable incomes in the SAME calendar year are
            # ADDED, not raced: a household with employment + self-employment
            # (or two jobs) contributes both, and returning the first match
            # would silently drop the rest -- the AGENTS.md trap that once cost
            # a household 40% of its debt. A year the declared history already
            # states is left alone, so history still wins outright.
            if y in history_years:
                continue
            # Issue #415: `amount` is an ANNUAL RATE (schema/defs/people.json,
            # $defs/income.amount: "Annual gross amount in effect over
            # [from, to)"). Crediting the whole rate to a year the window only
            # touches in part overstates that year's pensionable earnings -- up
            # to 198% of the real figure for a job that started on July 1 --
            # and CPP contribution years are CALENDAR years, so the year that is
            # wrong is the year the CRA would actually see a short one. The
            # same day-count weight the fold already applies to this window
            # (simulation._income_components_for_year), now from one shared
            # primitive so the two cannot drift apart again.
            by_year[y] = by_year.get(y, 0.0) + amount * year_fraction(
                window_from, window_to, y)

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
        if start_s[:10] > as_of[:10]:
            continue
        if inc.get("amount") is None:
            continue
        end = inc.get("to")
        if end is not None and not str(end)[:4].isdigit():
            continue  # same guard as the overlay loop
        # Half-open [from, to): inactive when to <= as_of. income_window states
        # that convention once; asking it as a question rather than restating
        # the comparison is what stopped #415's two readers disagreeing on the
        # boundary instant.
        if not covers_date(parse_date(start_s[:10]),
                          parse_date(str(end)[:10]) if end is not None else None,
                          parse_date(as_of[:10])):
            continue
        active_at_as_of += float(inc["amount"])
        if end is None:
            open_ended = True
        else:
            to_s = str(end)[:10]
            # Last calendar year overlapping [from, to). to on Jan 1 of Y
            # means the last overlapping year is Y-1 (half-open).
            last_y = int(to_s[:4]) - (0 if to_s[5:10] > "01-01" else 1)
            closed_future_ends.append(last_y)

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


# ── Plan resolution + one-call person estimate (issue #364 refactor) ────────
#
# The contract adapter (``contract_people.py``) used to OWN the CPP policy:
# which plan a province runs, how the age-65 base and the CPP2 tier aggregate
# into one monthly figure, what the provenance vocabulary is, and where the
# contributory period ends when a household models an early retirement. All
# four are program facts, so they live HERE (DP#10) and the adapter makes a
# single call that it maps.

#: Claim age the estimate is stored at. Issue #388: the adapter stores the
#: age-65 figure and the engine applies any start-age adjustment once, from
#: ``cpp_start_age``.
DEFAULT_CLAIM_AGE = 65

#: Provinces whose contributory plan is the Quebec Pension Plan rather than
#: the federal CPP. Keyed on the contract's ``residency.province`` value,
#: case-insensitively. Adding a province is a data edit here, not a code
#: change in the ingestion layer.
QPP_PROVINCES = frozenset({"quebec"})

#: Provenance labels the estimator assigns. ``statement`` is NOT one of them:
#: it is written by the adapter when a Service Canada / Retraite Québec
#: Statement supplied the amount, and the estimator never sees one.
SOURCE_FROM_EARNINGS_HISTORY = "estimated_from_earnings_history"
SOURCE_FROM_INCOMES = "estimated_from_incomes"


@dataclass(frozen=True)
class CPPEstimateRefusal:
    """One reason the estimator cannot answer for a person.

    Carried as DATA rather than raised: the caller owns the exception type its
    layer speaks in (``ContractAdaptationError`` in the ingestion layer). What
    it must not do is decide *whether* the answer exists — that is this
    module's call (DP#32: absence fails loudly, and the loudness is the
    domain's to declare).
    """

    reason: str
    #: The absent input, in this module's vocabulary. A caller that reads a
    #: different document names where its own document DECLARES that input;
    #: it does not re-derive the reason.
    missing: str
    #: Why the answer is impossible — the part that must survive into the
    #: caller's exception (DP#32: a refusal is a feature, and a paraphrase
    #: of one is not).
    detail: str


@dataclass(frozen=True)
class CPPPersonEstimate:
    """The estimator's whole answer for one person.

    ``refusals`` non-empty means ``monthly`` and ``source`` are NOT answers —
    the caller must raise rather than map a plausible number.
    """

    plan: str
    monthly: float
    source: str
    earnings: List[EarningsEntry]
    refusals: List[CPPEstimateRefusal]


def _refusal(reason: str, missing: str, detail: str) -> CPPPersonEstimate:
    """A refusal-shaped result. ``plan`` is empty: no plan was determined."""
    return CPPPersonEstimate(
        plan="", monthly=0.0, source="", earnings=[],
        refusals=[CPPEstimateRefusal(
            reason=reason, missing=missing, detail=detail,
        )],
    )


def resolve_plan(province: str) -> str:
    """Which contributory plan a province runs: ``"qpp"`` or ``"cpp"``."""
    return "qpp" if str(province).strip().lower() in QPP_PROVINCES else "cpp"


def contributory_end_age(earliest_retirement_age: Optional[int] = None) -> int:
    """Age the contributory series stops at.

    Age 65 in general; earlier when the household models a retirement before
    it. ``None`` means "no modeled retirement for this person", which is an
    ABSENCE of a constraint, not a constraint of 65 spelled differently —
    both branches agree, so the two are stated once here.
    """
    if earliest_retirement_age is None:
        return DEFAULT_CLAIM_AGE
    return min(DEFAULT_CLAIM_AGE, int(earliest_retirement_age))


def estimate_person_cpp(
    *,
    province: Optional[str],
    birth_year: Optional[int],
    earnings_history: Optional[Sequence[Mapping]] = None,
    incomes: Optional[Sequence[Mapping]] = None,
    salary_growth: float = 0.0,
    as_of_year: int,
    as_of_date: Optional[str] = None,
    earliest_retirement_age: Optional[int] = None,
) -> CPPPersonEstimate:
    """Answer "what is this person's monthly contributory benefit at 65?".

    The one call the ingestion layer makes (DP#10). It owns, in order:
    the plan the province runs, the end of the contributory period, the
    earnings series, the base+CPP2 aggregation, and the provenance label.

    Refuses — loudly, with a reason — on two absences the caller cannot
    paper over: no real birth year to date the series against (DP#1) and no
    province to select a plan from. An empty earnings series is NOT a
    refusal: it is an answer of zero with a stated provenance, because the
    caller already decided the person has a pensionable source.
    """
    if birth_year is None:
        return _refusal(
            "missing_birth_year",
            "birth_year",
            "the estimator needs a real birth year to date the member; "
            "refusing rather than inventing a fabricated birth year",
        )
    if province is None or not str(province).strip():
        return _refusal(
            "missing_province",
            "province",
            "Quebec residency selects QPP max-benefit tables; refusing "
            "rather than guessing a plan",
        )

    plan = resolve_plan(province)
    entries = build_earnings_for_estimate(
        earnings_history=earnings_history,
        incomes=incomes,
        salary_growth=salary_growth,
        as_of_year=as_of_year,
        birth_year=birth_year,
        end_age=contributory_end_age(earliest_retirement_age),
        as_of_date=as_of_date,
    )
    if not entries:
        # No series to estimate from: the honest answer is zero, and the
        # provenance still travels so "estimated zero" stays
        # distinguishable from "never estimated" (issue #390). Not a refusal
        # -- the caller already established the person has a pensionable
        # source; there is simply nothing projectable to build a series from.
        return CPPPersonEstimate(
            plan=plan,
            monthly=0.0,
            source=(SOURCE_FROM_EARNINGS_HISTORY if earnings_history
                    else SOURCE_FROM_INCOMES),
            earnings=[],
            refusals=[],
        )

    # Age-65 convention (issue #388): always request the age-65 figure and
    # let the engine apply the start-age adjustment once, from
    # ``cpp_start_age``. The CPP2 tier is a SEPARATE component and is summed,
    # never discarded.
    estimate = compute_benefit_estimate(entries, start_age=DEFAULT_CLAIM_AGE,
                                        plan=plan)
    return CPPPersonEstimate(
        plan=plan,
        monthly=age_65_monthly_total(estimate),
        source=(SOURCE_FROM_EARNINGS_HISTORY if earnings_history
                else SOURCE_FROM_INCOMES),
        earnings=entries,
        refusals=[],
    )


def age_65_monthly_total(estimate: CPPBenefitEstimate) -> float:
    """The ONE aggregation rule: base tier at 65 plus the CPP2 tier at 65.

    CPP2 is a SEPARATE component with its own ceiling, so summing the two
    age-65 fields is the whole definition of "this person's monthly
    contributory benefit at 65". It lived inline in two call sites
    (``contract_people.py`` and ``countries/canada/retirement.py``), which is
    how the two drifted apart. Stated once, here, in the module that owns
    the tiers (DP#10).
    """
    return estimate.age_65_monthly + estimate.cpp2_age_65_monthly
