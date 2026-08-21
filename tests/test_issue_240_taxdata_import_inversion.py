#!/usr/bin/env python3
"""Tests for issue #240 (DP#25/DP#10): invert tax_data → countries.canada.

The data layer (tax_data) must not import jurisdiction code. Instead the
Canada package registers its fallback data into TaxDataProvider at import
time (DP#16 package-presence trigger). These tests lock that inversion.

Run with: python3 -m pytest tests/test_issue_240_taxdata_import_inversion.py -v
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import tax_data
from tax_data import TaxDataProvider


def _imported_modules(source_path):
    """Return every module name referenced by an import in a Python source file."""
    with open(source_path) as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestTaxDataDoesNotImportCanada(unittest.TestCase):
    """tax_data must not import any countries.canada module (DP#25/DP#10)."""

    def test_no_canada_import_anywhere_in_source(self):
        # Catches both top-level and function-local imports via AST, so a
        # re-introduced `from countries.canada... import` fails this test.
        canada_imports = {
            m for m in _imported_modules(tax_data.__file__)
            if m == "countries.canada" or m.startswith("countries.canada.")
        }
        self.assertEqual(canada_imports, set())


class TestCanadaRegistersIntoDataLayer(unittest.TestCase):
    """Importing countries.canada registers its fallbacks into tax_data."""

    def test_canada_registers_oas_fallback(self):
        import countries.canada  # noqa: F401  (import-time registration)
        # The registry the data layer reads from is populated by the country
        # package, not by tax_data importing Canada.
        self.assertIn(2026, tax_data._OAS_FALLBACK_BY_YEAR)

    def test_canada_registers_bracket_fallback_builder(self):
        import countries.canada  # noqa: F401
        self.assertTrue(tax_data._FALLBACK_BUILDERS)


class TestFallbacksFlowThroughRegistration(unittest.TestCase):
    """The registered data must drive the provider's fallback behaviour."""

    def test_oas_fallback_matches_registered_value(self):
        import countries.canada  # noqa: F401
        provider = TaxDataProvider(auto_register=True)
        # Federal data omits OAS for 2024, so the getter uses the registered
        # fallback — the two must agree (relational, no magic snapshot).
        self.assertEqual(
            provider.get_oas_annual_max(2024),
            tax_data._OAS_FALLBACK_BY_YEAR[2024]["oas_annual_max"],
        )

    def test_hardcoded_fallbacks_use_registered_builder(self):
        import countries.canada  # noqa: F401
        provider = TaxDataProvider(auto_register=False)
        provider._build_hardcoded_fallbacks()
        # Builder-registered years become available without tax_data ever
        # importing the bracket data module.
        self.assertIn(2026, provider.available_years("canada", "quebec"))


if __name__ == "__main__":
    unittest.main()
