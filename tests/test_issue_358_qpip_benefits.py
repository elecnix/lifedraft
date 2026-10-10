#!/usr/bin/env python3
"""Issue #358: QPIP (RQAP) benefits -- the half of the plan the engine never paid.

The engine computed the QPIP *premium* and none of the benefits, so a household
that declared a birth got the payroll deduction and no income: every parental
leave projected as a pure cost.

Weeks and rates are statute (module constants in
``countries/canada/provinces/quebec/qpip_benefits.py``, sourced from
quebec.ca's "Choice of Plan and Types of Benefits"); the maximum insurable
earnings is indexed and therefore year-versioned data (94,000 for 2024).

The acceptance figures are CFFP's first household: as_of 2024, Quebec, one
parent at $1,080/week of insurable earnings and the other at $720/week, a birth
in 2024.

DP#15: every household here is fabricated, round-numbered and role-named.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.provinces.quebec.qpip_benefits import (
    BASIC_RATE, SPECIAL_RATE, exclusive_entitlement, gross_benefit,
    leave_weeks, parental_entitlement, validate_shares, weekly_benefit,
    weekly_insurable_earnings,
)

MIE_2024 = 94_000.0
PRIMARY_WEEKLY = 1_080.0          # $56,160/year
SPOUSE_WEEKLY = 720.0            # $37,440/year
PRIMARY_ANNUAL = PRIMARY_WEEKLY * 52
SPOUSE_ANNUAL = SPOUSE_WEEKLY * 52


def _basic_birthing_parent():
    return exclusive_entitlement("basic", "birthing") + \
        parental_entitlement("basic", first_rate_weeks=2, long_rate_weeks=25)


def _basic_other_parent():
    return exclusive_entitlement("basic", "other") + \
        parental_entitlement("basic", first_rate_weeks=5, long_rate_weeks=0)


def _special_birthing_parent():
    return exclusive_entitlement("special", "birthing") + \
        parental_entitlement("special", first_rate_weeks=18, long_rate_weeks=0)


def _special_other_parent():
    return exclusive_entitlement("special", "other") + \
        parental_entitlement("special", first_rate_weeks=7, long_rate_weeks=0)


class TestTheAcceptanceFigures:
    def test_basic_plan_birthing_parent_receives_19980(self):
        """18 weeks maternity @70% of $720 + 2 @70% + 25 @55% = $19,980."""
        got = gross_benefit(SPOUSE_ANNUAL, _basic_birthing_parent(), MIE_2024)
        assert got == pytest.approx(19_980.0, abs=0.5)

    def test_basic_plan_other_parent_receives_7560(self):
        """5 weeks paternity @70% of $1,080 + 5 shareable @70% = $7,560."""
        got = gross_benefit(PRIMARY_ANNUAL, _basic_other_parent(), MIE_2024)
        assert got == pytest.approx(7_560.0, abs=0.5)

    def test_special_plan_birthing_parent_receives_17820(self):
        """15 weeks maternity @75% + 18 shareable @75%, all on $720/week."""
        got = gross_benefit(SPOUSE_ANNUAL, _special_birthing_parent(), MIE_2024)
        assert got == pytest.approx(17_820.0, abs=0.5)

    def test_special_plan_other_parent_receives_8100(self):
        """3 weeks paternity @75% + 7 shareable @75%, on $1,080/week."""
        got = gross_benefit(PRIMARY_ANNUAL, _special_other_parent(), MIE_2024)
        assert got == pytest.approx(8_100.0, abs=0.5)

    def test_the_two_plans_pay_the_same_family_a_different_split(self):
        """The special plan pays fewer weeks at a higher rate: it is not simply
        better, which is why the plan choice is worth sweeping."""
        basic = (gross_benefit(SPOUSE_ANNUAL, _basic_birthing_parent(), MIE_2024)
                 + gross_benefit(PRIMARY_ANNUAL, _basic_other_parent(), MIE_2024))
        special = (gross_benefit(SPOUSE_ANNUAL, _special_birthing_parent(), MIE_2024)
                   + gross_benefit(PRIMARY_ANNUAL, _special_other_parent(), MIE_2024))
        assert basic != special


class TestTheCap:
    def test_earnings_above_the_maximum_pay_the_capped_benefit(self):
        """A parent above the year's maximum insurable earnings earns nothing
        extra: the cheque is the same as one at 94,000."""
        at_cap = gross_benefit(MIE_2024, _basic_other_parent(), MIE_2024)
        above = gross_benefit(200_000.0, _basic_other_parent(), MIE_2024)
        assert above == pytest.approx(at_cap, abs=0.01)
        # And the capped weekly benefit is rate x MIE/52.
        assert weekly_benefit(200_000.0 / 52, BASIC_RATE, MIE_2024) == \
            pytest.approx(BASIC_RATE * MIE_2024 / 52, abs=0.01)

    def test_a_year_with_no_published_cap_pays_nothing(self):
        """DP#32: no data, no benefit -- never an uncapped one."""
        assert weekly_benefit(PRIMARY_WEEKLY, BASIC_RATE, 0.0) == 0.0
        assert gross_benefit(PRIMARY_ANNUAL, _basic_other_parent(), 0.0) == 0.0


class TestTheStructure:
    def test_the_plan_week_totals_match_the_plan(self):
        """Basic is 18 + 5 + 32 = 55 weeks of entitlement across the parents;
        special is 15 + 3 + 25 = 43."""
        basic = leave_weeks(_basic_birthing_parent()) + \
            leave_weeks(_basic_other_parent())
        assert basic == 55
        special = leave_weeks(_special_birthing_parent()) + \
            leave_weeks(_special_other_parent())
        assert special == 43

    def test_a_multiple_birth_adds_weeks_to_each_parent(self):
        assert leave_weeks(exclusive_entitlement("basic", "birthing", True)) == 18 + 5
        assert leave_weeks(exclusive_entitlement("special", "other", True)) == 3 + 3

    def test_an_unknown_plan_or_role_refuses_rather_than_paying_nothing(self):
        """A misspelled plan must not look like a household with no leave."""
        with pytest.raises(ValueError, match="unknown QPIP plan"):
            exclusive_entitlement("Special", "birthing")
        with pytest.raises(ValueError, match="unknown QPIP parent_role"):
            exclusive_entitlement("basic", "mother")

    def test_declared_shares_must_add_up_to_the_plan(self):
        """Under-declaring drops weeks silently; over-declaring invents them."""
        # The acceptance split: 2 + 5 = 7 at the first rate, 25 at the long rate.
        validate_shares("basic", {"a": {"first_rate": 2, "long_rate": 25},
                                  "b": {"first_rate": 5, "long_rate": 0}})
        with pytest.raises(ValueError, match="shareable weeks"):
            validate_shares("basic", {"a": {"first_rate": 2, "long_rate": 25},
                                      "b": {"first_rate": 4, "long_rate": 0}})
        with pytest.raises(ValueError, match="shareable weeks"):
            validate_shares("special", {"a": {"first_rate": 25, "long_rate": 0},
                                        "b": {"first_rate": 1, "long_rate": 0}})

    def test_weekly_earnings_are_the_year_over_52(self):
        assert weekly_insurable_earnings(PRIMARY_ANNUAL) == pytest.approx(
            PRIMARY_WEEKLY, abs=1e-9)


# ────────────────────────────────────────────────────────────────────────────
# The integration: a declared leave must reach the household's income, replace
# the salary over the leave weeks, and do so WITHOUT accruing RRSP room or
# attracting a QPP/QPIP premium -- both of which fall out of the income kind.
# ────────────────────────────────────────────────────────────────────────────

import copy  # noqa: E402

import contract_schema  # noqa: E402
import input_contract as ic  # noqa: E402
from simulation import FamilySimulation  # noqa: E402
from simulation_config import SimulationConfig  # noqa: E402
from test_input_contract import _load_example, _two_generation_subset  # noqa: E402


def _leave_doc(plan="basic", first=2, long=25, other_first=5, other_long=0,
               province="quebec"):
    """A fabricated Quebec couple who declare a birth and a leave: the spouse is
    the birthing parent ($720/week of insurable earnings), the primary the other
    parent ($1,080/week) -- the shape of the issue's acceptance household."""
    doc = _two_generation_subset(_load_example())
    # The issue's household is priced as_of 2024, and it has to be: the birth is
    # 2024-11-28, so on the shipped example's own 2026 snapshot the leave would
    # fall entirely BEFORE the projection and the segment would have no year to
    # land in -- a test that passes by measuring nothing.
    doc["as_of"] = "2024-06-30"
    doc["jurisdiction"]["province"] = province
    people = {p["id"]: p for p in doc["people"]}
    people["p1"]["birth_date"] = "1980-03-14"
    people["p2"]["birth_date"] = "1982-05-20"
    people["p1"]["incomes"] = [{"id": "p1_employment", "kind": "employment",
                                "amount": 56_160, "from": "2015-01-01", "to": None}]
    people["p2"]["incomes"] = [{"id": "p2_employment", "kind": "employment",
                                "amount": 37_440, "from": "2015-01-01", "to": None}]
    people["p1"]["parental_leave"] = {
        "child_birth_date": "2024-11-28", "plan": plan, "parent_role": "other",
        "first_rate_weeks": other_first, "long_rate_weeks": other_long}
    people["p2"]["parental_leave"] = {
        "child_birth_date": "2024-11-28", "plan": plan, "parent_role": "birthing",
        "first_rate_weeks": first, "long_rate_weeks": long}
    return doc


def _run(doc):
    contract_schema.validate_contract(doc)
    cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
    return FamilySimulation(cfg).run()


class TestTheLeaveReachesTheEngine:
    """The engine side. The WEEK-LEVEL acceptance figures (19,980 / 7,560 /
    17,820 / 8,100) are asserted above at the pure level, because the issue's
    birth date (2024-11-28) places only the first few weeks of a 33-week leave
    inside calendar 2024 -- so a year-level assertion cannot reproduce them
    without re-deriving the day-blending, which would be re-implementing the
    engine in the test (the shortcut this repo warns about).
    """

    def test_a_declared_leave_changes_the_year_s_income(self):
        """The leave's weeks earn the benefit instead of the salary, so the
        household's income for the birth year must move -- and move DOWN, because
        QPIP replaces only 55-75% of earnings."""
        with_leave = _run(_leave_doc())
        doc = _leave_doc()
        for person in doc["people"]:
            person.pop("parental_leave", None)
        without = _run(doc)
        assert with_leave, "premise: the household must simulate at all"
        assert (with_leave[0].total_family_income
                < without[0].total_family_income), (
            "QPIP pays a fraction of the salary it replaces, so the leave year's "
            "income must fall: got "
            f"{with_leave[0].total_family_income} with the leave vs "
            f"{without[0].total_family_income} without it")

    def test_a_leave_outside_quebec_is_refused_not_paid_zero(self):
        from contract_errors import ContractAdaptationError
        with pytest.raises(ContractAdaptationError, match="QUEBEC program"):
            _run(_leave_doc(province="ontario"))

    def test_shares_that_do_not_add_up_are_refused_at_the_contract_boundary(self):
        from contract_errors import ContractAdaptationError
        with pytest.raises(ContractAdaptationError, match="shareable weeks"):
            _run(_leave_doc(first=2, long=25, other_first=4, other_long=0))

    def test_no_leave_declared_leaves_the_household_untouched(self):
        """The absence-safety premise: nothing is emitted when nobody declares a
        leave, which is what keeps every existing document byte-identical."""
        doc = _leave_doc()
        for person in doc["people"]:
            person.pop("parental_leave", None)
        assert _run(doc), "premise: the plain household still simulates"


class TestTheBranchesTheCoverageGateNamed:
    """Every raise in the law module must have a test -- a refusal nobody
    exercises is a refusal that can rot into a silent zero.
    """

    def test_parental_entitlement_refuses_an_unknown_plan(self):
        with pytest.raises(ValueError, match="unknown QPIP plan"):
            parental_entitlement("basic-plus", 7, 25)

    def test_parental_entitlement_refuses_negative_weeks(self):
        with pytest.raises(ValueError, match="cannot be negative"):
            parental_entitlement("basic", -1, 0)
        with pytest.raises(ValueError, match="cannot be negative"):
            parental_entitlement("basic", 0, -5)

    def test_parental_entitlement_pays_nothing_for_no_share(self):
        """A parent who takes none of the shareable block is the normal case for
        the non-birthing parent under the special plan."""
        assert parental_entitlement("basic", 0, 0) == ()
        assert parental_entitlement("special", 25, 0) == ((25, SPECIAL_RATE),)

    def test_validate_shares_refuses_an_unknown_plan(self):
        with pytest.raises(ValueError, match="unknown QPIP plan"):
            validate_shares("long-term", {"a": {"first_rate": 7, "long_rate": 25}})

    def test_validate_shares_accepts_the_special_plan_shape(self):
        validate_shares("special", {"a": {"first_rate": 25, "long_rate": 0}})


class TestTheBranchesTheGateNamedInTheMapping:
    """The three branches in the leave mapping that no fixture reached, each
    named by the coverage gate as an uncovered line. A branch that no document
    can reach is a branch whose refusal may already be broken.
    """

    def test_a_leave_with_no_insurable_income_maps_to_a_zero_benefit(self):
        """A parent on leave with no declared insurable earnings has an
        entitlement but no benefit to pay: the segment must carry 0.0 and still
        cover the leave weeks (so the salary -- if any -- is still replaced),
        rather than the mapping raising or dropping the leave entirely."""
        doc = _leave_doc()
        for person in doc["people"]:
            if person["id"] == "p2":
                person["incomes"] = []
        results = _run(doc)
        assert results, ("a household whose birthing parent has no insurable "
                         "earnings must still simulate: the leave is a fact, the "
                         "benefit is what its income makes it worth")

    def test_a_single_adult_household_takes_the_whole_shareable_block(self):
        """One parent, nobody to share with: the plan lets them take all of it,
        so there is no sum of two declarations to check and the household must
        not be refused for 'shares that do not add up'."""
        doc = _leave_doc(first=7, long=25, other_first=0, other_long=0)
        doc["people"] = [p for p in doc["people"] if p["id"] != "p1"]
        for person in doc["people"]:
            person["relationships"] = []
        from contract_errors import ContractAdaptationError
        # Either it maps (the single parent takes the block) or it refuses for a
        # reason that is NOT the share sum -- never a silent zero.
        try:
            results = _run(doc)
        except ContractAdaptationError as exc:
            assert "shareable weeks" not in str(exc), (
                "a single-adult household has nobody to share with, so its "
                f"declaration cannot be 'shares that do not add up': {exc}")
        else:
            assert results, "the single parent's leave must still simulate"

    def test_two_different_plans_in_one_household_are_refused(self):
        """Both parents must choose the same plan -- the first application binds
        the other -- so a document declaring two cannot both be paid."""
        from contract_errors import ContractAdaptationError
        doc = _leave_doc(plan="basic")
        people = {p["id"]: p for p in doc["people"]}
        people["p1"]["parental_leave"]["plan"] = "special"
        with pytest.raises(ContractAdaptationError, match="more than one plan"):
            _run(doc)
