"""Issue #375 (foundation): a retired member can still earn wages.

## The silent zero this removes

The fold zeroed a retired member's employment income outright::

    primary_income_pre, spouse_income_pre = primary_income, spouse_income
    if p_retired:
        primary_income = 0.0

That is a silent zero in the repo's founding sense: not a modelling choice, but
an income the engine *assumed away* and then reported numbers around. It forbids
the most common real retirement transition there is — stop the career job and
take a lighter one, or go back to work for a few years — and it is why #375's
supplement could never accrue: its base, post-retirement earnings above the
exemption, is identically zero for every member.

## What replaces it

A retired member's earnings are taken **only** from an explicit dated
declaration: an ``income_segment`` whose ``[from, to)`` window **opens on or
after the year the member reaches retirement age**. That window is the source of
truth (DP#1) and it is the household saying "this job starts after I retired".

The pre-retirement ``gross_income`` snapshot is deliberately NOT carried forward.
It describes the career job, and the retirement transition is precisely the
declaration that that job ended — carrying it would have turned retirement into a
no-op rather than a transition.

## Why this is the FIRST layer of #375 and not the whole of it

#375 (the QPP supplement / CPP Post-Retirement Benefit) needs a second layer on
top: charging the employee contribution that funds the supplement (#289, carried
by PR #315), and the year-versioned accrual rate from Retraite Quebec. This PR
lands the foundation only, because a supplement computed on an income base the
engine guarantees is zero is the dead-module failure the ``unreached_rule_modules``
guard exists to stop. #375 stays open.
"""
import pytest

from simulation import _post_retirement_income_for_year


def _member(birth_year=1960, retirement_age=65, segments=None):
    m = {'role': 'primary', 'birth_year': birth_year,
         'retirement_age': retirement_age, 'gross_income': 80_000}
    if segments is not None:
        m['income_segments'] = segments
    return m


def _seg(amount, start, end=None):
    return {'kind': 'employment', 'amount': amount, 'from': start, 'to': end}


class TestNothingDeclaredIsNothing:
    """DP#32: every pre-existing household must be byte-identical."""

    def test_no_segments_means_no_post_retirement_income(self):
        assert _post_retirement_income_for_year(_member(), 2026, 5) == 0.0

    def test_an_empty_segment_list_means_the_same(self):
        assert _post_retirement_income_for_year(
            _member(segments=[]), 2026, 5) == 0.0

    def test_an_undated_member_claims_nothing(self):
        """No birth year -> the retirement is undatable, so nothing can be
        dated to start after it."""
        m = _member(segments=[_seg(20_000, '2026-01-01')])
        m['birth_year'] = 0
        assert _post_retirement_income_for_year(m, 2026, 5) == 0.0

    def test_the_career_job_is_never_carried_forward(self):
        """The snapshot describes the job that ENDED. This is the assertion
        that separates 'returning to work' from 'never stopped working'."""
        assert _post_retirement_income_for_year(_member(), 2030, 9) == 0.0


class TestOnlyDatedPostRetirementJobsCount:
    def test_a_job_opening_the_year_of_retirement_counts(self):
        # birth 1960 + retirement_age 65 -> retires in 2025.
        m = _member(segments=[_seg(10_000, '2025-06-01')])
        assert _post_retirement_income_for_year(m, 2025, 4) == 10_000.0

    def test_a_job_opening_after_retirement_counts(self):
        m = _member(segments=[_seg(10_000, '2027-01-15')])
        assert _post_retirement_income_for_year(m, 2027, 6) == 10_000.0

    def test_the_same_job_is_not_counted_before_it_starts(self):
        m = _member(segments=[_seg(10_000, '2027-01-15')])
        assert _post_retirement_income_for_year(m, 2026, 5) == 0.0

    def test_two_jobs_sum(self):
        m = _member(segments=[_seg(8_000, '2026-01-01'),
                              _seg(6_000, '2027-03-01')])
        assert _post_retirement_income_for_year(m, 2027, 6) == 14_000.0


class TestTheJobThatStartedBeforeRetirementIsNotPostRetirementIncome:
    def test_a_job_opening_before_retirement_is_excluded(self):
        m = _member(segments=[_seg(80_000, '2020-01-01')])
        assert _post_retirement_income_for_year(m, 2026, 5) == 0.0

    def test_a_job_opening_the_year_before_retirement_is_excluded(self):
        """The boundary: retirement_year itself is in, the year before is out.
        Getting this wrong would let a career job masquerade as a new one."""
        m = _member(segments=[_seg(80_000, '2024-01-01')])
        assert _post_retirement_income_for_year(m, 2025, 4) == 0.0


class TestTheWindowBoundsTheWork:
    """DP#1: the stored [from, to) window is the source of truth."""

    def test_work_before_the_window_closes_counts(self):
        m = _member(segments=[_seg(10_000, '2026-01-01', '2027-12-31')])
        assert _post_retirement_income_for_year(m, 2026, 5) == 10_000.0

    def test_the_end_year_is_exclusive(self):
        """`[from, to)` -- the year the job ends is not a year of work."""
        m = _member(segments=[_seg(10_000, '2026-01-01', '2027-01-01')])
        assert _post_retirement_income_for_year(m, 2026, 5) == 10_000.0
        assert _post_retirement_income_for_year(m, 2027, 6) == 0.0

    def test_an_open_ended_job_never_stops(self):
        m = _member(segments=[_seg(10_000, '2026-01-01', None)])
        for year in (2026, 2030, 2045):
            assert _post_retirement_income_for_year(
                m, year, year - 2021) == 10_000.0


class TestMalformedInputIsNotInventedAsIncome:
    """A missing or broken date is an ABSENCE. Reading it as 'now' would
    manufacture a post-retirement job the household never declared."""

    def test_a_segment_with_no_from_is_skipped(self):
        m = _member(segments=[{'kind': 'employment', 'amount': 10_000}])
        assert _post_retirement_income_for_year(m, 2026, 5) == 0.0

    def test_a_malformed_from_is_skipped(self):
        m = _member(segments=[_seg(10_000, 'not-a-date')])
        assert _post_retirement_income_for_year(m, 2026, 5) == 0.0

    def test_a_segment_with_no_amount_is_skipped(self):
        m = _member(segments=[{'kind': 'employment', 'from': '2026-01-01'}])
        assert _post_retirement_income_for_year(m, 2026, 5) == 0.0

    def test_a_malformed_to_does_not_crash(self):
        m = _member(segments=[_seg(10_000, '2026-01-01', 'whenever')])
        # An unparseable end is not "no end"; it is an unusable bound, so the
        # window cannot be shown to include this year and nothing is claimed.
        assert _post_retirement_income_for_year(m, 2026, 5) == 0.0


class TestTheFoldKeepsTheEarnings:
    """The helper is only worth having if the fold actually keeps the income."""

    def test_a_retired_member_with_no_declaration_earns_nothing(self):
        from simulation import (
            FamilySimulation, SimulationConfig,
        )
        cfg = SimulationConfig.from_dict(_projection({'gross_income': 80_000}))
        sim = FamilySimulation(cfg, use_readvanceable=False)
        results = sim.run()
        assert results[-1].primary_income < 30_000.0

    def test_a_retired_member_with_a_declared_job_earns_it(self):
        """The whole point: the same member, differing only by a dated segment
        declaring a job that opens after retirement."""
        plain = _run({'gross_income': 80_000})
        working = _run({
            'gross_income': 80_000,
            'income_segments': [
                {'kind': 'employment', 'amount': 10_000,
                 'from': '2027-01-15', 'to': None}],
        })
        assert working > plain

    def test_the_extra_income_is_exactly_the_declared_amount(self):
        plain = _run({'gross_income': 80_000})
        working = _run({
            'gross_income': 80_000,
            'income_segments': [
                {'kind': 'employment', 'amount': 10_000,
                 'from': '2027-01-15', 'to': None}],
        })
        assert working - plain == pytest.approx(10_000.0, rel=0.02)


def _run(overrides):
    from simulation import FamilySimulation, SimulationConfig
    cfg = SimulationConfig.from_dict(_projection(overrides))
    return FamilySimulation(cfg, use_readvanceable=False).run()[-1].primary_income


def _projection(overrides=None):
    member = {'role': 'primary', 'birth_year': 1960, 'retirement_age': 65,
              'gross_income': 80_000}
    if overrides:
        member.update(overrides)
    return {
        'assumptions': {'projection_years': 4, 'investment_return': 0.05,
                        'salary_growth': 0.0, 'start_year': 2026,
                        'frozen_brackets': True, 'time_step': 'yearly'},
        'savings': {'rate': 0.15},
        'property': {'house_value': 500_000, 'mortgage_balance': 0,
                     'mortgage_rate': 0.0, 'ltv_max': 0.8,
                     'current_payment_monthly': 0, 'amortization_years': 25,
                     'margin_available': 0},
        'family': {'members': [member], 'children': []},
        'accounts': {}, 'cash_flows': [],
    }

class TestTheDefaultsAndTheSpousePath:
    """The coverage gate's three lines: the `retirement_age` default and the
    whole spouse branch. Both are real paths a household can take, and both
    were unreached because every test above used a primary-only household."""

    def test_a_member_with_no_declared_retirement_age_uses_the_default(self):
        """DP#32: an ABSENT retirement age is a real answer and takes the
        statutory default. A member who declares a post-retirement job but no
        retirement age must still have it counted against the DEFAULT age, not
        silently omitted."""
        m = {'role': 'primary', 'birth_year': 1960, 'gross_income': 80_000,
             'income_segments': [_seg(10_000, '2025-06-01')]}
        assert 'retirement_age' not in m
        # DEFAULT_RETIREMENT_AGE is 65, so 1960 + 65 = 2025 and the job counts.
        assert _post_retirement_income_for_year(m, 2025, 4) == 10_000.0

    def test_the_spouse_path_keeps_a_return_to_work_income(self):
        """The spouse half of the transition. A two-adult household where only
        the spouse returns to work is the common case, and it exercises a
        different branch from the primary's."""
        plain = _run_two_adult(spouse_segment=None)
        working = _run_two_adult(spouse_segment=[
            {'kind': 'employment', 'amount': 12_000,
             'from': '2027-01-15', 'to': None}])
        assert working - plain == pytest.approx(12_000.0, rel=0.02)


def _run_two_adult(spouse_segment):
    from simulation import FamilySimulation, SimulationConfig
    spouse = {'role': 'spouse', 'birth_year': 1962, 'retirement_age': 65,
              'gross_income': 60_000}
    if spouse_segment is not None:
        spouse['income_segments'] = spouse_segment
    cfg = SimulationConfig.from_dict({
        'assumptions': {'projection_years': 4, 'investment_return': 0.05,
                        'salary_growth': 0.0, 'start_year': 2026,
                        'frozen_brackets': True, 'time_step': 'yearly'},
        'savings': {'rate': 0.15},
        'property': {'house_value': 500_000, 'mortgage_balance': 0,
                     'mortgage_rate': 0.0, 'ltv_max': 0.8,
                     'current_payment_monthly': 0, 'amortization_years': 25,
                     'margin_available': 0},
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1960, 'retirement_age': 65,
             'gross_income': 80_000},
            spouse],
            'children': []},
        'accounts': {}, 'cash_flows': [],
    })
    return (FamilySimulation(cfg, use_readvanceable=False).run()[-1]
            .spouse_income)
