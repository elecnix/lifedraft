#!/usr/bin/env python3
"""Tests for Quebec LIF (FRV) withdrawal factors.

DP#11 — this is an *integration* file. ``quebec_lif`` is a thin Quebec wrapper
that DELEGATES to the federal ``locked_in_account`` module (DP#7: model the
mechanism, not the branded product — ``quebec_lif_maximum_withdrawal`` calls
``lif_maximum_withdrawal(..., jurisdiction='quebec')`` and the withdrawal range
reuses ``lif_minimum_withdrawal``). Because the module under test composes with
``locked_in_account`` by design, its tests necessarily exercise that
composition: ``lif_maximum_withdrawal`` / ``lif_minimum_withdrawal`` are used as
the delegation oracle and ``get_ympe`` / ``QUEBEC_PRESCRIBED_RATES`` as the
year-versioned data source. These are not standalone ``locked_in_account``
tests (those live in ``test_locked_in_account.py``); they assert Quebec's
delegation, divergence, and boundary behaviour against the federal baseline.

Run with: python3 -m pytest countries/canada/tests/ -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.provinces.quebec.quebec_lif import (
    quebec_lif_maximum_withdrawal,
    quebec_lif_withdrawal_range,
    quebec_lif_temporary_income_max,
    quebec_lif_annuity_conversion,
    QUEBEC_LIF_MAX_WITHDRAWAL_FACTORS,
    QUEBEC_LIF_TEMPORARY_INCOME_MPE_FRACTION,
)
from countries.canada.locked_in_account import (
    lif_minimum_withdrawal,
    lif_maximum_withdrawal,
    QUEBEC_PRESCRIBED_RATES,
    get_ympe,
)


class TestQuebecLIFMaximum(unittest.TestCase):
    """Quebec LIF maximum withdrawal — no maximum for ages 55+ (effective 2025)."""

    def test_quebec_no_maximum_age_55_2025(self):
        """Quebec: age 55 in 2025 → no maximum (returns balance)."""
        balance = 300000
        age = 55
        max_w = quebec_lif_maximum_withdrawal(balance, age, year=2025)
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_no_maximum_age_65(self):
        """Quebec: age 65 in 2026 → no maximum."""
        balance = 500000
        max_w = quebec_lif_maximum_withdrawal(balance, age=65, year=2026)
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_no_maximum_age_71(self):
        """Quebec: age 71 in 2026 → no maximum."""
        balance = 300000
        max_w = quebec_lif_maximum_withdrawal(balance, age=71, year=2026)
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_no_maximum_age_80(self):
        """Quebec: age 80 in 2026 → no maximum."""
        balance = 200000
        max_w = quebec_lif_maximum_withdrawal(balance, age=80, year=2026)
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_has_maximum_before_2025(self):
        """Quebec: before 2025, ages 55+ still had LIF maximum."""
        balance = 300000
        age = 65
        max_w = quebec_lif_maximum_withdrawal(balance, age, year=2024)
        self.assertLess(max_w, balance)

    def test_quebec_delegates_to_federal(self):
        """quebec_lif_maximum_withdrawal delegates to lif_maximum_withdrawal."""
        balance = 300000
        age = 65
        year = 2026
        qc_max = quebec_lif_maximum_withdrawal(balance, age, year)
        fed_max = lif_maximum_withdrawal(balance, age, year, jurisdiction='quebec')
        self.assertAlmostEqual(qc_max, fed_max)

    def test_federal_vs_quebec_differ_at_55(self):
        """Federal has a maximum at age 55; Quebec does not (2025+)."""
        balance = 300000
        age = 55
        year = 2026
        fed_max = lif_maximum_withdrawal(balance, age, year, jurisdiction='federal')
        qc_max = quebec_lif_maximum_withdrawal(balance, age, year)
        self.assertLess(fed_max, balance)
        self.assertAlmostEqual(qc_max, balance)


class TestQuebecLIFRange(unittest.TestCase):
    """Quebec LIF withdrawal range."""

    def test_range_includes_jurisdiction(self):
        result = quebec_lif_withdrawal_range(300000, 71, year=2026)
        self.assertEqual(result['jurisdiction'], 'quebec')

    def test_range_valid(self):
        result = quebec_lif_withdrawal_range(300000, 71, year=2026)
        self.assertLessEqual(result['minimum'], result['maximum'])

    def test_minimum_equals_rrif(self):
        balance = 300000
        age = 71
        min_qc = quebec_lif_withdrawal_range(balance, age, year=2026)['minimum']
        min_rrif = lif_minimum_withdrawal(balance, age)
        self.assertAlmostEqual(min_qc, min_rrif, places=2)

    def test_maximum_equals_balance_quebec(self):
        """Quebec: maximum = balance for ages 55+."""
        result = quebec_lif_withdrawal_range(300000, 65, year=2026)
        self.assertAlmostEqual(result['maximum'], 300000)

    def test_range_age_54_below_age_55_boundary(self):
        """DP#17: age 54 (2025+), one year below the no-maximum boundary,
        still has a maximum strictly below the balance."""
        balance = 200000
        result = quebec_lif_withdrawal_range(balance, age=54, year=2025)
        self.assertLess(result['minimum'], result['maximum'])
        self.assertLess(result['maximum'], balance)
        self.assertEqual(result['jurisdiction'], 'quebec')

    def test_range_age_55_at_age_55_boundary_2025(self):
        """DP#17: age 55 in 2025+, the no-maximum side of the boundary —
        maximum equals the full balance."""
        balance = 200000
        result = quebec_lif_withdrawal_range(balance, age=55, year=2025)
        self.assertLess(result['minimum'], result['maximum'])
        self.assertAlmostEqual(result['maximum'], balance)

    def test_range_age_55_pre_2025_still_has_maximum(self):
        """DP#17, DP#20: age 55 in 2024 (pre-2025 rule) still had a
        maximum — the no-maximum rule is year-versioned, not just
        age-gated."""
        balance = 200000
        result = quebec_lif_withdrawal_range(balance, age=55, year=2024)
        self.assertLess(result['maximum'], balance)


class TestQuebecUnder55PrescribedRate(unittest.TestCase):
    """Quebec LIF under-55 uses prescribed-rate formula.

    Per Retraite Québec, for FRV holders under age 55, the maximum
    withdrawal is prescribed_rate × balance.
    """

    def test_quebec_under_55_prescribed_rate_2026(self):
        """Quebec age 54 in 2026: max = 6.25% × balance (Retraite Québec prescribed rate)."""
        balance = 200000
        max_w = quebec_lif_maximum_withdrawal(balance, age=54, year=2026)
        self.assertAlmostEqual(max_w, balance * 0.0625, places=2)

    def test_quebec_under_55_prescribed_rate_2025(self):
        """Quebec age 54 in 2025: max = 6% × balance (Retraite Québec prescribed rate)."""
        balance = 200000
        max_w = quebec_lif_maximum_withdrawal(balance, age=54, year=2025)
        self.assertAlmostEqual(max_w, balance * 0.06, places=2)

    def test_quebec_under_55_prescribed_rate_2024(self):
        """Quebec age 54 in 2024: max = 6.1% × balance (Schedule 0.6 factor)."""
        balance = 200000
        max_w = quebec_lif_maximum_withdrawal(balance, age=54, year=2024)
        self.assertAlmostEqual(max_w, balance * 0.061, places=2)

    def test_quebec_under_55_no_factor_table_entries(self):
        """Quebec 2025+ factor tables should NOT have under-55 entries."""
        for year in [2025, 2026]:
            if year in QUEBEC_LIF_MAX_WITHDRAWAL_FACTORS:
                for age in QUEBEC_LIF_MAX_WITHDRAWAL_FACTORS[year]:
                    self.assertGreaterEqual(age, 55,
                        f"Year {year} should not have age {age} entry (under-55 uses prescribed rate)")

    def test_quebec_convenience_passes_temporary_income(self):
        """Quebec convenience function passes temporary_income to Article 20."""
        balance = 200000
        # Without temporary income
        max_no_ti = quebec_lif_maximum_withdrawal(balance, age=54, year=2025)
        self.assertAlmostEqual(max_no_ti, balance * 0.06)
        # With temporary income
        max_with_ti = quebec_lif_maximum_withdrawal(balance, age=54, year=2025,
                                                     temporary_income=3000)
        self.assertAlmostEqual(max_with_ti, balance * 0.06 - 3000)

    def test_quebec_withdrawal_range_passes_temporary_income(self):
        """Quebec withdrawal range passes temporary_income for under-55."""
        from countries.canada.provinces.quebec.quebec_lif import (
            quebec_lif_withdrawal_range,
        )
        balance = 200000
        result = quebec_lif_withdrawal_range(balance, age=54, year=2025,
                                              temporary_income=3000)
        self.assertAlmostEqual(result['maximum'], balance * 0.06 - 3000)

    def test_quebec_withdrawal_range_min_max_guard(self):
        """When temporary_income reduces max below min, min is clamped to max."""
        from countries.canada.provinces.quebec.quebec_lif import (
            quebec_lif_withdrawal_range,
        )
        balance = 200000
        # prescribed_rate * balance = 200000 * 0.06 = 12000
        # temporary_income = 15000 > 12000 → max = 0
        result = quebec_lif_withdrawal_range(balance, age=54, year=2025,
                                              temporary_income=15000)
        self.assertAlmostEqual(result['maximum'], 0.0)
        self.assertAlmostEqual(result['minimum'], 0.0)
        self.assertLessEqual(result['minimum'], result['maximum'])


class TestQuebecLIFTemporaryIncome(unittest.TestCase):
    """#322 — Quebec FRV temporary income (revenu temporaire), 40% of MPE for 54-64."""

    def test_age_54_eligible_full_ceiling(self):
        """Age 54 with no other temporary income: ceiling = 40% × YMPE."""
        expected = QUEBEC_LIF_TEMPORARY_INCOME_MPE_FRACTION * get_ympe(2026)
        self.assertAlmostEqual(quebec_lif_temporary_income_max(54, 2026), expected)

    def test_age_64_eligible(self):
        """Age 64 is still within the eligible band (under 65)."""
        self.assertGreater(quebec_lif_temporary_income_max(64, 2026), 0)

    def test_age_65_not_eligible(self):
        """Age 65 is outside the band → no temporary income."""
        self.assertEqual(quebec_lif_temporary_income_max(65, 2026), 0.0)

    def test_under_54_not_eligible(self):
        """Age 53 is below the band → no temporary income."""
        self.assertEqual(quebec_lif_temporary_income_max(53, 2026), 0.0)

    def test_other_temporary_income_reduces_ceiling(self):
        """Temporary income drawn elsewhere reduces the available ceiling."""
        ceiling = QUEBEC_LIF_TEMPORARY_INCOME_MPE_FRACTION * get_ympe(2026)
        self.assertAlmostEqual(
            quebec_lif_temporary_income_max(54, 2026, other_temporary_income=5000),
            ceiling - 5000)


class TestQuebecLIFAnnuityConversion(unittest.TestCase):
    """#322 — Quebec FRV conversion to a life annuity."""

    def test_annual_payment_is_rate_times_balance(self):
        result = quebec_lif_annuity_conversion(100000, 0.06)
        self.assertAlmostEqual(result['annual_payment'], 6000)

    def test_periodic_payment_splits_annual(self):
        result = quebec_lif_annuity_conversion(120000, 0.06, payments_per_year=12)
        self.assertAlmostEqual(result['periodic_payment'], result['annual_payment'] / 12)

    def test_annuity_preserves_locking_in(self):
        result = quebec_lif_annuity_conversion(50000, 0.05)
        self.assertTrue(result['locked_in'])

    def test_zero_balance_raises(self):
        with self.assertRaises(ValueError):
            quebec_lif_annuity_conversion(0, 0.06)


if __name__ == '__main__':
    unittest.main()
