#!/usr/bin/env python3
"""Tests for Quebec tax data extensions: solidarity credit, health services fund,
QPIP premiums, non-refundable credits, and 2027+ projections.

Covers issue #67 coverage gaps.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/test_quebec_tax_data.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from tax_data import TaxDataProvider, TaxYearData, TaxBracket
from countries.canada.provinces.quebec.tax_data import QuebecTaxData
from countries.canada.provinces.quebec.quebec_credits import (
    quebec_solidarity_credit,
    quebec_health_services_fund,
    quebec_qpip_premium,
    quebec_non_refundable_credits,
    quebec_charitable_donation_credit,
    quebec_medical_expense_credit,
)


class TestQuebecTaxDataYearFields(unittest.TestCase):
    """Test that QuebecTaxData year entries include the new fields."""

    def test_2026_has_solidarity_fields(self):
        data = QuebecTaxData.year_2026()
        self.assertGreater(data.qc_solidarity_single_max, 0)
        self.assertGreater(data.qc_solidarity_couple_max, 0)
        self.assertGreater(data.qc_solidarity_single_threshold, 0)
        self.assertGreater(data.qc_solidarity_couple_threshold, 0)

    def test_2026_has_health_services_fund(self):
        data = QuebecTaxData.year_2026()
        self.assertGreater(data.qc_fss_self_employed_rate, 0)

    def test_2026_has_qpip(self):
        data = QuebecTaxData.year_2026()
        self.assertGreater(data.qpip_employee_rate, 0)
        self.assertGreater(data.qpip_self_employed_rate, 0)
        self.assertGreater(data.qpip_max_insurable_earnings, 0)

    def test_2026_has_non_refundable_credit_rates(self):
        data = QuebecTaxData.year_2026()
        self.assertGreater(data.qc_charitable_donation_rate_low, 0)
        self.assertGreater(data.qc_charitable_donation_rate_high, 0)
        self.assertGreater(data.qc_medical_expense_threshold_pct, 0)
        self.assertGreater(data.qc_charitable_donation_threshold, 0)

    def test_2025_has_new_fields(self):
        data = QuebecTaxData.year_2025()
        self.assertGreater(data.qc_solidarity_single_max, 0)
        self.assertGreater(data.qpip_employee_rate, 0)

    def test_2024_has_new_fields(self):
        data = QuebecTaxData.year_2024()
        self.assertGreater(data.qc_solidarity_single_max, 0)
        self.assertGreater(data.qpip_employee_rate, 0)

    def test_2023_has_new_fields(self):
        data = QuebecTaxData.year_2023()
        self.assertGreater(data.qc_solidarity_single_max, 0)
        self.assertGreater(data.qpip_employee_rate, 0)


class TestQuebecSolidarityCreditSingle(unittest.TestCase):
    """Test quebec_solidarity_credit for single filers."""

    def test_below_threshold(self):
        """Single person below income threshold gets full credit."""
        credit = quebec_solidarity_credit(
            income=20000, is_couple=False, year=2026)
        self.assertGreater(credit, 0)

    def test_at_zero_income(self):
        """Single person with zero income gets max credit."""
        credit = quebec_solidarity_credit(
            income=0, is_couple=False, year=2026)
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(credit, data.qc_solidarity_single_max, places=0)

    def test_above_threshold_reduced(self):
        """Single person above threshold: credit reduced by progressive rates."""
        data = QuebecTaxData.year_2026()
        threshold = data.qc_solidarity_single_threshold
        max_credit = data.qc_solidarity_single_max
        high_threshold = data.qc_solidarity_high_threshold
        rate_low = data.qc_solidarity_reduction_rate_low
        rate_high = data.qc_solidarity_reduction_rate_high
        # $10k above threshold, below high threshold
        income = threshold + 10000
        if income <= high_threshold:
            expected = max(max_credit - rate_low * (income - threshold), 0)
        else:
            reduction_low = rate_low * (high_threshold - threshold)
            reduction_high = rate_high * (income - high_threshold)
            expected = max(max_credit - reduction_low - reduction_high, 0)
        credit = quebec_solidarity_credit(
            income=threshold + 10000, is_couple=False, year=2026)
        self.assertAlmostEqual(credit, expected, places=0)

    def test_zero_point_boundary(self):
        """At very high income, credit is fully clawed back to zero."""
        data = QuebecTaxData.year_2026()
        credit = quebec_solidarity_credit(
            income=200000, is_couple=False, year=2026)
        self.assertAlmostEqual(credit, 0, places=1)

    def test_high_income_zero_credit(self):
        """Income well above threshold: credit fully clawed back."""
        credit = quebec_solidarity_credit(
            income=200000, is_couple=False, year=2026)
        self.assertAlmostEqual(credit, 0)

    def test_negative_income_treated_as_zero(self):
        """Negative income is treated as zero: returns max credit."""
        data = QuebecTaxData.year_2026()
        credit = quebec_solidarity_credit(
            income=-5000, is_couple=False, year=2026)
        self.assertAlmostEqual(credit, data.qc_solidarity_single_max, places=0)


class TestQuebecSolidarityCreditCouple(unittest.TestCase):
    """Test quebec_solidarity_credit for couples."""

    def test_couple_gets_more(self):
        """Couple gets higher max credit than single."""
        credit_single = quebec_solidarity_credit(
            income=20000, is_couple=False, year=2026)
        credit_couple = quebec_solidarity_credit(
            income=20000, is_couple=True, year=2026)
        self.assertGreater(credit_couple, credit_single)

    def test_couple_at_zero_income(self):
        """Couple with zero income gets couple max credit."""
        data = QuebecTaxData.year_2026()
        credit = quebec_solidarity_credit(
            income=0, is_couple=True, year=2026)
        self.assertAlmostEqual(credit, data.qc_solidarity_couple_max, places=0)

    def test_couple_zero_point_boundary(self):
        """Couple: at very high income, credit is fully clawed back."""
        data = QuebecTaxData.year_2026()
        credit = quebec_solidarity_credit(
            income=200000, is_couple=True, year=2026)
        self.assertAlmostEqual(credit, 0, places=1)

    def test_couple_negative_income_treated_as_zero(self):
        """Couple with negative income: treated as zero, returns couple max."""
        data = QuebecTaxData.year_2026()
        credit = quebec_solidarity_credit(
            income=-5000, is_couple=True, year=2026)
        self.assertAlmostEqual(credit, data.qc_solidarity_couple_max, places=0)

    def test_couple_above_threshold_reduced(self):
        """Couple above threshold: credit reduced by progressive rates."""
        data = QuebecTaxData.year_2026()
        threshold = data.qc_solidarity_couple_threshold
        max_credit = data.qc_solidarity_couple_max
        high_threshold = data.qc_solidarity_high_threshold
        rate_low = data.qc_solidarity_reduction_rate_low
        rate_high = data.qc_solidarity_reduction_rate_high
        income = threshold + 10000
        if income <= high_threshold:
            expected = max(max_credit - rate_low * (income - threshold), 0)
        else:
            reduction_low = rate_low * (high_threshold - threshold)
            reduction_high = rate_high * (income - high_threshold)
            expected = max(max_credit - reduction_low - reduction_high, 0)
        credit = quebec_solidarity_credit(
            income=threshold + 10000, is_couple=True, year=2026)
        self.assertAlmostEqual(credit, expected, places=0)


class TestQuebecSolidarityCreditYearVariation(unittest.TestCase):
    """Test quebec_solidarity_credit across years (DP#17)."""

    def test_solidarity_differs_2023_vs_2026(self):
        """Solidarity credit differs between 2023 and 2026 (different max/threshold)."""
        credit_2023 = quebec_solidarity_credit(
            income=20000, is_couple=False, year=2023)
        credit_2026 = quebec_solidarity_credit(
            income=20000, is_couple=False, year=2026)
        self.assertNotAlmostEqual(credit_2023, credit_2026)

    def test_solidarity_2023_max_lower(self):
        """2023 max credit is lower than 2026 due to indexation."""
        data_2023 = QuebecTaxData.year_2023()
        data_2026 = QuebecTaxData.year_2026()
        self.assertLess(data_2023.qc_solidarity_single_max,
                        data_2026.qc_solidarity_single_max)

    def test_year_2025(self):
        """Solidarity credit for 2025 uses 2025 data."""
        credit = quebec_solidarity_credit(
            income=20000, is_couple=False, year=2025)
        self.assertGreater(credit, 0)


class TestQuebecHealthServicesFund(unittest.TestCase):
    """Test quebec_health_services_fund pure function."""

    def test_self_employed_basic(self):
        """Self-employed pays FSS on QPP-eligible income."""
        contribution = quebec_health_services_fund(
            self_employment_income=50000, year=2026)
        self.assertGreater(contribution, 0)

    def test_self_employed_capped(self):
        """FSS contribution capped at max pensionable earnings."""
        data = QuebecTaxData.year_2026()
        max_pensionable = data.cpp_max_pensionable
        rate = data.qc_fss_self_employed_rate
        contribution = quebec_health_services_fund(
            self_employment_income=max_pensionable * 2, year=2026)
        expected = max_pensionable * rate
        self.assertAlmostEqual(contribution, expected, places=0)

    def test_zero_income(self):
        """Zero self-employment income: zero FSS."""
        contribution = quebec_health_services_fund(
            self_employment_income=0, year=2026)
        self.assertAlmostEqual(contribution, 0)

    def test_negative_income_treated_as_zero(self):
        """Negative self-employment income: zero FSS."""
        contribution = quebec_health_services_fund(
            self_employment_income=-5000, year=2026)
        self.assertAlmostEqual(contribution, 0)

    def test_fss_year_variation(self):
        """FSS differs across years due to different YMPE."""
        contribution_2023 = quebec_health_services_fund(
            self_employment_income=70000, year=2023)
        contribution_2026 = quebec_health_services_fund(
            self_employment_income=70000, year=2026)
        # 2023 YMPE = $66,600; 2026 YMPE = $71,300 → different caps
        self.assertNotAlmostEqual(contribution_2023, contribution_2026)


class TestQuebecQPIPPremium(unittest.TestCase):
    """Test quebec_qpip_premium pure function."""

    def test_employee_basic(self):
        """Employee pays QPIP on insurable earnings."""
        premium = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=False, year=2026)
        self.assertGreater(premium, 0)

    def test_employee_capped(self):
        """Employee QPIP capped at max insurable earnings."""
        data = QuebecTaxData.year_2026()
        max_insurable = data.qpip_max_insurable_earnings
        rate = data.qpip_employee_rate
        premium = quebec_qpip_premium(
            insurable_earnings=max_insurable * 2, is_self_employed=False, year=2026)
        expected = max_insurable * rate
        self.assertAlmostEqual(premium, expected, places=0)

    def test_self_employed_higher_rate(self):
        """Self-employed pays both employee+employer portions."""
        premium_emp = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=False, year=2026)
        premium_se = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=True, year=2026)
        self.assertGreater(premium_se, premium_emp)

    def test_self_employed_capped(self):
        """Self-employed QPIP capped at max insurable earnings."""
        data = QuebecTaxData.year_2026()
        max_insurable = data.qpip_max_insurable_earnings
        rate = data.qpip_self_employed_rate
        premium = quebec_qpip_premium(
            insurable_earnings=max_insurable * 2, is_self_employed=True, year=2026)
        expected = max_insurable * rate
        self.assertAlmostEqual(premium, expected, places=0)

    def test_zero_earnings(self):
        """Zero insurable earnings: zero premium."""
        premium = quebec_qpip_premium(
            insurable_earnings=0, is_self_employed=False, year=2026)
        self.assertAlmostEqual(premium, 0)

    def test_negative_earnings_treated_as_zero(self):
        """Negative insurable earnings: zero premium."""
        premium = quebec_qpip_premium(
            insurable_earnings=-5000, is_self_employed=False, year=2026)
        self.assertAlmostEqual(premium, 0)

    def test_year_2023_employee(self):
        """QPIP premium differs between 2023 and 2026 (different rates and max insurable)."""
        premium_2023 = quebec_qpip_premium(
            insurable_earnings=90000, is_self_employed=False, year=2023)
        premium_2026 = quebec_qpip_premium(
            insurable_earnings=90000, is_self_employed=False, year=2026)
        # 2026 employee rate (0.430%) is lower than 2023 (0.494%),
        # but max insurable is higher ($103k vs $91k).
        # At $90k earnings: 2023 = min(90k, 91k) × 0.494% = $444.60
        #                    2026 = min(90k, 103k) × 0.430% = $387.00
        self.assertNotEqual(premium_2023, premium_2026)
        self.assertAlmostEqual(premium_2023, 444.60, places=1)
        self.assertAlmostEqual(premium_2026, 387.0, places=1)


class TestQuebecCharitableDonationCredit(unittest.TestCase):
    """Test quebec_charitable_donation_credit pure function."""

    def test_low_tier_only(self):
        """Donations under threshold: low rate applies."""
        data = QuebecTaxData.year_2026()
        credit = quebec_charitable_donation_credit(
            donations=100, year=2026)
        expected = 100 * data.qc_charitable_donation_rate_low
        self.assertAlmostEqual(credit, expected, places=2)

    def test_at_threshold(self):
        """Donations at exactly the threshold: low rate on full amount."""
        data = QuebecTaxData.year_2026()
        threshold = data.qc_charitable_donation_threshold
        credit = quebec_charitable_donation_credit(
            donations=threshold, year=2026)
        expected = threshold * data.qc_charitable_donation_rate_low
        self.assertAlmostEqual(credit, expected, places=2)

    def test_both_tiers(self):
        """Donations over threshold: low rate on threshold, high rate on remainder."""
        data = QuebecTaxData.year_2026()
        threshold = data.qc_charitable_donation_threshold
        credit = quebec_charitable_donation_credit(
            donations=500, year=2026)
        expected = threshold * data.qc_charitable_donation_rate_low + \
            (500 - threshold) * data.qc_charitable_donation_rate_high
        self.assertAlmostEqual(credit, expected, places=2)

    def test_zero_donations(self):
        """Zero donations: zero credit."""
        credit = quebec_charitable_donation_credit(
            donations=0, year=2026)
        self.assertAlmostEqual(credit, 0)

    def test_negative_donations_treated_as_zero(self):
        """Negative donations: zero credit."""
        credit = quebec_charitable_donation_credit(
            donations=-100, year=2026)
        self.assertAlmostEqual(credit, 0)

    def test_donation_credit_year_variation(self):
        """Donation credit is stable across years (same rates/threshold in 2023-2026)."""
        credit_2023 = quebec_charitable_donation_credit(
            donations=500, year=2023)
        credit_2026 = quebec_charitable_donation_credit(
            donations=500, year=2026)
        # Rates and threshold are the same across all years currently
        self.assertAlmostEqual(credit_2023, credit_2026)


class TestQuebecMedicalExpenseCredit(unittest.TestCase):
    """Test quebec_medical_expense_credit pure function."""

    def test_above_threshold(self):
        """Medical expenses above 3% of net income: credit on excess."""
        data = QuebecTaxData.year_2026()
        income = 50000
        expenses = 5000
        threshold = income * data.qc_medical_expense_threshold_pct
        credit = quebec_medical_expense_credit(
            medical_expenses=expenses, net_income=income,
            lowest_mtr=0.20, year=2026)
        eligible = expenses - threshold
        expected = max(0, eligible) * 0.20
        self.assertAlmostEqual(credit, expected, places=2)

    def test_at_threshold(self):
        """Medical expenses at exactly 3% of net income: zero credit."""
        data = QuebecTaxData.year_2026()
        income = 50000
        expenses = income * data.qc_medical_expense_threshold_pct
        credit = quebec_medical_expense_credit(
            medical_expenses=expenses, net_income=income,
            lowest_mtr=0.20, year=2026)
        self.assertAlmostEqual(credit, 0)

    def test_below_threshold(self):
        """Medical expenses below 3% of net income: zero credit."""
        credit = quebec_medical_expense_credit(
            medical_expenses=500, net_income=50000,
            lowest_mtr=0.20, year=2026)
        self.assertAlmostEqual(credit, 0)

    def test_zero_income(self):
        """Zero income: all medical expenses are eligible."""
        credit = quebec_medical_expense_credit(
            medical_expenses=5000, net_income=0,
            lowest_mtr=0.20, year=2026)
        self.assertGreater(credit, 0)

    def test_negative_net_income(self):
        """Negative net income treated as zero: all expenses eligible."""
        credit = quebec_medical_expense_credit(
            medical_expenses=5000, net_income=-10000,
            lowest_mtr=0.20, year=2026)
        expected = 5000 * 0.20
        self.assertAlmostEqual(credit, expected, places=2)

    def test_zero_expenses(self):
        """Zero medical expenses: zero credit."""
        credit = quebec_medical_expense_credit(
            medical_expenses=0, net_income=50000,
            lowest_mtr=0.20, year=2026)
        self.assertAlmostEqual(credit, 0)

    def test_negative_expenses_treated_as_zero(self):
        """Negative medical expenses: zero credit."""
        credit = quebec_medical_expense_credit(
            medical_expenses=-1000, net_income=50000,
            lowest_mtr=0.20, year=2026)
        self.assertAlmostEqual(credit, 0)


class TestQuebecNonRefundableCredits(unittest.TestCase):
    """Test quebec_non_refundable_credits aggregate function."""

    def test_with_donations_and_medical(self):
        """Aggregate of charitable donations + medical expenses."""
        result = quebec_non_refundable_credits(
            net_income=80000,
            charitable_donations=500,
            medical_expenses=3000,
            lowest_mtr=0.20,
            year=2026,
        )
        self.assertGreater(result['total_credit'], 0)
        self.assertIn('charitable_donation_credit', result)
        self.assertIn('medical_expense_credit', result)

    def test_no_credits(self):
        """No donations or medical expenses: total is zero."""
        result = quebec_non_refundable_credits(
            net_income=80000,
            charitable_donations=0,
            medical_expenses=0,
            lowest_mtr=0.20,
            year=2026,
        )
        self.assertAlmostEqual(result['total_credit'], 0)

    def test_only_donations(self):
        """Only charitable donations: medical credit is zero."""
        result = quebec_non_refundable_credits(
            net_income=80000,
            charitable_donations=500,
            medical_expenses=0,
            lowest_mtr=0.20,
            year=2026,
        )
        self.assertGreater(result['charitable_donation_credit'], 0)
        self.assertAlmostEqual(result['medical_expense_credit'], 0)
        self.assertAlmostEqual(result['total_credit'],
                               result['charitable_donation_credit'])

    def test_only_medical(self):
        """Only medical expenses: donation credit is zero."""
        result = quebec_non_refundable_credits(
            net_income=80000,
            charitable_donations=0,
            medical_expenses=5000,
            lowest_mtr=0.20,
            year=2026,
        )
        self.assertAlmostEqual(result['charitable_donation_credit'], 0)
        self.assertGreater(result['medical_expense_credit'], 0)
        self.assertAlmostEqual(result['total_credit'],
                               result['medical_expense_credit'])

    def test_negative_inputs_through_aggregate(self):
        """Negative donations/expenses passed through aggregate: treated as zero."""
        result = quebec_non_refundable_credits(
            net_income=80000,
            charitable_donations=-500,
            medical_expenses=-3000,
            lowest_mtr=0.20,
            year=2026,
        )
        self.assertAlmostEqual(result['total_credit'], 0)


class TestQuebec2027Projection(unittest.TestCase):
    """Test 2027+ projected brackets (DP#20)."""

    def test_provider_projects_2027(self):
        """TaxDataProvider projects 2027 Quebec data from 2026 base."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2027, 'canada', 'quebec')
        self.assertEqual(data.year, 2027)
        self.assertEqual(data.source, "projected")
        self.assertGreater(len(data.provincial_brackets), 0)

    def test_projected_brackets_have_higher_thresholds(self):
        """Projected brackets have escalated thresholds."""
        provider = TaxDataProvider()
        data_2026 = provider.get_year_data(2026, 'canada', 'quebec')
        data_2027 = provider.get_year_data(2027, 'canada', 'quebec')
        self.assertGreater(
            data_2027.provincial_brackets[0].max_income,
            data_2026.provincial_brackets[0].max_income,
        )

    def test_projected_rates_stay_constant(self):
        """Projected rates stay the same (only thresholds escalate)."""
        provider = TaxDataProvider()
        data_2026 = provider.get_year_data(2026, 'canada', 'quebec')
        data_2027 = provider.get_year_data(2027, 'canada', 'quebec')
        for b26, b27 in zip(data_2026.provincial_brackets,
                            data_2027.provincial_brackets):
            self.assertAlmostEqual(b26.rate, b27.rate)

    def test_projected_all_new_fields(self):
        """Projected 2027 data preserves all 16 new Quebec fields."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2027, 'canada', 'quebec')
        # Solidarity (6 fields)
        self.assertGreater(data.qc_solidarity_single_max, 0)
        self.assertGreater(data.qc_solidarity_couple_max, 0)
        self.assertGreater(data.qc_solidarity_single_threshold, 0)
        self.assertGreater(data.qc_solidarity_couple_threshold, 0)
        self.assertGreater(data.qc_solidarity_reduction_rate_low, 0)
        self.assertGreater(data.qc_solidarity_reduction_rate_high, 0)
        # FSS (1 field)
        self.assertGreater(data.qc_fss_self_employed_rate, 0)
        # QPIP (4 fields)
        self.assertGreater(data.qpip_employee_rate, 0)
        self.assertGreater(data.qpip_employer_rate, 0)
        self.assertGreater(data.qpip_self_employed_rate, 0)
        self.assertGreater(data.qpip_max_insurable_earnings, 0)
        # Non-refundable credits (5 fields)
        self.assertGreater(data.qc_charitable_donation_threshold, 0)
        self.assertGreater(data.qc_charitable_donation_rate_low, 0)
        self.assertGreater(data.qc_charitable_donation_rate_high, 0)
        self.assertGreater(data.qc_medical_expense_threshold_pct, 0)

    def test_projected_2030(self):
        """Can project all the way to 2030."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2030, 'canada', 'quebec')
        self.assertEqual(data.year, 2030)
        self.assertGreater(len(data.provincial_brackets), 0)

    def test_projected_escalates_solidarity(self):
        """Solidarity credit max amounts escalate with inflation."""
        provider = TaxDataProvider()
        data_2026 = provider.get_year_data(2026, 'canada', 'quebec')
        data_2028 = provider.get_year_data(2028, 'canada', 'quebec')
        self.assertGreater(data_2028.qc_solidarity_single_max,
                           data_2026.qc_solidarity_single_max)

    def test_projected_escalates_qpip_max(self):
        """QPIP max insurable earnings escalate with inflation."""
        provider = TaxDataProvider()
        data_2026 = provider.get_year_data(2026, 'canada', 'quebec')
        data_2028 = provider.get_year_data(2028, 'canada', 'quebec')
        self.assertGreater(data_2028.qpip_max_insurable_earnings,
                           data_2026.qpip_max_insurable_earnings)


class TestQuebecAllYearsCovers2023to2026(unittest.TestCase):
    """Verify all_years returns 2023-2026."""

    def test_all_years_count(self):
        years = QuebecTaxData.all_years()
        self.assertEqual(len(years), 4)

    def test_all_years_range(self):
        year_nums = [y.year for y in QuebecTaxData.all_years()]
        self.assertIn(2023, year_nums)
        self.assertIn(2024, year_nums)
        self.assertIn(2025, year_nums)
        self.assertIn(2026, year_nums)


class TestTaxDataProviderPublicAPI(unittest.TestCase):
    """Test that TaxDataProvider.get_year_data is the public API."""

    def test_get_year_data_returns_tax_year_data(self):
        """get_year_data returns a TaxYearData instance."""
        provider = TaxDataProvider()
        data = provider.get_year_data(2026, 'canada', 'quebec')
        self.assertIsInstance(data, TaxYearData)

    def test_get_year_data_same_as_load_year(self):
        """get_year_data returns same result as _load_year."""
        provider = TaxDataProvider()
        public = provider.get_year_data(2026, 'canada', 'quebec')
        private = provider._load_year(2026, 'canada', 'quebec')
        self.assertEqual(public.year, private.year)
        self.assertEqual(public.province, private.province)


class TestProviderParameter(unittest.TestCase):
    """Test that functions accept optional provider parameter (DP#8)."""

    def test_solidarity_with_shared_provider(self):
        """Passing a shared provider avoids repeated construction."""
        provider = TaxDataProvider()
        credit = quebec_solidarity_credit(
            income=20000, is_couple=False, year=2026, provider=provider)
        self.assertGreater(credit, 0)

    def test_qpip_with_shared_provider(self):
        """QPIP with shared provider produces same result."""
        provider = TaxDataProvider()
        premium_default = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=False, year=2026)
        premium_shared = quebec_qpip_premium(
            insurable_earnings=50000, is_self_employed=False, year=2026,
            provider=provider)
        self.assertAlmostEqual(premium_default, premium_shared)


if __name__ == '__main__':
    unittest.main()
