"""#415: ONE fixture, TWO readers -- they must agree, and must keep agreeing.

Four sites read the same declared ``[from, to)`` income window and disagreed
about what it meant, with nothing cross-checking them because the arithmetic
was inlined four times, four ways:

  1. ``countries/canada/cpp_estimator.build_earnings_for_estimate`` -- admitted a
     whole CALENDAR YEAR on any overlap, crediting ~198% of the declared annual
     rate for a job that started on July 1.
  2. ``contract_people._active_employment_income`` -- ``inc["to"] < as_of``, so
     an income whose ``to`` IS ``as_of`` stayed ACTIVE and the fold grew that
     salary across the whole horizon (measured: still paying it in 2035).
  3. ``simulation._income_components_for_year`` -- day-count blending, which
     ``countries/canada/earned_income.py`` already names as the more correct
     approach.
  4. ``countries/canada/pre_designation.period_years`` -- INCLUSIVE ``to``.
     Deliberately NOT unified; see that module's docstring and this file's
     ``test_pre_designation_is_documented_as_deliberately_not_unified``.

The fix was not four patches; it was ``income_window.py`` -- one module owning
half-open ``[from, to)`` with day-count overlap as the primitive. These tests
are the enforcement half. Without them the next reader who inlines the
arithmetic again reproduces the original bug silently, and the ~6,600-test suite
would not notice: the four readers are called from different layers, so no
existing test ever puts two of them side by side on the same fixture.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import income_window
from contract_people import _active_employment_income
from countries.canada.cpp_estimator import build_earnings_for_estimate
from countries.canada.pre_designation import period_years
from simulation import (
    _income_components_for_year,
    _self_employment_income_for_year,
)

# ── The one fixture every reading below is taken from ────────────────────────
#
# A $100,000/yr employment income running 2018-07-01 -> 2026-03-31, snapped at
# 2026-06-30. Round numbers, a role name, no personal data (DP#4/#15).
AS_OF = "2026-06-30"
FROM = "2018-07-01"
TO = "2026-03-31"
RATE = 100_000.0

INCOME = {"id": "primary_employment", "kind": "employment",
          "amount": RATE, "from": FROM, "to": TO}
PERSON = {"id": "primary", "incomes": [INCOME]}


def _cpp_estimate(income=INCOME, as_of=AS_OF):
    return {e.year: e.employment_income for e in build_earnings_for_estimate(
        incomes=[income], salary_growth=0.0, as_of_year=int(as_of[:4]),
        birth_year=1970, end_age=65, as_of_date=as_of)}


def _fold_year(calendar_year, income=INCOME):
    """What the FOLD pays this person in ``calendar_year``, driven through the
    engine's own day-count path with no base scalar (segments only)."""
    total, earned = _income_components_for_year(
        0.0, [dict(income)], calendar_year, 0.0, 0)
    return total, earned


# ── THE CROSS-CHECK ──────────────────────────────────────────────────────────
# One fixture. Two readings of the SAME window, in the SAME year. If these
# diverge, the day-count arithmetic has been re-inlined somewhere and one of
# the two sites has drifted -- which is exactly the failure #415 shipped.

@pytest.mark.parametrize("calendar_year", [2018, 2019, 2020, 2025, 2026])
def test_cpp_estimator_and_fold_credit_the_same_year_the_same_amount(calendar_year):
    """Readers 1 and 3, side by side, on one fixture.

    Before #415 these disagreed by up to 2x: the fold credited 50,410.96 for
    2018 (184 days of a 365-day year) and the CPP overlay credited the full
    100,000, because a calendar year it merely *touched* was enough to earn the
    whole annual rate.
    """
    cpp_year = _cpp_estimate().get(calendar_year, 0.0)
    fold_year, _earned = _fold_year(calendar_year)

    assert fold_year == pytest.approx(cpp_year), (
        f"{calendar_year}: fold reads {fold_year:,.2f}, CPP estimator reads "
        f"{cpp_year:,.2f}. Both are answering 'how much of the "
        f"{FROM} -> {TO} window lands in {calendar_year}' and must be the "
        f"same number (issue #415)."
    )


@pytest.mark.parametrize("calendar_year", [2018, 2019, 2020, 2025, 2026])
def test_both_readings_equal_the_shared_primitive(calendar_year):
    """Not merely 'the two agree' -- 'they agree because they both ask
    income_window'. Pins the SOURCE of the agreement, so re-inlining the
    arithmetic at either site fails even if the other site happened to match.
    """
    expected = RATE * income_window.year_fraction(
        income_window.parse_date(FROM), income_window.parse_date(TO),
        calendar_year)

    assert _fold_year(calendar_year)[0] == pytest.approx(expected)
    assert _cpp_estimate().get(calendar_year, 0.0) == pytest.approx(expected)


def test_a_part_year_is_never_credited_a_whole_years_rate():
    """The 1.98x bug, stated as its own assertion so it cannot come back
    wearing a different fixture."""
    partial = _cpp_estimate()[2018]
    assert partial < RATE, (
        f"2018 covered only 184 of 365 days but was credited {partial:,.2f} "
        f"of the {RATE:,.0f} annual rate -- the declared `amount` is an ANNUAL "
        f"RATE, not a per-year total (issue #415).")
    assert partial == pytest.approx(RATE * 184 / 365)


# ── The boundary instant ────────────────────────────────────────────────────
# The sharpest half-open consequence, and the one #415's reader 2 got backwards.

@pytest.mark.parametrize("to_iso, as_of, expected_active", [
    ("2026-03-30", "2026-03-28", True),   # comfortably inside
    ("2026-03-30", "2026-03-29", True),   # the last day the window covers
    ("2026-03-30", "2026-03-30", False),  # `to` IS as_of: half-open, job OVER
    ("2026-03-31", "2026-03-30", True),   # still one day inside
    ("2026-03-31", "2026-03-31", False),  # `to` IS as_of again
    ("2026-03-31", "2026-04-01", False),  # past the end
])
def test_income_ending_on_as_of_is_over_on_as_of(to_iso, as_of, expected_active):
    """`from <= on < to`. An income whose `to` equals the snapshot date does
    not cover that date -- which is what the contract's own `[from, to)`
    notation in schema/defs/people.json means."""
    income = dict(INCOME, to=to_iso)
    active = _active_employment_income({"id": "primary", "incomes": [income]}, as_of)
    assert (active > 0.0) is expected_active


def test_the_fold_does_not_grow_a_job_that_ended_on_the_snapshot_date():
    """Reader 2 feeds reader 3. Before #415 this paid $100,000/yr in 2035 for a
    job that ended 2026-03-31, because `inc["to"] < as_of` let a job ending
    ON as_of stay active and the fold grew that scalar across the horizon."""
    ended_on_as_of = dict(INCOME, to=AS_OF)
    base = _active_employment_income(
        {"id": "primary", "incomes": [ended_on_as_of]}, AS_OF)
    assert base == 0.0

    for year in (2027, 2030, 2035):
        income_2035, _ = _income_components_for_year(base, [], year, 0.0, 0)
        assert income_2035 == 0.0, (
            f"fold paid {income_2035:,.2f} in {year} for a job that ended "
            f"{AS_OF} (issue #415).")


def test_cpp_estimator_agrees_the_boundary_job_is_over():
    """Reader 1 on the same instant. `active_at_as_of` seeds the projection; a
    job that ended on the snapshot date must not seed it."""
    ended_on_as_of = dict(INCOME, to=AS_OF)
    series = build_earnings_for_estimate(
        incomes=[ended_on_as_of], salary_growth=0.0, as_of_year=2026,
        birth_year=1970, end_age=65, as_of_date=AS_OF)
    by_year = {e.year: e.employment_income for e in series}

    # 2026 is credited the days actually worked (Jan 1 -> Jun 29, `to`
    # exclusive). Nothing is grown off an ACTIVE base: `active_at_as_of` is 0,
    # so the open-ended-salary projection branch never fires and the years past
    # 2026 come from the documented fallback (last known series year, grown at
    # the same rate -- here growth is 0, so they simply repeat 2026).
    expected_2026 = RATE * income_window.overlap_days(
        income_window.parse_date(FROM), income_window.parse_date(AS_OF), 2026) / 365
    assert by_year[2026] == pytest.approx(expected_2026)
    later = {y: v for y, v in by_year.items() if y > 2026}
    assert later, "the documented fallback still projects past the last known year"
    assert all(v == pytest.approx(expected_2026) for v in later.values())
    # The decisive assertion: the projection is NOT seeded from an active
    # income. Before #415 the boundary read `to < as_of`, this income stayed
    # active in the fold, and the two sites disagreed on the same instant.
    assert _active_employment_income(
        {"id": "primary", "incomes": [ended_on_as_of]}, AS_OF) == 0.0


# ── The primitive itself ─────────────────────────────────────────────────────

@pytest.mark.parametrize("from_iso, to_iso, year, expected_days", [
    ("2018-07-01", "2026-03-31", 2018, 184),   # Jul 1 -> Jan 1
    ("2018-07-01", "2026-03-31", 2025, 365),   # a fully covered year
    ("2018-07-01", "2026-03-31", 2026, 89),    # Jan 1 -> Mar 30 (`to` excl.)
    ("2018-07-01", "2026-03-31", 2017, 0),     # entirely before
    ("2018-07-01", "2026-03-31", 2030, 0),     # entirely after
    ("2018-01-01", "2019-01-01", 2018, 365),   # `to` on Jan 1 excludes 2019
    ("2019-01-01", "2019-01-01", 2019, 0),     # empty window: touches nothing
])
def test_overlap_days_is_the_primitive(from_iso, to_iso, year, expected_days):
    got = income_window.overlap_days(
        income_window.parse_date(from_iso), income_window.parse_date(to_iso), year)
    assert got == expected_days


def test_open_ended_window_runs_forward_forever():
    """`to = None` is the schema's EXPLICIT "no known end", not a missing value
    coerced to a default (DP#32). It supplies no upper bound of its own; the
    caller's year does."""
    start = income_window.parse_date(FROM)
    for year in (2018, 2050, 2200):
        assert income_window.overlap_days(start, None, year) == \
            income_window.overlap_days(start, None, year)
        assert income_window.overlaps_year(start, None, year) is True
    assert income_window.covers_date(start, None, date(2200, 1, 1)) is True


def test_two_touching_windows_share_no_day():
    """`[a, b)` and `[b, c)` are disjoint. This is the whole content of the
    half-open convention and the thing #415's readers each got differently."""
    a = date(2018, 7, 1)
    b = date(2020, 1, 1)
    c = date(2022, 1, 1)
    assert income_window.overlap_days_between(a, b, b, c) == 0
    assert income_window.covers_date(a, b, b) is False
    assert income_window.covers_date(a, b, b - timedelta(days=1)) is True


def test_year_fraction_is_the_share_of_the_year():
    frac = income_window.year_fraction(
        income_window.parse_date(FROM), income_window.parse_date(TO), 2018)
    assert frac == pytest.approx(184 / 365)
    assert 0.0 <= frac <= 1.0


def test_parse_date_refuses_rather_than_defaults():
    """A malformed date is loud, never silently coerced to something plausible
    (DP#32)."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        income_window.parse_date("not-a-date")


def test_resolve_window_parses_the_stored_iso_pair():
    window_from, window_to = income_window.resolve_window(FROM, TO)
    assert window_from == date(2018, 7, 1)
    assert window_to == date(2026, 3, 31)
    assert income_window.resolve_window(FROM, None)[1] is None


# ── The self-employment slice reads the same days as the total ───────────────

@pytest.mark.parametrize("year", [2018, 2026])
def test_self_employment_slice_uses_the_same_days_as_total_income(year):
    """`_self_employment_income_for_year` is the second inlined copy #415 found.
    It must not be able to disagree with the first about the days."""
    seg = {"kind": "self_employment", "amount": RATE,
           "from": FROM, "to": TO, "expenses_annual": 0.0}
    total, _ = _income_components_for_year(0.0, [seg], year, 0.0, 0)
    slice_ = _self_employment_income_for_year(0.0, [seg], year, 0.0, 0)
    assert slice_ == pytest.approx(total)


# ── The deliberate NON-unification, pinned so it stays deliberate ────────────

def test_pre_designation_is_documented_as_deliberately_not_unified():
    """#415 does NOT route pre_designation through income_window. A PRE
    designation is a whole-YEAR election (ITA s.40(2)(b) counts designated
    YEARS) and its `to = None` means 'still designated as of as_of_year' --
    it STOPS at as_of_year, where an open-ended income window runs forever.

    This test exists so the choice stays a choice. If someone unifies it, the
    inclusive range below stops matching and they must go back to #415 and
    decide the exemption-fraction consequence deliberately, not by accident.
    """
    period = {"from": "2018-07-01", "to": "2026-01-01"}
    assert period_years(period, 2026) == set(range(2018, 2027))
    # Under the income window's half-open reading, 2026 is NOT covered --
    # that divergence is the whole reason this site stays out of #415.
    assert income_window.overlaps_year(
        income_window.parse_date("2018-07-01"),
        income_window.parse_date("2026-01-01"), 2026) is False