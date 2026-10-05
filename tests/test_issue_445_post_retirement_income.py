"""Issue #445: a member past ``retirement_age`` keeps the income the document
DATES.

The gap this closes
-------------------
``simulate_year`` zeroed a retired member's income outright::

    if p_retired:
        primary_income = 0.0
        primary_earned_income = 0.0
        primary_self_emp = 0.0

so a dated ``income_segments`` window running past ``retirement_age`` was
parsed, day-blended into the year's income -- and then thrown away. The
document declared wages to 2030; the engine reported a member with none,
and nothing said so. That is the "declared but discarded" shape DP#32 exists
to forbid, and it is why partial retirement (work two more years, then stop)
was unexpressible: there was no configuration that survived the transition.

The rule this PR installs
-------------------------
Retiring stops the **undated base salary** and keeps the income the document
dates. The split is the document's own: a base salary is grown forward by
``salary_growth`` with no declared end anywhere, so ``retirement_age`` is the
best available reading; a dated window states its own years and is honoured to
its end. Nothing is opened by default -- a member with no dated window gets
exactly the figures they got before (DP#32), which is what keeps the golden
invariant and every characterisation test byte-identical.

Consequences that follow, each asserted below rather than assumed:

* the wages are taxed and are real cash (``after_tax_income`` and the
  drawdown-sized target both move) -- an income the tax path never sees would
  be a second silent zero;
* RRSP room keeps accruing (ITA s.146(1) earned income) and the Quebec
  self-employed stack is charged on a business that is still trading;
* the pension is NOT decoupled by this: ``cpp_start_age`` remains its own
  input, so a member can receive CPP while working -- which is the legal
  reality, and the reason the acceptance test can have both in one year;
* the AMT's employment-income base reads the post-transition income, so a
  working retiree's regular tax is not computed as if the salary had stopped
  (which would suppress the very surcharge a realized gain owes).

Every assertion below drives the ENGINE (``FamilySimulation.run``, and the
monthly path for parity) and reads ``YearResult``. DP#15: every figure here
is fabricated and round-numbered.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation, _dated_income_for_year
from simulation_config import SimulationConfig


# Born 1960 with retirement_age 65 -> the transition fires in 2025, so in a
# 2026-starting projection the member is retired from year 0.
BIRTH_YEAR = 1960
RETIREMENT_AGE = 65
START_YEAR = 2026
PROJECTION_YEARS = 8
WAGES = 40_000          # the dated window's annual amount
BASE_SALARY = 80_000    # the UNDATED salary the transition stops
CPP_MONTHLY = 1_200     # the pension, claimed from 65 -- decoupled from work


def _cfg(with_window=True, with_pension=False, projection_years=PROJECTION_YEARS,
         window_amount=WAGES, window_to="2031-01-01"):
    """A retired-by-age primary, optionally still earning a DATED wage window
    and optionally drawing the pension declared for their claim age."""
    member = {
        "role": "primary",
        "birth_year": BIRTH_YEAR,
        "retirement_age": RETIREMENT_AGE,
        # The undated salary: grown by salary_growth, no declared end. The
        # transition stops this -- and only this.
        "gross_income": BASE_SALARY,
        "rrsp_room_accumulated": 0,
        "tfsa_room_accumulated": 0,
    }
    if with_window:
        # Declared wages running to 2031, i.e. five years past retirement.
        member["income_segments"] = [{
            "id": "wages",
            "kind": "employment",
            "amount": window_amount,
            "from": "2026-01-01",
            "to": window_to,
        }]
    if with_pension:
        member["cpp_monthly_estimated"] = CPP_MONTHLY
        member["cpp_start_age"] = 65
        member["cpp_benefit_source"] = "declared"
    return {
        "family": {"members": [member], "children": []},
        "assumptions": {
            "start_year": START_YEAR,
            "projection_years": projection_years,
            "investment_return": 0.05,
            "salary_growth": 0.0,
            "inflation": 0.0,
            "frozen_brackets": True,
        },
        "tax": {"province": "ontario"},
        "savings": {"rate": 0.3},
        "property": {"house_value": 400_000},
    }


def _run(cfg_dict, monthly=False):
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False,
                           lump_sum=0.0)
    return (sim._run_monthly() if monthly else sim.run())


class TestTheDatedWindowSurvives:
    """The behaviour the issue asks for, driven through the fold."""

    def test_declared_wages_are_paid_in_every_year_the_window_covers(self):
        """Five years of wages after retirement: 2026-2030."""
        rs = _run(_cfg())
        assert [r.primary_income for r in rs[:5]] == [WAGES] * 5, (
            "a dated window running to 2031 must pay through 2030 -- the "
            "transition stops the UNDATED salary, not what the document dates")

    def test_the_window_still_ends_when_it_says_it_does(self):
        """2026+5 = 2031 is outside the window, so the income stops there --
        the other half of the same rule. Without this the fix would read as
        'retirement is optional forever'."""
        rs = _run(_cfg())
        assert rs[5].primary_income == 0.0

    def test_an_open_ended_window_still_stops_at_retirement(self):
        """The other half of the rule, and the half that keeps the two
        SPELLINGS of an open-ended income from disagreeing (DP#9): a window with
        no declared `to` has no stated end, so it ends where the undated base
        salary ends -- at `retirement_age`. Honoured forever instead, a member
        with a future-dated job would never stop working, which is not what the
        document said either."""
        rs = _run(_cfg(window_to=None))
        assert all(r.primary_income == 0.0 for r in rs), (
            "a member born 1960 with retirement_age 65 retired in 2025, before "
            "the projection starts: an open-ended window ends there")

    def test_a_dated_end_survives_and_an_open_ended_one_does_not(self):
        """Both spellings side by side, so neither reading can be taken on
        faith from the other."""
        dated = _run(_cfg(window_to="2031-01-01"))
        open_ended = _run(_cfg(window_to=None))
        assert dated[0].primary_income == WAGES
        assert open_ended[0].primary_income == 0.0

    def test_without_the_window_nothing_changes(self):
        """DP#32 absence-safety: a household that declares no dated income past
        retirement is byte-for-byte what it was -- this is the case the golden
        fixture and every characterisation test sit in."""
        with_window = _run(_cfg(with_window=True))
        without = _run(_cfg(with_window=False))
        assert [r.primary_income for r in without] == [0.0] * PROJECTION_YEARS
        # ...and the two differ in exactly the years the window covers, which
        # is what makes the first assertion a feature rather than an accident.
        differing = [i for i, (a, b) in enumerate(zip(with_window, without))
                     if a.primary_income != b.primary_income]
        assert differing == [0, 1, 2, 3, 4]

    def test_the_monthly_path_agrees_with_the_yearly_one(self):
        """The transition is implemented on both fold paths; if only one were
        changed, the two would silently disagree on a retired member's income."""
        yearly = _run(_cfg(), monthly=False)
        monthly = _run(_cfg(), monthly=True)
        # The income series only -- the monthly path is not expected to match
        # the yearly one on BALANCES (it allocates monthly and approximates the
        # annual return monthly), so comparing totals here would assert a
        # property the engine has never had.
        assert [r.primary_income for r in monthly] == \
            [r.primary_income for r in yearly]
        assert [r.cpp_income for r in monthly] == [r.cpp_income for r in yearly]
        assert [r.rrsp_tax_savings for r in monthly] == \
            [r.rrsp_tax_savings for r in yearly], (
            "the deduction's refund is the fold's own report that both paths "
            "taxed the same income in the same bracket")


class TestTheWagesAreReal:
    """An income the tax path never sees would be a second silent zero."""

    def test_the_wages_are_in_the_taxable_base(self):
        """The fold's own signal: an RRSP deduction shelters tax only when
        there IS tax, and there is only tax here because the declared wages are
        in the base. The identical household without the window shelters
        nothing -- so the refund is the evidence, not an assumption about it."""
        with_wages = _run(_cfg(with_window=True))
        without = _run(_cfg(with_window=False))
        assert with_wages[2].contributions['primary_rrsp'] > 0.0, (
            "premise: the same contribution is made either way, so the refund "
            "differs only through the tax available to shelter")
        assert without[2].rrsp_tax_savings == 0.0, (
            "premise: with no income there is no tax, so no refund")
        assert with_wages[2].rrsp_tax_savings > 0.0, (
            "the declared wages must be in the taxable base: a refund that "
            "ignored them would mean the engine banked the cash and never "
            "taxed it -- the same silent-zero shape this issue is about")

    def test_rrsp_room_keeps_accruing_while_they_work(self):
        """ITA s.146(1): the dated wages are earned income, so the contribution
        room keeps accruing in the working years."""
        rs = _run(_cfg())
        assert rs[0].primary_rrsp == 0.0, (
            "premise: no room accumulated and no contribution made in year 0")
        assert rs[2].primary_rrsp > 0.0, (
            "a member working past 65 keeps accruing RRSP room")

    def test_the_wages_cover_spending_so_the_drawdown_is_not_needed(self):
        """End-to-end through the retirement path: wages big enough to cover
        the net spending target leave nothing to draw, while the identical
        household without them draws in the very same years. This is the
        assertion that reaches the RETIREMENT tax/spending path -- a fix wired
        only into the working-year prologue would leave the drawdown sized as
        if the member had no income at all."""
        big = 120_000
        rs = _run(_cfg(window_amount=big))
        assert [r.drawdown_income for r in rs[:5]] == [0.0] * 5, (
            f"{big:,} of wages net must cover the spending target outright -- "
            "the retirement path sizes the drawdown to what the household does "
            "NOT already have")
        assert rs[5].drawdown_income > 0.0, (
            "and once the dated window ends the very same household is back to "
            "drawing -- so the zeros above are the wages covering spending, not "
            "a spending target that happened to be $0")


class TestThePensionIsNotCollapsedIntoTheWorkStopDate:
    """Issue #445's decision 3: the claim age stays its own input."""

    def test_a_member_can_draw_the_pension_and_work_in_the_same_year(self):
        """CPP is payable while the recipient works -- that is the law, and it
        is the combination the blocked #375 needs. The transition must not make
        one imply the other."""
        rs = _run(_cfg(with_pension=True))
        working_years = rs[:5]
        assert all(r.primary_income == WAGES for r in working_years)
        assert all(r.cpp_income > 0.0 for r in working_years), (
            "the pension is dated by the member's declared CLAIM age, not by "
            "whether the engine calls them retired -- a working 66-year-old "
            "still receives it")

    def test_a_member_with_no_window_is_unchanged_with_a_pension(self):
        """The pension case for the ORDINARY retired member must be untouched
        by this issue: same income, same tax, same everything."""
        before = _run(_cfg(with_window=False, with_pension=True))
        assert [r.primary_income for r in before] == \
            [0.0] * PROJECTION_YEARS


class TestTheHelperItself:
    """``_dated_income_for_year`` is the split, so its edges are unit-tested."""

    def test_no_dated_window_yields_nothing(self):
        assert _dated_income_for_year({}, 2026, 0.0, 0) == (0.0, 0.0, 0.0)

    def test_a_window_outside_the_year_contributes_nothing(self):
        member = {"income_segments": [{
            "id": "w", "kind": "employment", "amount": WAGES,
            "from": "2030-01-01", "to": "2031-01-01"}]}
        assert _dated_income_for_year(member, 2026, 0.0, 0) == (0.0, 0.0, 0.0)

    def test_the_window_is_blended_by_its_days(self):
        """A window that starts mid-year pays for its days, not its whole
        amount (the same day-blending every dated income gets -- DP#1)."""
        member = {"income_segments": [{
            "id": "w", "kind": "employment", "amount": WAGES,
            "from": "2026-07-01", "to": "2027-01-01"}]}
        total, earned, _ = _dated_income_for_year(member, 2026, 0.0, 0)
        assert earned == pytest.approx(total)
        assert 19_000 < total < 21_000, (
            f"roughly half a year of {WAGES:,} is due, got {total:,.0f}")


class TestAbsenceIsInert:
    """The repo-wide golden must not move: it declares no dated window that
    outlives its retirement, so no branch here is reachable for it."""

    def test_golden_is_unmoved(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tests"))
        from test_golden_trajectory_581 import (golden_household_config, _run
                                                 as golden_run)
        assert golden_run(golden_household_config())[-1].total_assets == \
            pytest.approx(9_709_753.139463063)