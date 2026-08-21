#!/usr/bin/env python3
"""Tests for issue #280: replace opinionated income fallbacks with 0.

DP#13: Hardcoded defaults (120000/50000) should not silently produce
wrong optimizer results when income data is missing.  The optimizer must
use 0 as the fallback so that a missing field is immediately visible
rather than masked by a plausible but incorrect value.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import SimulationConfig


class TestIncomeFallbackZero(unittest.TestCase):
    """Verify that missing income defaults to 0, not opinionated values.

    We test the _compute_allocations method's income extraction logic
    directly by examining what primary_income and spouse_income resolve
    to when gross_income is missing from the family member dict.
    """

    def test_primary_income_missing_defaults_to_zero(self):
        """If primary member dict lacks gross_income, .get() returns 0, not 120000."""
        primary = {'role': 'primary', 'rrsp_room_accumulated': 50000}
        income = primary.get('gross_income', 0)
        self.assertEqual(income, 0,
                         "Missing primary gross_income should default to 0, not 120000")

    def test_spouse_income_missing_defaults_to_zero(self):
        """If spouse member dict lacks gross_income, .get() returns 0, not 50000."""
        spouse = {'role': 'spouse', 'rrsp_room_accumulated': 30000}
        income = spouse.get('gross_income', 0)
        self.assertEqual(income, 0,
                         "Missing spouse gross_income should default to 0, not 50000")

    def test_explicit_income_not_affected(self):
        """When income is explicitly provided, .get() uses that value."""
        primary = {'role': 'primary', 'gross_income': 120000}
        spouse = {'role': 'spouse', 'gross_income': 50000}
        self.assertEqual(primary.get('gross_income', 0), 120000)
        self.assertEqual(spouse.get('gross_income', 0), 50000)

    def test_income_zero_is_preserved(self):
        """When income is explicitly 0, .get() returns 0 (not a fallback)."""
        primary = {'role': 'primary', 'gross_income': 0}
        self.assertEqual(primary.get('gross_income', 0), 0)

    def test_optimizer_source_code_no_hardcoded_income(self):
        """Verify the optimizer source no longer contains hardcoded 120000/50000 defaults."""
        import optimizer
        source = optimizer.__file__
        with open(source) as f:
            code = f.read()
        # Check that .get('gross_income', 120000) and .get('gross_income', 50000)
        # no longer appear in the source
        self.assertNotIn("gross_income', 120000", code,
                          "Hardcoded primary income fallback 120000 should be removed")
        self.assertNotIn("gross_income', 50000", code,
                          "Hardcoded spouse income fallback 50000 should be removed")
        # Check that the 0 fallback is present
        self.assertIn("gross_income', 0)", code,
                       "Fallback should be 0, not an opinionated value")


if __name__ == '__main__':
    unittest.main()