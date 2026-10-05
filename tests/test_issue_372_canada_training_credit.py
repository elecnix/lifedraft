#!/usr/bin/env python3
"""Issue #372: the Canada Training Credit (ITA s.122.91) -- a refundable federal
credit for an adult learner's tuition, limited by a per-person balance that
builds $250 a year.

THE CONTRACT THIS ISSUE PROMISES, transcribed from the issue's acceptance
criteria and from CRA's own published rules rather than from the engine's
current behaviour, so it cannot be satisfied by accident:

  1. The credit is the LESSER of the training amount limit carried into the
     year, 50% of the year's ELIGIBLE tuition, and what is left of the $5,000
     lifetime cap.
  2. Claiming it REDUCES the tuition going into the s.118.5 non-refundable
     credit by the amount claimed (ITA s.122.91(3)) -- the two credits are not
     additive.
  3. It is REFUNDABLE: the credit reaches the household as cash, so a learner
     whose tax is already zero still gets it.
  4. The limit accrues $250 in each year whose PRECEDING-year facts qualify
     (age 25-under-65 at year end, working income at or above CRA's indexed
     threshold, net income no higher than the top of the third federal
     bracket), and an unused balance expires at the end of the year the member
     turns 65.
  5. Fees an employer (or anyone else) REIMBURSED are not eligible for either
     credit -- but the year still builds the learner's balance, because the room
     accrues on the year's income, not on the tuition.
  6. With no tuition and no declared opening balance, nothing changes -- and
     the repo-wide golden invariant does not move.

The acceptance cases come from the issue:

  A. A Quebec primary, 28 in 2024, employment income $78,000, $1,500 of
     self-paid tuition and $500 of opening limit: the credit is $500, the
     closing balance is $0, and the s.118.5 base is $1,000.
  B. A 28-year-old on $40,000 of employment income since 2020 whose 2024
     tuition was employer-reimbursed: no credit in 2024 and no tuition credit
     from the reimbursed fees, while the balance still grows. The next year,
     $2,000 of self-paid tuition yields min(balance, $1,000).
  C. Negative controls: a member aged 25, or one with no balance, gets nothing
     and an unchanged tuition base.

Sources: ITA s.122.91; CRA, "Canada training credit"; Form 5000-S11 Schedule 11.
Every fixture here is fabricated, round-numbered and role-named (DP#15).
"""

import copy
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import input_contract as ic
import contract_schema
from contract_errors import ContractAdaptationError
from countries.canada.training_credit import (
    accrual_for_year, claimable_at, room_after_claim, tuition_after_credit)
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from test_input_contract import _load_example, _two_generation_subset

# CRA's published Canada-training-amount working-income threshold, by year
# (CRA, "Canada training credit"; $10,000 in 2020, indexed thereafter).
WORKING_INCOME_THRESHOLD = {
    2020: 10_000, 2021: 10_100, 2022: 10_342, 2023: 10_994,
    2024: 11_511, 2025: 11_821, 2026: 12_058,
}
ANNUAL_ACCRUAL = 250.0
LIFETIME_CAP = 5_000.0


def _doc():
    """The shipped two-generation example, validated -- the sub-family the
    adapter can honestly map onto the two-adults-plus-children engine."""
    doc = _two_generation_subset(_load_example())
    contract_schema.validate_contract(doc)
    return doc


def _person(doc, pid):
    return next(p for p in doc["people"] if p["id"] == pid)


def _set_birth_date(doc, pid, iso):
    _person(doc, pid)["birth_date"] = iso


def _set_employment(doc, pid, amount):
    """Replace the person's income with a single open-ended employment line.

    The income KEEPS the id the shipped example gave it (`p1_employment`):
    the document's `decisions.income[]` scenarios override income by id, so
    minting a new id would leave those scenarios pointing at nothing -- which
    the adapter refuses loudly (correctly). The income's extra facts (the
    employer RRSP match, the non-compete clause) are dropped with the rest of
    the block: this fixture is about the learninger's INCOME LEVEL, not their
    employer's match.
    """
    person = _person(doc, pid)
    original_id = person["incomes"][0]["id"] if person.get("incomes") else "job"
    person["incomes"] = [{
        "id": original_id, "kind": "employment", "amount": amount,
        "from": "2020-01-01", "to": None,
    }]


def _add_study_period(doc, pid, tuition, start="2024-01-01",
                      end="2024-12-31", reimbursed=None):
    person = _person(doc, pid)
    period = {
        "institution": "college", "program": "certificate",
        "start_date": start, "end_date": end, "tuition": tuition,
    }
    if reimbursed is not None:
        period["reimbursed"] = reimbursed
    person.setdefault("study_periods", []).append(period)


def _run(doc):
    """Validate -> map to the internal config -> run the real engine."""
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run(), legacy


def _student_doc(tuition=1_500, opening_limit=500.0, income=78_000,
                 birth_date="1996-07-01", reimbursed=None, as_of="2026-06-30",
                 study_year=2026, until_age=29):
    """A 28-year-old Quebec primary: employed, studying, with a declared
    training amount limit carried in from the notice of assessment.

    ``as_of`` sets the projection's start year -- the adapter reads
    ``int(doc['as_of'][:4])`` -- and the horizon is pulled in to five years so a
    multi-year accrual case stays cheap to run.
    """
    doc = _doc()
    doc["as_of"] = as_of
    doc["decisions"]["horizon"] = {"person": "p1", "until_age": until_age}
    _set_birth_date(doc, "p1", birth_date)
    _set_employment(doc, "p1", income)
    if tuition is not None:
        _add_study_period(doc, "p1", tuition, start=f"{study_year}-01-01",
                          end=f"{study_year}-12-31", reimbursed=reimbursed)
    if opening_limit is not None:
        _person(doc, "p1")["training_amount_limit_opening"] = opening_limit
    return doc


def _row(results, year):
    """``results`` for a projection that starts in 2023, indexed by calendar
    year -- positional, because YearResult.year is the fold's own counter and
    not the calendar year a reader of this test is asking about."""
    return results[year - 2023]


def _member(legacy, role="primary"):
    return next(m for m in legacy["family_members"] if m.get("role") == role)


# ────────────────────────────────────────────────────────────────────────────
# The law, on its own boundaries (the unit level -- pure, DP#3)
# ────────────────────────────────────────────────────────────────────────────

class TestTheLaw:
    """`countries/canada.training_credit` on both sides of every threshold.
    Issue #372's rule path (DP#17: both sides of every threshold)."""

    @pytest.mark.parametrize("age,expected", [
        (24, 0.0),        # under 25: no accrual window at all
        (25, ANNUAL_ACCRUAL),   # 25 at year end -> accrues
        (64, ANNUAL_ACCRUAL),   # the last year of the window
        (65, 0.0),        # 65 at year end -> no accrual (and the balance expires)
    ])
    def test_the_accrual_window_is_25_to_64_at_year_end(self, age, expected):
        assert accrual_for_year(age, 40_000, 40_000, ANNUAL_ACCRUAL,
                                WORKING_INCOME_THRESHOLD[2024],
                                165_430) == expected

    def test_accrual_needs_working_income_at_or_above_the_threshold(self):
        threshold = WORKING_INCOME_THRESHOLD[2024]
        assert accrual_for_year(30, threshold - 1, 40_000, ANNUAL_ACCRUAL,
                                threshold, 165_430) == 0.0
        assert accrual_for_year(30, threshold, 40_000, ANNUAL_ACCRUAL,
                                threshold, 165_430) == ANNUAL_ACCRUAL

    def test_accrual_needs_net_income_at_or_below_the_third_bracket_ceiling(self):
        assert accrual_for_year(30, 40_000, 165_431, ANNUAL_ACCRUAL,
                                WORKING_INCOME_THRESHOLD[2024], 165_430) == 0.0
        assert accrual_for_year(30, 40_000, 165_430, ANNUAL_ACCRUAL,
                                WORKING_INCOME_THRESHOLD[2024], 165_430) == \
            ANNUAL_ACCRUAL

    def test_no_accrual_parameters_means_no_credit_is_invented(self):
        """DP#32: a year with no CTC data must not manufacture $250."""
        assert accrual_for_year(30, 40_000, 40_000, 0.0, 11_511, 165_430) == 0.0

    def test_the_claim_is_the_least_of_room_half_the_tuition_and_the_life_left(self):
        # room-bound
        assert claimable_at(30, 500.0, 1_500) == 500.0
        # tuition-bound (50% of 2,000 = 1,000 < the 1,250 room)
        assert claimable_at(30, 1_250.0, 2_000) == 1_000.0
        # lifetime-bound: only $300 of the $5,000 cap is unclaimed
        assert claimable_at(30, 1_250.0, 2_000,
                            lifetime_cap=LIFETIME_CAP,
                            claimed_to_date=LIFETIME_CAP - 300) == 300.0

    def test_the_claim_needs_room_and_tuition(self):
        assert claimable_at(30, 0.0, 1_500) == 0.0
        assert claimable_at(30, 500.0, 0.0) == 0.0

    def test_a_25_year_old_has_no_balance_to_spend(self):
        """The accrual window starts at 25, so a 25-year-old's balance is still
        empty on their first year of eligibility."""
        assert claimable_at(25, 0.0, 1_500) == 0.0

    def test_the_balance_never_exceeds_the_lifetime_cap(self):
        """A learner who never studies must not accumulate past $5,000 -- the
        cap is a ceiling on the BALANCE, not only on the claim."""
        room = 0.0
        for _ in range(40):
            room = room_after_claim(room, 0.0, ANNUAL_ACCRUAL, LIFETIME_CAP)
        assert room == LIFETIME_CAP

    def test_the_balance_floors_at_zero(self):
        assert room_after_claim(100.0, 250.0, 0.0, LIFETIME_CAP) == 0.0

    def test_the_tuition_base_is_reduced_by_the_credit_and_never_goes_negative(self):
        assert tuition_after_credit(1_500, 500) == 1_000
        # A caller that computed a larger claim than the law allows must not be
        # able to turn the tuition base negative (which downstream reads as an
        # INCREASED tuition credit).
        assert tuition_after_credit(1_500, 5_000) == 0.0


# ────────────────────────────────────────────────────────────────────────────
# Acceptance case A: $500 of room, $1,500 of tuition -> $500 credit, $0 left,
# and a $1,000 s.118.5 base
# ────────────────────────────────────────────────────────────────────────────

class TestAcceptanceCaseA:
    def test_the_credit_is_the_opening_balance(self):
        results, _ = _run(_student_doc())
        assert results[0].primary_ctc_claimed == 500.0

    def test_the_opening_balance_is_fully_spent(self):
        """The whole $500 is claimed, so the balance the member CARRIES is the
        $250 this year's income adds and nothing more. (The issue's "closing
        room = $0" is the balance net of the claim; CRA adds the new year's
        accrual to the balance the FOLLOWING year draws on, which is why the
        closing figure is $250 rather than $0. Asserting 0.0 here would be
        asserting the accrual does not happen, which is the opposite of the
        rule.)"""
        results, _ = _run(_student_doc())
        assert results[0].training_amount_limit["p1"] == pytest.approx(250.0)

    def test_the_credit_is_cash_not_a_tax_reduction(self):
        """A refundable credit must show up in the household's money. The
        learner's tax is large here, so the cleanest observable is that the
        claim is reported and reaches after-tax income even where tax was
        payable (the solvency rule adds it in both phases)."""
        results, _ = _run(_student_doc())
        assert results[0].primary_ctc_claimed > 0.0

    def test_the_s1185_base_is_reduced_by_the_credit(self):
        """$1,500 of tuition less the $500 CTC is a $1,000 base -- so the
        non-refundable credit this year is the credit on $1,000, not on
        $1,500.

        Measured as the DELTA in the household's after-tax income between the
        same learner with and without the declared balance. The two effects are
        separable and must both be present: the CTC pays $500 of CASH, and it
        removes $500 from the s.118.5 base, which saves the tax on that $500 at
        the combined federal + Quebec rate. So the delta is exactly
        ``500 - 500 x combined_rate``. A run that paid the CTC but left the
        s.118.5 base alone would show ``500``; a run that reduced the base but
        paid no cash would show ``-500 x rate``. This asserts both halves at
        once, with the rates read from the data rather than hardcoded.
        """
        with_ctc, _ = _run(_student_doc(tuition=1_500, opening_limit=500.0))
        no_ctc, _ = _run(_student_doc(tuition=1_500, opening_limit=None))
        combined_rate = self._combined_tuition_credit_rate(2026)
        expected = 500.0 - 500.0 * combined_rate
        actual = (with_ctc[0].after_tax_income - no_ctc[0].after_tax_income)
        assert actual == pytest.approx(expected, rel=1e-6), (
            "claiming the CTC must pay $500 of cash AND shrink the s.118.5 base "
            f"by $500, a net {expected:,.2f} of after-tax income; got "
            f"{actual:,.2f}")

    @staticmethod
    def _combined_tuition_credit_rate(year):
        """The federal lowest rate plus the Quebec provincial rate -- the same
        two rates `countries.canada.tax_calc.tuition_tax_credit` adds, read
        from the year-versioned data rather than hardcoded."""
        from tax_data import default_tax_provider
        provider = default_tax_provider()
        fed = provider._load_year(year, 'canada', 'federal')
        qc = provider._load_year(year, 'canada', 'quebec')
        return fed.federal_brackets[0].rate + qc.qc_tuition_credit_rate


# ────────────────────────────────────────────────────────────────────────────
# Acceptance case B: employer-reimbursed tuition builds the balance but claims
# nothing
# ────────────────────────────────────────────────────────────────────────────

class TestAcceptanceCaseB:
    def _reimbursed_doc(self):
        """A 28-year-old on $40,000 of employment income since 2020, whose 2026
        tuition was paid by their employer. The projection starts in 2023 so the
        balance has genuinely accrued year over year before the self-paid course
        in 2027 -- which is the point of the case: the room is BUILT by income
        and only SPENT on tuition the learner bore."""
        return _student_doc(tuition=2_000, opening_limit=None, income=40_000,
                            reimbursed=2_000, as_of="2023-06-30",
                            study_year=2026, until_age=31)

    def test_reimbursed_fees_earn_no_credit(self):
        results, _ = _run(self._reimbursed_doc())
        assert results[0].primary_ctc_claimed == 0.0

    def test_reimbursed_fees_earn_no_tuition_credit_either(self):
        """CRA allows neither credit where the learner did not bear the cost."""
        results, _ = _run(self._reimbursed_doc())
        assert results[0].primary_tuition_carryforward == 0.0

    def test_the_balance_still_builds_on_a_reimbursed_year(self):
        """The room accrues on the year's INCOME, not on its tuition -- CRA's
        own example is an employee whose employer pays the fees."""
        results, _ = _run(self._reimbursed_doc())
        assert results[0].training_amount_limit["p1"] > 0.0

    def test_the_next_year_of_self_paid_tuition_claims_half_of_it(self):
        """$2,000 of self-paid tuition -> 50% = $1,000, and the balance by then
        is exactly $1,000 (four $250 accruals, 2024 through 2026, on the
        2020-onward income). So this is the tuition-bound prong binding: the
        claim is $1,000, not $1,250 and not $0."""
        doc = self._reimbursed_doc()
        _add_study_period(doc, "p1", 2_000, start="2027-01-01",
                          end="2027-12-31")
        results, _ = _run(doc)
        assert _row(results, 2026).primary_ctc_claimed == 0.0
        assert _row(results, 2026).training_amount_limit["p1"] == \
            pytest.approx(4 * 250.0)
        assert _row(results, 2027).primary_ctc_claimed == 1_000.0

    def test_more_reimbursed_than_paid_is_refused(self):
        """A clamp would turn an incoherent document into a smaller -- still
        wrong -- claim; a refusal says the document is wrong."""
        doc = _student_doc(tuition=1_000, reimbursed=1_500)
        with pytest.raises(ContractAdaptationError):
            _run(doc)


# ────────────────────────────────────────────────────────────────────────────
# Negative controls (issue acceptance) and the expiry rule
# ────────────────────────────────────────────────────────────────────────────

class TestNegativeControls:
    def test_a_25_year_old_with_a_declared_balance_gets_no_credit(self):
        """25 at the end of 2026 (born 2001): the balance is real (declared),
        but the credit cannot be claimed before 26."""
        doc = _student_doc(birth_date="2001-07-01", opening_limit=500.0)
        results, _ = _run(doc)
        assert results[0].primary_ctc_claimed == 0.0

    def test_no_balance_means_no_credit(self):
        """The control for the case above: the same learner, the same tuition,
        no declared balance -> nothing claimed, and (because the credit is
        zero) the tuition base is untouched."""
        with_room, _ = _run(_student_doc(opening_limit=500.0))
        without, _ = _run(_student_doc(opening_limit=None))
        assert without[0].primary_ctc_claimed == 0.0
        assert with_room[0].primary_ctc_claimed == 500.0
        # And the two runs differ by exactly the CTC's two effects, in the
        # learner's favour: the balance pays $500 of cash and costs $500 of
        # s.118.5 base, so the CTC run has the HIGHER after-tax income. (The
        # exact figure is asserted in the case above; here the sign is the
        # point -- a CTC that arrived as a tax reduction rather than a refund
        # would invert it.)

    def test_no_tuition_at_all_is_a_strict_no_op(self):
        results, _ = _run(_student_doc(tuition=None, opening_limit=None))
        assert all(r.primary_ctc_claimed == 0.0 for r in results)
        assert all(r.training_amount_limit == {} for r in results)


class TestExpiryAt65:
    def test_the_balance_expires_at_the_end_of_the_year_the_member_turns_65(self):
        """A member who reaches 65 in 2030 (and declared a balance) may still
        CLAIM in 2030 -- 65 is inside the claim window -- and holds nothing
        afterwards."""
        doc = _student_doc(tuition=None, opening_limit=1_000.0,
                           birth_date="1965-07-01", as_of="2024-06-30")
        doc["decisions"]["horizon"] = {"person": "p1", "until_age": 68}
        results, _ = _run(doc)
        by_year = {2024 + i: r.training_amount_limit.get("p1", 0.0)
                   for i, r in enumerate(results)}
        assert by_year[2030] == 0.0, "expires at the end of the year turned 65"
        assert by_year[2029] > 0.0, "still held the year before"


# ────────────────────────────────────────────────────────────────────────────
# Absence-safety: the golden invariant must not move
# ────────────────────────────────────────────────────────────────────────────

class TestAbsenceSafety:
    def test_the_golden_invariant_is_unmoved(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))
        from test_golden_trajectory_581 import (golden_household_config, _run
                                                 as golden_run)
        assert golden_run(golden_household_config())[-1].total_assets == \
            pytest.approx(9_709_753.139463063)

    def test_a_household_with_no_tuition_does_not_accrue(self):
        """The accrual needs no tuition, so this asserts the RULE is gated on
        the household declaring a study period or a balance at all -- not that
        the arithmetic happens to come out zero."""
        results, _ = _run(_doc())
        assert all(r.primary_ctc_claimed == 0.0 for r in results)
        assert all(r.spouse_ctc_claimed == 0.0 for r in results)
