"""Unit tests for `retirement_analysis.py`'s pure helpers.

This module was at ZERO coverage -- nothing in the suite imported it. It is a CLI
script, but it is not *only* a CLI script: `net_worth`, `liquid`, `longevity` and
`money` are pure functions over a YearResult, and pure functions are exactly what
DP#3 says should be unit-tested. So it comes off the zero-coverage list by being
tested, not by being allowlisted.

Fabricated round numbers and role-based naming throughout (DP#4, DP#15).
"""

import importlib
from dataclasses import dataclass

import pytest

ra = importlib.import_module("retirement_analysis")


@dataclass
class FakeYear:
    """Minimal stand-in for a YearResult: only the fields these helpers read."""

    total_assets: float = 0.0
    total_debt: float = 0.0
    total_rrsp: float = 0.0
    total_tfsa: float = 0.0
    non_reg_balance: float = 0.0
    lif_balance: float = 0.0
    lira_balance: float = 0.0


def test_net_worth_is_assets_minus_debt():
    assert ra.net_worth(FakeYear(total_assets=800_000, total_debt=300_000)) == 500_000


def test_net_worth_can_be_negative_rather_than_clamped_to_zero():
    """DP#32: a household that is underwater must be REPRESENTABLE. Clamping a negative
    net worth to zero is the silent-substitution bug this codebase exists to prevent."""
    assert ra.net_worth(FakeYear(total_assets=100_000, total_debt=250_000)) == -150_000


def test_liquid_sums_the_drawable_accounts():
    y = FakeYear(
        total_rrsp=200_000,
        total_tfsa=100_000,
        non_reg_balance=50_000,
        lif_balance=30_000,
        lira_balance=20_000,
    )
    assert ra.liquid(y) == 400_000


def test_liquid_excludes_the_house():
    """The house is not a drawable asset -- it must not leak into `liquid` via
    total_assets. A household with a large house and no savings has zero drawable."""
    y = FakeYear(total_assets=1_000_000)  # house-heavy, no financial accounts
    assert ra.liquid(y) == 0.0


def test_money_formats_as_dollars_with_thousands_separators():
    # `money` right-aligns inside a 13-wide field, so the padding sits between the
    # '$' and the digits: "$    1,234,567". Compare on the non-padding content.
    assert ra.money(1_234_567).replace(" ", "") == "$1,234,567"


def test_money_renders_zero_as_zero_not_blank():
    """Zero is a value, not an absence (DP#32). A formatter that renders 0 as an empty
    cell is how a real zero becomes indistinguishable from missing data in a report."""
    assert ra.money(0).replace(" ", "") == "$0"


# ── longevity() ─────────────────────────────────────────────────────────────
# Issue #756: longevity() used to hardcode birth_year=1979, so every age it
# computed was wrong for any household not born in 1979. The `age >= retire_age`
# gate could skip the whole scan and report a household that runs out of money
# as fine. These tests pin the fix: age is date-computed from a required
# birth_year, and absence fails loudly (DP#1/DP#32).

def _year(i, liquid_amount):
    return FakeYear(
        total_assets=liquid_amount,
        total_rrsp=liquid_amount,
    )


def test_longevity_computes_age_from_birth_year_not_a_constant():
    """The age used by longevity() must track the supplied birth_year, not 1979.

    A household born in 1955 reaches retire_age 60 in 2015. With the old
    hardcoded 1979, 2015 would compute as age 36 -- below retire_age, so the
    run-out year would be skipped entirely. Pin a birth_year that differs from
    1979 and assert the scan keys off the correct age."""
    # start_year 2015, retire_age 60, birth_year 1955 -> age 60 in year 0.
    results = [_year(0, 500_000), _year(1, 0.0)]  # year 1 (age 61): liquid gone
    lasts, ran_out, _term = ra.longevity(results, start_year=2015,
                                         retire_age=60, birth_year=1955)
    assert not lasts
    assert ran_out == 61  # age 61 = (2015 + 1) - 1955


def test_longevity_reports_lasts_when_assets_never_run_out():
    results = [_year(0, 500_000), _year(1, 400_000), _year(2, 300_000)]
    lasts, ran_out, term = ra.longevity(results, start_year=2026,
                                        retire_age=60, birth_year=1970)
    assert lasts is True
    assert ran_out is None
    assert term == 300_000


def test_longevity_ignores_run_out_before_retirement_age():
    """A year that is liquid <= 1000 BEFORE retire_age must not register as the
    run-out age -- the gate is `age >= retire_age and liquid <= 1000`."""
    # birth_year 2000, start_year 2026 -> age 26 in year 0. retire_age 60.
    # Year 0 (age 26) is below retire_age even though liquid is ~0.
    results = [_year(0, 0.0), _year(1, 500_000)]
    lasts, ran_out, _term = ra.longevity(results, start_year=2026,
                                         retire_age=60, birth_year=2000)
    assert lasts is True
    assert ran_out is None


def test_longevity_returns_terminal_liquid_of_last_year():
    results = [_year(0, 100_000), _year(1, 42_000)]
    _lasts, _ran, term = ra.longevity(results, start_year=2026,
                                      retire_age=60, birth_year=1960)
    assert term == 42_000


def test_longevity_requires_birth_year_absence_must_fail_loudly():
    """DP#32: a missing birth_year must raise, not default to a plausible
    person. This is the exact regression #756 is about -- the old code silently
    substituted 1979 and produced a confident wrong age."""
    results = [_year(0, 500_000)]
    for bad in (None, 0):
        with pytest.raises(ValueError, match="birth_year is required"):
            ra.longevity(results, start_year=2026, retire_age=60, birth_year=bad)


def test_longevity_does_not_hardcode_1979():
    """The defining regression test for #756: a household born in 1960 must be
    evaluated at the 1960-derived age, not the 1979-derived age. With the old
    constant, a run-out at calendar year 2026 (age 66 for a 1960 birth, but
    age 47 under the hardcoded 1979) would have been below the retire_age=60
    gate and skipped -- reporting `lasts` when the money had in fact run out
    in retirement."""
    # start_year 2026, birth_year 1960, retire_age 60 -> age 66 in year 0.
    # Year 0 is already retired (66 >= 60) and liquid is gone -> runs out at 66.
    results = [_year(0, 0.0)]
    lasts, ran_out, _term = ra.longevity(results, start_year=2026,
                                         retire_age=60, birth_year=1960)
    assert not lasts
    assert ran_out == 66
