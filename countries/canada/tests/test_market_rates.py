#!/usr/bin/env python3
"""Unit tests for market_rates.py — market rate provider (#326, DP#17).

All test data uses round numbers per DP#15. The BoC upstream is mocked.
Tests cover rate composition and fallback paths per DP#17.

Run with: python3 -m pytest countries/canada/tests/test_market_rates.py -v
"""

import os
import sys
from unittest import mock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)

import unittest

from countries.canada.market_rates import (
    MarketRatesProvider,
    MarketRates,
    FALLBACK_PRIME_RATE,
    FALLBACK_OVERNIGHT_RATE,
)


class _FakeBoC:
    """Stand-in for BoCDataProvider returning fixed rates."""

    def __init__(self, prime=0.06, overnight=0.04, raises=False):
        self._prime = prime
        self._overnight = overnight
        self._raises = raises

    def get_prime_rate(self):
        if self._raises:
            raise RuntimeError("boc down")
        return self._prime

    def get_overnight_rate(self):
        if self._raises:
            raise RuntimeError("boc down")
        return self._overnight


class TestCurrentRatesComposition(unittest.TestCase):
    """get_current_rates composes BoC prime/overnight into a snapshot."""

    def test_uses_boc_provider_rates(self):
        provider = MarketRatesProvider(boc_provider=_FakeBoC(prime=0.06, overnight=0.04))
        rates = provider.get_current_rates()
        self.assertIsInstance(rates, MarketRates)
        self.assertEqual(rates.prime_rate, 0.06)
        self.assertEqual(rates.overnight_rate, 0.04)
        self.assertEqual(rates.source, "boc_provider")

    def test_heloc_defaults_to_prime(self):
        # HELOC is conventionally set at prime with zero spread.
        provider = MarketRatesProvider(boc_provider=_FakeBoC(prime=0.065))
        rates = provider.get_current_rates()
        self.assertEqual(rates.heloc_typical_rate, 0.065)
        self.assertEqual(rates.heloc_typical_spread, 0.0)

    def test_no_provider_uses_fallback(self):
        provider = MarketRatesProvider(boc_provider=None)
        rates = provider.get_current_rates()
        self.assertEqual(rates.prime_rate, FALLBACK_PRIME_RATE)
        self.assertEqual(rates.overnight_rate, FALLBACK_OVERNIGHT_RATE)
        self.assertEqual(rates.source, "fallback")

    def test_boc_error_falls_back(self):
        # A failing BoC provider must not crash; it degrades to fallback.
        provider = MarketRatesProvider(boc_provider=_FakeBoC(raises=True))
        rates = provider.get_current_rates()
        self.assertEqual(rates.prime_rate, FALLBACK_PRIME_RATE)
        self.assertEqual(rates.overnight_rate, FALLBACK_OVERNIGHT_RATE)


class TestMortgageRates(unittest.TestCase):
    """get_mortgage_rates: no posted-rate source → empty list."""

    def test_returns_empty_without_external_data(self):
        provider = MarketRatesProvider(boc_provider=_FakeBoC())
        self.assertEqual(provider.get_mortgage_rates(term_years=5), [])

    def test_returns_empty_without_provider(self):
        provider = MarketRatesProvider()
        self.assertEqual(provider.get_mortgage_rates(), [])


if __name__ == "__main__":
    unittest.main()
