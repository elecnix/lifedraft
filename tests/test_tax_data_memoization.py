#!/usr/bin/env python3
"""Memoization of TaxDataProvider._load_year (issue #295 hot path).

A calendar year's projected brackets/limits are invariant across every
optimizer scenario and year-step, so _load_year must project a given
(jurisdiction, year, indexation_rate) once and reuse the result — without
changing WHAT it returns. These tests pin both properties: the cache is hit
(projection runs once) and the cached value equals a freshly-projected one.

All data is government-published; no personal information.

Run with: python3 -m pytest tests/test_tax_data_memoization.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from tax_data import TaxDataProvider


class TestLoadYearMemoization(unittest.TestCase):
    def setUp(self):
        self.provider = TaxDataProvider()
        self.provider.indexation_rate = 0.02

    def test_repeated_year_projects_only_once(self):
        """A projected year is computed once, then served from cache."""
        calls = []
        original = self.provider._project_from_base

        def counting(base, target_year, indexation_rate=None):
            calls.append(target_year)
            return original(base, target_year, indexation_rate)

        self.provider._project_from_base = counting

        first = self.provider.get_year_data(2040, "canada", "quebec")
        second = self.provider.get_year_data(2040, "canada", "quebec")

        # Same cached object returned; projection ran exactly once.
        self.assertIs(first, second)
        self.assertEqual(calls, [2040])

    def test_cached_equals_fresh_projection(self):
        """Memoization must not change the value — cached == freshly built."""
        warmed = self.provider.get_year_data(2040, "canada", "quebec")
        _ = self.provider.get_year_data(2040, "canada", "quebec")  # from cache

        fresh_provider = TaxDataProvider()
        fresh_provider.indexation_rate = 0.02
        fresh = fresh_provider.get_year_data(2040, "canada", "quebec")

        self.assertEqual(warmed, fresh)

    def test_indexation_rate_change_is_not_stale(self):
        """Changing indexation_rate must recompute, not serve a stale year."""
        low = self.provider.get_year_data(2040, "canada", "federal")
        self.provider.indexation_rate = 0.05
        high = self.provider.get_year_data(2040, "canada", "federal")

        # Higher indexation escalates thresholds further — proves the cache
        # key includes the rate rather than returning the 2% projection.
        self.assertGreater(
            high.federal_brackets[-1].min_income,
            low.federal_brackets[-1].min_income,
        )

    def test_register_year_invalidates_cache(self):
        """Registering new base data drops memoized projections."""
        from tax_data import TaxYearData, TaxBracket

        before = self.provider.get_year_data(2040, "canada", "quebec")
        # Register a 2035 Quebec base with a distinctly higher first bracket,
        # which becomes the nearest base for a 2040 projection.
        self.provider.register_year(TaxYearData(
            year=2035, country="canada", province="quebec",
            federal_brackets=[TaxBracket(0, 999999, 0.15, "test")],
            provincial_brackets=[TaxBracket(0, 999999, 0.14, "test")],
        ))
        after = self.provider.get_year_data(2040, "canada", "quebec")

        self.assertNotEqual(before.federal_brackets, after.federal_brackets)


if __name__ == "__main__":
    unittest.main()
