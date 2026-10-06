"""Issue #307: the benefit-side CPP2 ceiling must be the CRA's published AYMPE.

The CPP2 pension estimate is sized against the band between the year's
Maximum Pensionable Earnings (YMPE) and the year's Additional Maximum
Pensionable Earnings (AYMPE), a.k.a. the second earnings ceiling. The benefit
side carried figures the CRA never published, so the band was too narrow and
the estimated CPP2 pension for an earner between the true and the stored
ceiling was overstated.

Source for every figure below (AYMPE, 4% rate, maximum employee contribution):
https://www.canada.ca/en/revenue-agency/services/tax/businesses/topics/payroll/calculating-deductions/making-deductions/second-additional-cpp-contribution-rates-maximums.html
"""

import pytest

from countries.canada.cpp_estimator import EarningsEntry, _yampe_for_year, compute_benefit_estimate
from countries.canada.retirement import (
    CPP2_MAX_BENEFIT,
    CPP2_MAX_PENSIONABLE,
    CPP_MAX_PENSIONABLE,
    CPP_OAS_BY_YEAR,
    cpp2_benefit,
)

# CRA "Second additional CPP (CPP2) contribution rates and maximums", table
# "CPP2 contribution rates and maximums": 2026 $85,000 / 4% / $416,
# 2025 $81,200 / 4% / $396, 2024 $73,200 / 4% / $188.
CRA_AYMPE = {2024: 73200, 2025: 81200, 2026: 85000}

CPP2_START_YEAR = 2024


def _published_range(year: int) -> float:
    """Earnings band CPP2 contributions accrue over, per the CRA ceilings."""
    return CRA_AYMPE[year] - CPP_OAS_BY_YEAR[year]["cpp_max_pensionable"]


class TestBenefitSideCeiling:
    def test_2024_aympe_is_the_published_ceiling(self):
        assert CPP_OAS_BY_YEAR[2024]["cpp2_max_pensionable"] == CRA_AYMPE[2024]

    def test_2025_aympe_is_the_published_ceiling(self):
        assert CPP_OAS_BY_YEAR[2025]["cpp2_max_pensionable"] == CRA_AYMPE[2025]

    def test_2026_aympe_is_the_published_ceiling(self):
        assert CPP_OAS_BY_YEAR[2026]["cpp2_max_pensionable"] == CRA_AYMPE[2026]

    def test_every_second_ceiling_year_is_pinned(self):
        """A new row past the CPP2 start year needs a sourced figure, not a guess."""
        unverified = sorted(
            year for year in CPP_OAS_BY_YEAR
            if year >= CPP2_START_YEAR and year not in CRA_AYMPE
        )
        assert unverified == []

    def test_module_fallback_is_the_2026_ceiling(self):
        """CPP2_MAX_PENSIONABLE is declared in the "CPP 2026 parameters" block."""
        assert CPP2_MAX_PENSIONABLE == CRA_AYMPE[2026]

    def test_2024_band_is_4700(self):
        """CRA: 4% of the 2024 band is the $188 maximum employee contribution."""
        assert _published_range(2024) * 0.04 == pytest.approx(188, abs=0.005)

    def test_2025_band_is_9900(self):
        """CRA: 4% of the 2025 band is the $396 maximum employee contribution."""
        assert _published_range(2025) * 0.04 == pytest.approx(396, abs=0.005)

    def test_2026_band_is_10400(self):
        """CRA: 4% of the 2026 band is the $416 maximum employee contribution."""
        assert _published_range(2026) * 0.04 == pytest.approx(416, abs=0.005)

    def test_agrees_with_the_contribution_side(self):
        """Both sides read the same statutory ceiling (issue #307).

        2026 is the exception: the contribution-side row still carries 81,900
        and is corrected in #315, which cannot be asserted here without
        pinning a known-wrong number. It is pinned to the CRA figure above.
        """
        from tax_data import default_tax_provider

        provider = default_tax_provider()
        for year in (2024, 2025):
            assert (CPP_OAS_BY_YEAR[year]["cpp2_max_pensionable"]
                    == provider.get_cpp2_max_pensionable(year))


class TestCPP2BenefitProRation:
    """cpp2_benefit() sizes the pension against YMPE→AYMPE, so an earner under
    the second ceiling must be pro-rated, not handed the maximum."""

    def test_full_band_gets_the_maximum(self):
        assert cpp2_benefit(
            _published_range(2026), start_age=65, year=2026
        ) == pytest.approx(CPP2_MAX_BENEFIT, rel=1e-9)

    def test_earnings_500_below_the_ceiling_are_pro_rated(self):
        band = _published_range(2026)
        below = band - 500
        benefit = cpp2_benefit(below, start_age=65, year=2026)
        assert benefit == pytest.approx(CPP2_MAX_BENEFIT * below / band, rel=1e-9)
        assert benefit < CPP2_MAX_BENEFIT

    def test_earnings_above_the_ceiling_are_capped(self):
        band = _published_range(2026)
        assert cpp2_benefit(
            band + 50000, start_age=65, year=2026
        ) == pytest.approx(CPP2_MAX_BENEFIT, rel=1e-9)


class TestEstimatorUsesThePublishedCeiling:
    def test_yampe_matches_cra_for_every_second_ceiling_year(self):
        for year, published in CRA_AYMPE.items():
            assert _yampe_for_year(year) == published

    def test_estimate_below_the_ceiling_is_pro_rated(self):
        """An earner at $84,500 in 2026 did not reach the $85,000 ceiling."""
        at_ceiling = compute_benefit_estimate(
            [EarningsEntry(year=2026, employment_income=85000)], start_age=65
        )
        below_ceiling = compute_benefit_estimate(
            [EarningsEntry(year=2026, employment_income=84500)], start_age=65
        )
        assert at_ceiling.cpp2_age_65_monthly > below_ceiling.cpp2_age_65_monthly > 0
        band = _published_range(2026)
        assert (below_ceiling.cpp2_age_65_monthly / at_ceiling.cpp2_age_65_monthly
                == pytest.approx((band - 500) / band, rel=0.01))

# ---------------------------------------------------------------------------
# Issue #411 follow-on (raised by Cite on PR #411): the same rows also carry
# cpp2_max_benefit, the maximum CPP2 pension at 65 for a full-range earner.
# Correcting the ceiling without correcting this left the rows internally
# inconsistent with the statutory 4% rate.
# ---------------------------------------------------------------------------

CPP2_RATE = 0.04


class TestCpp2MaxBenefitMatchesStatutoryRate:
    """cpp2_max_benefit is 4% of the year's CPP2 band, by definition.

    cpp2_benefit() returns max_benefit * (capped_earnings / (AYMPE - YMPE)),
    so for an earner at the full ceiling the stored value IS the annual
    pension. Since the statutory CPP2 rate is 4%, that value must be exactly
    4% of the band. Anything above 4% is arithmetically impossible: nobody can
    draw a CPP2 pension larger than the maximum they could have contributed.
    """

    @pytest.mark.parametrize("year", [2024, 2025, 2026])
    def test_implied_rate_is_the_statutory_four_percent(self, year):
        row = CPP_OAS_BY_YEAR[year]
        band = row["cpp2_max_pensionable"] - row["cpp_max_pensionable"]
        assert band > 0
        implied = row["cpp2_max_benefit"] / band
        assert implied == pytest.approx(CPP2_RATE, abs=1e-9), (
            f"{year}: cpp2_max_benefit {row['cpp2_max_benefit']} over a band of "
            f"{band} implies {implied:.4%}, not the statutory {CPP2_RATE:.0%}"
        )

    @pytest.mark.parametrize("year,expected", [(2024, 188), (2025, 396), (2026, 416)])
    def test_values_match_the_cra_published_maximum_contribution(self, year, expected):
        """4% of band reproduces the CRA's published maximum employee contribution.

        CRA "Second additional CPP (CPP2) contribution rates and maximums":
        2024 $188, 2025 $396, 2026 $416. These are 100% of the maximum
        contribution, and the maximum CPP2 pension at 65 is 100% of that.
        """
        assert CPP_OAS_BY_YEAR[year]["cpp2_max_benefit"] == expected

    def test_module_default_matches_the_2026_row(self):
        """The unknown-year fallback is labelled 2026, so it must equal 2026."""
        assert CPP2_MAX_BENEFIT == CPP_OAS_BY_YEAR[2026]["cpp2_max_benefit"]
        assert CPP2_MAX_BENEFIT == 416

    @pytest.mark.parametrize("year,expected", [(2024, 188), (2025, 396), (2026, 416)])
    def test_full_range_earner_receives_exactly_the_stored_maximum(self, year, expected):
        """End to end: an earner at the ceiling gets the maximum, via the real fold."""
        row = CPP_OAS_BY_YEAR[year]
        band = row["cpp2_max_pensionable"] - row["cpp_max_pensionable"]
        benefit = cpp2_benefit(
            earnings_above_ympe=band, years_contributing=40, start_age=65, year=year
        )
        assert benefit == pytest.approx(expected, abs=0.01)

    def test_2025_is_no_longer_the_2024_value(self):
        """Regression guard for the specific defect Cite reported."""
        assert CPP_OAS_BY_YEAR[2025]["cpp2_max_benefit"] != CPP_OAS_BY_YEAR[2024]["cpp2_max_benefit"]


class EveryRowImpliesTheStatutoryAccrualRate:
    """The stored CPP2 maximum must be 4% of that year's second-ceiling band.

    This is the identity that makes the table self-checking, and it is the
    check that was missing: the ceiling was pinned (CRA_AYMPE, above) but the
    BENEFIT the ceiling implies never was, so a stale copy of one year's
    benefit could sit in another year's row looking plausible. Two rounds of
    review flagged exactly that shape -- a 2025 row carrying 2024's value, and
    a 2026 row carrying a figure no rate can produce.

    CPP2 began in 2024, so a pre-2024 row carries no CPP2 benefit at all; the
    2023 row read 188 (a stale copy of 2024's) and now reads 0. Absence is
    treated as 0 there because for a year before CPP2 existed the two mean the
    same thing -- there is no second-ceiling pension to claim -- and the
    2023 second ceiling itself is issue #416's (PR #418) to correct.
    """

    ACCRUAL_RATE = 0.04   # CPP2: 4% of earnings between the YMPE and the AYMPE

    def test_pre_cpp2_rows_carry_no_cpp2_benefit(self):
        for year, row in CPP_OAS_BY_YEAR.items():
            if year >= CPP2_START_YEAR:
                continue
            assert row.get("cpp2_max_benefit", 0.0) == 0.0, (
                f"{year} predates CPP2 but stores a benefit of "
                f"{row.get('cpp2_max_benefit')!r}")

    def test_every_cpp2_row_implies_four_percent_of_its_band(self):
        checked = []
        for year, row in CPP_OAS_BY_YEAR.items():
            if year < CPP2_START_YEAR:
                continue
            band = row["cpp2_max_pensionable"] - row["cpp_max_pensionable"]
            stored = row["cpp2_max_benefit"]
            assert stored == pytest.approx(self.ACCRUAL_RATE * band, abs=0.01), (
                f"{year}: stored {stored} but 4% of the {band} band is "
                f"{self.ACCRUAL_RATE * band} -- one of the two is wrong")
            checked.append(year)
        assert checked, "no CPP2 year was checked; the table or the start year moved"

    def test_the_module_fallback_is_the_newest_published_row(self):
        """The fallback is what a year outside the table is priced at, so a
        fallback that disagrees with the newest row silently mis-prices every
        year the table has not caught up with."""
        newest = max(CPP_OAS_BY_YEAR)
        assert CPP2_MAX_BENEFIT == CPP_OAS_BY_YEAR[newest]["cpp2_max_benefit"]
