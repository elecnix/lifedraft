#!/usr/bin/env python3
"""Tests for issue #30: Remove _provider singleton from tax_calculator.py.

The module-level _provider singleton persists across calls and test runs,
creating hidden mutable state. These tests verify:
1. No module-level _provider singleton persists across calls
2. TaxDataProvider.get_combined_brackets() works on an explicit provider
3. default_tax_provider() gives a usable shared provider (DP#9: no free-function shim)
4. Repeated calls with the same explicit provider are consistent
5. _get_provider() is removed (not accessible)
6. countries.canada.tax_calc functions don't depend on a global provider
"""

import sys
import os
import unittest
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tax_data import TaxDataProvider, default_tax_provider


class TestNoProviderSingleton(unittest.TestCase):
    """Verify that _provider module-level singleton no longer exists."""

    def test_no_module_level_provider(self):
        """Module should not have a _provider attribute that persists."""
        import tax_calculator
        # _provider should not exist as a module-level mutable singleton
        self.assertFalse(
            hasattr(tax_calculator, '_provider'),
            "tax_calculator should not have a module-level _provider singleton"
        )

    def test_no_get_provider_function(self):
        """_get_provider() function should be removed."""
        import tax_calculator
        self.assertFalse(
            hasattr(tax_calculator, '_get_provider'),
            "tax_calculator should not have a _get_provider() function"
        )


class TestGetCombinedBracketsExplicitProvider(unittest.TestCase):
    """Verify get_combined_brackets works with explicit provider."""

    def setUp(self):
        self.provider = TaxDataProvider()

    def test_explicit_provider_returns_brackets(self):
        """Passing provider explicitly should return valid brackets."""
        brackets = self.provider.get_combined_brackets()
        self.assertIsInstance(brackets, list)
        self.assertGreater(len(brackets), 0)

    def test_explicit_provider_quebec(self):
        """Explicit provider with Quebec province."""
        brackets = self.provider.get_combined_brackets(year=2026, province='quebec')
        self.assertIsInstance(brackets, list)
        for b in brackets:
            self.assertIn('min', b)
            self.assertIn('max', b)
            self.assertIn('rate', b)

    def test_explicit_provider_ontario(self):
        """Explicit provider with Ontario province."""
        brackets = self.provider.get_combined_brackets(year=2026, province='ontario')
        self.assertIsInstance(brackets, list)
        self.assertGreater(len(brackets), 0)

    def test_explicit_provider_no_mutable_global_state(self):
        """Two calls with different providers should be independent."""
        provider1 = TaxDataProvider()
        provider2 = TaxDataProvider()
        b1 = provider1.get_combined_brackets()
        b2 = provider2.get_combined_brackets()
        # Both should return equivalent data
        self.assertEqual(len(b1), len(b2))
        self.assertEqual(b1[0]['rate'], b2[0]['rate'])

    def test_consistent_results_with_same_provider(self):
        """Same provider should give consistent results across calls."""
        provider = TaxDataProvider()
        b1 = provider.get_combined_brackets()
        b2 = provider.get_combined_brackets()
        self.assertEqual(b1, b2)


class TestDefaultProvider(unittest.TestCase):
    """Verify default_tax_provider() is a usable explicit provider (DP#9)."""

    def test_default_provider_works(self):
        """The shared default provider returns valid brackets."""
        brackets = default_tax_provider().get_combined_brackets()
        self.assertIsInstance(brackets, list)
        self.assertGreater(len(brackets), 0)

    def test_default_provider_no_persistent_state(self):
        """Repeated calls on the default provider are consistent."""
        b1 = default_tax_provider().get_combined_brackets()
        b2 = default_tax_provider().get_combined_brackets()
        # Should return equivalent data even without explicit provider
        self.assertEqual(len(b1), len(b2))

    def test_marginal_rate_default_provider(self):
        """marginal_rate without brackets should still work."""
        from tax_calculator import marginal_rate
        rate = marginal_rate(100000)
        self.assertGreater(rate, 0)
        self.assertLess(rate, 1)

    def test_tax_on_income_default_provider(self):
        """tax_on_income without brackets should still work."""
        from tax_calculator import tax_on_income
        tax = tax_on_income(100000)
        self.assertGreater(tax, 0)


class TestCanadaTaxCalcNoGlobalProvider(unittest.TestCase):
    """Verify countries.canada.tax_calc doesn't depend on _get_provider."""

    def test_federal_tax_works(self):
        """federal_tax should work without relying on module-level provider."""
        from countries.canada.tax_calc import federal_tax
        tax = federal_tax(100000, 2026, "quebec")
        self.assertGreater(tax, 0)

    def test_quebec_tax_works(self):
        """quebec_tax should work without relying on module-level provider."""
        from countries.canada.tax_calc import quebec_tax
        tax = quebec_tax(100000, 2026)
        self.assertGreater(tax, 0)

    def test_federal_tax_with_provider(self):
        """federal_tax should accept an explicit provider parameter."""
        from countries.canada.tax_calc import federal_tax
        provider = TaxDataProvider()
        # federal_tax should work with explicit provider
        tax = federal_tax(100000, 2026, "quebec", provider=provider)
        self.assertGreater(tax, 0)

    def test_quebec_tax_with_provider(self):
        """quebec_tax should accept an explicit provider parameter."""
        from countries.canada.tax_calc import quebec_tax
        provider = TaxDataProvider()
        tax = quebec_tax(100000, 2026, provider=provider)
        self.assertGreater(tax, 0)

    def test_no_import_of_get_provider(self):
        """countries.canada.tax_calc should not import _get_provider."""
        import countries.canada.tax_calc
        self.assertFalse(
            hasattr(countries.canada.tax_calc, '_get_provider'),
            "countries.canada.tax_calc should not import _get_provider"
        )


class TestBracketProxyNoSingleton(unittest.TestCase):
    """Verify _BracketProxy works without the singleton provider."""

    def test_proxy_iteration(self):
        from tax_calculator import QUEBEC_TAX_BRACKETS_2026
        brackets = list(QUEBEC_TAX_BRACKETS_2026)
        self.assertGreater(len(brackets), 0)

    def test_proxy_indexing(self):
        from tax_calculator import QUEBEC_TAX_BRACKETS_2026
        first = QUEBEC_TAX_BRACKETS_2026[0]
        self.assertIsNotNone(first)

    def test_proxy_len(self):
        from tax_calculator import QUEBEC_TAX_BRACKETS_2026
        self.assertGreater(len(QUEBEC_TAX_BRACKETS_2026), 0)


if __name__ == '__main__':
    unittest.main()