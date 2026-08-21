#!/usr/bin/env python3
"""Tests for Issue #86: Move RRIF minimum withdrawal rates to data provider (DP#2/DP#12).

Per DP#2: Configuration belongs in input, not in code.
Per DP#12: Real data is fetched, cached, and segregated from library code.
Per DP#20: Tax data is year-versioned.

The RRIF minimum withdrawal rate table is now provided by TaxDataProvider
instead of being hardcoded in retirement.py.
"""

import unittest

from tax_data import TaxDataProvider, _CRA_RRIF_FALLBACK_RATES
from countries.canada.retirement import (
    rrif_minimum_withdrawal, _get_rrif_rates, RRIF_MIN_WITHDRAWAL_RATES,
)


class TestRRIFRratesFromProvider(unittest.TestCase):
    """Test that RRIF rates come from the data provider (DP#12)."""

    def test_provider_has_rrif_rates(self):
        """TaxDataProvider should return RRIF rates."""
        provider = TaxDataProvider()
        rates = provider.get_rrif_min_withdrawal_rates(year=2026)
        self.assertIsInstance(rates, dict)
        self.assertGreater(len(rates), 0)

    def test_provider_rates_match_cra_table(self):
        """Provider rates should match CRA-prescribed factors."""
        provider = TaxDataProvider()
        rates = provider.get_rrif_min_withdrawal_rates(year=2026)
        # Check key ages from CRA RC4167
        self.assertAlmostEqual(rates[65], 0.0400, places=4)
        self.assertAlmostEqual(rates[71], 0.0528, places=4)  # Mandatory conversion age
        self.assertAlmostEqual(rates[95], 0.1129, places=4)

    def test_provider_rates_include_young_ages(self):
        """Provider should include ages 55-64 (early RRIF conversion)."""
        provider = TaxDataProvider()
        rates = provider.get_rrif_min_withdrawal_rates(year=2026)
        self.assertIn(55, rates)
        self.assertIn(64, rates)
        # 1/(90-age) formula for age 60
        self.assertAlmostEqual(rates[60], 0.0333, places=3)

    def test_fallback_rates_available(self):
        """CRA fallback rates should be available even without provider."""
        self.assertIn(65, _CRA_RRIF_FALLBACK_RATES)
        self.assertIn(71, _CRA_RRIF_FALLBACK_RATES)
        self.assertIn(95, _CRA_RRIF_FALLBACK_RATES)
        self.assertEqual(len(_CRA_RRIF_FALLBACK_RATES), 41)  # Ages 55-95


class TestRRIFMinimumWithdrawalWithProvider(unittest.TestCase):
    """Test rrif_minimum_withdrawal with data provider."""

    def test_default_uses_provider(self):
        """rrif_minimum_withdrawal without explicit rates uses provider."""
        result = rrif_minimum_withdrawal(100000, 71)
        # Age 71: 5.28% minimum withdrawal
        self.assertAlmostEqual(result, 5280, places=0)

    def test_custom_rates_override_provider(self):
        """Explicitly provided rates override the provider."""
        custom_rates = {71: 0.10}  # 10% instead of 5.28%
        result = rrif_minimum_withdrawal(100000, 71, rates=custom_rates)
        self.assertAlmostEqual(result, 10000, places=0)

    def test_year_parameter(self):
        """year parameter is passed to the provider."""
        result_2026 = rrif_minimum_withdrawal(100000, 71, year=2026)
        self.assertAlmostEqual(result_2026, 5280, places=0)

    def test_age_above_table_defaults_to_20_pct(self):
        """Ages above the table default to 20% withdrawal."""
        result = rrif_minimum_withdrawal(100000, 100)
        self.assertAlmostEqual(result, 20000, places=0)

    def test_zero_balance(self):
        """Zero balance should return zero withdrawal."""
        result = rrif_minimum_withdrawal(0, 71)
        self.assertEqual(result, 0)


class TestBackwardCompat(unittest.TestCase):
    """Test backward compatibility with RRIF_MIN_WITHDRAWAL_RATES."""

    def test_rrif_min_withdrawal_rates_still_accessible(self):
        """RRIF_MIN_WITHDRAWAL_RATES should still be importable (backward compat)."""
        self.assertIsInstance(RRIF_MIN_WITHDRAWAL_RATES, dict)
        self.assertGreater(len(RRIF_MIN_WITHDRAWAL_RATES), 0)

    def test_rrif_min_withdrawal_rates_has_key_ages(self):
        """Module-level RRIF_MIN_WITHDRAWAL_RATES should have standard ages."""
        self.assertIn(65, RRIF_MIN_WITHDRAWAL_RATES)
        self.assertIn(71, RRIF_MIN_WITHDRAWAL_RATES)
        self.assertIn(95, RRIF_MIN_WITHDRAWAL_RATES)


class TestGetRRIFRates(unittest.TestCase):
    """Test _get_rrif_rates helper function."""

    def test_get_rrif_rates_returns_dict(self):
        """_get_rrif_rates should return a dict."""
        rates = _get_rrif_rates()
        self.assertIsInstance(rates, dict)

    def test_get_rrif_rates_with_explicit_rates(self):
        """Explicit rates parameter should be returned as-is."""
        custom = {70: 0.05}
        rates = _get_rrif_rates(rates=custom)
        self.assertIs(rates, custom)

    def test_get_rrif_rates_with_provider(self):
        """Provider parameter should be used when provided."""
        provider = TaxDataProvider()
        rates = _get_rrif_rates(provider=provider, year=2026)
        self.assertIsInstance(rates, dict)
        self.assertIn(65, rates)


if __name__ == '__main__':
    unittest.main()