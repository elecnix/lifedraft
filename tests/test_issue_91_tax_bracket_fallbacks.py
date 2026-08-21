#!/usr/bin/env python3
"""Tests for issue #91: Move hardcoded tax bracket fallbacks to segregated data module.

Verifies:
- QC 4th bracket rate is 24.75% (not 24%)
- Federal thresholds are 2026 values (not 2023)
- Fallbacks come from a separate data module (DP#12)

Run with: python3 -m pytest tests/test_issue_91_tax_bracket_fallbacks.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from tax_data import TaxDataProvider, TaxYearData


class TestQCBracketRate2475(unittest.TestCase):
    """QC top bracket rate must be 25.75%, and no bracket at 24% over $82,385-$109,755 (issue #91)."""

    def _get_qc_fallback_brackets(self):
        """Get QC provincial brackets from the fallback path."""
        provider = TaxDataProvider(auto_register=False)
        provider._build_hardcoded_fallbacks()
        data = provider._load_year(2026, 'canada', 'quebec')
        return data.provincial_brackets

    def test_qc_no_24pc_bracket_at_82385(self):
        """The old buggy bracket at $82,385 with 24% rate must not exist.

        The old fallback had a separate bracket $82,385-$109,755 at 24%,
        but QC has a 4-tier system. That bracket was incorrect.
        """
        brackets = self._get_qc_fallback_brackets()
        for b in brackets:
            if b.min_income == 82385:
                self.assertNotAlmostEqual(b.rate, 0.24, places=2,
                    msg="Old buggy 24% bracket at $82,385 still exists")

    def test_qc_top_bracket_rate_is_2575(self):
        """QC top bracket rate is 25.75%."""
        brackets = self._get_qc_fallback_brackets()
        top_bracket = [b for b in brackets if b.max_income == 0]
        self.assertGreater(len(top_bracket), 0)
        self.assertAlmostEqual(top_bracket[0].rate, 0.2575, places=4)

    def test_qc_brackets_match_quebec_tax_data(self):
        """QC fallback brackets match QuebecTaxData.year_2026() structure."""
        from countries.canada.provinces.quebec.tax_data import QuebecTaxData
        registered = QuebecTaxData.year_2026().provincial_brackets
        fallback = self._get_qc_fallback_brackets()
        self.assertEqual(len(fallback), len(registered))
        for fb, reg in zip(fallback, registered, strict=True):
            self.assertAlmostEqual(fb.rate, reg.rate, places=4)


class TestFederalBracketThresholds2026(unittest.TestCase):
    """Federal bracket thresholds must be 2026 values (issue #91)."""

    def _get_federal_fallback_brackets(self):
        """Get federal brackets from the fallback path."""
        provider = TaxDataProvider(auto_register=False)
        provider._build_hardcoded_fallbacks()
        data = provider._load_year(2026, 'canada', 'federal')
        return data.federal_brackets

    def test_federal_first_bracket_threshold(self):
        """First federal bracket threshold should be ~$57,375 for 2026, not $54,345 (2023)."""
        brackets = self._get_federal_fallback_brackets()
        first_max = brackets[0].max_income
        self.assertGreater(first_max, 55000,
                           f"Federal first bracket threshold should be >$55,000 for 2026, got ${first_max:,.0f}")

    def test_federal_second_bracket_threshold(self):
        """Second federal bracket threshold should be ~$114,750 for 2026, not $108,690."""
        brackets = self._get_federal_fallback_brackets()
        second_max = brackets[1].max_income
        self.assertGreater(second_max, 110000,
                           f"Federal second bracket threshold should be >$110,000 for 2026, got ${second_max:,.0f}")

    def test_federal_not_using_2023_values(self):
        """Federal fallback brackets must not use 2023-specific thresholds."""
        brackets = self._get_federal_fallback_brackets()
        # These are the thresholds that were wrong in the old fallback:
        # $54,345 and $108,690 are 2023 values; $173,205 is from an even older schedule.
        # $117,045 is a valid 2026 threshold (coincidentally similar) so we don't flag it.
        old_wrong_thresholds = [54345, 108690, 173205]
        actual_thresholds = [b.max_income for b in brackets if b.max_income > 0]
        for old_t in old_wrong_thresholds:
            self.assertNotIn(old_t, actual_thresholds,
                             f"Found wrong threshold ${old_t:,} in 2026 federal brackets")

    def test_federal_brackets_have_five_tiers(self):
        """Federal brackets should be 5-tier."""
        brackets = self._get_federal_fallback_brackets()
        self.assertEqual(len(brackets), 5,
                         f"Federal should have 5 brackets, got {len(brackets)}")


class TestFallbackDataModuleExists(unittest.TestCase):
    """DP#12: Fallback bracket data must live in a separate module, not inline."""

    def test_fallback_data_module_importable(self):
        """countries.canada.tax_bracket_fallbacks must be importable."""
        from countries.canada.tax_bracket_fallbacks import build_fallback_data
        self.assertTrue(callable(build_fallback_data))

    def test_fallback_data_returns_list(self):
        """build_fallback_data() returns a list of TaxYearData objects."""
        from countries.canada.tax_bracket_fallbacks import build_fallback_data
        results = build_fallback_data()
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        for item in results:
            self.assertIsInstance(item, TaxYearData)

    def test_fallback_data_includes_quebec_2026(self):
        """Fallback data includes Quebec 2026."""
        from countries.canada.tax_bracket_fallbacks import build_fallback_data
        results = build_fallback_data()
        qc_2026 = [d for d in results
                    if d.country == 'canada' and d.province == 'quebec' and d.year == 2026]
        self.assertGreater(len(qc_2026), 0, "Fallback data must include QC 2026")

    def test_fallback_data_includes_federal_2026(self):
        """Fallback data includes federal 2026."""
        from countries.canada.tax_bracket_fallbacks import build_fallback_data
        results = build_fallback_data()
        fed_2026 = [d for d in results
                     if d.country == 'canada' and d.province == 'federal' and d.year == 2026]
        self.assertGreater(len(fed_2026), 0, "Fallback data must include federal 2026")

    def test_build_hardcoded_fallbacks_uses_module(self):
        """_build_hardcoded_fallbacks delegates to the new data module."""
        provider = TaxDataProvider(auto_register=False)
        provider._build_hardcoded_fallbacks()
        qc_data = provider._load_year(2026, 'canada', 'quebec')
        self.assertGreater(len(qc_data.provincial_brackets), 0)
        self.assertGreater(len(qc_data.federal_brackets), 0)


class TestFallbackValuesMatchRegisteredData(unittest.TestCase):
    """Fallback bracket values should match the registered data from country modules."""

    def test_qc_fallback_matches_quebec_tax_data(self):
        """QC fallback brackets match QuebecTaxData values."""
        from countries.canada.provinces.quebec.tax_data import QuebecTaxData
        provider_fallback = TaxDataProvider(auto_register=False)
        provider_fallback._build_hardcoded_fallbacks()
        qc_fallback = provider_fallback._load_year(2026, 'canada', 'quebec')

        qc_registered = QuebecTaxData.year_2026()
        fb_rates = [b.rate for b in qc_fallback.provincial_brackets]
        reg_rates = [b.rate for b in qc_registered.provincial_brackets]
        self.assertEqual(fb_rates, reg_rates,
                         f"Fallback QC rates {fb_rates} != registered {reg_rates}")

    def test_federal_fallback_matches_canada_init(self):
        """Federal fallback brackets match countries.canada.__init__ values."""
        from countries.canada import federal_all_years
        provider_fallback = TaxDataProvider(auto_register=False)
        provider_fallback._build_hardcoded_fallbacks()
        fed_fallback = provider_fallback._load_year(2026, 'canada', 'federal')

        fed_registered = [y for y in federal_all_years() if y.year == 2026][0]
        fb_thresholds = [(b.min_income, b.max_income) for b in fed_fallback.federal_brackets]
        reg_thresholds = [(b.min_income, b.max_income) for b in fed_registered.federal_brackets]
        self.assertEqual(fb_thresholds, reg_thresholds,
                         f"Fallback federal thresholds {fb_thresholds} != registered {reg_thresholds}")


if __name__ == '__main__':
    unittest.main()
