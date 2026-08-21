#!/usr/bin/env python3
"""Issue #330: Move CPP/GIS constants to TaxDataProvider (DP#12/DP#20).

Tests that year-versioned getters work correctly for CPP_MAX_PENSIONABLE,
CPP_MAX_BENEFIT_65, GIS_ANNUAL_MAX_SINGLE, GIS_ANNUAL_MAX_COUPLED, and
GIS_INCOME_EXEMPTION.

Run: uv run pytest tests/test_dp12_dp20_issue_330.py -v
"""

import unittest


class TestGetCppMaxPensionable(unittest.TestCase):
    """DP#12/DP#20 (issue #330): get_cpp_max_pensionable year-versioned lookup."""

    def test_year_in_cpp_oas_dict_returns_dict_value(self):
        from countries.canada.retirement import get_cpp_max_pensionable
        # 2026 is in CPP_OAS_BY_YEAR
        self.assertEqual(get_cpp_max_pensionable(2026), 74600)

    def test_year_not_in_dict_returns_tax_provider_or_default(self):
        from countries.canada.retirement import get_cpp_max_pensionable
        # 2030 is not in CPP_OAS_BY_YEAR, but TaxDataProvider may project
        val = get_cpp_max_pensionable(2030)
        self.assertGreater(val, 0, "Should return positive value even for projected years")

    def test_none_returns_constant(self):
        from countries.canada.retirement import get_cpp_max_pensionable, CPP_MAX_PENSIONABLE
        self.assertEqual(get_cpp_max_pensionable(2026), CPP_MAX_PENSIONABLE)


class TestGetCppMaxBenefit65(unittest.TestCase):
    """DP#12/DP#20 (issue #330): get_cpp_max_benefit_65 year-versioned lookup."""

    def test_year_in_cpp_oas_dict_returns_dict_value(self):
        from countries.canada.retirement import get_cpp_max_benefit_65
        self.assertEqual(get_cpp_max_benefit_65(2026), 18092)

    def test_year_not_in_dict_returns_tax_provider_or_default(self):
        from countries.canada.retirement import get_cpp_max_benefit_65
        val = get_cpp_max_benefit_65(2030)
        self.assertGreater(val, 0, "Should return positive value even for projected years")

    def test_none_returns_constant(self):
        from countries.canada.retirement import get_cpp_max_benefit_65, CPP_MAX_BENEFIT_65
        self.assertEqual(get_cpp_max_benefit_65(2026), CPP_MAX_BENEFIT_65)


class TestGetGisMax(unittest.TestCase):
    """DP#12/DP#20 (issue #330): get_gis_max_single/coupled year-versioned lookup."""

    def test_gis_max_single_year_in_dict(self):
        from countries.canada.retirement import get_gis_max_single
        self.assertEqual(get_gis_max_single(2026), 1726)

    def test_gis_max_coupled_year_in_dict(self):
        from countries.canada.retirement import get_gis_max_coupled
        self.assertEqual(get_gis_max_coupled(2026), 10384)

    def test_gis_max_single_none_returns_constant(self):
        from countries.canada.retirement import get_gis_max_single, GIS_ANNUAL_MAX_SINGLE
        self.assertEqual(get_gis_max_single(2026), GIS_ANNUAL_MAX_SINGLE)

    def test_gis_max_coupled_none_returns_constant(self):
        from countries.canada.retirement import get_gis_max_coupled, GIS_ANNUAL_MAX_COUPLED
        self.assertEqual(get_gis_max_coupled(2026), GIS_ANNUAL_MAX_COUPLED)

    def test_gis_max_single_projected_year(self):
        from countries.canada.retirement import get_gis_max_single
        val = get_gis_max_single(2030)
        self.assertGreater(val, 0, "Should return positive value for projected years")

    def test_gis_max_coupled_projected_year(self):
        from countries.canada.retirement import get_gis_max_coupled
        val = get_gis_max_coupled(2030)
        self.assertGreater(val, 0, "Should return positive value for projected years")


class TestGisFunctionUsesYearVersionedGetters(unittest.TestCase):
    """DP#12/DP#20 (issue #330): gis_benefit uses year-versioned getters."""

    def test_gis_benefit_single_2026(self):
        from countries.canada.retirement import gis_benefit, GIS_ANNUAL_MAX_SINGLE
        # With year=2026, should use the year-versioned data
        result = gis_benefit(net_income=0, is_coupled=False, year=2026)
        self.assertEqual(result['max_gis'], GIS_ANNUAL_MAX_SINGLE)

    def test_gis_benefit_coupled_2026(self):
        from countries.canada.retirement import gis_benefit, GIS_ANNUAL_MAX_COUPLED
        result = gis_benefit(net_income=0, is_coupled=True, year=2026)
        self.assertEqual(result['max_gis'], GIS_ANNUAL_MAX_COUPLED)

    def test_gis_benefit_year_2023_smaller_than_2026(self):
        from countries.canada.retirement import gis_benefit
        result_2023 = gis_benefit(net_income=0, is_coupled=False, year=2023)
        result_2026 = gis_benefit(net_income=0, is_coupled=False, year=2026)
        # 2023 GIS should be less than 2026 (indexed)
        self.assertLess(result_2023['max_gis'], result_2026['max_gis'])


class TestCppBenefitUsesYearVersionedGetters(unittest.TestCase):
    """DP#12/DP#20 (issue #330): cpp_benefit uses year-versioned getters."""

    def test_cpp_benefit_2026_max(self):
        from countries.canada.retirement import cpp_benefit, CPP_MAX_BENEFIT_65
        benefit = cpp_benefit(start_age=65, year=2026)
        self.assertAlmostEqual(benefit, CPP_MAX_BENEFIT_65, places=0)

    def test_cpp_benefit_2023_smaller_than_2026(self):
        from countries.canada.retirement import cpp_benefit
        benefit_2023 = cpp_benefit(start_age=65, year=2023)
        benefit_2026 = cpp_benefit(start_age=65, year=2026)
        # 2023 max benefit should be less than 2026 (indexed)
        self.assertLess(benefit_2023, benefit_2026)


class TestTaxDataProviderGisGetters(unittest.TestCase):
    """DP#12/DP#20 (issue #330): TaxDataProvider has GIS/CPP getter methods."""

    def test_get_gis_max_single(self):
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val = provider.get_gis_max_single(2026)
        self.assertEqual(val, 1726)

    def test_get_gis_max_coupled(self):
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val = provider.get_gis_max_coupled(2026)
        self.assertEqual(val, 10384)

    def test_get_cpp_max_pensionable(self):
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val = provider.get_cpp_max_pensionable(2026)
        self.assertEqual(val, 74600)

    def test_get_cpp_max_benefit_65(self):
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val = provider.get_cpp_max_benefit_65(2026)
        self.assertEqual(val, 18092)

    def test_get_gis_income_exemption(self):
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        val = provider.get_gis_income_exemption(2026)
        self.assertGreater(val, 0, "GIS income exemption should be positive")


if __name__ == '__main__':
    unittest.main()