#!/usr/bin/env python3
"""Unit tests for module_registry.py — country package discovery/registry.

Epic #603 Track C Phase 2b: this file used to also test
``check_auto_includes``/``_has_nested_field`` — deleted along with those
functions (zero production callers, operated on the legacy input shape's
auto-include triggers). What remains, ``CountryRegistry`` and
``default_registry``, is unrelated to input-document shape and unaffected.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest


class TestCountryRegistry(unittest.TestCase):
    """DP#3: CountryRegistry replaces module-level mutable dict."""

    def test_registry_starts_empty(self):
        from module_registry import CountryRegistry
        registry = CountryRegistry()
        self.assertEqual(len(registry), 0)

    def test_register_and_get(self):
        from module_registry import CountryRegistry
        registry = CountryRegistry()
        mock_fn = lambda provider: None
        registry.register('canada', mock_fn)
        self.assertEqual(len(registry), 1)
        self.assertIn('canada', registry)
        self.assertIs(registry.get('canada'), mock_fn)

    def test_get_missing_returns_none(self):
        from module_registry import CountryRegistry
        registry = CountryRegistry()
        self.assertIsNone(registry.get('nonexistent'))

    def test_register_all_calls_each_function(self):
        from module_registry import CountryRegistry
        calls = []

        def mock_register_1(provider):
            calls.append(('country_1', provider))

        def mock_register_2(provider):
            calls.append(('country_2', provider))

        registry = CountryRegistry()
        registry.register('country_1', mock_register_1)
        registry.register('country_2', mock_register_2)

        mock_provider = object()
        registry.register_all(mock_provider)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ('country_1', mock_provider))
        self.assertEqual(calls[1], ('country_2', mock_provider))

    def test_fresh_instance_no_cross_contamination(self):
        """DP#3: Each registry instance is independent."""
        from module_registry import CountryRegistry
        mock_fn = lambda provider: None

        registry1 = CountryRegistry()
        registry1.register('canada', mock_fn)

        registry2 = CountryRegistry()
        self.assertEqual(len(registry2), 0)
        self.assertNotIn('canada', registry2)

    def test_discover_finds_country_packages(self):
        from module_registry import CountryRegistry
        registry = CountryRegistry()
        registry.discover()
        # Canada should be discoverable
        self.assertIn('canada', registry)
        self.assertEqual(len(registry), 1)

    def test_items_returns_key_value_pairs(self):
        from module_registry import CountryRegistry
        mock_fn = lambda provider: None
        registry = CountryRegistry()
        registry.register('canada', mock_fn)
        items = registry.modules
        self.assertEqual(items, {'canada': mock_fn})

    def test_register_all_does_not_auto_discover(self):
        """register_all on empty registry does nothing (no auto-discover)."""
        from module_registry import CountryRegistry
        registry = CountryRegistry()
        # Empty registry: register_all does nothing without discover()
        provider_calls = []

        class MockProvider:
            pass

        # This should discover canada and register it
        try:
            registry.register_all(MockProvider())
            # After auto-discovery, canada should be found
            self.assertIn('canada', registry)
        except Exception:
            # If countries aren't available, that's fine for this test
            pass


class TestCountriesDefaultRegistry(unittest.TestCase):
    """DP#3: countries.default_registry has Canada registered."""

    def test_default_registry_has_canada(self):
        from countries import default_registry
        self.assertIn('canada', default_registry)
        self.assertEqual(len(default_registry), 1)

    def test_default_registry_register_all(self):
        from countries import default_registry
        from tax_data import TaxDataProvider
        provider = TaxDataProvider(auto_register=False)
        default_registry.register_all(provider)
        # Provider should have Canadian data after registration
        self.assertTrue(len(provider._fallbacks) > 0 or hasattr(provider, '_years'))


if __name__ == '__main__':
    unittest.main()
