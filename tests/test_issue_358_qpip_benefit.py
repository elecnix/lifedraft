"""Issue #358: QPIP / RQAP maternity, paternity and parental benefits.

The engine modelled the QPIP PREMIUM and never the benefit, so a Quebec
household having a child was mispriced by the whole replacement income.

The acceptance figures come from the CFFP case "Couple ayant un premier enfant":
primary at $1,080/week and spouse at $720/week of insurable earnings, child born
2024-11-28, with the 2024 maximum insurable earnings of $94,000.

Reading those figures carefully matters: the BIRTHING parent is the SPOUSE at
$720/week, not the primary. The reconciliations are 18x504 + 2x504 + 25x396 =
19,980 (the spouse: 504 = 720x0.70, 396 = 720x0.55) and 10x756 = 7,560 (the
primary: 756 = 1080x0.70). Attaching the maternity weeks to the higher earner
would produce plausible, wrong numbers.
"""

from datetime import date

import pytest

from countries.canada.provinces.quebec.qpip_benefit import (
    QPIPLeave,
    QPIP_MAX_INSURABLE_EARNINGS,
    qpip_benefit,
    qpip_household_benefits,
)

BIRTH = date(2024, 11, 28)
PRIMARY_WEEKLY = 1_080.0    # the non-birthing parent
SPOUSE_WEEKLY = 720.0       # the birthing parent


def _spouse(weeks, plan="basic", weekly=SPOUSE_WEEKLY):
    return QPIPLeave(person_id="spouse", event_date=BIRTH, plan=plan,
                     weekly_insurable_earnings=weekly, is_birthing_parent=True,
                     parental_weeks_taken=weeks)


def _primary(weeks, plan="basic", weekly=PRIMARY_WEEKLY):
    return QPIPLeave(person_id="primary", event_date=BIRTH, plan=plan,
                     weekly_insurable_earnings=weekly,
                     is_birthing_parent=False, parental_weeks_taken=weeks)


class TestTheCFFPAcceptanceFigures:
    def test_the_shared_70pct_split_is_DECLARED_not_inferred_from_order(self):
        """The CFFP split is 5 weeks to the other parent, 2 to the birthing.

        Inferring it from argument order would silently flip the answer, so the
        split is declared. With the birthing parent listed FIRST and her share
        declared as 2, the CFFP figures are still reproduced.
        """
        spouse = _spouse(27)
        spouse.shared_full_rate_weeks = 2
        primary = _primary(5)
        primary.shared_full_rate_weeks = 5
        r = qpip_household_benefits((spouse, primary))
        assert r["spouse"].total == pytest.approx(19_980.00)
        assert r["primary"].total == pytest.approx(7_560.00)

    def test_basic_plan_needs_the_HOUSEHOLD_because_the_70pct_block_is_shared(self):
        """The basic plan's first 7 parental weeks at 70% are a SHARED POOL.

        The CFFP arithmetic proves it: 18x504 + 2x504 + 25x396 = 19,980 gives
        the birthing parent only 2 of the 7 at 70%, because the OTHER parent
        takes 5. A per-person call would give each parent the whole 70% block
        and double-count it -- which is exactly why the per-person function
        alone returns 20,520 here and the household entry point is required.
        """
        spouse, primary = _spouse(27), _primary(5)
        spouse.shared_full_rate_weeks = 2
        primary.shared_full_rate_weeks = 5
        r = qpip_household_benefits((primary, spouse))
        assert r["primary"].total == pytest.approx(7_560.00), (
            f"expected 5 paternity + 5 parental weeks at 756; got {r['primary'].total!r}"
        )
        assert r["spouse"].total == pytest.approx(19_980.00), (
            f"expected 18x504 + 2x504 + 25x396 = 19,980; got {r['spouse'].total!r}"
        )
        assert len({id(x) for x in (spouse, primary)}) == 2

    def test_the_shared_block_is_not_double_counted(self):
        """Per-person would pay the 7 at 70% twice; the household pays it once."""
        spouse, primary = _spouse(27), _primary(5)
        spouse.shared_full_rate_weeks = 2
        primary.shared_full_rate_weeks = 5
        household = qpip_household_benefits((primary, spouse))
        per_person = qpip_benefit(spouse).total + qpip_benefit(primary).total
        assert household["spouse"].total + household["primary"].total < per_person
        assert household["spouse"].total + household["primary"].total == pytest.approx(27_540.00)

    def test_special_plan_birthing_parent_is_17820(self):
        """33 weeks at 75% of $720 = 540/week."""
        got = qpip_benefit(_spouse(18, plan="special")).total
        assert got == pytest.approx(17_820.00), (
            f"expected 15 maternity + 18 parental weeks at 540; got {got!r}"
        )

    def test_special_plan_other_parent_is_8100(self):
        """10 weeks at 75% of $1,080 = 810/week."""
        got = qpip_benefit(_primary(7, plan="special")).total
        assert got == pytest.approx(8_100.00), (
            f"expected 3 paternity + 7 parental weeks at 810; got {got!r}"
        )


class TestTheMaximumInsurableEarningsCap:
    def test_weekly_earnings_above_the_cap_are_capped(self):
        """2024 cap = 94,000/52 = 1,807.69 weekly."""
        above = qpip_benefit(_primary(0, weekly=5_000.0))
        at_cap = qpip_benefit(_primary(0, weekly=94_000.0 / 52))
        assert above.paternity == pytest.approx(at_cap.paternity)
        assert above.paternity == pytest.approx(5 * 0.70 * (94_000.0 / 52))

    def test_a_year_with_no_ceiling_refuses_rather_than_borrowing_one(self):
        leave = QPIPLeave(person_id="spouse", event_date=date(2019, 6, 1),
                          plan="basic", weekly_insurable_earnings=700.0,
                          is_birthing_parent=True, parental_weeks_taken=0)
        with pytest.raises(ValueError, match="maximum insurable earnings"):
            qpip_benefit(leave)


class TestTheParentalShareIsSplitBetweenParents:
    def test_the_two_parents_shares_are_independent(self):
        """The shareable weeks are split by declaration, not duplicated."""
        both = qpip_benefit(_spouse(7)).parental + qpip_benefit(_primary(5)).parental
        assert both == pytest.approx(7 * 0.70 * SPOUSE_WEEKLY
                                     + 5 * 0.70 * PRIMARY_WEEKLY)

    def test_parental_weeks_are_capped_at_the_plan_total(self):
        """A share cannot exceed the plan's parental weeks."""
        over = qpip_benefit(_spouse(99))
        full = qpip_benefit(_spouse(32))
        assert over.parental == pytest.approx(full.parental)

    def test_the_basic_plan_parental_is_progressive(self):
        """First 7 weeks at 70%, the next 25 at 55% -- not a flat rate."""
        p = qpip_benefit(_spouse(32)).parental
        assert p == pytest.approx(7 * 0.70 * SPOUSE_WEEKLY
                                  + 25 * 0.55 * SPOUSE_WEEKLY)
        flat = 32 * 0.70 * SPOUSE_WEEKLY
        assert p < flat, "the progressive tail must pay less than a flat 70%"


class TestTheThreePropertiesThatAreEasyToGetWrong:
    def test_the_benefit_is_taxable(self):
        assert qpip_benefit(_spouse(5)).taxable is True

    def test_only_the_birthing_parent_gets_maternity_weeks(self):
        """Paternity is the other parent's; maternity the birthing parent's."""
        birthing = qpip_benefit(_spouse(0))
        other = qpip_benefit(_primary(0))
        assert birthing.maternity > 0 and birthing.paternity == 0
        assert other.paternity > 0 and other.maternity == 0

    def test_an_unknown_plan_refuses(self):
        with pytest.raises(ValueError, match="unknown QPIP plan"):
            qpip_benefit(_spouse(0, plan="deluxe"))

    def test_negative_earnings_refuse(self):
        with pytest.raises(ValueError, match="negative"):
            qpip_benefit(_primary(0, weekly=-1.0))

    def test_a_leave_with_no_date_refuses(self):
        leave = QPIPLeave(person_id="spouse", event_date=None, plan="basic",
                          weekly_insurable_earnings=700.0,
                          is_birthing_parent=True, parental_weeks_taken=0)
        with pytest.raises(ValueError, match="birth or adoption date"):
            qpip_benefit(leave)


class TestTheCeilingsAreYearVersioned:
    def test_each_year_carries_its_own_ceiling(self):
        assert QPIP_MAX_INSURABLE_EARNINGS[2024] == 94_000.0
        assert len(set(QPIP_MAX_INSURABLE_EARNINGS.values())) == len(
            QPIP_MAX_INSURABLE_EARNINGS), "a copied ceiling is a stale ceiling"


class TestTheLegIsReachableFromAConfig:
    """The core is only half a feature until production can reach it.

    tests/architecture/test_unreached_rule_modules.py fired on the unwired
    version -- a module nothing calls contributes zero to every run while its
    own tests stay green. That is this class's reason to exist.
    """

    def _cfg(self):
        return {"people": [
            {"person_id": "spouse", "role": "spouse", "gross_income": 720.0 * 52,
             "parental_leave": {"child_birth_date": "2024-11-28",
                                "plan": "basic", "parent_role": "birthing",
                                "parental_weeks": 27,
                                "shared_full_rate_weeks": 2}},
            {"person_id": "primary", "role": "primary", "gross_income": 1080.0 * 52,
             "parental_leave": {"child_birth_date": "2024-11-28",
                                "plan": "basic", "parent_role": "other",
                                "parental_weeks": 5,
                                "shared_full_rate_weeks": 5}},
        ]}

    def test_the_leg_prices_the_cffp_household(self):
        from net_benefit_legs import qpip_benefit_total
        assert qpip_benefit_total(self._cfg()) == pytest.approx(27_540.00), (
            "7,560 (other parent) + 19,980 (birthing parent)"
        )

    def test_a_household_with_no_leave_is_byte_identical(self):
        from net_benefit_legs import qpip_benefit_total
        assert qpip_benefit_total({"people": []}) == 0.0
        assert qpip_benefit_total({}) == 0.0

    def test_a_leave_outside_quebec_is_still_priced_here(self):
        """The module is Quebec-owned; the jurisdiction gate is the caller's.

        Pinned so the behaviour is deliberate rather than accidental: this leg
        prices a declared leave, and refusing a non-Quebec declaration belongs
        at the contract boundary, not silently inside a benefit computation.
        """
        from net_benefit_legs import qpip_benefit_total
        cfg = self._cfg()
        cfg["tax"] = {"province": "ontario"}
        assert qpip_benefit_total(cfg) > 0.0

    def test_a_leave_without_a_person_id_refuses(self):
        from net_benefit_legs import qpip_benefit_total
        cfg = {"people": [{"role": "spouse", "gross_income": 37_000.0,
                           "parental_leave": {"child_birth_date": "2024-11-28",
                                              "plan": "basic",
                                              "parent_role": "birthing",
                                              "parental_weeks": 27}}]}
        with pytest.raises(ValueError, match="person_id"):
            qpip_benefit_total(cfg)

    def test_a_leave_without_a_birth_date_refuses(self):
        from net_benefit_legs import qpip_benefit_total
        cfg = {"people": [{"person_id": "spouse", "gross_income": 37_000.0,
                           "parental_leave": {"plan": "basic",
                                              "parent_role": "birthing",
                                              "parental_weeks": 27}}]}
        with pytest.raises(ValueError, match="child_birth_date"):
            qpip_benefit_total(cfg)

    def test_a_non_numeric_income_refuses(self):
        from net_benefit_legs import qpip_benefit_total
        cfg = {"people": [{"person_id": "spouse", "gross_income": "lots",
                           "parental_leave": {"child_birth_date": "2024-11-28",
                                              "plan": "basic",
                                              "parent_role": "birthing",
                                              "parental_weeks": 27}}]}
        with pytest.raises(ValueError, match="gross_income"):
            qpip_benefit_total(cfg)

    def test_the_two_parents_may_not_declare_different_plans(self):
        from net_benefit_legs import qpip_benefit_total
        cfg = self._cfg()
        cfg["people"][1]["parental_leave"]["plan"] = "special"
        with pytest.raises(ValueError, match="different QPIP plans"):
            qpip_benefit_total(cfg)


class TestEveryRefusalPathInTheHouseholdEntryPoint:
    """A refusal nobody exercises is a refusal nobody gets.

    Coverage named six of these lines when the gate first ran; all are the
    branches that REFUSE rather than guess, so leaving them untested would ship
    the loud failures without proof they fire.
    """

    def test_an_empty_household_returns_nothing(self):
        assert qpip_household_benefits(()) == {}

    def test_a_leave_with_no_date_refuses(self):
        leave = QPIPLeave("spouse", None, "basic", 700.0, True, 0)
        with pytest.raises(ValueError, match="birth or adoption date"):
            qpip_household_benefits((leave,))

    def test_an_unknown_plan_refuses(self):
        leave = QPIPLeave("spouse", BIRTH, "deluxe", 700.0, True, 0)
        with pytest.raises(ValueError, match="unknown QPIP plan"):
            qpip_household_benefits((leave,))

    def test_negative_earnings_refuse(self):
        leave = QPIPLeave("spouse", BIRTH, "basic", -1.0, True, 0)
        with pytest.raises(ValueError, match="negative"):
            qpip_household_benefits((leave,))

    def test_leaves_in_different_years_refuse(self):
        a = QPIPLeave("spouse", date(2024, 11, 28), "basic", 700.0, True, 5)
        b = QPIPLeave("primary", date(2025, 3, 1), "basic", 700.0, False, 5)
        with pytest.raises(ValueError, match="maximum insurable earnings is per year"):
            qpip_household_benefits((a, b))

    def test_without_a_declared_share_the_block_is_allocated_in_order(self):
        """The deterministic fallback, pinned so its behaviour is deliberate.

        Declaration order decides who consumes the shared 70 % block when
        neither parent declares a share. The birthing parent listed first takes
        all 7, which is the 20,520 answer -- correct as a fallback, and the
        reason the declared split exists.
        """
        spouse, primary = _spouse(27), _primary(5)
        r = qpip_household_benefits((spouse, primary))
        assert r["spouse"].total == pytest.approx(20_520.00)
        assert r["primary"].total == pytest.approx(6_750.00)

    def test_the_declared_split_is_what_produces_the_cffp_figures(self):
        """The contrast that makes the declared field worth its schema entry."""
        spouse, primary = _spouse(27), _primary(5)
        spouse.shared_full_rate_weeks = 2
        primary.shared_full_rate_weeks = 5
        ordered = qpip_household_benefits((spouse, primary))
        reversed_ = qpip_household_benefits((primary, spouse))
        assert ordered["spouse"].total == reversed_["spouse"].total == pytest.approx(19_980.00)
        assert ordered["primary"].total == reversed_["primary"].total == pytest.approx(7_560.00)


class TestTheLegsRefusalPaths:
    def test_a_non_dict_member_is_skipped(self):
        from net_benefit_legs import qpip_benefit_total
        cfg = {"people": ["nonsense", 42, None]}
        assert qpip_benefit_total(cfg) == 0.0

    def test_a_malformed_birth_date_refuses(self):
        from net_benefit_legs import qpip_benefit_total
        cfg = {"people": [{"person_id": "spouse", "gross_income": 37_000.0,
                           "parental_leave": {"child_birth_date": "2024-7-1",
                                              "plan": "basic",
                                              "parent_role": "birthing",
                                              "parental_weeks": 27}}]}
        with pytest.raises(ValueError, match="ISO date"):
            qpip_benefit_total(cfg)

    def test_non_numeric_weeks_refuse(self):
        from net_benefit_legs import qpip_benefit_total
        cfg = {"people": [{"person_id": "spouse", "gross_income": 37_000.0,
                           "parental_leave": {"child_birth_date": "2024-11-28",
                                              "plan": "basic",
                                              "parent_role": "birthing",
                                              "parental_weeks": "many"}}]}
        with pytest.raises(ValueError, match="parental_weeks"):
            qpip_benefit_total(cfg)
