#!/usr/bin/env python3
"""Tests for Quebec credits added in issues #321 and #323.

Covers the new pure functions:
- quebec_senior_assistance_credit (#321)
- quebec_work_premium (#321)
- quebec_drug_insurance_premium (#321)
- quebec_health_services_fund_individual (#323)
- quebec_health_services_fund_employer / quebec_fss_employer_rate (#323)
- quebec_age_amount_credit (senior tax credit, #323)
- quebec_charitable_donation_credit top-rate verification (#323)

Tests are LEAN and relational (assertions derive expected values from the
year-versioned data provider, DP#3/DP#20), with fabricated round-number
inputs. One behaviour per test.

Run with: python3 -m pytest tests/test_quebec_credits_321_323.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.provinces.quebec.tax_data import QuebecTaxData
from countries.canada.provinces.quebec.quebec_credits import (
    quebec_senior_assistance_credit,
    quebec_work_premium,
    quebec_drug_insurance_premium,
    quebec_health_services_fund_individual,
    quebec_health_services_fund_employer,
    quebec_fss_employer_rate,
    quebec_age_amount_credit,
    quebec_charitable_donation_credit,
)


class TestSeniorAssistanceCredit(unittest.TestCase):
    """#321 — Quebec senior assistance tax credit (refundable)."""

    def test_zero_eligible_persons_returns_zero(self):
        self.assertEqual(quebec_senior_assistance_credit(0, eligible_persons=0, year=2026), 0.0)

    def test_below_threshold_returns_full_max(self):
        data = QuebecTaxData.year_2026()
        credit = quebec_senior_assistance_credit(0, eligible_persons=1, year=2026)
        self.assertAlmostEqual(credit, data.qc_senior_assistance_max_per_person)

    def test_couple_doubles_the_maximum(self):
        data = QuebecTaxData.year_2026()
        credit = quebec_senior_assistance_credit(0, eligible_persons=2, is_couple=True, year=2026)
        self.assertAlmostEqual(credit, 2 * data.qc_senior_assistance_max_per_person)

    def test_reduction_above_single_threshold(self):
        data = QuebecTaxData.year_2026()
        income = data.qc_senior_assistance_threshold_single + 1000
        expected = data.qc_senior_assistance_max_per_person - data.qc_senior_assistance_reduction_rate * 1000
        self.assertAlmostEqual(
            quebec_senior_assistance_credit(income, eligible_persons=1, year=2026), expected)

    def test_high_income_floors_at_zero(self):
        self.assertEqual(
            quebec_senior_assistance_credit(1_000_000, eligible_persons=1, year=2026), 0.0)


class TestWorkPremium(unittest.TestCase):
    """#321 — Quebec general work premium (persons without children)."""

    def test_work_income_below_excluded_returns_zero(self):
        data = QuebecTaxData.year_2026()
        self.assertEqual(
            quebec_work_premium(data.qc_work_premium_excluded_single, 0, year=2026), 0.0)

    def test_growth_phase_single(self):
        data = QuebecTaxData.year_2026()
        work = data.qc_work_premium_excluded_single + 5000
        expected = data.qc_work_premium_growth_rate * 5000
        self.assertAlmostEqual(quebec_work_premium(work, work, year=2026), expected)

    def test_premium_capped_at_maximum(self):
        data = QuebecTaxData.year_2026()
        # Work income well past the cap; family income at the reduction
        # threshold so no reduction applies → premium equals the maximum.
        threshold = data.qc_work_premium_reduction_threshold_single
        self.assertAlmostEqual(
            quebec_work_premium(threshold + 5000, threshold, year=2026),
            data.qc_work_premium_max_single)

    def test_reduction_above_family_threshold(self):
        data = QuebecTaxData.year_2026()
        threshold = data.qc_work_premium_reduction_threshold_single
        family = threshold + 2000
        # Work income past the cap so the premium starts at the maximum.
        expected = data.qc_work_premium_max_single - data.qc_work_premium_reduction_rate * 2000
        self.assertAlmostEqual(
            quebec_work_premium(threshold + 5000, family, year=2026), expected)

    def test_couple_uses_couple_parameters(self):
        data = QuebecTaxData.year_2026()
        work = data.qc_work_premium_excluded_couple + 3000
        expected = data.qc_work_premium_growth_rate * 3000
        self.assertAlmostEqual(
            quebec_work_premium(work, work, is_couple=True, year=2026), expected)


class TestDrugInsurancePremium(unittest.TestCase):
    """#321 — RAMQ prescription drug insurance premium."""

    def test_private_plan_pays_nothing(self):
        self.assertEqual(
            quebec_drug_insurance_premium(is_covered_by_private_plan=True, year=2026), 0.0)

    def test_full_fraction_is_max_premium(self):
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(
            quebec_drug_insurance_premium(income_tested_fraction=1.0, year=2026),
            data.qc_drug_insurance_max_premium)

    def test_fraction_is_clamped(self):
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(
            quebec_drug_insurance_premium(income_tested_fraction=2.0, year=2026),
            data.qc_drug_insurance_max_premium)


class TestIndividualHSFContribution(unittest.TestCase):
    """#323 — individual contribution to the Health Services Fund (line 446)."""

    def test_below_exemption_returns_zero(self):
        data = QuebecTaxData.year_2026()
        self.assertEqual(
            quebec_health_services_fund_individual(data.qc_fss_individual_exemption, year=2026), 0.0)

    def test_first_bracket_capped_at_first_cap(self):
        data = QuebecTaxData.year_2026()
        # Just below the second threshold the contribution hits the first cap.
        result = quebec_health_services_fund_individual(
            data.qc_fss_individual_second_threshold, year=2026)
        self.assertAlmostEqual(result, data.qc_fss_individual_first_cap)

    def test_second_bracket_formula(self):
        data = QuebecTaxData.year_2026()
        income = data.qc_fss_individual_second_threshold + 10000
        expected = data.qc_fss_individual_first_cap + data.qc_fss_individual_rate * 10000
        self.assertAlmostEqual(
            quebec_health_services_fund_individual(income, year=2026), expected)

    def test_overall_maximum(self):
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(
            quebec_health_services_fund_individual(1_000_000, year=2026),
            data.qc_fss_individual_max)


class TestEmployerFSSRate(unittest.TestCase):
    """#323 — employer FSS contribution rate by sector and payroll."""

    def test_small_primary_payroll_uses_reduced_min(self):
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(
            quebec_fss_employer_rate(500000, sector='primary', year=2026),
            data.qc_fss_employer_rate_primary_min)

    def test_small_services_payroll_uses_services_min(self):
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(
            quebec_fss_employer_rate(500000, sector='services', year=2026),
            data.qc_fss_employer_rate_services_min)

    def test_large_payroll_uses_max_rate(self):
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(
            quebec_fss_employer_rate(data.qc_fss_employer_payroll_upper + 1, year=2026),
            data.qc_fss_employer_rate_max)

    def test_midrange_interpolates(self):
        data = QuebecTaxData.year_2026()
        lower = data.qc_fss_employer_payroll_lower
        upper = data.qc_fss_employer_payroll_upper
        mid = (lower + upper) / 2
        rate = quebec_fss_employer_rate(mid, sector='services', year=2026)
        expected = (data.qc_fss_employer_rate_services_min + data.qc_fss_employer_rate_max) / 2
        self.assertAlmostEqual(rate, expected)

    def test_employer_amount_is_rate_times_payroll(self):
        payroll = 10_000_000  # above upper threshold → max rate
        data = QuebecTaxData.year_2026()
        self.assertAlmostEqual(
            quebec_health_services_fund_employer(payroll, year=2026),
            payroll * data.qc_fss_employer_rate_max)


class TestAgeAmountCredit(unittest.TestCase):
    """#323 — senior tax credit: age + living alone + retirement income."""

    def test_under_65_no_age_amount(self):
        # Age 60, not living alone, no retirement income → no credit.
        self.assertEqual(quebec_age_amount_credit(0, age=60, year=2026), 0.0)

    def test_age_amount_at_65(self):
        data = QuebecTaxData.year_2026()
        expected = data.qc_age_amount * data.qc_non_refundable_credit_rate
        self.assertAlmostEqual(quebec_age_amount_credit(0, age=65, year=2026), expected)

    def test_components_sum_before_rate(self):
        data = QuebecTaxData.year_2026()
        amount = data.qc_age_amount + data.qc_living_alone_amount + data.qc_retirement_income_amount
        expected = amount * data.qc_non_refundable_credit_rate
        self.assertAlmostEqual(
            quebec_age_amount_credit(0, age=70, lives_alone=True,
                                     retirement_income=999_999, year=2026),
            expected)

    def test_reduction_above_threshold(self):
        data = QuebecTaxData.year_2026()
        income = data.qc_age_credit_reduction_threshold + 1000
        reduced_amount = data.qc_age_amount - data.qc_age_credit_reduction_rate * 1000
        expected = reduced_amount * data.qc_non_refundable_credit_rate
        self.assertAlmostEqual(
            quebec_age_amount_credit(income, age=65, year=2026), expected)


class TestCharitableDonationTopRate(unittest.TestCase):
    """#323 — charitable donation credit verified top-rate structure."""

    def test_above_200_without_top_income_uses_high_rate(self):
        data = QuebecTaxData.year_2026()
        threshold = data.qc_charitable_donation_threshold
        donations = threshold + 500
        expected = (threshold * data.qc_charitable_donation_rate_low
                    + 500 * data.qc_charitable_donation_rate_high)
        self.assertAlmostEqual(
            quebec_charitable_donation_credit(donations, year=2026), expected)

    def test_top_bracket_income_applies_top_rate(self):
        data = QuebecTaxData.year_2026()
        threshold = data.qc_charitable_donation_threshold
        donations = threshold + 500
        # Taxable income $1,000 into the top bracket → $500 above-$200 all at top rate.
        taxable = data.qc_charitable_donation_top_threshold + 1000
        expected = (threshold * data.qc_charitable_donation_rate_low
                    + 500 * data.qc_charitable_donation_rate_top)
        self.assertAlmostEqual(
            quebec_charitable_donation_credit(donations, taxable_income=taxable, year=2026),
            expected)

    def test_top_rate_capped_at_top_bracket_income(self):
        data = QuebecTaxData.year_2026()
        threshold = data.qc_charitable_donation_threshold
        donations = threshold + 1000
        # Only $400 of taxable income in the top bracket → $400 at top, $600 at high.
        taxable = data.qc_charitable_donation_top_threshold + 400
        expected = (threshold * data.qc_charitable_donation_rate_low
                    + 400 * data.qc_charitable_donation_rate_top
                    + 600 * data.qc_charitable_donation_rate_high)
        self.assertAlmostEqual(
            quebec_charitable_donation_credit(donations, taxable_income=taxable, year=2026),
            expected)


if __name__ == '__main__':
    unittest.main()
