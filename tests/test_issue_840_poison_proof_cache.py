#!/usr/bin/env python3
"""Issue #840 / #1056 — registry-generation counter makes default_tax_provider() poison-proof.

Before the #1056 fix, the generation counter was bumped by every
register_year / register_year_alias call — meaning every TaxDataProvider()
construction (which auto-registers via register_all) bumped it. At VOI scale
(416 run_optimization calls), each construction invalidated the
default_tax_provider() cache, causing 416 full rebuilds — a 2-3× slowdown.

After the fix, the generation counter lives on CountryRegistry and only
advances when the registry's _modules dict changes (register/discover),
NOT when a provider absorbs the current registry state. Building a
non-default TaxDataProvider() no longer invalidates the cache.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tax_data
from tax_data import TaxDataProvider, TaxYearData
import countries


class TestRegistryGenerationCounter(unittest.TestCase):
    """CountryRegistry.generation advances only on registry mutations."""

    def test_register_country_bumps_generation(self):
        """Adding a country register function to the registry bumps generation."""
        gen_before = countries.default_registry.generation
        countries.default_registry.register('xx_test', lambda provider: None)
        try:
            gen_after = countries.default_registry.generation
            self.assertGreater(
                gen_after, gen_before,
                "CountryRegistry.register must advance generation",
            )
        finally:
            # Clean up so other tests aren't affected.
            del countries.default_registry._modules['xx_test']
            # Don't decrement _generation — it's monotonic, which is fine.

    def test_register_year_does_not_bump_generation(self):
        """register_year does NOT bump the registry generation — it's per-instance."""
        gen_before = countries.default_registry.generation
        provider = TaxDataProvider()
        gen_after = countries.default_registry.generation
        self.assertEqual(
            gen_after, gen_before,
            "Building a TaxDataProvider must NOT bump the registry generation",
        )

    def test_register_year_alias_does_not_bump_generation(self):
        """register_year_alias does NOT bump the registry generation."""
        gen_before = countries.default_registry.generation
        p = TaxDataProvider()
        existing_key = next(iter(p._fallbacks))
        existing_data = p._fallbacks[existing_key]
        p.register_year_alias("zz", existing_data)
        gen_after = countries.default_registry.generation
        self.assertEqual(
            gen_after, gen_before,
            "register_year_alias must NOT advance the registry generation",
        )


class TestPoisonProofCache(unittest.TestCase):
    """default_tax_provider() rebuilds when the registry generation advances."""

    def setUp(self):
        self._saved_provider = tax_data._DEFAULT_PROVIDER
        self._saved_cache_gen = tax_data._DEFAULT_PROVIDER_GENERATION
        # Clear the cached provider so each test starts fresh.
        tax_data._DEFAULT_PROVIDER = None
        tax_data._DEFAULT_PROVIDER_GENERATION = -1

    def tearDown(self):
        tax_data._DEFAULT_PROVIDER = self._saved_provider
        tax_data._DEFAULT_PROVIDER_GENERATION = self._saved_cache_gen

    def test_cache_hit_when_generation_unchanged(self):
        """No registry changes between calls → same cached provider."""
        first = tax_data.default_tax_provider()
        second = tax_data.default_tax_provider()
        self.assertIs(first, second)

    def test_cache_rebuilds_after_generation_bump(self):
        """A registry mutation invalidates the cached provider, causing a rebuild.

        This reproduces the #840 poisoning scenario: after the cache is primed
        with a complete provider, a later mutation of the global registry
        should cause default_tax_provider() to rebuild, not return the stale
        cache.
        """
        # 1. Prime the cache with a complete provider (has 'qc' alias).
        first = tax_data.default_tax_provider()
        self.assertTrue(first._registration_complete)
        # Sanity: qc alias resolves.
        self.assertGreater(len(first.get_combined_brackets(2026, "qc")), 0)

        # 2. Record how many TaxDataProvider.__init__ calls happen after this.
        real = tax_data.TaxDataProvider
        builds = {"n": 0}

        class CountingProvider(real):
            def __init__(self, *args, **kwargs):
                builds["n"] += 1
                super().__init__(*args, **kwargs)

        tax_data.TaxDataProvider = CountingProvider
        try:
            # 3. Bump the generation by mutating the registry (adding a country).
            countries.default_registry.register('zz_test', lambda p: None)
            try:
                # 4. Now default_tax_provider() should rebuild (cache miss).
                rebuilt = tax_data.default_tax_provider()
                self.assertEqual(
                    builds["n"], 1,
                    "default_tax_provider() must rebuild after a registry generation bump",
                )
                # The rebuilt provider must still resolve 'qc' — the #840 invariant.
                self.assertGreater(
                    len(rebuilt.get_combined_brackets(2075, "qc")), 0,
                    "rebuilt provider must resolve 'qc' alias (issue #840)",
                )
            finally:
                # Clean up the registry mutation.
                del countries.default_registry._modules['zz_test']
        finally:
            tax_data.TaxDataProvider = real

    def test_qc_alias_resolves_after_mutation(self):
        """Registry-mutation cache-invalidation scenario: after a registry
        mutation advances the generation, default_tax_provider() must rebuild
        and still resolve the 'qc' alias.

        Before the generation-counter fix, constructing a second TaxDataProvider
        (which registers its years into the global registry, bumping
        _REGISTRY_GENERATION) would leave the cached default provider stale.
        A subsequent call to default_tax_provider() would return the old cache
        rather than rebuilding against the current registry state.

        After the fix, constructing a non-default TaxDataProvider does NOT
        bump the registry generation. Only actual registry mutations do.
        """
        # Prime the cache.
        p1 = tax_data.default_tax_provider()
        self.assertGreater(len(p1.get_combined_brackets(2026, "qc")), 0)

        # Building a non-default provider does NOT bump the registry generation.
        p2 = TaxDataProvider()
        # The cache should still be valid (same object).
        p3 = tax_data.default_tax_provider()
        self.assertIs(p1, p3, "building a non-default provider must not invalidate the cache")

        # Now mutate the registry to bump the generation.
        countries.default_registry.register('yy_test', lambda p: None)
        try:
            p4 = tax_data.default_tax_provider()
            # The rebuilt provider resolves 'qc' even for a future year.
            self.assertGreater(
                len(p4.get_combined_brackets(2075, "qc")), 0,
                "rebuilt default provider must still resolve 'qc' (issue #840)",
            )
        finally:
            del countries.default_registry._modules['yy_test']

    def test_incomplete_build_still_not_cached(self):
        """Even with the generation counter, a degraded build is not memoized."""

        class FlakyProvider:
            _registration_complete = False

        real = tax_data.TaxDataProvider
        tax_data.TaxDataProvider = FlakyProvider
        try:
            tax_data._DEFAULT_PROVIDER = None
            tax_data._DEFAULT_PROVIDER_GENERATION = -1
            result = tax_data.default_tax_provider()
            self.assertFalse(result._registration_complete)
            self.assertIsNone(tax_data._DEFAULT_PROVIDER)
        finally:
            tax_data.TaxDataProvider = real


if __name__ == "__main__":
    unittest.main()