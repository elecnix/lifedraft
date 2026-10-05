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
    multigenerational_reno_credit,
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