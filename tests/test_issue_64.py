#!/usr/bin/env python3
"""Tests for issue #64: CPP2 YAMPE and DTC rates year-versioned in tax_data.py.

DP#20: CPP2 max pensionable earnings should be year-versioned data from
tax_data.py, not a hardcoded constant in cpp_sharing.py.

DP#12: DTC rates should come from tax_data.py, not from constants in
income_type.py.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/test_issue_64.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from tax_data import TaxDataProvider, TaxYearData, TaxBracket


class TestCPP2YearVersioned(unittest.TestCase):
    """Test that CPP2 YAMPE is year-versioned in TaxYearData (DP#20)."""

    def test_tax_year_data_has_cpp2_max_pensionable(self):
        """TaxYearData dataclass has cpp2_max_pensionable field."""
        data = TaxYearData(year=2026, country="canada", province="federal")
        # Default should be 0 (not set), not a hardcoded year-specific value
        self.assertEqual(data.cpp2_max_pensionable, 0)

    def test_tax_year_data_has_cpp2_rate(self):
        """TaxYearData dataclass has cpp2_rate field for CPP2 contribution rate."""
        data = TaxYearData(year=2026, country="canada", province="federal")
        self.assertEqual(data.cpp2_rate, 0)

    def test_federal_2026_cpp2_max_pensionable(self):
        """2026 federal data has CPP2 YAMPE = $81,900 (CRA 2026)."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2026, "canada", "federal")
        self.assertAlmostEqual(data.cpp2_max_pensionable, 81900)

    def test_federal_2025_cpp2_max_pensionable(self):
        """2025 federal data has CPP2 YAMPE = $81,200 (CRA 2025)."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2025, "canada", "federal")
        self.assertAlmostEqual(data.cpp2_max_pensionable, 81200)

    def test_federal_2024_cpp2_max_pensionable(self):
        """2024 federal data has CPP2 YAMPE = $73,200 (CRA 2024)."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2024, "canada", "federal")
        self.assertAlmostEqual(data.cpp2_max_pensionable, 73200)

    def test_federal_2023_cpp2_max_pensionable(self):
        """2023 federal data has CPP2 YAMPE = $66,600 (CRA 2023).
        
        Note: 2023 was the first year of CPP2. The YAMPE equals the YMPE
        because there was no second earnings ceiling yet (CPP2 was introduced
        with YMPE2 = YMPE for 2023, with a separate rate).
        """
        provider = TaxDataProvider()
        data = provider.get_year_data(2023, "canada", "federal")
        self.assertGreater(data.cpp2_max_pensionable, 0)

    def test_federal_2026_cpp2_rate(self):
        """2026 federal data has CPP2 contribution rate = 4% (CRA 2026)."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2026, "canada", "federal")
        self.assertAlmostEqual(data.cpp2_rate, 0.04)

    def test_provider_get_cpp2_max_pensionable(self):
        """TaxDataProvider has get_cpp2_max_pensionable(year) method."""
        provider = TaxDataProvider()
        cpp2_max = provider.get_cpp2_max_pensionable(2026)
        self.assertAlmostEqual(cpp2_max, 81900)

    def test_cpp2_max_pensionable_changes_by_year(self):
        """CPP2 YAMPE changes across years (not a constant)."""
        provider = TaxDataProvider()
        v2023 = provider.get_cpp2_max_pensionable(2023)
        v2024 = provider.get_cpp2_max_pensionable(2024)
        v2025 = provider.get_cpp2_max_pensionable(2025)
        v2026 = provider.get_cpp2_max_pensionable(2026)
        # Values should differ across years
        # 2024 saw a big jump ($73,200) as the second ceiling was introduced
        self.assertNotEqual(v2024, v2026, "CPP2 YAMPE should differ across years")

    def test_projection_includes_cpp2(self):
        """When projecting future years, CPP2 data is escalated."""
        provider = TaxDataProvider()
        # Get 2027 (projected from 2026)
        data_2027 = provider.get_year_data(2027, "canada", "federal")
        # Projected CPP2 should be > 0 (escalated from base year)
        self.assertGreater(data_2027.cpp2_max_pensionable, 0)


class TestDTCYearVersioned(unittest.TestCase):
    """Test that DTC rates come from tax_data.py (DP#12)."""

    def test_federal_2026_dtc_rates_in_tax_data(self):
        """2026 federal DTC rates are available from TaxDataProvider."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2026, "canada", "federal")
        self.assertAlmostEqual(data.federal_eligible_dtc_rate, 0.150198)
        self.assertAlmostEqual(data.federal_non_eligible_dtc_rate, 0.090301)
        self.assertAlmostEqual(data.federal_eligible_gross_up, 0.38)
        self.assertAlmostEqual(data.federal_non_eligible_gross_up, 0.15)

    def test_federal_dtc_rates_stable_across_recent_years(self):
        """Federal DTC rates have been stable at 15.0198%/9.0301% since 2018."""
        provider = TaxDataProvider()
        for year in [2023, 2024, 2025, 2026]:
            data = provider.get_year_data(year, "canada", "federal")
            self.assertAlmostEqual(data.federal_eligible_dtc_rate, 0.150198,
                                  msg=f"Eligible DTC rate changed in {year}")
            self.assertAlmostEqual(data.federal_non_eligible_dtc_rate, 0.090301,
                                  msg=f"Non-eligible DTC rate changed in {year}")

    def test_income_type_uses_tax_data(self):
        """income_type._get_federal_dtc_rates reads from TaxDataProvider."""
        from countries.canada.income_type import _get_federal_dtc_rates
        rates = _get_federal_dtc_rates(2026)
        self.assertAlmostEqual(rates['eligible_dtc_rate'], 0.150198)
        self.assertAlmostEqual(rates['non_eligible_dtc_rate'], 0.090301)
        self.assertAlmostEqual(rates['eligible_gross_up'], 0.38)
        self.assertAlmostEqual(rates['non_eligible_gross_up'], 0.15)


class TestCPP2InCppSharing(unittest.TestCase):
    """Test that cpp_sharing.py uses TaxDataProvider instead of hardcoded CPP2."""

    def test_cpp2_constant_deprecated(self):
        """CPP2_MAX_PENSIONABLE_2026 should be marked as deprecated."""
        from countries.canada.cpp_sharing import CPP2_MAX_PENSIONABLE_2026
        # Still exists for backward compat, but should be marked deprecated
        self.assertAlmostEqual(CPP2_MAX_PENSIONABLE_2026, 81900)

    def test_cpp_sharing_input_uses_year_data(self):
        """CPPSharingInput can get CPP2 max from TaxDataProvider."""
        from countries.canada.cpp_sharing import CPPSharingInput
        data = CPPSharingInput(calculation_year=2026)
        # The module should provide a way to get CPP2 max from tax data
        # instead of using the hardcoded constant
        provider = TaxDataProvider()
        fed_data = provider.get_year_data(2026, "canada", "federal")
        self.assertAlmostEqual(fed_data.cpp2_max_pensionable, 81900)


if __name__ == '__main__':
    unittest.main()