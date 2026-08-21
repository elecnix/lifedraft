#!/usr/bin/env python3
"""Unit tests for Quebec tax data and credits (issue #390).

Per DP#17: tests exercise every rule path, not just every module. This file
gives the previously-untested Quebec tax_data module relational coverage of its
brackets, abatement, QPP rates, solidarity credit, FSS parameters, QPIP rates,
non-refundable credits, senior assistance, work premium, and year-versioned data.

Run with: python3 -m pytest countries/canada/provinces/quebec/tests/test_quebec_tax_data.py -v
"""

import os
import sys

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    ),
)

import unittest

from countries.canada.provinces.quebec.quebec_credits import (
    quebec_age_amount_credit,
    quebec_charitable_donation_credit,
    quebec_drug_insurance_premium,
    quebec_fss_employer_rate,
    quebec_health_services_fund,
    quebec_health_services_fund_employer,
    quebec_health_services_fund_individual,
    quebec_medical_expense_credit,
    quebec_qpip_premium,
    quebec_senior_assistance_credit,
    quebec_solidarity_credit,
    quebec_work_premium,
)
from countries.canada.provinces.quebec.tax_data import QuebecTaxData
from tax_data import TaxDataProvider


def _provider() -> TaxDataProvider:
    return TaxDataProvider()


# =============================================================================
# Quebec tax brackets, abatement, and QPP parameters
# =============================================================================

class TestQuebecBrackets(unittest.TestCase):
    """Quebec provincial tax brackets (4 rates) — DP#17."""

    def setUp(self):
        self.d2026 = QuebecTaxData.year_2026()
        self.d2025 = QuebecTaxData.year_2025()

    def test_four_brackets(self):
        self.assertEqual(len(self.d2026.provincial_brackets), 4)
        self.assertEqual(len(self.d2025.provincial_brackets), 4)

    def test_bracket_rates_in_order(self):
        rates = [b.rate for b in self.d2026.provincial_brackets]
        self.assertEqual(rates, [0.14, 0.19, 0.24, 0.2575])

    def test_brackets_are_contiguous(self):
        for data in (self.d2026, self.d2025):
            br = data.provincial_brackets
            for i in range(len(br) - 1):
                self.assertEqual(br[i].max_income, br[i + 1].min_income)
            self.assertEqual(br[-1].max_income, 0)  # top bracket unlimited

    def test_year_specific_bracket_thresholds(self):
        # 2026 brackets should be higher than 2025 (indexed)
        self.d2024 = QuebecTaxData.year_2024()
        self.assertGreater(self.d2026.provincial_brackets[0].max_income,
                          self.d2025.provincial_brackets[0].max_income)
        self.assertGreater(self.d2025.provincial_brackets[0].max_income,
                          self.d2024.provincial_brackets[0].max_income)


class TestQuebecAbatement(unittest.TestCase):
    """Quebec 16.5% federal tax abatement — DP#17."""

    def test_quebec_abatement_rate(self):
        self.assertEqual(QuebecTaxData.ABATEMENT, 0.165)
        self.assertEqual(QuebecTaxData.year_2026().provincial_abatement, 0.165)
        self.assertEqual(QuebecTaxData.year_2025().provincial_abatement, 0.165)


class TestQuebecQPP(unittest.TestCase):
    """QPP rates and max benefit (higher than CPP) — DP#17."""

    def test_qpp_rate_higher_than_cpp(self):
        # QPP rate 6.40% vs CPP rate 5.95%
        self.assertEqual(QuebecTaxData.year_2026().qpp_rate, 0.0640)
        self.assertEqual(QuebecTaxData.year_2026().cpp_rate, 0.0595)
        self.assertGreater(QuebecTaxData.year_2026().qpp_rate,
                           QuebecTaxData.year_2026().cpp_rate)

    def test_qpp_max_benefit_year_versioned(self):
        # QPP max benefit at 65 should vary by year
        self.assertEqual(QuebecTaxData.year_2026().qpp_max_benefit_65, 17334)
        self.assertEqual(QuebecTaxData.year_2023().qpp_max_benefit_65, 15170)
        self.assertGreater(QuebecTaxData.year_2026().qpp_max_benefit_65,
                           QuebecTaxData.year_2023().qpp_max_benefit_65)

    def test_qpp_survivor_flat_rate_year_versioned(self):
        # Survivor benefit changed in 2024
        self.assertEqual(QuebecTaxData.year_2026().qpp_survivor_flat_rate, 6498)
        self.assertEqual(QuebecTaxData.year_2023().qpp_survivor_flat_rate, 5640)


class TestQuebecRegistration(unittest.TestCase):
    """Quebec data is discoverable through TaxDataProvider — DP#16."""

    def test_provider_resolves_quebec(self):
        p = _provider()
        d = p.get_year_data(2026, 'canada', 'quebec')
        self.assertEqual(d.province, 'quebec')
        self.assertEqual(len(d.provincial_brackets), 4)

    def test_postal_alias_resolves(self):
        p = _provider()
        d = p.get_year_data(2026, 'canada', 'qc')
        self.assertEqual(len(d.provincial_brackets), 4)


# =============================================================================
# Quebec solidarity tax credit (crédit de solidarité)
# =============================================================================

class TestQuebecSolidarityCredit(unittest.TestCase):
    """Quebec solidarity tax credit with progressive reduction — DP#17."""

    def test_full_credit_below_single_threshold(self):
        # Income below threshold → full credit
        # 2026 single threshold is $40,225
        result = quebec_solidarity_credit(30000, is_couple=False, year=2026)
        self.assertAlmostEqual(result, 1028.0, places=2)  # single max

    def test_full_credit_below_couple_threshold(self):
        # 2026 couple threshold is $50,310
        result = quebec_solidarity_credit(40000, is_couple=True, year=2026)
        self.assertAlmostEqual(result, 1515.0, places=2)  # couple max

    def test_3_percent_reduction_single(self):
        # Income $50,000 (between single threshold $40,225 and high threshold $56,738)
        # Single: max $1,028, reduced at 3% of income above $40,225
        result = quebec_solidarity_credit(50000, is_couple=False, year=2026)
        self.assertAlmostEqual(result, 734.75, places=2)

    def test_6_percent_reduction_above_high_threshold(self):
        # At $65,000 (above $56,738 high threshold for singles):
        # Reduction = 3% × ($56,738 - $40,225) + 6% × ($65,000 - $56,738)
        # Credit = $1,028 - reduction
        result = quebec_solidarity_credit(65000, is_couple=False, year=2026)
        self.assertAlmostEqual(result, 36.89, places=2)

    def test_negative_income_treated_as_zero(self):
        # Negative income set to 0, which is below threshold → full credit
        self.assertEqual(quebec_solidarity_credit(-50000, is_couple=False, year=2026), 1028.0)

    def test_year_versioned_thresholds(self):
        # 2023 single threshold was $34,190; 2026 is $40,225
        r2023 = quebec_solidarity_credit(45000, is_couple=False, year=2023)
        r2026 = quebec_solidarity_credit(45000, is_couple=False, year=2026)
        self.assertGreater(r2026, r2023)  # Higher threshold in 2026


# =============================================================================
# Quebec health services fund (FSS) - self-employed
# =============================================================================

class TestQuebecHealthServicesFund(unittest.TestCase):
    """Quebec health services fund for self-employed — DP#17."""

    def test_zero_without_income(self):
        self.assertEqual(quebec_health_services_fund(0, year=2026), 0.0)
        self.assertEqual(quebec_health_services_fund(-10000, year=2026), 0.0)

    def test_rate_applied_to_capped_income(self):
        # 1.65% rate on income capped at YMPE (2026 = $74,600)
        result = quebec_health_services_fund(50000, year=2026)
        self.assertAlmostEqual(result, 825.0, places=2)

    def test_income_capped_at_ympe(self):
        # Income $100,000 should still use $74,600 cap
        result = quebec_health_services_fund(100000, year=2026)
        expected = 74600 * 0.0165
        self.assertAlmostEqual(result, expected, places=2)
        self.assertLess(result, 100000 * 0.0165)


# =============================================================================
# Quebec individual Health Services Fund (line 446)
# =============================================================================

class TestQuebecIndividualFSS(unittest.TestCase):
    """Quebec individual contribution to HSF (line 446) — DP#17."""

    def test_zero_below_exemption(self):
        # 2026 exemption is $18,500
        self.assertEqual(quebec_health_services_fund_individual(18000, year=2026), 0.0)
        self.assertEqual(quebec_health_services_fund_individual(0, year=2026), 0.0)

    def test_first_bracket_below_first_cap(self):
        # Income $25,000: 1% × ($25,000 - $18,500) = $65
        result = quebec_health_services_fund_individual(25000, year=2026)
        self.assertAlmostEqual(result, 65.0, places=2)

    def test_first_bracket_at_cap(self):
        # Income $38,500: 1% × ($38,500 - $18,500) = $200 → min(150, 200) = $150
        result = quebec_health_services_fund_individual(38500, year=2026)
        self.assertAlmostEqual(result, 150.0, places=2)

    def test_second_bracket(self):
        # Income $70,000 (above $64,355 second threshold)
        result = quebec_health_services_fund_individual(70000, year=2026)
        self.assertAlmostEqual(result, 206.45, places=2)

    def test_max_cap(self):
        # Income $200,000: capped at $1,000
        result = quebec_health_services_fund_individual(200000, year=2026)
        self.assertAlmostEqual(result, 1000.0, places=2)


# =============================================================================
# Quebec employer FSS
# =============================================================================

class TestQuebecEmployerFSS(unittest.TestCase):
    """Quebec employer FSS by sector — DP#17."""

    def test_primary_sector_minimum(self):
        # Payroll below $1M → min rate (1.25% for primary/manufacturing)
        result = quebec_health_services_fund_employer(500000, sector='primary')
        self.assertAlmostEqual(result, 500000 * 0.0125, places=2)

    def test_services_sector_minimum(self):
        # Payroll below $1M → min rate (1.65% for services)
        result = quebec_health_services_fund_employer(500000, sector='services')
        self.assertAlmostEqual(result, 500000 * 0.0165, places=2)

    def test_medium_payroll_interpolated(self):
        # Payroll $4M (between $1M and $7.8M): interpolated rate
        result = quebec_health_services_fund_employer(4000000, sector='services')
        expected_rate = 0.0165 + (0.0426 - 0.0165) * 3000000 / 6800000
        self.assertAlmostEqual(result, 4000000 * expected_rate, places=2)

    def test_large_payroll_max(self):
        # Payroll above $7.8M → max rate (4.26%)
        result = quebec_health_services_fund_employer(10000000, sector='services')
        self.assertAlmostEqual(result, 10000000 * 0.0426, places=2)

    def test_fss_employer_rate_function(self):
        # Direct test of the rate function
        self.assertAlmostEqual(quebec_fss_employer_rate(500000, 'primary'), 0.0125, places=4)
        self.assertAlmostEqual(quebec_fss_employer_rate(500000, 'services'), 0.0165, places=4)
        self.assertAlmostEqual(quebec_fss_employer_rate(10000000, 'services'), 0.0426, places=4)


# =============================================================================
# Quebec Parental Insurance Plan (QPIP)
# =============================================================================

class TestQuebecQPIP(unittest.TestCase):
    """Quebec QPIP premium rates and max insurable earnings — DP#17."""

    def test_employee_rate_2026(self):
        self.assertEqual(QuebecTaxData.year_2026().qpip_employee_rate, 0.00430)

    def test_self_employed_rate_2026(self):
        self.assertEqual(QuebecTaxData.year_2026().qpip_self_employed_rate, 0.00764)

    def test_max_insurable_earnings_year_versioned(self):
        self.assertEqual(QuebecTaxData.year_2026().qpip_max_insurable_earnings, 103000)
        self.assertEqual(QuebecTaxData.year_2025().qpip_max_insurable_earnings, 98000)

    def test_employee_premium_capped(self):
        result = quebec_qpip_premium(150000, is_self_employed=False, year=2026)
        expected = 103000 * 0.00430
        self.assertAlmostEqual(result, expected, places=2)

    def test_self_employed_premium_capped(self):
        result = quebec_qpip_premium(150000, is_self_employed=True, year=2026)
        expected = 103000 * 0.00764
        self.assertAlmostEqual(result, expected, places=2)

    def test_negative_income_treated_as_zero(self):
        self.assertEqual(quebec_qpip_premium(-10000, year=2026), 0.0)


# =============================================================================
# Quebec charitable donation credit
# =============================================================================

class TestQuebecCharitableDonation(unittest.TestCase):
    """Quebec charitable donation credit rates — DP#17."""

    def test_zero_without_donations(self):
        self.assertEqual(quebec_charitable_donation_credit(0, year=2026), 0.0)
        self.assertEqual(quebec_charitable_donation_credit(-100, year=2026), 0.0)

    def test_first_200_at_20_percent(self):
        result = quebec_charitable_donation_credit(200, year=2026)
        self.assertAlmostEqual(result, 40.0, places=2)

    def test_200_to_1000_at_24_percent(self):
        result = quebec_charitable_donation_credit(1000, taxable_income=50000, year=2026)
        self.assertAlmostEqual(result, 232.0, places=2)

    def test_top_bracket_25_75_percent(self):
        result = quebec_charitable_donation_credit(500, taxable_income=150000, year=2026)
        expected = 200 * 0.20 + 300 * 0.2575
        self.assertAlmostEqual(result, expected, places=2)

    def test_year_versioned_threshold(self):
        self.assertEqual(QuebecTaxData.year_2025().qc_charitable_donation_top_threshold, 129590)
        self.assertEqual(QuebecTaxData.year_2026().qc_charitable_donation_top_threshold, 132245)


# =============================================================================
# Quebec senior assistance credit
# =============================================================================

class TestQuebecSeniorAssistance(unittest.TestCase):
    """Quebec senior assistance tax credit — DP#17."""

    def test_zero_without_eligible_persons(self):
        self.assertEqual(quebec_senior_assistance_credit(30000, eligible_persons=0, year=2026), 0.0)

    def test_max_per_person(self):
        result = quebec_senior_assistance_credit(20000, eligible_persons=1, year=2026)
        self.assertAlmostEqual(result, 2000.0, places=2)

    def test_couple_max(self):
        result = quebec_senior_assistance_credit(20000, eligible_persons=2, is_couple=True, year=2026)
        self.assertAlmostEqual(result, 4000.0, places=2)

    def test_reduction_on_high_income_single(self):
        result = quebec_senior_assistance_credit(35000, eligible_persons=1, year=2026)
        self.assertAlmostEqual(result, 1639.25, places=1)

    def test_reduction_on_high_income_couple(self):
        result = quebec_senior_assistance_credit(50000, eligible_persons=2, is_couple=True, year=2026)
        self.assertAlmostEqual(result, 3792.14, places=1)

    def test_year_versioned_thresholds(self):
        self.assertEqual(QuebecTaxData.year_2025().qc_senior_assistance_threshold_single, 27835)
        self.assertEqual(QuebecTaxData.year_2026().qc_senior_assistance_threshold_single, 28405)


# =============================================================================
# Quebec work premium (prime au travail)
# =============================================================================

class TestQuebecWorkPremium(unittest.TestCase):
    """Quebec general work premium for persons without children — DP#17."""

    def test_zero_without_work_income(self):
        self.assertEqual(quebec_work_premium(0, 30000, year=2026), 0.0)

    def test_zero_below_excluded_single(self):
        self.assertEqual(quebec_work_premium(2000, 30000, year=2026), 0.0)

    def test_premium_on_work_income(self):
        # Work income $10,000, family income $20,000 (above $12,808 reduction threshold)
        # Premium = 0.116 × ($10,000 - $2,400) = $881.6
        # Reduction = 10% × ($20,000 - $12,808) = $719.2
        # Final = $881.6 - $719.2 = $162.4
        result = quebec_work_premium(10000, 20000, year=2026)
        self.assertAlmostEqual(result, 162.4, places=2)

    def test_max_cap_single(self):
        # Work income $40,000, family income $20,000 (above reduction threshold)
        # Premium = min(1207.33, 0.116 × ($40,000 - $2,400)) = min(1207.33, 4286.4) = $1,207.33
        # Reduction = 10% × ($20,000 - $12,808) = $719.2
        # Final = $1,207.33 - $719.2 = $488.13
        result = quebec_work_premium(40000, 20000, year=2026)
        self.assertAlmostEqual(result, 488.13, places=2)

    def test_premium_below_reduction_threshold(self):
        # Work income $10,000, family income $10,000 (below $12,808 threshold)
        # No reduction applied
        result = quebec_work_premium(10000, 10000, year=2026)
        expected = 0.116 * (10000 - 2400)
        self.assertAlmostEqual(result, expected, places=2)

    def test_max_cap_below_threshold(self):
        # Work income $40,000, family income $10,000 (below threshold)
        # Premium = min(1207.33, 0.116 × ($40,000 - $2,400)) = $1,207.33 (capped)
        result = quebec_work_premium(40000, 10000, year=2026)
        self.assertAlmostEqual(result, 1207.33, places=2)

    def test_couple_excluded_and_reduction(self):
        result = quebec_work_premium(10000, 25000, is_couple=True, year=2026)
        self.assertAlmostEqual(result, 225.20, places=2)


# =============================================================================
# Quebec drug insurance premium (RAMQ)
# =============================================================================

class TestQuebecDrugInsurance(unittest.TestCase):
    """Quebec prescription drug insurance premium — DP#17."""

    def test_zero_with_private_plan(self):
        self.assertEqual(quebec_drug_insurance_premium(is_covered_by_private_plan=True, year=2026), 0.0)

    def test_full_premium_with_fraction_one(self):
        result = quebec_drug_insurance_premium(income_tested_fraction=1.0, year=2026)
        self.assertAlmostEqual(result, 766.0, places=2)

    def test_scaled_by_fraction(self):
        result = quebec_drug_insurance_premium(income_tested_fraction=0.5, year=2026)
        self.assertAlmostEqual(result, 383.0, places=2)

    def test_fraction_clamped_at_one(self):
        result = quebec_drug_insurance_premium(income_tested_fraction=2.0, year=2026)
        self.assertAlmostEqual(result, 766.0, places=2)


# =============================================================================
# Quebec age/living alone/retirement income credit (line 361)
# =============================================================================

class TestQuebecAgeCredits(unittest.TestCase):
    """Quebec age amount credit with income reduction — DP#17."""

    def test_age_amount_at_65_below_threshold(self):
        result = quebec_age_amount_credit(40000, age=65, lives_alone=False, year=2026)
        self.assertAlmostEqual(result, 3986 * 0.14, places=2)

    def test_living_alone_addition_below_threshold(self):
        result = quebec_age_amount_credit(40000, age=65, lives_alone=True, year=2026)
        expected = (3986 + 2172) * 0.14
        self.assertAlmostEqual(result, expected, places=2)

    def test_retirement_income_addition_below_threshold(self):
        result = quebec_age_amount_credit(40000, age=65, retirement_income=5000, year=2026)
        expected = (3986 + 3541) * 0.14
        self.assertAlmostEqual(result, expected, places=2)

    def test_reduction_on_high_income(self):
        result = quebec_age_amount_credit(60000, age=65, lives_alone=True, year=2026)
        self.assertAlmostEqual(result, 414.69, places=2)

    def test_no_credit_below_65(self):
        result = quebec_age_amount_credit(50000, age=60, lives_alone=False, year=2026)
        self.assertEqual(result, 0.0)


# =============================================================================
# Quebec medical expense credit
# =============================================================================

class TestQuebecMedicalExpenseCredit(unittest.TestCase):
    """Quebec medical expense credit — DP#17."""

    def test_zero_without_expenses(self):
        self.assertEqual(quebec_medical_expense_credit(0, 50000, 0.14, year=2026), 0.0)

    def test_eligible_above_threshold(self):
        # Expenses $3,000, net income $50,000
        # Eligible = $3,000 - 3% × $50,000 = $3,000 - $1,500 = $1,500
        # Credit = $1,500 × 14% = $210
        result = quebec_medical_expense_credit(3000, 50000, 0.14, year=2026)
        self.assertAlmostEqual(result, 210.0, places=2)

    def test_zero_when_below_threshold(self):
        # Expenses $500, net income $50,000
        # Eligible = $500 - $1,500 = negative → 0
        result = quebec_medical_expense_credit(500, 50000, 0.14, year=2026)
        self.assertEqual(result, 0.0)


# =============================================================================
# RESP/CESG/QESI/CLB thresholds (Quebec-specific indexing)
# =============================================================================

class TestQuebecRespThresholds(unittest.TestCase):
    """RESB/QESI/CLB income thresholds — DP#17."""

    def test_cesg_thresholds_year_versioned(self):
        self.assertEqual(QuebecTaxData.year_2026().cesg_first_threshold, 58523)
        self.assertEqual(QuebecTaxData.year_2025().cesg_first_threshold, 57375)

    def test_qesi_thresholds_year_versioned(self):
        self.assertEqual(QuebecTaxData.year_2026().qesi_first_threshold, 54345)
        self.assertEqual(QuebecTaxData.year_2025().qesi_first_threshold, 53255)

    def test_clb_thresholds_year_versioned(self):
        self.assertEqual(QuebecTaxData.year_2026().clb_threshold_1_3_children, 58523)
        self.assertEqual(QuebecTaxData.year_2026().clb_threshold_4_children, 66078)
        self.assertEqual(QuebecTaxData.year_2026().clb_threshold_5plus_children, 73633)


# =============================================================================
# Year-versioned data verification through TaxDataProvider
# =============================================================================

class TestQuebecYearVersionedDataProvider(unittest.TestCase):
    """TaxDataProvider returns year-specific Quebec data — DP#20."""

    def test_ympe_increases_by_year(self):
        p = _provider()
        ympe_2023 = p.get_cpp_max_pensionable(2023)
        ympe_2026 = p.get_cpp_max_pensionable(2026)
        self.assertGreater(ympe_2026, ympe_2023)

    def test_qpp_data_year_specific(self):
        p = _provider()
        d2023 = p.get_year_data(2023, 'canada', 'quebec')
        d2026 = p.get_year_data(2026, 'canada', 'quebec')
        self.assertEqual(d2023.qpp_max_benefit_65, 15170)
        self.assertEqual(d2026.qpp_max_benefit_65, 17334)


if __name__ == '__main__':
    unittest.main()
