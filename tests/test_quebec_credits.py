#!/usr/bin/env python3
"""Tests for Quebec credit pure functions in quebec_credits.py.

Covers:
- quebec_solidarity_credit: zero income, exact threshold, above threshold
- quebec_health_services_fund: basic calculation
- quebec_qpip_premium: zero income, max insurable, above max

All test data uses round numbers. No personal information.
DP#3: pure functions — same inputs → same outputs.

Run with: python3 -m pytest tests/test_quebec_credits.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.provinces.quebec.quebec_credits import (
    quebec_solidarity_credit,
    quebec_health_services_fund,
    quebec_qpip_premium,
)


class TestQuebecSolidarityCredit(unittest.TestCase):
    """Test quebec_solidarity_credit pure function."""

    def test_zero_income_returns_max_credit(self):
        """Zero income returns full solidarity credit."""
        credit = quebec_solidarity_credit(income=0, is_couple=False, year=2026)
        self.assertGreater(credit, 0)

    def test_negative_income_treated_as_zero(self):
        """Negative income treated as zero — returns full credit."""
        credit = quebec_solidarity_credit(income=-5000, is_couple=False, year=2026)
        self.assertEqual(credit, quebec_solidarity_credit(income=0, is_couple=False, year=2026))

    def test_exact_threshold_returns_max_credit(self):
        """Income exactly at threshold returns full credit (no reduction)."""
        provider_result = quebec_solidarity_credit(
            income=40225, is_couple=False, year=2026)
        credit_below = quebec_solidarity_credit(
            income=40224, is_couple=False, year=2026)
        # At threshold, credit = max_credit (reduction starts above threshold)
        self.assertEqual(provider_result, credit_below)
        # Just above threshold, credit decreases
        credit_above = quebec_solidarity_credit(
            income=40226, is_couple=False, year=2026)
        self.assertLess(credit_above, provider_result)

    def test_above_threshold_reduces_credit(self):
        """Income above threshold reduces credit by low rate × excess."""
        # 2026 single: max=1028, threshold=40225, rate_low=3%, high_threshold=56738
        # At income=50225 (below high threshold), reduction = 3% * (50225 - 40225) = 300
        credit = quebec_solidarity_credit(income=50225, is_couple=False, year=2026)
        self.assertAlmostEqual(credit, 1028 - 0.03 * (50225 - 40225), places=2)

    def test_high_income_zero_credit(self):
        """Very high income produces zero credit (reduction exceeds max)."""
        credit = quebec_solidarity_credit(income=200000, is_couple=False, year=2026)
        self.assertEqual(credit, 0)

    def test_couple_higher_max_credit(self):
        """Couple gets higher max credit than single."""
        single_credit = quebec_solidarity_credit(income=0, is_couple=False, year=2026)
        couple_credit = quebec_solidarity_credit(income=0, is_couple=True, year=2026)
        self.assertGreater(couple_credit, single_credit)

    def test_couple_higher_threshold(self):
        """Couple has higher threshold than single."""
        # At an income between single and couple thresholds, single gets reduced but couple doesn't
        single = quebec_solidarity_credit(income=42000, is_couple=False, year=2026)
        couple = quebec_solidarity_credit(income=42000, is_couple=True, year=2026)
        self.assertGreater(couple, single)

    def test_pure_function_same_inputs(self):
        """DP#3: Same inputs produce same outputs (pure function)."""
        credit1 = quebec_solidarity_credit(income=50000, is_couple=False, year=2026)
        credit2 = quebec_solidarity_credit(income=50000, is_couple=False, year=2026)
        self.assertEqual(credit1, credit2)


class TestQuebecHealthServicesFund(unittest.TestCase):
    """Test quebec_health_services_fund pure function."""

    def test_basic_calculation(self):
        """Basic FSS calculation: self-employed income × rate."""
        fss = quebec_health_services_fund(self_employment_income=50000, year=2026)
        self.assertGreater(fss, 0)
        # 2026 rate is 1.65% but capped at YMPE ($74600)
        self.assertAlmostEqual(fss, 50000 * 0.0165, places=2)

    def test_zero_income_zero_fss(self):
        """Zero self-employment income → zero FSS."""
        fss = quebec_health_services_fund(self_employment_income=0, year=2026)
        self.assertEqual(fss, 0)

    def test_negative_income_zero_fss(self):
        """Negative self-employment income → zero FSS."""
        fss = quebec_health_services_fund(self_employment_income=-5000, year=2026)
        self.assertEqual(fss, 0)

    def test_capped_at_ympe(self):
        """FSS capped at YMPE: income above YMPE pays same as at YMPE."""
        fss_at_ympe = quebec_health_services_fund(
            self_employment_income=74600, year=2026)
        fss_above_ympe = quebec_health_services_fund(
            self_employment_income=100000, year=2026)
        # Both should be the same (capped at YMPE)
        self.assertAlmostEqual(fss_at_ympe, fss_above_ympe, places=2)

    def test_pure_function_same_inputs(self):
        """DP#3: Same inputs produce same outputs."""
        fss1 = quebec_health_services_fund(self_employment_income=60000, year=2026)
        fss2 = quebec_health_services_fund(self_employment_income=60000, year=2026)
        self.assertEqual(fss1, fss2)


class TestQuebecQPIPPremium(unittest.TestCase):
    """Test quebec_qpip_premium pure function."""

    def test_zero_earnings_zero_premium(self):
        """Zero insurable earnings → zero premium."""
        premium = quebec_qpip_premium(
            insurable_earnings=0, is_self_employed=False, year=2026)
        self.assertEqual(premium, 0)

    def test_negative_earnings_zero_premium(self):
        """Negative insurable earnings → zero premium."""
        premium = quebec_qpip_premium(
            insurable_earnings=-5000, is_self_employed=False, year=2026)
        self.assertEqual(premium, 0)

    def test_employee_basic_calculation(self):
        """Employee QPIP: earnings × employee_rate."""
        premium = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=False, year=2026)
        # 2026 employee rate = 0.430%, max insurable = $103,000
        self.assertAlmostEqual(premium, 50000 * 0.00430, places=2)

    def test_employee_capped_at_max_insurable(self):
        """Employee premium capped at max insurable earnings."""
        premium = quebec_qpip_premium(
            insurable_earnings=200000, is_self_employed=False, year=2026)
        # Capped at $103,000: 103000 × 0.00430 = $442.90
        self.assertAlmostEqual(premium, 103000 * 0.00430, places=2)

    def test_self_employed_rate_higher(self):
        """Self-employed rate is higher than employee rate."""
        employee_premium = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=False, year=2026)
        self_employed_premium = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=True, year=2026)
        self.assertGreater(self_employed_premium, employee_premium)

    def test_self_employed_capped(self):
        """Self-employed premium capped at max insurable earnings."""
        premium = quebec_qpip_premium(
            insurable_earnings=200000, is_self_employed=True, year=2026)
        # Capped at $103,000: 103000 × 0.00764 = $786.92
        self.assertAlmostEqual(premium, 103000 * 0.00764, places=2)

    def test_pure_function_same_inputs(self):
        """DP#3: Same inputs produce same outputs."""
        p1 = quebec_qpip_premium(
            insurable_earnings=60000, is_self_employed=False, year=2026)
        p2 = quebec_qpip_premium(
            insurable_earnings=60000, is_self_employed=False, year=2026)
        self.assertEqual(p1, p2)


class TestQuebecSolidarityProgressiveRate(unittest.TestCase):
    """Test progressive 3%/6% reduction rates for solidarity credit (issue #215)."""

    def test_low_rate_below_high_threshold(self):
        """Income between threshold and high threshold uses 3% rate."""
        # 2026 single: max=1028, threshold=40225, rate_low=3%, high_threshold=56738
        # At income=50000 (between thresholds), reduction = 3% * (50000 - 40225)
        credit = quebec_solidarity_credit(income=50000, is_couple=False, year=2026)
        expected = 1028 - 0.03 * (50000 - 40225)
        self.assertAlmostEqual(credit, expected, places=2)

    def test_high_rate_above_high_threshold(self):
        """Income above high threshold uses 3% + 6% progressive reduction."""
        # 2026 single: threshold=40225, high_threshold=56738, rate_low=3%, rate_high=6%
        # At income=70000 (above high threshold):
        #   reduction_low = 3% * (56738 - 40225) = 3% * 16513 = 495.39
        #   reduction_high = 6% * (70000 - 56738) = 6% * 13262 = 795.72
        #   credit = 1028 - 495.39 - 795.72 = -263.11 → 0
        credit = quebec_solidarity_credit(income=70000, is_couple=False, year=2026)
        self.assertAlmostEqual(credit, 0, places=2)

    def test_at_high_threshold(self):
        """At exactly the high threshold, only low rate applies."""
        # 2026 single: max=1028, threshold=40225, high_threshold=56738
        # At income=56738, reduction = 3% * (56738 - 40225) = 495.39
        credit = quebec_solidarity_credit(income=56738, is_couple=False, year=2026)
        expected = 1028 - 0.03 * (56738 - 40225)
        self.assertAlmostEqual(credit, expected, places=2)

    def test_progressive_rate_lower_than_old_flat_rate_at_moderate_income(self):
        """Progressive 3%/6% produces different result than flat 2% at moderate income."""
        # At income=45000 (between thresholds), 3% rate applies
        # Old: 1028 - 2% * (45000 - 40225) = 1028 - 95.5 = 932.5
        # New: 1028 - 3% * (45000 - 40225) = 1028 - 143.25 = 884.75
        credit = quebec_solidarity_credit(income=45000, is_couple=False, year=2026)
        self.assertAlmostEqual(credit, 1028 - 0.03 * (45000 - 40225), places=2)
        # 3% rate at low income gives lower credit than old 2% would have
        self.assertLess(credit, 1028 - 0.02 * (45000 - 40225))

    def test_couple_progressive_rate(self):
        """Progressive rates work for couple filing status too."""
        # 2026 couple: max=1515, threshold=50310, high_threshold=56738
        # At income=60000 (above high threshold):
        #   reduction_low = 3% * (56738 - 50310) = 3% * 6428 = 192.84
        #   reduction_high = 6% * (60000 - 56738) = 6% * 3262 = 195.72
        #   credit = 1515 - 192.84 - 195.72 = 1126.44
        credit = quebec_solidarity_credit(income=60000, is_couple=True, year=2026)
        reduction_low = 0.03 * (56738 - 50310)
        reduction_high = 0.06 * (60000 - 56738)
        expected = max(0, 1515 - reduction_low - reduction_high)
        self.assertAlmostEqual(credit, expected, places=2)


if __name__ == '__main__':
    unittest.main()