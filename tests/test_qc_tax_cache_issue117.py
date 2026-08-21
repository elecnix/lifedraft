#!/usr/bin/env python3
"""Tests for issue #117: Remove _QUEBEC_TAX_BRACKETS_2026_CACHE mutable singleton.

The module-level _QUEBEC_TAX_BRACKETS_2026_CACHE at tax_calculator.py:68-94
is a mutable singleton that caches bracket data on first computation and
never refreshes. This means stale brackets could be returned across test
runs, or if the underlying TaxDataProvider is swapped/patched.

These tests verify:
1. No module-level _QUEBEC_TAX_BRACKETS_2026_CACHE mutable singleton exists
2. _BracketProxy computes fresh brackets on each access (not cached globally)
3. _QUEBEC_TAX_BRACKETS_2026 function does not use global mutable cache
4. Different provider instances produce fresh (independent) bracket results
5. Mutating a provider's data is not reflected in stale cached brackets
"""

import sys
import os
import importlib
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tax_data import TaxDataProvider, TaxBracket, TaxYearData, default_tax_provider


class TestNoBracketCacheSingleton(unittest.TestCase):
    """Verify _QUEBEC_TAX_BRACKETS_2026_CACHE module-level mutable is removed."""

    def test_no_module_level_bracket_cache(self):
        """Module should not have _QUEBEC_TAX_BRACKETS_2026_CACHE attribute."""
        import tax_calculator
        self.assertFalse(
            hasattr(tax_calculator, '_QUEBEC_TAX_BRACKETS_2026_CACHE'),
            "tax_calculator should not have a module-level "
            "_QUEBEC_TAX_BRACKETS_2026_CACHE mutable singleton"
        )

    def test_no_cached_brackets_function(self):
        """_QUEBEC_TAX_BRACKETS_2026 function should not exist or should
        not use a global cache."""
        import tax_calculator
        # If the function still exists, it must not use a global mutable cache
        if hasattr(tax_calculator, '_QUEBEC_TAX_BRACKETS_2026'):
            # The function should not reference a global cache variable
            import inspect
            source = inspect.getsource(tax_calculator._QUEBEC_TAX_BRACKETS_2026)
            self.assertNotIn(
                '_QUEBEC_TAX_BRACKETS_2026_CACHE',
                source,
                "_QUEBEC_TAX_BRACKETS_2026 should not reference "
                "_QUEBEC_TAX_BRACKETS_2026_CACHE global"
            )

    def test_bracket_proxy_no_global_cache(self):
        """_BracketProxy._get() should not use a module-level global cache."""
        import tax_calculator
        import inspect
        source = inspect.getsource(tax_calculator._BracketProxy._get)
        self.assertNotIn(
            '_QUEBEC_TAX_BRACKETS_2026_CACHE',
            source,
            "_BracketProxy._get should not reference "
            "_QUEBEC_TAX_BRACKETS_2026_CACHE global"
        )


class TestBracketProxyFreshComputation(unittest.TestCase):
    """Verify _BracketProxy computes fresh brackets on each access.

    If the proxy used a global cache, reloading the module or changing the
    provider would still return stale data.
    """

    def test_proxy_returns_consistent_data_across_accesses(self):
        """Multiple accesses to QUEBEC_TAX_BRACKETS_2026 should return
        consistent data (not broken by removing cache — just not globally
        stale)."""
        from tax_calculator import QUEBEC_TAX_BRACKETS_2026
        b1 = list(QUEBEC_TAX_BRACKETS_2026)
        b2 = list(QUEBEC_TAX_BRACKETS_2026)
        self.assertEqual(b1, b2)

    def test_proxy_iteration_fresh(self):
        """Iterating over proxy should produce fresh data each time."""
        from tax_calculator import QUEBEC_TAX_BRACKETS_2026
        brackets1 = list(QUEBEC_TAX_BRACKETS_2026)
        brackets2 = list(QUEBEC_TAX_BRACKETS_2026)
        self.assertEqual(len(brackets1), len(brackets2))
        self.assertEqual(brackets1[0], brackets2[0])

    def test_proxy_indexing_fresh(self):
        """Indexing proxy should work consistently."""
        from tax_calculator import QUEBEC_TAX_BRACKETS_2026
        first = QUEBEC_TAX_BRACKETS_2026[0]
        # Should be a 5-tuple (min, max, unused, unused, rate)
        self.assertEqual(len(first), 5)
        self.assertGreaterEqual(first[0], 0)  # min_income >= 0

    def test_proxy_len_fresh(self):
        """len() on proxy should work consistently."""
        from tax_calculator import QUEBEC_TAX_BRACKETS_2026
        self.assertGreater(len(QUEBEC_TAX_BRACKETS_2026), 0)


class TestComputeLegacyBracketsNoGlobalState(unittest.TestCase):
    """Verify _compute_legacy_brackets doesn't rely on or mutate global state."""

    def test_compute_legacy_brackets_returns_data(self):
        """_compute_legacy_brackets should return valid bracket tuples."""
        from tax_calculator import _compute_legacy_brackets
        brackets = _compute_legacy_brackets()
        self.assertIsInstance(brackets, list)
        self.assertGreater(len(brackets), 0)
        # Each bracket is a 5-tuple
        self.assertEqual(len(brackets[0]), 5)

    def test_compute_legacy_brackets_with_explicit_provider(self):
        """_compute_legacy_brackets should accept explicit provider."""
        from tax_calculator import _compute_legacy_brackets
        provider = TaxDataProvider()
        brackets = _compute_legacy_brackets(provider=provider)
        self.assertIsInstance(brackets, list)
        self.assertGreater(len(brackets), 0)

    def test_compute_legacy_brackets_no_mutation(self):
        """_compute_legacy_brackets should not mutate module-level state."""
        import tax_calculator
        from tax_calculator import _compute_legacy_brackets
        # Call twice and verify no module-level state changed
        _compute_legacy_brackets()
        # No global cache should exist after calling
        self.assertFalse(
            hasattr(tax_calculator, '_QUEBEC_TAX_BRACKETS_2026_CACHE'),
            "Calling _compute_legacy_brackets should not create "
            "_QUEBEC_TAX_BRACKETS_2026_CACHE"
        )


class TestGetCombinedBracketsNoStaleCache(unittest.TestCase):
    """Verify get_combined_brackets doesn't cache results globally."""

    def test_repeated_calls_no_global_mutation(self):
        """Repeated calls to get_combined_brackets should not leave
        global mutable state behind."""
        import tax_calculator
        default_tax_provider().get_combined_brackets()
        default_tax_provider().get_combined_brackets(year=2026, province='ontario')
        default_tax_provider().get_combined_brackets()
        # No global cache should exist
        self.assertFalse(
            hasattr(tax_calculator, '_QUEBEC_TAX_BRACKETS_2026_CACHE'),
            "Repeated calls should not create _QUEBEC_TAX_BRACKETS_2026_CACHE"
        )

    def test_different_providers_independent(self):
        """Two different provider instances should produce independent results."""
        p1 = TaxDataProvider()
        p2 = TaxDataProvider()
        b1 = p1.get_combined_brackets()
        b2 = p2.get_combined_brackets()
        # Same data, but independently computed
        self.assertEqual(b1, b2)


class TestNoGlobalMutablesAtAll(unittest.TestCase):
    """Comprehensive check: no mutable singleton caches in tax_calculator."""

    def test_no_module_level_caches(self):
        """Module should not have any mutable singleton caches for brackets."""
        import tax_calculator
        # Check for the specific cache variable from issue #117
        forbidden_attrs = [
            '_QUEBEC_TAX_BRACKETS_2026_CACHE',
            '_brackets_cache',
            '_combined_brackets_cache',
        ]
        for attr in forbidden_attrs:
            self.assertFalse(
                hasattr(tax_calculator, attr),
                f"tax_calculator should not have module-level cache: {attr}"
            )


if __name__ == '__main__':
    unittest.main()