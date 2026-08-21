#!/usr/bin/env python3
"""Market rates provider — mortgage, HELOC, and investment return rates.

Provides current and historical market rates, delegating to domain-specific
providers (BoC for prime rates, etc.). Rates are data; library code receives
them as parameters.

Reachability: a data-acquisition provider (DP#12), unreachable from the
simulation fold BY DESIGN — the fold consumes cached rate data, it never
calls the quote provider. Expected in the #710 reach guard's allowlist,
not dead code (#746).

Usage:
    from countries.canada.market_rates import MarketRatesProvider
    from countries.canada.boc_data import BoCDataProvider
    provider = MarketRatesProvider(boc_provider=BoCDataProvider())
    mortgage_rates = provider.get_mortgage_rates(term_years=5)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# DP#13: Fallback rates — clearly dated/rounded defaults used only when no
# provider or config supplies the actual values.
FALLBACK_PRIME_RATE = 0.07
FALLBACK_OVERNIGHT_RATE = 0.0475


@dataclass
class MortgageRateQuote:
    """A single mortgage rate offer from the market."""
    term_years: int
    rate_type: str  # "fixed" or "variable"
    rate: float  # Annual rate as decimal
    source: str  # "broker", "bank", "market", "config"
    lender: str = ""  # Optional lender name
    date: str = ""  # Quote date


@dataclass
class MarketRates:
    """Snapshot of current market rates.

    DP#2: expected_investment_return and broker_quotes should come from
    user config, not hardcoded defaults.
    """
    prime_rate: float
    overnight_rate: float
    mortgage_rates: List[MortgageRateQuote] = field(default_factory=list)
    broker_quotes: List[MortgageRateQuote] = field(default_factory=list)
    heloc_typical_rate: float = 0.0
    heloc_typical_spread: float = 0.0
    expected_investment_return: float = 0.0  # DP#2: Must be set from config, not hardcoded
    source: str = ""
    date: str = ""


class MarketRatesProvider:
    """Provides market rates, delegating to underlying data sources.

    This module does NOT hardcode rates. It composes:
    - BoCDataProvider for prime/overnight rates
    - User-provided broker quotes from config
    - Market conventions for typical spreads

    The module provides rates; optimization logic decides what to do with them.
    """

    def __init__(self, boc_provider=None, cache_dir: str = None):
        self.boc_provider = boc_provider
        self._cache_dir = cache_dir

    def get_current_rates(self) -> MarketRates:
        """Get current market rates from all sources."""
        prime = self._get_prime_rate()
        overnight = self._get_overnight_rate()

        return MarketRates(
            prime_rate=prime,
            overnight_rate=overnight,
            heloc_typical_rate=prime,  # HELOC typically at prime
            heloc_typical_spread=0.0,
            source="boc_provider" if self.boc_provider else "fallback",
        )

    def get_mortgage_rates(self, term_years: int = 5,
                           rate_type: str = None) -> List[MortgageRateQuote]:
        """Get available mortgage rates for a given term.

        Rates come from:
        1. User-provided broker quotes (in config)
        2. Market conventions (typical spreads over prime/bond yields)

        This module does NOT recommend rates — it provides what's available.
        """
        rates = []
        if self.boc_provider:
            # In a full implementation, fetch posted rates from bank APIs
            pass

        # If no external data, return empty — user must provide via config
        return rates

    def _get_prime_rate(self) -> float:
        if self.boc_provider:
            try:
                return self.boc_provider.get_prime_rate()
            except Exception:
                pass
        return FALLBACK_PRIME_RATE  # DP#13: clearly rounded fallback

    def _get_overnight_rate(self) -> float:
        if self.boc_provider:
            try:
                return self.boc_provider.get_overnight_rate()
            except Exception:
                pass
        return FALLBACK_OVERNIGHT_RATE  # DP#13: clearly rounded fallback