#!/usr/bin/env python3
"""Tests for federal medical, charitable, and CPP-exemption rules (issue #315).

LEAN, single-responsibility, fabricated round-number data. No personal info.

Sources:
- Medical expense credit (ITA s.118.2, line 33099)
- Charitable donation credit (ITA s.118.1, line 34000)
- CPP/QPP basic exemption (CPP Act s.20, line 30800)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.tax_calc import (
    FederalCreditParameters,
    medical_expense_credit,
    charitable_donation_credit,
    cpp_basic_exemption_pensionable,
    compute_non_refundable_credits,
)


class TestMedicalExpenseCredit(unittest.TestCase):
    """ITA s.118.2 — lesser of 3% of net income or the indexed cap."""

    def test_threshold_is_3pct_when_below_cap(self):
        """Low income: threshold = 3% of net income."""
        # net income 40000 -> threshold 1200. expenses 5000 -> eligible 3800.
        params = FederalCreditParameters.for_year(2026)
        credit = medical_expense_credit(5000, net_income=40000, params=params)
        expected = (5000 - 1200) * params.lowest_rate
        self.assertAlmostEqual(credit, expected)

    def test_threshold_capped_at_dollar_limit(self):
        """High income: threshold capped at the fixed dollar cap, not 3%."""
        params = FederalCreditParameters.for_year(2026)
        # 3% of 200000 = 6000 > cap, so threshold = cap.
        credit = medical_expense_credit(10000, net_income=200000, params=params)
        expected = (10000 - params.medical_threshold_cap) * params.lowest_rate
        self.assertAlmostEqual(credit, expected)

    def test_below_threshold_no_credit(self):
        self.assertAlmostEqual(
            medical_expense_credit(500, net_income=40000, year=2026), 0.0)


class TestCharitableDonationCredit(unittest.TestCase):
    """ITA s.118.1 — first $200 at lowest rate, remainder at 29% (33% in top bracket)."""

    def test_first_200_at_lowest_rate(self):
        params = FederalCreditParameters.for_year(2026)
        self.assertAlmostEqual(
            charitable_donation_credit(200, params=params),
            200 * params.lowest_rate)

    def test_above_200_general_29pct(self):
        """No top-bracket income: above-$200 at 29%."""
        params = FederalCreditParameters.for_year(2026)
        credit = charitable_donation_credit(1200, taxable_income=80000, params=params)
        expected = 200 * params.lowest_rate + 1000 * 0.29
        self.assertAlmostEqual(credit, expected)

    def test_top_bracket_portion_at_33pct(self):
        """High earner: above-$200 donations matched by top-bracket income at 33%."""
        params = FederalCreditParameters.for_year(2026)
        # 2026 top bracket starts at 258482; income 260000 -> 1518 in top bracket.
        credit = charitable_donation_credit(2200, taxable_income=260000, params=params)
        above = 2000.0
        top_eligible = min(above, 260000 - 258482)  # 1518
        expected = (200 * params.lowest_rate
                    + top_eligible * 0.33
                    + (above - top_eligible) * 0.29)
        self.assertAlmostEqual(credit, expected)

    def test_zero_donation(self):
        self.assertAlmostEqual(charitable_donation_credit(0), 0.0)


class TestCppBasicExemption(unittest.TestCase):
    """CPP/QPP basic exemption clarified to live in tax_calc (issue #315)."""

    def test_exemption_subtracted_from_pensionable(self):
        r = cpp_basic_exemption_pensionable(50000, year=2026, province="ontario")
        # YMPE 74600, exemption 3500 -> pensionable 46500.
        self.assertAlmostEqual(r['basic_exemption'], 3500)
        self.assertAlmostEqual(r['pensionable_earnings'], 46500)

    def test_capped_at_ympe(self):
        r = cpp_basic_exemption_pensionable(120000, year=2026, province="ontario")
        # min(120000, 74600) - 3500 = 71100.
        self.assertAlmostEqual(r['pensionable_earnings'], 71100)

    def test_quebec_uses_qpp_rate(self):
        r = cpp_basic_exemption_pensionable(50000, year=2026, province="quebec")
        self.assertGreater(r['contribution_rate'], 0.0595)  # QPP > CPP


class TestCreditsAdditive(unittest.TestCase):
    """compute_non_refundable_credits is additive: defaults unchanged (issue #315)."""

    def test_no_medical_no_charitable_matches_legacy(self):
        base = compute_non_refundable_credits(80000, taxable_income=80000, province="ontario")
        self.assertAlmostEqual(base['medical_expense'], 0.0)
        self.assertAlmostEqual(base['charitable_donation'], 0.0)
        self.assertAlmostEqual(
            base['total'], base['basic_personal_amount'] + base['canada_employment'])

    def test_medical_and_charitable_increase_total(self):
        with_extra = compute_non_refundable_credits(
            80000, taxable_income=80000, province="ontario",
            medical_expenses=5000, charitable_donations=1000, net_income=80000)
        base = compute_non_refundable_credits(80000, taxable_income=80000, province="ontario")
        self.assertGreater(with_extra['total'], base['total'])
        self.assertGreater(with_extra['medical_expense'], 0)
        self.assertGreater(with_extra['charitable_donation'], 0)


if __name__ == '__main__':
    unittest.main()
