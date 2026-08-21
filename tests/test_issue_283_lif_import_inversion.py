#!/usr/bin/env python3
"""Tests for issue #283 (DP#25/DP#10): invert simulation_state → countries.canada.

The simulation layer (simulation_state) must not import jurisdiction code such
as countries.canada.locked_in_account. Instead the Canada package registers a
LIF-conversion provider into simulation_state at import time (DP#16
package-presence trigger). These tests lock that inversion and confirm the
LIRA→LIF conversion still resolves through the registered adapter.

Run with: python3 -m pytest tests/test_issue_283_lif_import_inversion.py -v
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import simulation_state


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


class TestSimulationStateDoesNotImportCanada(unittest.TestCase):
    """simulation_state must not import countries.canada.locked_in_account (DP#25)."""

    def test_no_locked_in_account_import_anywhere_in_source(self):
        # AST-walk catches both top-level and function-local imports, so a
        # re-introduced `from countries.canada.locked_in_account import` fails.
        bad = {
            m for m in _imported_modules(simulation_state.__file__)
            if m == "countries.canada.locked_in_account"
            or m.startswith("countries.canada.locked_in_account.")
        }
        self.assertEqual(bad, set())


class TestCanadaRegistersLIFProvider(unittest.TestCase):
    """Importing countries.canada registers its LIF-conversion provider."""

    def test_canada_registers_provider(self):
        import countries.canada  # noqa: F401  (import-time registration)
        provider = simulation_state._get_lif_conversion_provider()
        self.assertIsNotNone(provider)
        # The provider must satisfy the core's LIF-conversion contract.
        for attr in ("must_convert_by_year", "make_locked_in_account",
                     "make_lif_fund"):
            self.assertTrue(hasattr(provider, attr), attr)


class TestConversionResolvesThroughAdapter(unittest.TestCase):
    """The LIRA→LIF conversion (incl. issue #343) flows through the provider."""

    def test_conversion_fires_at_age_71_via_adapter(self):
        # End-to-end: a ≥26-yr Quebec projection with a CRI/LIRA must convert at
        # age 71 (calendar 2050 for birth_year 1979) entirely through the
        # registered provider — simulation_state imports no Canada code.
        from simulation import FamilySimulation
        from simulation_config import SimulationConfig

        cfg = SimulationConfig(
            projection_years=28,
            investment_return=0.07,
            house_value=600000, mortgage_balance=300000, mortgage_rate=0.05,
            margin_available=100000, start_year=2026,
            family_members=[
                {'role': 'primary', 'gross_income': 130000, 'birth_year': 1979,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 70000,
                 'retirement_age': 65},
                {'role': 'spouse', 'gross_income': 90000, 'birth_year': 1981,
                 'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 60000,
                 'retirement_age': 65},
            ],
            children=[],
            lira_data={'balance': 100000, 'birth_year': 1979,
                       'jurisdiction': 'quebec', 'reference_rate': 0.06},
        )
        results = FamilySimulation(cfg).run()
        by_cal = {2026 + r.year - 1: r for r in results}
        conv = by_cal[2050]
        # Relational guard (no magic snapshot): at age 71 the LIRA is depleted
        # and the LIF holds a positive balance.
        self.assertEqual(conv.lira_balance, 0.0)
        self.assertGreater(conv.lif_balance, 0.0)

    def test_missing_provider_raises_clear_error(self):
        # If no provider is registered but a locked-in account is present, the
        # pure step must fail loudly rather than silently dropping the LIRA.
        saved = simulation_state._LIF_CONVERSION_PROVIDER
        try:
            simulation_state._LIF_CONVERSION_PROVIDER = None
            with self.assertRaises(RuntimeError):
                simulation_state._get_lif_conversion_provider()
        finally:
            simulation_state._LIF_CONVERSION_PROVIDER = saved


if __name__ == "__main__":
    unittest.main()
