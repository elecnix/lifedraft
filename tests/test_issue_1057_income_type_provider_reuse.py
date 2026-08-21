#!/usr/bin/env python3
"""Issue #1057 — income_type._get_tax_provider() reuses the cached singleton.

Chain: #837 (first audit), #839 (three read-only sites fixed),
#840 (poison-proof cache), #1056 (registry-generation counter makes cache
safe to reuse across tests), #1057 (fourth site: income_type._get_tax_provider
was still building a fresh TaxDataProvider per call instead of returning
default_tax_provider()).

If someone reverts _get_tax_provider to ``return TaxDataProvider()``, these
tests must fail:
- Identity check: a fresh provider is NOT the same object as the cached
  singleton, so ``assertIs(caller_result, default_tax_provider())`` fails.
- Construction-count check: a fresh build per call means the CountingProvider
  __init__ fires >0 times across the three callers, so
  ``assertEqual(builds, 0)`` fails.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tax_data
from countries.canada import income_type


class TestGetTaxProviderIdentity(unittest.TestCase):
    """#1057: _get_tax_provider() returns the same object as default_tax_provider()."""

    def test_identity_with_cached_singleton(self):
        # Prime the cache so default_tax_provider() has a stable instance.
        cached = tax_data.default_tax_provider()

        # The three callers all go through _get_tax_provider; exercise each
        # to confirm they receive the cached singleton, not a fresh build.
        fed = income_type._get_federal_dtc_rates(2026)
        prov = income_type._get_provincial_dtc_rates("quebec", 2026)
        cg = income_type.capital_gains_inclusion_rate(0.0, 2026)

        # If _get_tax_provider() builds a fresh TaxDataProvider(), the
        # returned provider object will NOT be the same as the cached one.
        # Each caller discards its provider after use, so we verify the
        # function itself: _get_tax_provider must return the singleton.
        provider = income_type._get_tax_provider()
        self.assertIs(
            provider, cached,
            "_get_tax_provider() must return the default_tax_provider() "
            "singleton (#1057); a fresh TaxDataProvider() is not the same object",
        )


class TestGetTaxProviderNoFreshConstruction(unittest.TestCase):
    """#1057: exercising the three callers must not build a new TaxDataProvider.

    This is the more robust check: even if identity were somehow spoofed
    (e.g. a proxy), counting actual __init__ calls proves no new instance
    is constructed.
    """

    def test_no_fresh_provider_built_by_callers(self):
        # Prime the cache with a complete provider first.
        tax_data.default_tax_provider()

        real = tax_data.TaxDataProvider
        builds = {"n": 0}

        class CountingProvider(real):
            def __init__(self, *args, **kwargs):
                builds["n"] += 1
                super().__init__(*args, **kwargs)

        tax_data.TaxDataProvider = CountingProvider
        try:
            # Exercise the three callers that go through _get_tax_provider.
            income_type._get_federal_dtc_rates(2026)
            income_type._get_provincial_dtc_rates("quebec", 2026)
            income_type.capital_gains_inclusion_rate(0.0, 2026)
        finally:
            tax_data.TaxDataProvider = real

        self.assertEqual(
            builds["n"], 0,
            "income_type callers must reuse default_tax_provider(), "
            "not build a fresh TaxDataProvider each call (#1057)",
        )


if __name__ == "__main__":
    unittest.main()