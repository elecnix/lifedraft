"""Issue #427: ``_ympe_for_year`` projected a year BACKWARDS past a published one.

The forward extrapolation anchored on ``max(_HISTORICAL_YMPE.keys())``, which is
2022, and ignored the four newer years already published in ``CPP_OAS_BY_YEAR``.
Measured before the fix:

    _ympe_for_year(2026) -> 74,600.00    from CPP_OAS_BY_YEAR  (correct)
    _ympe_for_year(2027) -> 71,654.84    extrapolated from 2022  (WRONG)

The projection lands **2,945 below** a value that is already known.

## The invariant, which needs no external source to test

A projection may be wrong by a little. It may not run backwards past an actual.
The YMPE is indexed and has never decreased, so for every year ``y`` with a
published value, a projection for ``y+1`` must exceed it. That holds for every
real schedule and fails here -- which is why this test can be written without
citing anything.

The specific failure mode is the shape of the bug rather than the numbers: an
anchor chosen from one of two sources while the other held newer data.
"""

import countries.canada.cpp_estimator as est
from countries.canada.retirement import CPP_OAS_BY_YEAR

PUBLISHED_YEARS = sorted(CPP_OAS_BY_YEAR)


class TestProjectionNeverRunsBackwardsPastAKnownYear:
    def test_the_first_projected_year_exceeds_the_last_published_one(self):
        last = PUBLISHED_YEARS[-1]
        projected = est._ympe_for_year(last + 1)
        assert projected > CPP_OAS_BY_YEAR[last]["cpp_max_pensionable"], (
            f"{last + 1} projected at {projected:.2f}, which is BELOW the "
            f"published {last} figure of "
            f"{CPP_OAS_BY_YEAR[last]['cpp_max_pensionable']:.2f}"
        )

    def test_monotonic_across_the_whole_projection_range(self):
        """Not just the first step: every projected year rises on its predecessor.

        A fix that special-cases 2027 would pass the first test and fail this.
        """
        last = PUBLISHED_YEARS[-1]
        previous = CPP_OAS_BY_YEAR[last]["cpp_max_pensionable"]
        for year in range(last + 1, last + 11):
            current = est._ympe_for_year(year)
            assert current > previous, (
                f"{year} ({current:.2f}) does not exceed {year - 1} ({previous:.2f})"
            )
            previous = current

    def test_a_projected_year_matches_growth_from_the_last_published_one(self):
        """The anchor must be the newest year available ACROSS BOTH sources.

        The defect was anchoring on ``max(_HISTORICAL_YMPE)`` = 2022 while
        ignoring 2023-2026. So the projection is pinned against the published
        2026 value, not the 2022 one.
        """
        last = PUBLISHED_YEARS[-1]
        expected = CPP_OAS_BY_YEAR[last]["cpp_max_pensionable"] * (1.02 ** 2)
        assert est._ympe_for_year(last + 2) == expected


class TestTheAnchorCannotDriftAwayFromTheNewestData:
    """Guards the class, not the instance.

    If a future edit adds a year to ``CPP_OAS_BY_YEAR`` while ``_HISTORICAL_YMPE``
    stays behind, the anchor must move with it.
    """

    def test_published_years_are_newer_than_the_historical_table(self):
        assert max(est._HISTORICAL_YMPE.keys()) < min(PUBLISHED_YEARS), (
            "this issue's defect assumed _HISTORICAL_YMPE stops before "
            "CPP_OAS_BY_YEAR begins; if that changed, re-derive the anchor"
        )

    def test_every_projection_anchor_is_the_newest_year_overall(self):
        anchor = max(max(est._HISTORICAL_YMPE.keys()), max(PUBLISHED_YEARS))
        assert anchor == PUBLISHED_YEARS[-1]
        # Projecting from `anchor` must reproduce what the function returns.
        expected = (
            est._HISTORICAL_YMPE[anchor]
            if anchor in est._HISTORICAL_YMPE
            else CPP_OAS_BY_YEAR[anchor]["cpp_max_pensionable"]
        ) * (1.02 ** 3)
        assert est._ympe_for_year(anchor + 3) == expected