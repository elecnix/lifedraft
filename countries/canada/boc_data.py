#!/usr/bin/env python3
"""Bank of Canada rate data provider.

Fetches prime rate and overnight rate data from the Bank of Canada
(or uses local cache) to provide rate paths for mortgage simulation.

This module separates data acquisition from rate modeling per the
design principle: configuration and real data belong in external sources,
not hardcoded in library code.

Reachability: this is a data-acquisition provider (DP#12) and is
unreachable from the simulation fold BY DESIGN — the fold reads the cached
rate data this produces, it never calls the fetcher. It is therefore an
expected entry in the #710 reach guard's allowlist, not dead code (#746).

References:
    countries/canada/docs/GOVERNMENT_REFERENCES.md — Bank of Canada Rates entry
    BoC policy rate: https://www.bankofcanada.ca/rates/interest-rates/
    BoC prime rate series: https://www.bankofcanada.ca/valet/observations/V39079
    BoC Canadian interest rates: https://www.bankofcanada.ca/rates/interest-rates/canadian-interest-rates/

Usage:
    from countries.canada.boc_data import BoCDataProvider
    provider = BoCDataProvider(cache_dir="~/.cache/lifedraft")
    current_prime = provider.get_prime_rate()
    forecast = provider.get_rate_forecast(years=10)
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Cache duration: 1 day for current rates, 1 week for forecasts
CACHE_TTL_RATES = 86400  # 1 day
CACHE_TTL_FORECAST = 604800  # 1 week

# Bank of Canada API endpoints
BOC_API_BASE = "https://www.bankofcanada.ca/valet"
BOC_PRATE_SERIES = "V39079"  # Prime rate series

# Retry settings
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # seconds, doubles each retry

# Fallback: last known rates (updated manually when API unavailable)
FALLBACK_PRIME_RATE = 0.0695  # 6.95% as of 2025-12
FALLBACK_OVERNIGHT_RATE = 0.0475  # 4.75% as of 2025-12


class BoCRateLimitError(Exception):
    """BoC API returned HTTP 429 (rate limit)."""


class BoCNetworkError(Exception):
    """Network error reaching BoC API (timeout, connection failure)."""


class BoCDataError(Exception):
    """BoC API returned unexpected data format."""


@dataclass
class RateObservation:
    """A single rate observation at a point in time."""
    date: str  # ISO date string "YYYY-MM-DD"
    rate: float  # Annual rate as decimal (e.g., 0.0695)
    series: str  # BOC series name


@dataclass
class RateForecast:
    """A rate forecast point (forward-looking)."""
    year: int
    prime_rate: float  # Expected prime rate
    source: str  # "boc_mpr", "market_implied", "analyst_consensus"


class BoCDataProvider:
    """Provides Bank of Canada rate data with local caching.

    Data sources (in priority order):
    1. Local cache (if fresh)
    2. Bank of Canada API (if reachable)
    3. Fallback defaults

    The provider does NOT hardcode rate forecasts. Forecasts come from:
    - User-provided input config
    - Cached analyst consensus
    - Fallback: current rate unchanged
    """

    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir or os.path.expanduser(
            "~/.cache/lifedraft/boc"
        )
        os.makedirs(self.cache_dir, exist_ok=True)

    # ─── Current rates ───

    def get_prime_rate(self) -> float:
        """Get the current Bank of Canada prime rate.

        Returns the prime rate as a decimal (e.g., 0.0695 = 6.95%).
        Tries cache → API → fallback.
        """
        cached = self._load_cache("prime_rate.json", max_age=CACHE_TTL_RATES)
        if cached is not None:
            return cached["rate"]

        try:
            rate = self._fetch_prime_rate()
            self._save_cache("prime_rate.json", {"rate": rate, "date": time.strftime("%Y-%m-%d")})
            return rate
        except (BoCRateLimitError, BoCNetworkError, BoCDataError) as e:
            logger.warning("Using fallback prime rate after %s", type(e).__name__)
            return FALLBACK_PRIME_RATE

    def get_overnight_rate(self) -> float:
        """Get the current BoC overnight rate."""
        cached = self._load_cache("overnight_rate.json", max_age=CACHE_TTL_RATES)
        if cached is not None:
            return cached["rate"]

        try:
            rate = self._fetch_overnight_rate()
            self._save_cache("overnight_rate.json", {"rate": rate, "date": time.strftime("%Y-%m-%d")})
            return rate
        except (BoCRateLimitError, BoCNetworkError, BoCDataError) as e:
            logger.warning("Using fallback overnight rate after %s", type(e).__name__)
            return FALLBACK_OVERNIGHT_RATE

    # ─── Rate forecasts ───

    def get_rate_forecast(self, years: int = 10,
                          scenario: str = "expected") -> List[RateForecast]:
        """Get rate forecast for the next N years.

        Forecasts come from user config or cached analyst data.
        If neither is available, returns flat forecast at current rate.

        Args:
            years: Number of years to forecast
            scenario: "expected", "best_case", or "worst_case"
        """
        cached = self._load_cache(f"forecast_{scenario}.json", max_age=CACHE_TTL_FORECAST)
        if cached is not None:
            return [RateForecast(year=f["year"], prime_rate=f["prime_rate"],
                                 source=f.get("source", "cache"))
                    for f in cached]

        # No forecast data: return flat projection at current rate
        current = self.get_prime_rate()
        current_year = int(time.strftime("%Y"))
        return [
            RateForecast(year=current_year + i, prime_rate=current, source="flat_no_data")
            for i in range(years)
        ]

    def load_forecast_from_config(self, cfg: dict) -> List[RateForecast]:
        """Load forecast from input config's sensitivity_overlays section.

        This is the preferred way to provide rate forecasts — they come
        from the user's input JSON, not from hardcoded predictions.
        """
        overlays = cfg.get("sensitivity_overlays", {})
        renewal = overlays.get("renewal_rates", {})

        current_year = int(time.strftime("%Y"))
        forecasts = []
        for i, (key, rates) in enumerate(sorted(renewal.items())):
            if isinstance(rates, list):
                for j, rate in enumerate(rates):
                    forecasts.append(RateForecast(
                        year=current_year + j,
                        prime_rate=rate,
                        source=f"config:{key}",
                    ))
        return forecasts

    # ─── Prime spread calculation ───

    def heloc_rate_from_prime(self, prime_rate: float, spread: float = 0.0) -> float:
        """Calculate HELOC rate from prime + spread.

        Args:
            prime_rate: Current prime rate (e.g., 0.0695)
            spread: Lender spread above prime (typically 0-0.5%)

        Returns:
            HELOC rate as decimal
        """
        return prime_rate + spread

    # ─── Internal: data fetching ───

    def _fetch_prime_rate(self) -> float:
        """Fetch current prime rate from Bank of Canada API.

        Retries on transient errors (429, timeout) with exponential backoff.
        Raises BoCRateLimitError, BoCNetworkError, or BoCDataError on failure.
        """
        url = f"{BOC_API_BASE}/observations/{BOC_PRATE_SERIES}/json?recent=1"
        last_error = None

        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    data = json.loads(resp.read())
                    obs = data["observations"]
                    if not obs:
                        raise BoCDataError("BoC API returned empty observations")
                    return float(obs[-1][BOC_PRATE_SERIES]["v"]) / 100
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    last_error = BoCRateLimitError(
                        f"BoC API rate limit (429), attempt {attempt + 1}/{MAX_RETRIES}"
                    )
                    if attempt < MAX_RETRIES - 1:
                        wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                        logger.info("Rate limited, retrying in %.1fs", wait)
                        time.sleep(wait)
                        continue
                else:
                    last_error = BoCNetworkError(
                        f"BoC API HTTP {e.code}: {e.reason}"
                    )
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = BoCNetworkError(
                    f"Network error fetching BoC rates: {e}, attempt {attempt + 1}/{MAX_RETRIES}"
                )
                if attempt < MAX_RETRIES - 1:
                    wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                    logger.info("Network error, retrying in %.1fs", wait)
                    time.sleep(wait)
                    continue
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                last_error = BoCDataError(f"Unexpected BoC API response format: {e}")

        raise last_error or BoCDataError("Could not fetch prime rate from BoC API")

    def _fetch_overnight_rate(self) -> float:
        """Fetch current overnight rate from Bank of Canada API.

        Raises BoCDataError (not yet implemented for a specific series).
        """
        raise BoCDataError("Overnight rate series not yet implemented")

    # ─── Internal: caching ───

    def _cache_path(self, filename: str) -> str:
        return os.path.join(self.cache_dir, filename)

    def _load_cache(self, filename: str, max_age: int = 86400) -> Optional[dict]:
        """Load from cache if it exists and is fresh enough."""
        path = self._cache_path(filename)
        if not os.path.exists(path):
            return None

        mtime = os.path.getmtime(path)
        if time.time() - mtime > max_age:
            return None

        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def _save_cache(self, filename: str, data: dict) -> None:
        """Save data to cache."""
        path = self._cache_path(filename)
        try:
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass  # Cache write failure is non-fatal


def get_current_rates(cache_dir: str = None) -> Dict[str, float]:
    """Convenience function to get all current rates.

    Returns:
        Dict with 'prime', 'overnight', 'spread' keys
    """
    provider = BoCDataProvider(cache_dir=cache_dir)
    prime = provider.get_prime_rate()
    overnight = provider.get_overnight_rate()
    return {
        "prime": prime,
        "overnight": overnight,
        "spread": prime - overnight,
    }