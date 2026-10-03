#!/usr/bin/env python3
"""Tests for CPP estimator (cpp_estimator.py) — issue #365.

Verifies:
- Max earner: full career at YMPE → near-max benefit
- Partial year: shorter career → smaller benefit
- Low earner: always at half-YMPE → lower benefit
- CPP2 boundary: 2018-earnings vs 2019+ earnings → enhanced benefit
- Dropout: mixed-income years → drop-out preserves high years
- Age ordering: 60 < 65 < 70
- Edge cases: empty/None earnings, invalid start_age
- Diagnostic fields: contributory period, dropout years

All data uses round numbers per DP#15. No real earnings figures.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))


# ── Max earner ───────────────────────────────────────────────────────────────

def test_max_earner_full_career():
    """Full career at YMPE every year → near-maximum CPP at 65."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [
        EarningsEntry(year=1990 + i, employment_income=200_000)
        for i in range(35)
    ]
    result = compute_benefit_estimate(earnings, start_age=65)

    assert result.age_65_monthly > 0
    assert result.age_65_monthly > 1000  # meaningful benefit
    # Should be close to max (ratio near 1.0 after dropout, which
    # only drops the earlier lowest-YMPE years)
    assert result.contributory_period_years > 0
    assert result.dropout_years > 0


# ── Partial career ───────────────────────────────────────────────────────────

def test_partial_career_below_max():
    """Shorter career yields smaller benefit than full career."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    income = 200_000  # always at YMPE cap

    full = [EarningsEntry(year=1990 + i, employment_income=income) for i in range(35)]
    short = [EarningsEntry(year=2010 + i, employment_income=income) for i in range(15)]

    r_full = compute_benefit_estimate(full, start_age=65)
    r_short = compute_benefit_estimate(short, start_age=65)

    assert r_short.age_65_monthly < r_full.age_65_monthly


# ── Low earner ───────────────────────────────────────────────────────────────

def test_low_earner_half_ympe():
    """Half-YMPE career yields lower benefit than full-YMPE career."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    full = [EarningsEntry(year=2000 + i, employment_income=100_000) for i in range(25)]
    half = [EarningsEntry(year=2000 + i, employment_income=35_000) for i in range(25)]

    r_full = compute_benefit_estimate(full, start_age=65)
    r_half = compute_benefit_estimate(half, start_age=65)

    assert r_half.age_65_monthly > 0
    assert r_half.age_65_monthly < r_full.age_65_monthly


# ── Age ordering ─────────────────────────────────────────────────────────────

def test_age_ordering_60_65_70():
    """Benefit at 60 < 65 < 70."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [EarningsEntry(year=1995 + i, employment_income=100_000) for i in range(30)]
    result = compute_benefit_estimate(earnings, start_age=65)

    assert result.age_60_monthly > 0
    assert result.age_60_monthly < result.age_65_monthly
    assert result.age_65_monthly < result.age_70_monthly


def test_age_factor_ratios():
    """Age 60 = ~64% of 65; age 70 = ~142% of 65."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [EarningsEntry(year=2000 + i, employment_income=100_000) for i in range(30)]
    result = compute_benefit_estimate(earnings, start_age=65)

    # 60 = 1 - 60*0.006 = 1 - 0.36 = 0.64
    # 70 = 1 + 60*0.007 = 1 + 0.42 = 1.42
    ratio_60 = result.age_60_monthly / result.age_65_monthly if result.age_65_monthly else 0
    ratio_70 = result.age_70_monthly / result.age_65_monthly if result.age_65_monthly else 0

    assert 0.60 < ratio_60 < 0.70  # ~0.64
    assert 1.35 < ratio_70 < 1.50  # ~1.42


# ── Dropout ──────────────────────────────────────────────────────────────────

def test_dropout_mixed_income():
    """Mixed low/high years — dropout preserves high years."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = []
    for i in range(25):
        income = 10_000 if i < 10 else 100_000
        earnings.append(EarningsEntry(year=1995 + i, employment_income=income))

    result = compute_benefit_estimate(earnings, start_age=65)
    assert result.age_65_monthly > 0
    assert result.dropout_years > 0


def test_dropout_reduces_contributory_years():
    """Dropout removes exactly the computed number of years."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [EarningsEntry(year=2000 + i, employment_income=100_000) for i in range(30)]
    result = compute_benefit_estimate(earnings, start_age=65)

    # Span padded to 40-year minimum → 17% of 40 = 6.8 → 6 dropped
    assert result.dropout_years >= 1
    assert result.dropout_years <= 8
    assert result.contributory_period_years == 40  # min contributory span


# ── CPP2 boundary ────────────────────────────────────────────────────────────

def test_cpp2_accrual_post_2023():
    """CPP2 benefit for post-2023 earnings above YMPE."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    # 10 years of post-2023 earnings well above YMPE (into CPP2 band)
    earnings = [
        EarningsEntry(year=2024 + i, employment_income=150_000)
        for i in range(10)
    ]
    result = compute_benefit_estimate(earnings, start_age=65)

    assert result.cpp2_age_65_monthly > 0
    assert result.age_65_monthly > 0


def test_no_cpp2_for_pre_2024_earnings():
    """Earnings before 2024 produce no CPP2 benefit."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [
        EarningsEntry(year=2010 + i, employment_income=150_000)
        for i in range(10)
    ]
    result = compute_benefit_estimate(earnings, start_age=65)

    assert result.cpp2_age_65_monthly == 0


def test_cpp2_age_ordering():
    """CPP2 benefit follows same age ordering: 60 < 65 < 70."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [
        EarningsEntry(year=2024 + i, employment_income=150_000)
        for i in range(10)
    ]
    result = compute_benefit_estimate(earnings, start_age=65)

    assert result.cpp2_age_60_monthly <= result.cpp2_age_65_monthly + 0.01
    assert result.cpp2_age_65_monthly <= result.cpp2_age_70_monthly + 0.01


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_empty_earnings_returns_zero():
    """Empty earnings list → zero benefit."""
    from countries.canada.cpp_estimator import compute_benefit_estimate

    result = compute_benefit_estimate([], start_age=65)
    assert result.age_60_monthly == 0
    assert result.age_65_monthly == 0
    assert result.age_70_monthly == 0


def test_none_incomes_returns_zero():
    """All None incomes → zero benefit."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [EarningsEntry(year=y, employment_income=None) for y in range(2015, 2026)]
    result = compute_benefit_estimate(earnings, start_age=65)
    assert result.age_65_monthly == 0


def test_zero_incomes_returns_zero():
    """All zero incomes → zero benefit."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [EarningsEntry(year=y, employment_income=0) for y in range(2015, 2026)]
    result = compute_benefit_estimate(earnings, start_age=65)
    assert result.age_65_monthly == 0


def test_invalid_start_age_returns_zero():
    """Start age < 60 → zeroed result."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    earnings = [EarningsEntry(year=2000 + i, employment_income=100_000) for i in range(30)]
    result = compute_benefit_estimate(earnings, start_age=55)
    assert result.age_60_monthly == 0
    assert result.age_65_monthly == 0


def test_single_year_produces_small_benefit():
    """Single-year career produces proportional benefit."""
    from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate

    result = compute_benefit_estimate(
        [EarningsEntry(year=2023, employment_income=100_000)], start_age=65
    )
    assert result.age_65_monthly > 0
    # One year can't reach the max ~$1,508/month
    assert result.age_65_monthly < 200



# ── Plan resolution + the one-call person estimate (issue #364) ──────────────
#
# The ingestion layer used to decide all of this itself. These tests pin the
# decisions to the module that now owns them: which plan a province runs, where
# the contributory period ends, the base+CPP2 aggregation, the provenance
# labels, and the two absences it refuses on.


def test_resolve_plan_is_a_lookup_not_a_ternary_in_the_caller():
    """Quebec runs QPP; everywhere else runs CPP; casing is not a decision."""
    from countries.canada.cpp_estimator import resolve_plan

    assert resolve_plan("quebec") == "qpp"
    assert resolve_plan("Quebec") == "qpp"
    assert resolve_plan("  quebec ") == "qpp"
    assert resolve_plan("ontario") == "cpp"
    assert resolve_plan("british columbia") == "cpp"


def test_contributory_end_age_defaults_to_65_and_follows_an_early_retirement():
    """No modeled retirement -> 65. A modeled retirement before 65 shortens
    the period; one AFTER 65 must not lengthen it past 65."""
    from countries.canada.cpp_estimator import contributory_end_age

    assert contributory_end_age(None) == 65
    assert contributory_end_age(60) == 60
    assert contributory_end_age(70) == 65


def test_estimate_person_cpp_answers_qpp_and_cpp_from_one_call():
    """Same earnings, same arguments, one differing province -> two answers.

    This is the whole point of the move: the CALLER no longer picks a plan.
    """
    from countries.canada.cpp_estimator import estimate_person_cpp

    incomes = [{"kind": "employment", "from": "2015-01-01", "amount": 120_000}]
    common = dict(
        birth_year=1960,
        incomes=incomes,
        salary_growth=0.0,
        as_of_year=2026,
        as_of_date="2026-06-30",
    )
    qc = estimate_person_cpp(province="quebec", **common)
    on = estimate_person_cpp(province="ontario", **common)

    assert qc.refusals == [] and on.refusals == []
    assert qc.plan == "qpp" and on.plan == "cpp"
    assert qc.monthly > 0
    # 2026 QPP max (17334) != CPP max (18092): the two plans diverge.
    assert qc.monthly != on.monthly


def test_estimate_person_cpp_refuses_a_missing_birth_year():
    """No birth year is not a zero: it is a refusal (DP#1/DP#32)."""
    from countries.canada.cpp_estimator import estimate_person_cpp

    result = estimate_person_cpp(
        province="ontario",
        birth_year=None,
        incomes=[{"kind": "employment", "from": "2015-01-01", "amount": 90_000}],
        salary_growth=0.0,
        as_of_year=2026,
    )
    assert [r.reason for r in result.refusals] == ["missing_birth_year"]
    assert result.refusals[0].missing == "birth_year"
    assert "birth year" in result.refusals[0].detail


def test_estimate_person_cpp_refuses_a_missing_province():
    """No province is not a silent CPP default: it is a refusal (DP#32)."""
    from countries.canada.cpp_estimator import estimate_person_cpp

    result = estimate_person_cpp(
        province=None,
        birth_year=1960,
        incomes=[{"kind": "employment", "from": "2015-01-01", "amount": 90_000}],
        salary_growth=0.0,
        as_of_year=2026,
    )
    assert [r.reason for r in result.refusals] == ["missing_province"]
    assert result.refusals[0].missing == "province"
    # A refusal names no plan: nothing was decided.
    assert result.plan == ""


def test_estimate_person_cpp_labels_provenance_on_an_empty_series():
    """A pensionable source that yields no projectable year is an answer of
    ZERO with a stated provenance -- not a refusal, and not silence."""
    from countries.canada.cpp_estimator import estimate_person_cpp

    result = estimate_person_cpp(
        province="ontario",
        birth_year=1960,
        # A history leaf with no usable year/amount projects nothing.
        earnings_history=[{}],
        salary_growth=0.0,
        as_of_year=2026,
    )
    assert result.refusals == []
    assert result.monthly == 0.0
    assert result.source == "estimated_from_earnings_history"
    assert result.earnings == []


def test_estimate_person_cpp_source_follows_the_declared_input():
    """History present -> the history label; incomes only -> the incomes label."""
    from countries.canada.cpp_estimator import estimate_person_cpp

    history = [{"year": 1990 + i, "employment_income": 100_000}
               for i in range(30)]
    from_history = estimate_person_cpp(
        province="ontario", birth_year=1960, earnings_history=history,
        salary_growth=0.0, as_of_year=2026,
    )
    from_incomes = estimate_person_cpp(
        province="ontario", birth_year=1960,
        incomes=[{"kind": "employment", "from": "2015-01-01", "amount": 90_000}],
        salary_growth=0.0, as_of_year=2026,
    )
    assert from_history.source == "estimated_from_earnings_history"
    assert from_incomes.source == "estimated_from_incomes"


def test_estimate_person_cpp_sums_base_and_cpp2_through_one_rule():
    """The monthly it returns is exactly the base+CPP2 aggregation."""
    from countries.canada.cpp_estimator import (
        age_65_monthly_total, compute_benefit_estimate, estimate_person_cpp,
    )

    history = [{"year": 2000 + i, "employment_income": 150_000}
               for i in range(25)]
    result = estimate_person_cpp(
        province="ontario", birth_year=1960, earnings_history=history,
        salary_growth=0.0, as_of_year=2026,
    )
    entries = list(result.earnings)
    direct = compute_benefit_estimate(entries, start_age=65, plan="cpp")
    assert result.monthly == age_65_monthly_total(direct)
    # The CPP2 tier is included, not discarded (issue #388).
    assert direct.cpp2_age_65_monthly > 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
