#!/usr/bin/env python3
"""Unit tests for boc_data.py — Bank of Canada rate provider (#325, DP#17).

All test data uses round numbers per DP#15. No network access — the BoC API
is mocked. Tests exercise cache hits/misses, fallback, parsing, and error
handling per DP#17.

Run with: python3 -m pytest countries/canada/tests/test_boc_data.py -v
"""

import json
import os
import sys
import tempfile
import urllib.error
from unittest import mock

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)

import unittest

from countries.canada.boc_data import (
    BoCDataProvider,
    BoCRateLimitError,
    BoCNetworkError,
    BoCDataError,
    RateForecast,
    FALLBACK_PRIME_RATE,
    BOC_PRATE_SERIES,
    get_current_rates,
)


class _FakeResponse:
    """Minimal context-manager stand-in for urllib's HTTP response."""

    def __init__(self, payload):
        self._bytes = json.dumps(payload).encode()

    def read(self):
        return self._bytes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _observations(rate_pct):
    return {"observations": [{BOC_PRATE_SERIES: {"v": str(rate_pct)}}]}


class BoCTestCase(unittest.TestCase):
    """Base: each test gets a fresh empty cache directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = self._tmp.name
        self.provider = BoCDataProvider(cache_dir=self.cache_dir)

    def tearDown(self):
        self._tmp.cleanup()


class TestApiParsing(BoCTestCase):
    """API response parsing (mocked upstream)."""

    def test_parses_percent_to_decimal(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(_observations("6.95"))):
            self.assertAlmostEqual(self.provider._fetch_prime_rate(), 0.0695)

    def test_empty_observations_raises_data_error(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse({"observations": []})):
            with self.assertRaises(BoCDataError):
                self.provider._fetch_prime_rate()

    def test_malformed_payload_raises_data_error(self):
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse({"wrong": 1})):
            with self.assertRaises(BoCDataError):
                self.provider._fetch_prime_rate()


class TestErrorHandling(BoCTestCase):
    """Rate limits and network timeouts (DP#17)."""

    def test_429_raises_rate_limit_error(self):
        err = urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err), mock.patch("time.sleep"):
            with self.assertRaises(BoCRateLimitError):
                self.provider._fetch_prime_rate()

    def test_network_error_raises_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")), \
                mock.patch("time.sleep"):
            with self.assertRaises(BoCNetworkError):
                self.provider._fetch_prime_rate()

    def test_non_429_http_error_raises_network_error(self):
        err = urllib.error.HTTPError("u", 500, "Server Error", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(BoCNetworkError):
                self.provider._fetch_prime_rate()


class TestPrimeRateFallbackAndCache(BoCTestCase):
    """get_prime_rate: cache miss → fetch → cache hit; fallback on failure."""

    def test_fetch_then_cache_hit_avoids_second_fetch(self):
        with mock.patch.object(BoCDataProvider, "_fetch_prime_rate", return_value=0.05) as m:
            self.assertEqual(self.provider.get_prime_rate(), 0.05)  # miss → fetch
            self.assertEqual(self.provider.get_prime_rate(), 0.05)  # hit → no fetch
        self.assertEqual(m.call_count, 1)

    def test_fallback_when_api_unavailable(self):
        with mock.patch.object(
            BoCDataProvider, "_fetch_prime_rate", side_effect=BoCNetworkError("x")
        ):
            self.assertEqual(self.provider.get_prime_rate(), FALLBACK_PRIME_RATE)

    def test_failure_does_not_poison_cache(self):
        # After a failure (fallback used), a later success should still fetch.
        with mock.patch.object(
            BoCDataProvider, "_fetch_prime_rate", side_effect=BoCNetworkError("x")
        ):
            self.provider.get_prime_rate()
        with mock.patch.object(BoCDataProvider, "_fetch_prime_rate", return_value=0.05) as m:
            self.assertEqual(self.provider.get_prime_rate(), 0.05)
        self.assertEqual(m.call_count, 1)


class TestCacheFreshness(BoCTestCase):
    """Stale cache entries are ignored (DP#17)."""

    def test_stale_cache_is_ignored(self):
        self.provider._save_cache("prime_rate.json", {"rate": 0.01})
        # max_age=0 forces the entry to read as stale.
        self.assertIsNone(self.provider._load_cache("prime_rate.json", max_age=0))

    def test_missing_cache_returns_none(self):
        self.assertIsNone(self.provider._load_cache("does_not_exist.json"))


class TestOvernightRate(BoCTestCase):
    """Overnight series is unimplemented → fallback path."""

    def test_overnight_uses_fallback(self):
        from countries.canada.boc_data import FALLBACK_OVERNIGHT_RATE

        self.assertEqual(self.provider.get_overnight_rate(), FALLBACK_OVERNIGHT_RATE)


class TestRateForecast(BoCTestCase):
    """Forecasts: flat at current rate when no data; config-driven override."""

    def test_flat_forecast_when_no_data(self):
        with mock.patch.object(BoCDataProvider, "get_prime_rate", return_value=0.06):
            forecast = self.provider.get_rate_forecast(years=4)
        self.assertEqual(len(forecast), 4)
        self.assertTrue(all(isinstance(f, RateForecast) for f in forecast))
        self.assertTrue(all(f.prime_rate == 0.06 for f in forecast))
        self.assertEqual(forecast[0].source, "flat_no_data")
        # Years are sequential.
        self.assertEqual(forecast[1].year - forecast[0].year, 1)

    def test_load_forecast_from_config(self):
        cfg = {"sensitivity_overlays": {"renewal_rates": {"renewal-a": [0.05, 0.06, 0.07]}}}
        forecast = self.provider.load_forecast_from_config(cfg)
        self.assertEqual(len(forecast), 3)
        self.assertEqual([f.prime_rate for f in forecast], [0.05, 0.06, 0.07])
        self.assertTrue(forecast[0].source.startswith("config:"))

    def test_load_forecast_empty_config(self):
        self.assertEqual(self.provider.load_forecast_from_config({}), [])


class TestHelocAndConvenience(BoCTestCase):
    """HELOC spread math and the get_current_rates convenience function."""

    def test_heloc_rate_adds_spread(self):
        self.assertAlmostEqual(self.provider.heloc_rate_from_prime(0.07, 0.005), 0.075)

    def test_heloc_default_spread_zero(self):
        self.assertEqual(self.provider.heloc_rate_from_prime(0.07), 0.07)

    def test_get_current_rates_spread_is_prime_minus_overnight(self):
        rates = get_current_rates(cache_dir=self.cache_dir)
        self.assertIn("prime", rates)
        self.assertIn("overnight", rates)
        self.assertAlmostEqual(rates["spread"], rates["prime"] - rates["overnight"])


if __name__ == "__main__":
    unittest.main()
