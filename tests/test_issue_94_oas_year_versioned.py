#!/usr/bin/env python3
"""Tests for issue #94: Replace OAS_ANNUAL_MAX and OAS_CLAWBACK_THRESHOLD constants
with year-versioned lookups (DP#20).

OAS amounts and clawback thresholds are indexed annually. Using 2026 constants
in a 10-year projection produces systematic error in OAS clawback calculations,
which directly affects RRSP vs TFSA optimization advice.

Per DP#20: Data is year-versioned; simulate across tax years, not within a
single year's brackets.
"""

import pytest
from countries.canada.retirement import (
    get_oas_annual_max,
    get_oas_annual_max_75plus,
    get_oas_clawback_threshold,
    oas_clawback,
    oas_amount_for_age,
    OAS_ANNUAL_MAX,
    OAS_ANNUAL_MAX_75PLUS,
    OAS_CLAWBACK_THRESHOLD,
    CPP_OAS_BY_YEAR,
)


class TestGetOasAnnualMax:
    """Test get_oas_annual_max() year-versioned lookup (DP#20)."""

    def test_2026_returns_known_value(self):
        """2026 OAS max should match the CPP_OAS_BY_YEAR data."""
        result = get_oas_annual_max(2026)
        assert result == CPP_OAS_BY_YEAR[2026]["oas_annual_max"]
        assert result == 8908  # Known 2026 value

    def test_2023_returns_earlier_value(self):
        """2023 OAS max should be lower than 2026 (indexed)."""
        result = get_oas_annual_max(2023)
        assert result == CPP_OAS_BY_YEAR[2023]["oas_annual_max"]
        assert result < 8908  # Indexed upward over time

    def test_2026_returns_current_default(self):
        """year=2026 returns the 2026 value (DP#9/DP#20: year is required)."""
        result = get_oas_annual_max(2026)
        assert result == OAS_ANNUAL_MAX

    def test_monotonically_increasing(self):
        """OAS amounts should be monotonically increasing (indexed to inflation)."""
        years = sorted(CPP_OAS_BY_YEAR.keys())
        for i in range(1, len(years)):
            prev = get_oas_annual_max(years[i - 1])
            curr = get_oas_annual_max(years[i])
            assert curr >= prev, (
                f"OAS annual max decreased from {years[i-1]} ({prev}) "
                f"to {years[i]} ({curr})"
            )


class TestGetOasAnnualMax75plus:
    """Test get_oas_annual_max_75plus() year-versioned lookup (DP#20)."""

    def test_2026_returns_known_value(self):
        """2026 OAS 75+ max should match the CPP_OAS_BY_YEAR data."""
        result = get_oas_annual_max_75plus(2026)
        assert result == CPP_OAS_BY_YEAR[2026]["oas_annual_max_75plus"]
        assert result == 9800  # Known 2026 value

    def test_75plus_greater_than_base(self):
        """OAS 75+ amount should always be greater than base (10% enhancement)."""
        years = sorted(CPP_OAS_BY_YEAR.keys())
        for year in years:
            base = get_oas_annual_max(year)
            enhanced = get_oas_annual_max_75plus(year)
            assert enhanced > base, (
                f"OAS 75+ ({enhanced}) should be > base ({base}) for {year}"
            )

    def test_2026_returns_current_default(self):
        """year=2026 returns the 2026 value (DP#9/DP#20: year is required)."""
        result = get_oas_annual_max_75plus(2026)
        assert result == OAS_ANNUAL_MAX_75PLUS


class TestGetOasClawbackThreshold:
    """Test get_oas_clawback_threshold() year-versioned lookup (DP#20)."""

    def test_2026_returns_known_value(self):
        """2026 clawback threshold should match CPP_OAS_BY_YEAR data."""
        result = get_oas_clawback_threshold(2026)
        assert result == CPP_OAS_BY_YEAR[2026]["oas_clawback_threshold"]
        assert result == 95323  # Known 2026 value

    def test_2023_returns_earlier_value(self):
        """2023 clawback threshold should be lower than 2026 (indexed)."""
        result = get_oas_clawback_threshold(2023)
        assert result == CPP_OAS_BY_YEAR[2023]["oas_clawback_threshold"]
        assert result < 95323

    def test_2026_returns_current_default(self):
        """year=2026 returns the 2026 value (DP#9/DP#20: year is required)."""
        result = get_oas_clawback_threshold(2026)
        assert result == OAS_CLAWBACK_THRESHOLD

    def test_monotonically_increasing(self):
        """Clawback thresholds should be monotonically increasing (indexed)."""
        years = sorted(CPP_OAS_BY_YEAR.keys())
        for i in range(1, len(years)):
            prev = get_oas_clawback_threshold(years[i - 1])
            curr = get_oas_clawback_threshold(years[i])
            assert curr >= prev, (
                f"OAS clawback threshold decreased from {years[i-1]} ({prev}) "
                f"to {years[i]} ({curr})"
            )


class TestOasClawbackYearVersioned:
    """Test oas_clawback() with year-versioned data (DP#20)."""

    def test_clawback_with_2023_threshold(self):
        """Using 2023 threshold should produce different clawback than 2026."""
        result_2023 = oas_clawback(net_income=95000, year=2023)
        result_2026 = oas_clawback(net_income=95000, year=2026)
        # 2023 threshold is lower, so more clawback
        assert result_2023['clawback_amount'] > result_2026['clawback_amount']

    def test_clawback_with_year_uses_year_versioned_values(self):
        """oas_clawback(year=2023) should use 2023 OAS and threshold."""
        result = oas_clawback(net_income=95000, year=2023)
        # Verify threshold and OAS match 2023 data
        assert result['threshold'] == CPP_OAS_BY_YEAR[2023]["oas_clawback_threshold"]
        assert result['net_oas'] > 0  # Not fully clawed back

    def test_clawback_without_year_uses_2026(self):
        """oas_clawback() without year uses 2026 default (DP#9/DP#20)."""
        result = oas_clawback(net_income=100000)
        assert result['threshold'] == OAS_CLAWBACK_THRESHOLD
        assert result['net_oas'] < OAS_ANNUAL_MAX  # Partial clawback


class TestOasAmountForAgeYearVersioned:
    """Test oas_amount_for_age() with year-versioned data (DP#20)."""

    def test_age_65_with_year_2023(self):
        """Age 65 in 2023 should return 2023 OAS base amount."""
        result = oas_amount_for_age(65, year=2023)
        assert result == CPP_OAS_BY_YEAR[2023]["oas_annual_max"]

    def test_age_75_with_year_2023(self):
        """Age 75+ in 2023 should return 2023 OAS 75+ amount."""
        result = oas_amount_for_age(75, year=2023)
        assert result == CPP_OAS_BY_YEAR[2023]["oas_annual_max_75plus"]

    def test_age_74_vs_75_enhancement(self):
        """Age 75+ should get more than age 74 for any year."""
        for year in CPP_OAS_BY_YEAR:
            base = oas_amount_for_age(74, year=year)
            enhanced = oas_amount_for_age(75, year=year)
            assert enhanced > base, f"OAS 75+ ({enhanced}) should be > base ({base}) for {year}"


class TestTaxDataProviderOasMethods:
    """Test TaxDataProvider.get_oas_*() methods (DP#20)."""

    def test_get_oas_annual_max(self):
        """TaxDataProvider.get_oas_annual_max returns year-specific values."""
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val_2026 = provider.get_oas_annual_max(2026)
        val_2024 = provider.get_oas_annual_max(2024)
        # Both should return positive values
        assert val_2026 > 0
        assert val_2024 > 0

    def test_get_oas_clawback_threshold(self):
        """TaxDataProvider.get_oas_clawback_threshold returns year-specific values."""
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val_2026 = provider.get_oas_clawback_threshold(2026)
        val_2024 = provider.get_oas_clawback_threshold(2024)
        assert val_2026 > 0
        assert val_2024 > 0

    def test_get_oas_annual_max_75plus(self):
        """TaxDataProvider.get_oas_annual_max_75plus returns year-specific values."""
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val_2026 = provider.get_oas_annual_max_75plus(2026)
        val_2024 = provider.get_oas_annual_max_75plus(2024)
        assert val_2026 > 0
        assert val_2024 > 0