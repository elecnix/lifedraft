#!/usr/bin/env python3
"""Issue #839 / #840 — read-only tax sites reuse the cached default provider.

#839: three read-only construction sites still built a fresh ``TaxDataProvider``
(~20k builds/run) instead of reusing the process-wide cached
``default_tax_provider()``. The two module-level convenience wrappers were
removed outright (DP#9, #720) so read-only callers now hold the cached provider
explicitly; the remaining internal site reuses the cache:
    - ``default_tax_provider().get_combined_brackets`` (was ``tax_data.get_brackets`` /
      ``tax_calculator.get_combined_brackets``)
    - ``tax_calculator._compute_legacy_brackets``

#840: pointing them at the cache was previously unsafe because
``default_tax_provider()`` could memoize a DEGRADED provider — one built
re-entrantly during a partial (circular) import of the ``countries`` package,
missing provinces/aliases (the ``qc`` postal-code alias). The guard now refuses
to cache an incomplete build (``_registration_complete is False``).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tax_data
import tax_calculator


class TestReadOnlySitesReuseCachedProvider(unittest.TestCase):
    """#839: read-only bracket lookups must not build a fresh provider."""

    def test_no_fresh_provider_built_in_read_only_paths(self):
        # Prime the cache with a complete provider first, so any later build
        # would be a NEW (redundant) construction that this test must catch.
        tax_data.default_tax_provider()

        real = tax_data.TaxDataProvider
        builds = {"n": 0}

        class CountingProvider(real):
            def __init__(self, *args, **kwargs):
                builds["n"] += 1
                super().__init__(*args, **kwargs)

        tax_data.TaxDataProvider = CountingProvider
        tax_calculator.TaxDataProvider = CountingProvider
        try:
            tax_data.default_tax_provider().get_combined_brackets(2026, "quebec")
            tax_data.default_tax_provider().get_combined_brackets()
            tax_calculator._compute_legacy_brackets()
        finally:
            tax_data.TaxDataProvider = real
            tax_calculator.TaxDataProvider = real

        self.assertEqual(
            builds["n"], 0,
            "read-only sites must reuse default_tax_provider(), "
            "not build a fresh TaxDataProvider each call (#839)",
        )


class TestDefaultProviderPoisonGuard(unittest.TestCase):
    """#840: default_tax_provider() must not memoize a degraded build."""

    def setUp(self):
        self._saved = tax_data._DEFAULT_PROVIDER
        tax_data._DEFAULT_PROVIDER = None

    def tearDown(self):
        tax_data._DEFAULT_PROVIDER = self._saved

    def test_incomplete_build_is_not_cached(self):
        real = tax_data.TaxDataProvider
        built = []

        class FlakyProvider:
            """First build is degraded (partial import); later builds complete."""

            def __init__(self):
                self._registration_complete = len(built) > 0
                built.append(self)

        tax_data.TaxDataProvider = FlakyProvider
        try:
            first = tax_data.default_tax_provider()
            self.assertFalse(
                first._registration_complete,
                "sanity: the first build is the degraded one",
            )
            self.assertIsNone(
                tax_data._DEFAULT_PROVIDER,
                "a degraded build must NOT be memoized (#840)",
            )

            second = tax_data.default_tax_provider()
            self.assertTrue(second._registration_complete)
            self.assertIs(
                tax_data.default_tax_provider(), second,
                "the complete build is memoized on subsequent calls",
            )
        finally:
            tax_data.TaxDataProvider = real

    def test_real_default_provider_is_complete_and_cached(self):
        p1 = tax_data.default_tax_provider()
        self.assertTrue(p1._registration_complete)
        self.assertIs(tax_data.default_tax_provider(), p1)
        # The cached default resolves the postal-code alias that poisoning drops.
        self.assertGreater(len(p1.get_combined_brackets(2026, "qc")), 0)


if __name__ == "__main__":
    unittest.main()
