"""Issue #369: the Multigenerational Home Renovation Tax Credit (ITA s.122.92).

A household that renovates a self-contained secondary unit inside its principal
residence qualifies for a federal REFUNDABLE credit from 2023. lifedraft could
not model it at all, so the renovation year's cash flow was understated by the
full amount.

## The statutory shape

The credit is A x B:

- **A** is the appropriate percentage for the year, which is the LOWEST FEDERAL
  BRACKET rate -- read from the year-versioned data, never hard-coded, because
  it moves (0.15 for 2024, 0.145 for 2025, 0.14 for 2026).
- **B** is the least of $50,000 and the qualifying expenditures.

s.122.92 makes the credit a deemed payment on account of tax, so it is
REFUNDABLE and reaches the household's cash regardless of whether it reduces
tax payable.

## A number in the source publication is wrong, deliberately not used

The CFFP scenario prints $3,131 for a $25,000 renovation in 2024, which is
$25,000 x 12.525% -- that is 15% after the 16.5% Quebec abatement. The
abatement reduces tax otherwise payable; it does not reduce a deemed payment on
account of tax. The statutory 2024 value is $25,000 x 15% = **$3,750**. The
tests assert $3,750, and say why, so a future reader does not "correct" them
back to the publication.

## Lifetime and per-property limits

At most $50,000 may be claimed across all claimants for the same qualifying
renovation, and there may be only ONE qualifying renovation per qualifying
individual over that individual's lifetime. A second renovation for the same
person gives $0.
"""

import pytest

from countries.canada.mg_reno_credit import (
    MHRTC_LIFETIME_CAP,
    RENOVATION_CREDIT_MAX,
    MHRTC_FIRST_YEAR,
    MultigenerationalRenovation,
    QualifyingIndividual,
    mg_reno_credit_for_year,
    mg_reno_from_property,
    multigenerational_reno_credit,
    qualifiers_from_people,
)

LOWEST_FEDERAL_RATE = {2023: 0.15, 2024: 0.15, 2025: 0.145, 2026: 0.14}


def _renovation(year: int, expenditures: float = 25_000.0) -> MultigenerationalRenovation:
    return MultigenerationalRenovation(
        year=year, qualifying_expenditures=expenditures,
        has_secondary_unit=True, qualifying_person_ids=("parent",),
    )


def _qualifiers(**kw) -> list:
    base = dict(person_id="parent", reached_65_by_year_end=True, dtc_eligible=False)
    base.update(kw)
    return [QualifyingIndividual(**base)]


class TestTheStatutoryShape:
    @pytest.mark.parametrize("year,rate", sorted(LOWEST_FEDERAL_RATE.items()))
    def test_credit_is_rate_times_expenditures(self, year, rate):
        got = multigenerational_reno_credit(_renovation(year), _qualifiers())
        assert got == pytest.approx(25_000.0 * rate)

    def test_the_2024_value_is_3750_not_the_published_3131(self):
        """The source publication's figure is post-abatement and wrong here.

        3,131 = 25,000 x 12.525%, i.e. 15% net of the 16.5% Quebec abatement.
        The credit is a deemed payment ON ACCOUNT OF tax, so the abatement does
        not reduce it: the statutory value is 25,000 x 15% = 3,750.
        """
        got = multigenerational_reno_credit(_renovation(2024), _qualifiers())
        assert got == pytest.approx(3_750.00)
        assert got != pytest.approx(3_131.00)

    @pytest.mark.parametrize(
        "expenditures,expected_base",
        [
            (25_000.0, 25_000.0),   # below the cap: uncapped
            (49_999.0, 49_999.0),   # one below the cap: still uncapped
            (50_000.0, 50_000.0),   # exactly the cap
            (60_000.0, 50_000.0),   # above: capped
            (250_000.0, 50_000.0),  # far above: still capped
        ],
    )
    def test_expenditures_are_capped_at_50000(self, expenditures, expected_base):
        got = multigenerational_reno_credit(
            _renovation(2024, expenditures), _qualifiers())
        assert got == pytest.approx(expected_base * 0.15)

    def test_the_cap_is_50000_and_not_hard_coded_per_year(self):
        assert RENOVATION_CREDIT_MAX == 50_000.0


class TestEligibilityIsDateComputed:
    def test_a_qualifying_individual_is_one_who_reached_65_by_year_end(self):
        """DP#1: the age test is dated, not a stored boolean."""
        too_young = _qualifiers(reached_65_by_year_end=False, dtc_eligible=False)
        assert multigenerational_reno_credit(_renovation(2024), too_young) == 0.0

    def test_the_18_plus_dtc_route_also_qualifies(self):
        under_65 = _qualifiers(reached_65_by_year_end=False, dtc_eligible=True)
        assert multigenerational_reno_credit(
            _renovation(2024), under_65) == pytest.approx(3_750.00)

    def test_a_60_year_old_with_no_dtc_eligibility_gets_nothing(self):
        sixty = _qualifiers(reached_65_by_year_end=False, dtc_eligible=False)
        assert multigenerational_reno_credit(_renovation(2024), sixty) == 0.0

    def test_a_unit_that_is_not_self_contained_does_not_qualify(self):
        reno = _renovation(2024)
        reno.has_secondary_unit = False
        assert multigenerational_reno_credit(reno, _qualifiers()) == 0.0

    def test_a_renovation_naming_a_person_who_does_not_qualify_gives_nothing(self):
        reno = MultigenerationalRenovation(
            year=2024, qualifying_expenditures=25_000.0,
            has_secondary_unit=True, qualifying_person_ids=("someone_else",),
        )
        assert multigenerational_reno_credit(reno, _qualifiers()) == 0.0


class TestTheLifetimeLimitIsEnforced:
    """s.122.92(4): ONE qualifying renovation per qualifying individual, ever.

    Not per year, and not per property -- per individual, for life.
    """

    def test_a_second_qualifying_renovation_for_the_same_person_gives_zero(self):
        assert multigenerational_reno_credit(
            _renovation(2025), _qualifiers(), prior_renovations=1) == 0.0

    def test_the_first_renovation_for_a_person_is_still_allowed(self):
        assert multigenerational_reno_credit(
            _renovation(2024), _qualifiers(), prior_renovations=0
        ) == pytest.approx(3_750.00)

    def test_the_lifetime_cap_is_one(self):
        assert MHRTC_LIFETIME_CAP == 1


class TestTheCreditDoesNotExistBeforeItDid:
    def test_2022_and_earlier_give_zero(self):
        for year in (2022, 2021, 2020):
            assert multigenerational_reno_credit(
                _renovation(year), _qualifiers()) == 0.0

    def test_2023_the_first_year_gives_a_credit(self):
        assert MHRTC_FIRST_YEAR == 2023
        assert multigenerational_reno_credit(
            _renovation(2023), _qualifiers()) > 0.0


class TestAbsenceIsARefusalNotAZero:
    """DP#32: a missing or impossible input must fail loudly.

    A silent zero here would be indistinguishable from "this household does not
    qualify", which is precisely the confusion that makes a wrong number
    survive.
    """

    def test_negative_expenditures_are_refused(self):
        with pytest.raises(ValueError, match="expenditure"):
            multigenerational_reno_credit(
                _renovation(2024, -1.0), _qualifiers())

    def test_a_missing_year_is_refused(self):
        reno = _renovation(2024)
        reno.year = None
        with pytest.raises(ValueError, match="year"):
            multigenerational_reno_credit(reno, _qualifiers())

    def test_a_renovation_naming_an_absent_person_is_refused(self):
        """An empty qualifying list cannot be 'nobody qualified' -- it is a gap."""
        reno = _renovation(2024)
        reno.qualifying_person_ids = ()
        with pytest.raises(ValueError, match="qualifying"):
            multigenerational_reno_credit(reno, _qualifiers())

    def test_prior_renovations_cannot_be_negative(self):
        with pytest.raises(ValueError, match="prior"):
            multigenerational_reno_credit(
                _renovation(2024), _qualifiers(), prior_renovations=-1)

# ── Reaching it from a contract document ────────────────────────────────────
#
# The pure credit above is only half the feature. These tests drive the path a
# real document takes: properties[].mg_reno -> the builder -> the credit, and
# they assert the ABSENCE case is byte-identical, because an unwired credit is
# a credit that cannot be reached and therefore cannot be trusted.


class TestTheBuilderReachesItFromAProperty:
    def _facts(self, **over):
        base = dict(renovation_date="2024-03-15",
                    qualifying_expenditures=25_000.0,
                    has_secondary_unit=True,
                    qualifying_person_ids=("parent",))
        base.update(over)
        return base

    def test_a_declared_renovation_builds_and_prices(self):
        r = mg_reno_from_property(self._facts(), [])
        assert r is not None
        assert r.year == 2024, "the year comes from the DATE, not a bare year"
        assert r.qualifying_expenditures == 25_000.0

    def test_an_absent_block_means_the_module_does_not_participate(self):
        """DP#16: byte-identical to a household that never renovated."""
        assert mg_reno_from_property(None, []) is None
        assert mg_reno_credit_for_year(None, []) == 0.0

    def test_a_present_but_empty_block_refuses_rather_than_crediting_zero(self):
        with pytest.raises(ValueError, match="empty"):
            mg_reno_from_property({}, [])

    @pytest.mark.parametrize("bad", ["2024-7-1", "not-a-date", "2024-13-01"])
    def test_a_malformed_renovation_date_refuses(self, bad):
        with pytest.raises(ValueError, match="renovation_date|ISO"):
            mg_reno_from_property(self._facts(renovation_date=bad), [])

    def test_a_missing_expenditure_refuses(self):
        f = self._facts()
        del f["qualifying_expenditures"]
        with pytest.raises(ValueError, match="qualifying_expenditures"):
            mg_reno_from_property(f, [])

    def test_a_boolean_expenditure_is_not_money(self):
        with pytest.raises(ValueError, match="not money|number"):
            mg_reno_from_property(self._facts(qualifying_expenditures=True), [])

    def test_an_empty_person_list_refuses(self):
        with pytest.raises(ValueError, match="qualifying_person_ids"):
            mg_reno_from_property(self._facts(qualifying_person_ids=[]), [])

    def test_prior_renovations_defaults_to_zero_and_carries_through(self):
        assert mg_reno_from_property(self._facts(), []).prior_qualifying_renovations == 0
        r = mg_reno_from_property(
            self._facts(prior_qualifying_renovations=1), [])
        qual = [QualifyingIndividual("parent", reached_65_by_year_end=True)]
        assert mg_reno_credit_for_year(r, qual) == 0.0, (
            "a second qualifying renovation for the same person gives $0"
        )


class TestTheAgeTestIsDateComputedAgainstTheRenovationYear:
    """DP#1: 65 is reached in a YEAR, not declared as a standing boolean."""

    def _people(self, birth_year):
        return [{"person_id": "parent", "role": "parent",
                 "birth_year": birth_year}]

    def test_a_parent_65_by_the_renovation_year_end_qualifies(self):
        r = mg_reno_from_property(
            {"renovation_date": "2024-03-15", "qualifying_expenditures": 25_000.0,
             "has_secondary_unit": True, "qualifying_person_ids": ("parent",)}, [])
        qual = qualifiers_from_people(self._people(1959))
        assert mg_reno_credit_for_year(r, qual) == pytest.approx(3_750.00)

    def test_a_parent_64_at_the_renovation_year_end_with_no_dtc_does_not(self):
        r = mg_reno_from_property(
            {"renovation_date": "2024-03-15", "qualifying_expenditures": 25_000.0,
             "has_secondary_unit": True, "qualifying_person_ids": ("parent",)}, [])
        qual = qualifiers_from_people(self._people(1960))
        assert mg_reno_credit_for_year(r, qual) == 0.0

    def test_the_same_person_qualifies_in_a_later_renovation_year(self):
        """The same birth year flips once time moves: proof it is date-computed."""
        r = mg_reno_from_property(
            {"renovation_date": "2026-03-15", "qualifying_expenditures": 25_000.0,
             "has_secondary_unit": True, "qualifying_person_ids": ("parent",)}, [])
        qual = qualifiers_from_people(self._people(1960))
        assert mg_reno_credit_for_year(r, qual) > 0.0

    def test_the_dtc_route_qualifies_under_65(self):
        r = mg_reno_from_property(
            {"renovation_date": "2024-03-15", "qualifying_expenditures": 25_000.0,
             "has_secondary_unit": True, "qualifying_person_ids": ("child",)}, [])
        qual = qualifiers_from_people(
            [{"person_id": "child", "birth_year": 2000, "dtc_eligible": True}])
        assert mg_reno_credit_for_year(r, qual) == pytest.approx(3_750.00)


class TestItIsReachableThroughAWholeDocument:
    """The end-to-end proof: a real contract, validated then priced."""

    def test_a_document_declaring_the_renovation_validates_and_prices_it(self):
        import copy
        import json
        import contract_errors
        import contract_schema
        import pathlib

        doc = json.loads(
            (pathlib.Path(__file__).resolve().parents[1] / "schema" / "example.json")
            .read_text(encoding="utf-8"))
        doc = copy.deepcopy(doc)
        doc.setdefault("properties", []).append({
            "id": "mg_reno_home",
            "owner": "primary",
            "kind": "principal",
            "value": {"amount": 500_000, "as_of": "2026-07-12"},
            "acb": 400_000,
            "designated_principal_residence_years": [{"from": "2026-01-01", "to": None}],
            "purchase": None,
            "mg_reno": {
                "renovation_date": "2024-03-15",
                "qualifying_expenditures": 25_000.0,
                "has_secondary_unit": True,
                "qualifying_person_ids": ["elder_parent"],
            },
        })
        errors = list(contract_schema.get_validator().iter_errors(doc))
        assert not errors, (
            "a document declaring mg_reno must validate: "
            + "; ".join(e.message for e in errors[:3])
        )
        block = doc["properties"][-1]["mg_reno"]
        r = mg_reno_from_property(block, [])
        members = doc.get("people", [])
        if isinstance(members, dict):
            members = members.get("members", [])
        else:
            members = doc.get("family", {}).get("members", [])
        # The example document's adults are nowhere near 65, so name a person
        # who IS: s.122.92(3) is date-computed against the renovation year, and
        # a young primary correctly earns $0 rather than being a bug.
        members = list(members) + [{"person_id": "elder_parent",
                                    "role": "parent", "birth_year": 1955}]
        qual = qualifiers_from_people(members)
        credit = mg_reno_credit_for_year(r, qual)
        assert credit == pytest.approx(3_750.00), (
            f"a $25,000 renovation in 2024 must be worth the statutory $3,750, "
            f"got {credit!r}"
        )


class TestItReachesTheObjective:
    """The leg itself, not just the builder.

    A module whose unit tests pass but which production never calls is the
    failure mode tests/architecture/test_unreached_rule_modules.py exists to
    catch -- and it had already caught this one once.
    """

    def _cfg(self, **over):
        cfg = {
            "people": [{"person_id": "elder_parent", "role": "parent",
                        "birth_year": 1955}],
            "properties": [{
                "mg_reno": {"renovation_date": "2024-03-15",
                            "qualifying_expenditures": 25_000.0,
                            "has_secondary_unit": True,
                            "qualifying_person_ids": ["elder_parent"]},
            }],
        }
        cfg.update(over)
        return cfg

    def test_the_leg_credits_25000_at_the_2024_rate(self):
        from net_benefit_legs import mg_reno_credit_total
        assert mg_reno_credit_total(self._cfg()) == pytest.approx(3_750.00)

    def test_a_household_with_no_renovation_is_byte_identical(self):
        """DP#16: the absence case is a real $0, not an unknown."""
        from net_benefit_legs import mg_reno_credit_total
        assert mg_reno_credit_total({"people": [], "properties": []}) == 0.0
        assert mg_reno_credit_total({}) == 0.0

    def test_an_unrelated_member_without_person_id_cannot_stop_a_run(self):
        """A module with nothing to contribute must not be able to raise.

        Found by the architecture suite: building qualifiers before knowing
        whether any renovation existed let a member's missing person_id break
        every run in the repo.
        """
        from net_benefit_legs import mg_reno_credit_total
        cfg = self._cfg()
        cfg["people"] = cfg["people"] + [{"role": "spouse", "id": "p1",
                                          "birth_year": 1980}]
        assert mg_reno_credit_total(cfg) == pytest.approx(3_750.00)

    def test_a_renovation_naming_an_unknown_person_REFUSES(self):
        """Not $0. A named person who is not in the household is a document gap.

        Crediting $0 here would be indistinguishable from a household that
        legitimately does not qualify -- which is the confusion that lets a
        wrong number survive (DP#32).
        """
        from net_benefit_legs import mg_reno_credit_total
        cfg = self._cfg()
        cfg["properties"][0]["mg_reno"]["qualifying_person_ids"] = ["nobody"]
        with pytest.raises(ValueError, match="not in the household"):
            mg_reno_credit_total(cfg)

    def test_the_second_renovation_for_the_same_person_is_worth_zero(self):
        from net_benefit_legs import mg_reno_credit_total
        cfg = self._cfg()
        cfg["properties"][0]["mg_reno"]["prior_qualifying_renovations"] = 1
        assert mg_reno_credit_total(cfg) == 0.0


class TestEveryRefusalPathIsReachable:
    """A refusal nobody exercises is a refusal nobody gets.

    Coverage named seven of these lines as uncovered when the gate first ran.
    They are all DEFENCES -- the branches that refuse rather than guess a
    number -- so leaving them untested would mean shipping exactly the loud
    failures this module exists to raise, with no proof they fire.
    """

    def test_a_qualifier_cannot_qualify_before_the_credit_existed(self):
        from countries.canada.mg_reno_credit import QualifyingIndividual
        q = QualifyingIndividual("p", reached_65_by_year_end=True)
        assert q.qualifies(2022) is False, "s.122.92 does not reach before 2023"

    def test_a_missing_federal_bracket_year_refuses_rather_than_assuming_a_rate(self):
        """No brackets is a data gap, not a rate of zero.

        Reached by calling the rate reader directly: a year before 2023
        short-circuits earlier in the credit function, so going through it would
        never arrive at the lookup this defends.
        """
        from unittest.mock import patch
        from countries.canada import mg_reno_credit as mod
        # _load_fed_data carries its own last-resort rate, so the only way to
        # reach this guard is a year whose federal record cannot be loaded at
        # all -- which is precisely the data gap it exists for.
        # the loader is imported INSIDE the function, so patch its source
        with patch("countries.canada.tax_calc._load_fed_data",
                   side_effect=KeyError("no federal schedule")):
            with pytest.raises(ValueError, match="brackets"):
                mod._lowest_federal_rate(2031)

    def test_a_pre_2023_renovation_short_circuits_before_any_rate_lookup(self):
        """s.122.92 does not reach 1999, so no brackets are needed to say $0."""
        r = MultigenerationalRenovation(
            year=1999, qualifying_expenditures=25_000.0,
            has_secondary_unit=True, qualifying_person_ids=("parent",))
        qual = [QualifyingIndividual("parent", reached_65_by_year_end=True)]
        assert multigenerational_reno_credit(r, qual) == 0.0

    def test_zero_expenditures_credit_nothing(self):
        """A $0 outlay is a real zero, not an unknown one."""
        assert multigenerational_reno_credit(
            _renovation(2024, 0.0), _qualifiers()) == 0.0

    def test_a_member_with_no_birth_year_refuses(self):
        from countries.canada.mg_reno_credit import qualifiers_from_people
        with pytest.raises(ValueError, match="birth_year"):
            qualifiers_from_people([{"person_id": "parent"}])

    def test_a_boolean_birth_year_is_not_a_year(self):
        from countries.canada.mg_reno_credit import qualifiers_from_people
        with pytest.raises(ValueError, match="boolean"):
            qualifiers_from_people([{"person_id": "p", "birth_year": True}])

    def test_a_member_with_no_person_id_is_skipped_not_refused(self):
        """DP#16: an unnamed member cannot be named by a block, so it is not
        this credit's business -- and must not be able to stop a run."""
        from countries.canada.mg_reno_credit import qualifiers_from_people
        out = qualifiers_from_people(
            [{"role": "spouse", "birth_year": 1980},
             {"person_id": "parent", "birth_year": 1955}])
        assert [q.person_id for q in out] == ["parent"]


class TestTheLegsDefencesAreReachable:
    def test_people_can_come_from_the_legacy_family_block(self):
        from net_benefit_legs import mg_reno_credit_total
        cfg = {"family": {"members": [{"person_id": "elder_parent",
                                       "birth_year": 1955}]},
               "properties": [{"mg_reno": {
                   "renovation_date": "2024-03-15",
                   "qualifying_expenditures": 25_000.0,
                   "has_secondary_unit": True,
                   "qualifying_person_ids": ["elder_parent"]}}]}
        assert mg_reno_credit_total(cfg) == pytest.approx(3_750.00)

    def test_a_renovation_with_no_people_block_at_all_refuses(self):
        """The document names qualifying people and supplies none to match."""
        from net_benefit_legs import mg_reno_credit_total
        cfg = {"properties": [{"mg_reno": {
            "renovation_date": "2024-03-15", "qualifying_expenditures": 25_000.0,
            "has_secondary_unit": True, "qualifying_person_ids": ["parent"]}}]}
        with pytest.raises(ValueError, match="no people block"):
            mg_reno_credit_total(cfg)

    def test_non_dict_property_entries_are_skipped_not_crashed_on(self):
        from net_benefit_legs import mg_reno_credit_total
        assert mg_reno_credit_total(
            {"people": [], "properties": ["nonsense", 42, None]}) == 0.0

    def test_two_qualifying_renovations_are_both_priced(self):
        from net_benefit_legs import mg_reno_credit_total
        block = {"renovation_date": "2024-03-15",
                 "qualifying_expenditures": 25_000.0,
                 "has_secondary_unit": True,
                 "qualifying_person_ids": ["elder_parent"]}
        cfg = {"people": [{"person_id": "elder_parent", "birth_year": 1955}],
               "properties": [{"mg_reno": block}, {"mg_reno": dict(block)}]}
        assert mg_reno_credit_total(cfg) == pytest.approx(7_500.00)


def test_a_missing_renovation_date_refuses_rather_than_defaulting_a_year():
    """No date means no year, and the credit is a function of its year's rate."""
    from countries.canada.mg_reno_credit import mg_reno_from_property
    facts = {"qualifying_expenditures": 25_000.0, "has_secondary_unit": True,
             "qualifying_person_ids": ["parent"]}
    with pytest.raises(ValueError, match="renovation_date"):
        mg_reno_from_property(facts, [])


def test_a_mg_reno_block_that_is_none_is_not_a_renovation():
    """`mg_reno: null` on a property means the feature is off for it.

    The leg must skip it rather than treat the absence as a $0 credit, which
    would make "did not renovate" and "renovated but unpriceable" identical.
    """
    from net_benefit_legs import mg_reno_credit_total
    cfg = {"people": [{"person_id": "elder_parent", "birth_year": 1955}],
           "properties": [{"mg_reno": None}]}
    assert mg_reno_credit_total(cfg) == 0.0


class TestTheEighteenFloorOnTheDtcRoute:
    """s.122.92(3) gives two routes and they are NOT symmetric.

    (a) reached 65 by the end of the renovation-period year, OR
    (b) is 18 or older at the end of that year AND DTC-eligible.

    The 18+ floor applies to route (b) only. Without it, a DTC-eligible minor
    made a renovation qualify -- up to the full $7,500 the statute does not
    allow. Cite caught this on PR #447.
    """

    def _renovation(self):
        return MultigenerationalRenovation(
            year=2024, qualifying_expenditures=50_000.0,
            has_secondary_unit=True, qualifying_person_ids=("child",))

    def test_a_dtc_eligible_minor_does_not_qualify(self):
        from countries.canada.mg_reno_credit import QualifyingIndividual
        minor = QualifyingIndividual(
            "child", birth_year=2010, dtc_eligible=True)  # 14 in 2024
        assert minor.qualifies(2024) is False
        assert multigenerational_reno_credit(self._renovation(), [minor]) == 0.0

    def test_the_same_person_qualifies_once_they_turn_eighteen(self):
        """The floor is a DATE, not a standing boolean (DP#1)."""
        from countries.canada.mg_reno_credit import QualifyingIndividual
        born_2006 = QualifyingIndividual("child", birth_year=2006, dtc_eligible=True)
        assert born_2006.qualifies(2024) is True   # 18 exactly
        assert born_2006.qualifies(2023) is False  # 17

    def test_an_adult_without_dtc_eligibility_still_does_not_qualify(self):
        from countries.canada.mg_reno_credit import QualifyingIndividual
        adult = QualifyingIndividual("child", birth_year=1990, dtc_eligible=False)
        assert adult.qualifies(2024) is False

    def test_reaching_65_qualifies_without_any_dtc_claim(self):
        """Route (a) needs no disability test at all."""
        from countries.canada.mg_reno_credit import QualifyingIndividual
        elder = QualifyingIndividual("parent", birth_year=1955, dtc_eligible=False)
        assert elder.qualifies(2024) is True

    def test_the_minor_route_is_closed_even_at_the_full_cap(self):
        """The point of the fix: the money, not just the predicate."""
        from countries.canada.mg_reno_credit import QualifyingIndividual
        minor = QualifyingIndividual("child", birth_year=2010, dtc_eligible=True)
        assert multigenerational_reno_credit(self._renovation(), [minor]) == 0.0, (
            "a DTC-eligible minor must not unlock the credit"
        )
