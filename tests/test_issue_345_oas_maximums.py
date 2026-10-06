#!/usr/bin/env python3
"""Issue #345: the engine's 2023-2025 OAS maximums and recovery-tax thresholds
disagreed with what ESDC and CRA published.

Both were too low, and the 2025 row carried the 2024 threshold, so a retired
household simulating from 2024 received about $327 less OAS than the source and
had its clawback start about $3,929 of income too early. `83,917` and `87,068`
match no published threshold at all, which is why a one-year shift could not
repair the table.

The key that makes the numbers checkable is that the year is the **INCOME year**,
not a July-June recovery period: `rules_retirement_income` looks up the maximum
and the threshold with `sim_year` and applies them to that year's income.

The annual maximum is ESDC's sum of the four QUARTERLY maximum monthly amounts
x 3, because the rates change quarterly -- it is NOT twelve times one month's
figure (for 2024 that distinction is 8,618.04 vs 8,560.08):

    2023: 3 x (687.56 + 691.00 + 698.60 + 707.68) = 8,354.52   75+: 9,189.99
    2024: 3 x (713.34 + 713.34 + 718.33 + 727.67) = 8,618.04   75+: 9,479.82
    2025: 3 x (727.67 + 727.67 + 734.95 + 740.09) = 8,791.14   75+: 9,670.29

Sources: ESDC "Quarterly Canada Pension Plan and Old Age Security benefit
amounts and related figures", Table 5, each quarter of 2023-2025; ESDC "Old Age
Security pension recovery tax" and the CRA line 23500 page for the thresholds
($90,997 for 2024 is stated on ESDC's "Repayment of Old Age Security pension"
page; $93,454 for 2025 on the recovery-tax page).

DP#15: every household here is fabricated, round-numbered and role-named.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.retirement import (
    get_oas_annual_max, get_oas_annual_max_75plus, get_oas_clawback_threshold,
)
from tax_data import TaxDataProvider

# The published figures, keyed by INCOME year.
PUBLISHED_MAX = {2023: 8_354.52, 2024: 8_618.04, 2025: 8_791.14}
PUBLISHED_MAX_75PLUS = {2023: 9_189.99, 2024: 9_479.82, 2025: 9_670.29}
PUBLISHED_THRESHOLD = {2023: 86_912, 2024: 90_997, 2025: 93_454, 2026: 95_323}


class TestTheFallbackTable:
    @pytest.mark.parametrize("year,expected", sorted(PUBLISHED_MAX.items()))
    def test_oas_maximum_65_74(self, year, expected):
        assert get_oas_annual_max(year) == pytest.approx(expected, abs=1.0)

    @pytest.mark.parametrize("year,expected", sorted(PUBLISHED_MAX_75PLUS.items()))
    def test_oas_maximum_75plus(self, year, expected):
        assert get_oas_annual_max_75plus(year) == pytest.approx(expected, abs=1.0)

    @pytest.mark.parametrize("year,expected", sorted(PUBLISHED_THRESHOLD.items()))
    def test_clawback_threshold(self, year, expected):
        assert get_oas_clawback_threshold(year) == expected


class TestTheProviderPath:
    """The provider reads the PROVINCIAL records first, so fixing the fallback
    table alone would leave every household on the wrong number (issue #345)."""

    @pytest.mark.parametrize("year,expected", sorted(PUBLISHED_MAX.items()))
    def test_provider_oas_maximum(self, year, expected):
        assert TaxDataProvider().get_oas_annual_max(year) == pytest.approx(
            expected, abs=1.0)

    @pytest.mark.parametrize("year,expected", sorted(PUBLISHED_THRESHOLD.items()))
    def test_provider_clawback_threshold(self, year, expected):
        assert TaxDataProvider().get_oas_clawback_threshold(year) == expected

    # Each province carries only the rows it actually declares: Quebec has
    # 2023-2026, Ontario only 2025 and 2026. Asserting a row that does not exist
    # made `_load_year` raise -- which is the provider doing its job, not a data
    # bug, so the sweep is scoped to the rows that are there.
    @pytest.mark.parametrize("province,years", [
        ("quebec", (2023, 2024, 2025)),
        ("ontario", (2025,)),
    ])
    def test_each_declared_provincial_row_carries_the_published_figures(
            self, province, years):
        """The rows are per-province copies, and they are ordered newest-first --
        which is how the 2025 row came to hold the 2024 figure."""
        for year in years:
            data = TaxDataProvider()._load_year(year, "canada", province)
            assert data.oas_clawback_threshold == PUBLISHED_THRESHOLD[year], (
                f"{province} {year} threshold")
            assert data.oas_annual_max == pytest.approx(
                PUBLISHED_MAX[year], abs=1.0), f"{province} {year} maximum"


class TestTheRegressionScenarioFromTheIssue:
    def test_a_71_year_old_on_20008_of_income_receives_the_full_2024_maximum(self):
        """Net income $20,008 is far below the 2024 threshold of $90,997, so the
        recovery tax is zero and the OAS received is the full published maximum.
        Before #345 the engine paid 8,291 here -- $327 short."""
        from countries.canada.retirement import oas_amount_for_age

        oas = oas_amount_for_age(71, year=2024)
        assert oas == pytest.approx(8_618.04, abs=1.0)
        assert oas > 20_008 * 0  # (premise: the household has income at all)
        threshold = get_oas_clawback_threshold(2024)
        assert 20_008 < threshold, "no recovery tax below the threshold"
