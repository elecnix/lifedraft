#!/usr/bin/env python3
"""Unit tests for CountryRegistry — DP#3 compliance: no hidden global state.

The CountryRegistry class replaces the module-level COUNTRY_MODULES dict
and the `global COUNTRY_MODULES` statement in `register_all_countries()`.

Tests verify:
- Registry is an explicit instance, not a module-level global
- Same inputs produce same outputs (pure function behavior)
- register() adds a country module to the registry
- discover() auto-discovers country packages
- register_all() calls each registered country's register function
- Two independent instances don't share state
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import MagicMock
from module_registry import CountryRegistry


class TestCountryRegistryExplicitInstance(unittest.TestCase):
    """DP#3: Registry is an explicit instance, not hidden module state."""

    def test_create_empty_registry(self):
        registry = CountryRegistry()
        self.assertEqual(len(registry), 0)

    def test_create_registry_with_initial_modules(self):
        fn = MagicMock()
        registry = CountryRegistry(modules={"canada": fn})
        self.assertEqual(len(registry), 1)
        self.assertIn("canada", registry)

    def test_independent_instances_no_shared_state(self):
        """Two registry instances must not share state (DP#3)."""
        fn_a = MagicMock()
        fn_b = MagicMock()
        r1 = CountryRegistry(modules={"canada": fn_a})
        r2 = CountryRegistry(modules={"us": fn_b})
        self.assertIn("canada", r1)
        self.assertNotIn("us", r1)
        self.assertIn("us", r2)
        self.assertNotIn("canada", r2)

    def test_len(self):
        fn = MagicMock()
        registry = CountryRegistry(modules={"canada": fn, "us": fn})
        self.assertEqual(len(registry), 2)

    def test_iter(self):
        fn = MagicMock()
        registry = CountryRegistry(modules={"canada": fn, "uk": fn})
        codes = sorted(registry)
        self.assertEqual(codes, ["canada", "uk"])

    def test_contains(self):
        fn = MagicMock()
        registry = CountryRegistry(modules={"canada": fn})
        self.assertIn("canada", registry)
        self.assertNotIn("france", registry)


class TestCountryRegistryRegister(unittest.TestCase):
    """Register a country module with the registry."""

    def test_register_adds_country(self):
        registry = CountryRegistry()
        fn = MagicMock()
        registry.register("canada", fn)
        self.assertIn("canada", registry)
        self.assertEqual(len(registry), 1)

    def test_register_overwrites_existing(self):
        fn_old = MagicMock()
        fn_new = MagicMock()
        registry = CountryRegistry(modules={"canada": fn_old})
        registry.register("canada", fn_new)
        # Should have replaced the old function
        self.assertIn("canada", registry)
        self.assertEqual(len(registry), 1)

    def test_register_all_calls_each_register_fn(self):
        """register_all() passes tax_provider to each country's register function."""
        fn_canada = MagicMock()
        fn_us = MagicMock()
        registry = CountryRegistry(modules={"canada": fn_canada, "us": fn_us})
        tax_provider = MagicMock()
        registry.register_all(tax_provider)
        fn_canada.assert_called_once_with(tax_provider)
        fn_us.assert_called_once_with(tax_provider)

    def test_register_all_empty_registry(self):
        """register_all() on empty registry is a no-op."""
        registry = CountryRegistry()
        tax_provider = MagicMock()
        registry.register_all(tax_provider)  # Should not raise


class TestCountryRegistryDiscover(unittest.TestCase):
    """Auto-discover country packages from the countries/ directory."""

    def test_discover_finds_packages(self):
        registry = CountryRegistry()
        registry.discover()
        # Canada package should exist in the test environment
        self.assertIn("canada", registry)

    def test_discover_idempotent(self):
        """Calling discover() twice should not duplicate entries."""
        registry = CountryRegistry()
        registry.discover()
        count_after_first = len(registry)
        registry.discover()
        self.assertEqual(len(registry), count_after_first)

    def test_discover_returns_self(self):
        """discover() should return the registry for chaining."""
        registry = CountryRegistry()
        result = registry.discover()
        self.assertIs(result, registry)


class TestCountryRegistryPureBehavior(unittest.TestCase):
    """DP#3: Same inputs → same outputs. No globals, no caches, no side effects."""

    def test_same_initial_modules_same_state(self):
        fn = MagicMock()
        r1 = CountryRegistry(modules={"canada": fn})
        r2 = CountryRegistry(modules={"canada": fn})
        self.assertEqual(sorted(r1), sorted(r2))

    def test_module_level_default_registry_exists(self):
        """A module-level default_registry should exist."""
        from module_registry import default_registry
        self.assertIsInstance(default_registry, CountryRegistry)
        self.assertIn("canada", default_registry)


class TestCountryRegistryGet(unittest.TestCase):
    """Test getting register functions from the registry."""

    def test_get_existing_country(self):
        fn = MagicMock()
        registry = CountryRegistry(modules={"canada": fn})
        self.assertIs(registry.get("canada"), fn)

    def test_get_missing_country_returns_none(self):
        registry = CountryRegistry()
        self.assertIsNone(registry.get("unknown"))

    def test_get_missing_country_default(self):
        fn = MagicMock()
        registry = CountryRegistry()
        self.assertIs(registry.get("unknown", fn), fn)


class TestCountryRegistryBackwardCompat(unittest.TestCase):
    """DP#9: Backward compatibility rots; remove it promptly.

    The old module-level functions should still work by delegating
    to default_registry, but with deprecation notes in their docstrings.
    """

    def test_register_all_countries_still_works(self):
        """The old register_all_countries() should delegate to default_registry."""
        from module_registry import register_all_countries, default_registry
        # This should not raise — it delegates to default_registry
        tax_provider = MagicMock()
        register_all_countries(tax_provider)



if __name__ == '__main__':
    unittest.main()