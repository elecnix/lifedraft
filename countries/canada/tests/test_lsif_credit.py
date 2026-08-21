#!/usr/bin/env python3
"""Unit tests for lsif_credit.py module.

Per DP#17: tests exercise every rule path, not just every module.
Every eligibility gate, carry-forward mechanism, holding period,
income threshold, and cross-jurisdiction dependency gets at least one test.

Run with: python3 -m pytest countries/canada/tests/test_lsif_credit.py -v
"""

import sys
import os

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    ),
)

import unittest
from datetime import date

from countries.canada.lsif_credit import (
    LSIFPurchase,
    LSIFCreditResult,
    compute_lsif_credit,
    holding_period_years,
    lsif_hbp_credit_recovery,
    lsif_from_config,
    qc_highest_provincial_bracket_threshold,
    federal_lsif_rate,
    FEDERAL_LSIF_RATE,
)


class TestHoldingPeriodYears(unittest.TestCase):
    """Holding period based on redemption date (Fonds FTQ amended prospectus April 2024)."""

    def test_before_june_2027_is_2_years(self):
        self.assertEqual(holding_period_years(date(2026, 1, 1)), 2)
        self.assertEqual(holding_period_years(date(2027, 5, 31)), 2)

    def test_june_2027_to_may_2029_is_3_years(self):
        self.assertEqual(holding_period_years(date(2027, 6, 1)), 3)
        self.assertEqual(holding_period_years(date(2029, 5, 31)), 3)

    def test_june_2029_to_may_2031_is_4_years(self):
        self.assertEqual(holding_period_years(date(2029, 6, 1)), 4)
        self.assertEqual(holding_period_years(date(2031, 5, 31)), 4)

    def test_after_may_2031_is_5_years(self):
        self.assertEqual(holding_period_years(date(2031, 6, 1)), 5)
        self.assertEqual(holding_period_years(date(2040, 1, 1)), 5)


class TestQCHighestProvincialBracketThreshold(unittest.TestCase):
    """Income threshold uses the highest QC provincial bracket for the reference year."""

    def test_2023_threshold(self):
        self.assertEqual(qc_highest_provincial_bracket_threshold(2023), 119910)

    def test_2024_threshold(self):
        self.assertEqual(qc_highest_provincial_bracket_threshold(2024), 126000)

    def test_2025_threshold(self):
        self.assertEqual(qc_highest_provincial_bracket_threshold(2025), 129590)

    def test_2026_threshold(self):
        self.assertEqual(qc_highest_provincial_bracket_threshold(2026), 132245)


class TestReferenceYearIsYearMinus2(unittest.TestCase):
    """Reference year for income threshold is year-2 (deuxième année civile qui précède)."""

    def test_2027_purchase_uses_2025_threshold(self):
        """For a 2027 purchase, reference year is 2025 (year-2)."""
        threshold_2025 = qc_highest_provincial_bracket_threshold(2025)
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=threshold_2025 - 1,
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertTrue(result.quebec_eligible)

    def test_2028_purchase_uses_2026_threshold(self):
        """For a 2028 purchase, reference year is 2026 (year-2)."""
        threshold_2026 = qc_highest_provincial_bracket_threshold(2026)
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2028,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=threshold_2026 + 1,
        )
        result = compute_lsif_credit(purchase, year=2028)
        self.assertFalse(result.quebec_eligible)


class TestNoneReferenceYearIncome(unittest.TestCase):
    """When reference_year_taxable_income is None and year >= 2027, no threshold gate applies."""

    def test_none_reference_year_2027_eligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=None,
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertTrue(result.quebec_eligible)


class TestAgeGate(unittest.TestCase):
    """Eligibility gate — age 18-64."""

    def test_age_17_ineligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=2009,  # age 17
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.federal_eligible)
        self.assertFalse(result.quebec_eligible)
        self.assertEqual(result.federal_credit, 0)
        self.assertEqual(result.quebec_credit, 0)

    def test_age_18_eligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=2008,  # age 18
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)
        self.assertTrue(result.federal_eligible)

    def test_age_64_eligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1962,  # age 64
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_age_65_ineligible_for_quebec_but_eligible_for_federal(self):
        """Age 65+: ineligible for Quebec credit, but federal credit available
        per CRA ruling 2003-0006295."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1961,  # age 65
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)
        self.assertTrue(result.federal_eligible)
        self.assertAlmostEqual(result.federal_credit, 750.0)
        self.assertAlmostEqual(result.quebec_credit, 0.0)


class TestIncomeGate(unittest.TestCase):
    """The $3,500 employment income threshold only applies in the
    retirement/pension context (CFFP footnote 11, TA s.1029.8.5).
    A non-retired, non-pension person with any employment_income is eligible.
    """

    def test_income_3500_eligible_for_active_worker(self):
        """A non-retired, non-pension person with employment_income=$3,500 is eligible.
        The $3,500 threshold only applies when combined with retirement/pension status."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=3500,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_income_3501_eligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=3501,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_zero_income_eligible_for_active_worker(self):
        """A non-retired, non-pension person with employment_income=0 is eligible."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=0,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)


class TestRetirementGate(unittest.TestCase):
    """Retired/pension with employment income ≤ $3,500 and age < 65 → ineligible.
    Per CFFP footnote 11 and TA s.1029.8.5:
    - A retired or pension-receiving person with employment_income > $3,500 is eligible
    - A non-retired, non-pension person with any employment_income is eligible
    """

    def test_retired_low_income_ineligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=3500,
            retirement_year=2026,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_retired_zero_income_ineligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=0,
            retirement_year=2026,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_retired_with_high_income_eligible(self):
        """A retired person with employment_income > $3,500 is eligible (CFFP footnote 11)."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            retirement_year=2026,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_receives_pension_with_high_income_eligible(self):
        """A pension recipient with employment_income > $3,500 is eligible (CFFP footnote 11)."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            pension_start_year=2026,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_receives_pension_low_income_ineligible(self):
        """A pension recipient with employment_income ≤ $3,500 is ineligible."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=0,
            pension_start_year=2026,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_non_retired_zero_income_eligible(self):
        """A non-retired, non-pension person with employment_income=0 is eligible.
        The $3,500 threshold only applies in the retirement/pension context."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=0,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_not_retired_no_pension_eligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)


class TestPriorRedemptionGate(unittest.TestCase):
    """Prior LSIF redemption → ineligible."""

    def test_prior_redemption_ineligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            prior_redemption=True,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_no_prior_redemption_eligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            prior_redemption=False,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)


class TestQuebecResidency(unittest.TestCase):
    """Quebec credit requires Quebec residency; federal credit requires Quebec eligibility."""

    def test_non_quebec_resident_no_quebec_credit(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            is_quebec_resident=False,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)
        self.assertEqual(result.quebec_credit, 0)
        # Non-Quebec resident: no provincial program available to this individual,
        # so federal credit is also unavailable. Caller should pass provincial_eligible=False.
        result_no_prov = compute_lsif_credit(purchase, year=2026, provincial_eligible=False)
        self.assertFalse(result_no_prov.federal_eligible)
        self.assertEqual(result_no_prov.federal_credit, 0)


class TestFederalDependsOnProvincialProgram(unittest.TestCase):
    """Federal credit requires that a provincial LSIF program exists (ITA s.127.4)."""

    def test_federal_available_when_provincial_program_exists(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            is_quebec_resident=True,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)
        self.assertTrue(result.federal_eligible)
        self.assertGreater(result.federal_credit, 0)

    def test_federal_zero_when_no_provincial_program(self):
        """No provincial LSIF program → no federal credit."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            is_quebec_resident=False,
        )
        result = compute_lsif_credit(purchase, year=2026, provincial_eligible=False)
        self.assertFalse(result.federal_eligible)
        self.assertEqual(result.federal_credit, 0)


class TestCreditCalculation(unittest.TestCase):
    """Both credits: 15% up to $5,000, max $750 each."""

    def test_full_purchase_5000(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertAlmostEqual(result.quebec_credit, 750.0)
        self.assertAlmostEqual(result.federal_credit, 750.0)

    def test_partial_purchase_3000(self):
        purchase = LSIFPurchase(
            amount=3000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertAlmostEqual(result.quebec_credit, 450.0)
        self.assertAlmostEqual(result.federal_credit, 450.0)

    def test_purchase_over_5000_capped(self):
        purchase = LSIFPurchase(
            amount=10000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertAlmostEqual(result.quebec_credit, 750.0)
        self.assertAlmostEqual(result.federal_credit, 750.0)

    def test_credits_are_non_refundable(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.federal_refundable)
        self.assertFalse(result.quebec_refundable)


class TestReferenceYearIncomeThreshold(unittest.TestCase):
    """Starting 2027, no credit if reference-year taxable income exceeds highest QC provincial bracket."""

    def test_2026_no_income_threshold(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=500000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_2027_above_threshold_ineligible(self):
        """2027 purchase: reference year is 2025, threshold is $129,590."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=300000,
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertFalse(result.quebec_eligible)
        self.assertEqual(result.quebec_credit, 0)

    def test_2027_below_threshold_eligible(self):
        """2027 purchase: reference year is 2025, threshold is $129,590."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=100000,
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertTrue(result.quebec_eligible)
        self.assertAlmostEqual(result.quebec_credit, 750.0)

    def test_2027_at_threshold_eligible(self):
        """At the exact threshold, still eligible."""
        threshold_2025 = qc_highest_provincial_bracket_threshold(2025)
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=threshold_2025,
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertTrue(result.quebec_eligible)

    def test_2028_uses_2026_threshold(self):
        """2028 purchase: reference year is 2026, threshold is $132,245."""
        threshold_2026 = qc_highest_provincial_bracket_threshold(2026)
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2028,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=threshold_2026 + 1,
        )
        result = compute_lsif_credit(purchase, year=2028)
        self.assertFalse(result.quebec_eligible)


class TestHBPEligibility(unittest.TestCase):
    """HBP replacement shares → ineligible (ITA s.211.8).
    This is a boolean ineligibility gate, not a proportional reduction.
    """

    def test_hbp_used_ineligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            is_hbp_replacement=True,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)
        self.assertFalse(result.federal_eligible)
        self.assertEqual(result.quebec_credit, 0)
        self.assertEqual(result.federal_credit, 0)
        self.assertTrue(any("HBP" in r for r in result.ineligibility_reasons))

    def test_no_hbp_full_credit(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            is_hbp_replacement=False,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)
        self.assertAlmostEqual(result.quebec_credit, 750.0)
        self.assertAlmostEqual(result.federal_credit, 750.0)


class TestQuebecCarryforward(unittest.TestCase):
    """Quebec carryforward: any unused credit carries forward."""

    def test_quebec_carryforward_unused(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            quebec_carryforward=300,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertAlmostEqual(result.quebec_credit, 750.0 + 300.0)


class TestHoldingPeriodClawback(unittest.TestCase):
    """LSIF redeemed before holding period → credit must be repaid."""

    def test_redeemed_before_holding_2yr(self):
        """Redeemed in 2027 with 2-year holding (redemption before June 1, 2027)."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            acquisition_date=date(2026, 1, 1),
            redeemed_date=date(2027, 5, 31),
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.clawback_required)

    def test_redeemed_within_holding_3yr(self):
        """Redeemed before 3-year holding period."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            acquisition_date=date(2026, 1, 1),
            redeemed_date=date(2028, 6, 1),
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.clawback_required)

    def test_redeemed_within_holding_5yr(self):
        """Redeemed before 5-year holding period."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2031,
            birth_year=1990,
            employment_income=4000,
            acquisition_date=date(2031, 1, 1),
            redeemed_date=date(2034, 6, 1),
        )
        result = compute_lsif_credit(purchase, year=2031)
        self.assertTrue(result.clawback_required)

    def test_redeemed_after_holding_5yr(self):
        """Redeemed after 5-year holding period."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2031,
            birth_year=1990,
            employment_income=4000,
            acquisition_date=date(2031, 1, 1),
            redeemed_date=date(2037, 1, 1),
        )
        result = compute_lsif_credit(purchase, year=2031)
        self.assertFalse(result.clawback_required)

    def test_not_redeemed_no_clawback(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            redeemed_date=None,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.clawback_required)

    def test_holding_period_boundary_june_2027(self):
        """Redeeming on June 1, 2027 triggers 3-year holding (not 2)."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2025,
            birth_year=1990,
            employment_income=4000,
            acquisition_date=date(2025, 1, 1),
            redeemed_date=date(2027, 6, 1),
        )
        result = compute_lsif_credit(purchase, year=2025)
        self.assertTrue(result.clawback_required)


class TestInterestNonDeductible(unittest.TestCase):
    """Interest on borrowed money for LSIF shares is NOT deductible."""

    def test_interest_not_deductible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.interest_deductible)


class TestCombinedGates(unittest.TestCase):
    """Multiple gates failing simultaneously."""

    def test_retired_pension_low_income_ineligible(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=2008,  # age 18 passes age gate
            employment_income=2000,  # low income
            retirement_year=2026,  # combined with low income → ineligible
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_all_gates_pass(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            is_quebec_resident=True,
            prior_redemption=False,
            is_hbp_replacement=False,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)
        self.assertTrue(result.federal_eligible)
        self.assertAlmostEqual(result.quebec_credit, 750.0)
        self.assertAlmostEqual(result.federal_credit, 750.0)


class TestAutoTriggerDP16(unittest.TestCase):
    """DP#16: module auto-includes when lsif appears in input data."""

    def test_trigger_data_present(self):
        from countries.canada.lsif_credit import lsif_from_config

        cfg = {
            "lsif": {
                "purchase_amount": 5000,
                "purchase_year": 2026,
                "is_quebec_resident": True,
                "employment_income": 4000,
                "is_hbp_replacement": True,
                "acquisition_date": "2026-03-15",
            }
        }
        purchase = lsif_from_config(cfg, birth_year=1990, year=2026)
        self.assertAlmostEqual(purchase.amount, 5000)
        self.assertTrue(purchase.is_quebec_resident)
        self.assertTrue(purchase.is_hbp_replacement)
        self.assertEqual(purchase.acquisition_date, date(2026, 3, 15))

    def test_no_trigger_data(self):
        from countries.canada.lsif_credit import lsif_from_config

        cfg = {}
        result = lsif_from_config(cfg, birth_year=1990, year=2026)
        self.assertIsNone(result)


class TestClawbackAmount(unittest.TestCase):
    """Clawback amount equals the credit received when redeemed early."""

    def test_clawback_equals_credit(self):
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            acquisition_date=date(2026, 1, 1),
            redeemed_date=date(2027, 5, 1),
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.clawback_required)
        self.assertAlmostEqual(result.clawback_amount, 1500.0)  # 750 fed + 750 qc


class TestAcquisitionDateExemption(unittest.TestCase):
    """Shares acquired on or before 2026-12-31 are exempt from the income threshold."""

    def test_acquired_before_2027_exempt_from_threshold(self):
        """Shares acquired in 2026 are exempt even if income exceeds threshold."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=500000,
            acquisition_date=date(2026, 6, 15),
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertTrue(result.quebec_eligible)

    def test_acquired_on_2026_12_31_exempt_from_threshold(self):
        """Shares acquired on 2026-12-31 are still exempt."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=500000,
            acquisition_date=date(2026, 12, 31),
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertTrue(result.quebec_eligible)

    def test_acquired_2027_01_01_not_exempt(self):
        """Shares acquired on 2027-01-01 are subject to the income threshold."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=500000,
            acquisition_date=date(2027, 1, 1),
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertFalse(result.quebec_eligible)

    def test_no_acquisition_date_subject_to_threshold(self):
        """Without acquisition_date, 2027+ purchases are subject to the threshold."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2027,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=500000,
        )
        result = compute_lsif_credit(purchase, year=2027)
        self.assertFalse(result.quebec_eligible)

    def test_2026_purchase_no_acquisition_date_no_threshold(self):
        """2026 purchases without acquisition_date are not subject to income threshold."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=500000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)


class TestRetiredAndPensionCombo(unittest.TestCase):
    """DP#17: Test both retirement_year and pension_start_year set."""

    def test_both_retired_and_pension_high_income_eligible(self):
        """Retired AND receives pension with income > $3,500 → eligible."""
        purchase = LSIFPurchase(
            amount=5000,
            birth_year=1962,  # age 64 in 2026
            retirement_year=2026,
            pension_start_year=2026,
            employment_income=5000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)
        self.assertGreater(result.quebec_credit, 0)

    def test_both_retired_and_pension_low_income_ineligible(self):
        """Retired AND receives pension with income ≤ $3,500 → ineligible."""
        purchase = LSIFPurchase(
            amount=5000,
            birth_year=1962,  # age 64 in 2026
            retirement_year=2026,
            pension_start_year=2026,
            employment_income=3500,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)


class TestCarryforwardExcludesFromClawback(unittest.TestCase):
    """Carryforward must NOT be included in clawback amount (C1/G1 fix)."""

    def test_clawback_excludes_carryforward(self):
        """Clawback amount = current-year credit only, not including carryforward."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            quebec_carryforward=300.0,
            acquisition_date=date(2026, 1, 1),
            redeemed_date=date(2027, 5, 1),
        )
        result = compute_lsif_credit(purchase, year=2026)
        # Current-year credit: qc=750, fed=750, total=1500
        # Carryforward: 300
        # Total quebec_credit: 750+300=1050
        # Clawback should be current-year only: 750+750=1500
        self.assertAlmostEqual(result.current_qc_credit, 750.0)
        self.assertAlmostEqual(result.quebec_carryforward_used, 300.0)
        self.assertAlmostEqual(result.quebec_credit, 1050.0)
        self.assertAlmostEqual(result.clawback_amount, 1500.0)  # NOT 1800

    def test_clawback_no_carryforward(self):
        """Without carryforward, clawback = current credit (baseline)."""
        purchase = LSIFPurchase(
            amount=5000,
            purchase_year=2026,
            birth_year=1990,
            employment_income=4000,
            acquisition_date=date(2026, 1, 1),
            redeemed_date=date(2027, 5, 1),
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertAlmostEqual(result.current_qc_credit, 750.0)
        self.assertAlmostEqual(result.quebec_carryforward_used, 0.0)
        self.assertAlmostEqual(result.clawback_amount, 1500.0)


class TestCarryforwardPreservedWhenIneligible(unittest.TestCase):
    """Carryforward is preserved when ineligible per CFFP rules."""

    def test_carryforward_preserved_when_ineligible(self):
        """When ineligible, quebec_credit=0, carryforward is preserved for caller."""
        purchase = LSIFPurchase(
            amount=5000,
            birth_year=1960,
            retirement_year=2026,
            employment_income=3500,  # ≤ $3,500 → ineligible
            quebec_carryforward=500,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)
        self.assertAlmostEqual(result.quebec_credit, 0.0)
        self.assertAlmostEqual(result.quebec_carryforward_used, 0.0)
        # Per CFFP, carryforward is indefinitely reportable; preserved for caller
        self.assertAlmostEqual(result.quebec_carryforward_preserved, 500.0)


class TestNoneReferenceYearBefore2027(unittest.TestCase):
    """DP#17: reference_year_taxable_income=None in year < 2027."""

    def test_none_reference_year_2026_no_effect(self):
        """None income in 2026 (pre-threshold) has no effect on eligibility."""
        purchase = LSIFPurchase(
            amount=5000,
            birth_year=1990,
            employment_income=4000,
            reference_year_taxable_income=None,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)


class TestProvincialEligibleParameter(unittest.TestCase):
    """DP#17: every rule path tested, including provincial_eligible param."""

    def test_provincial_eligible_false_suppresses_federal_credit(self):
        """provincial_eligible=False: no federal credit even when QC eligible."""
        purchase = LSIFPurchase(amount=5000, birth_year=1990, employment_income=4000)
        result = compute_lsif_credit(purchase, year=2026, provincial_eligible=False)
        self.assertTrue(result.quebec_eligible)
        self.assertFalse(result.federal_eligible)
        self.assertAlmostEqual(result.federal_credit, 0.0)
        self.assertAlmostEqual(result.quebec_credit, 750.0)

    def test_provincial_eligible_true_enables_federal_credit(self):
        """provincial_eligible=True: federal credit available (default behavior)."""
        purchase = LSIFPurchase(amount=5000, birth_year=1990, employment_income=4000)
        result = compute_lsif_credit(purchase, year=2026, provincial_eligible=True)
        self.assertTrue(result.quebec_eligible)
        self.assertTrue(result.federal_eligible)
        self.assertAlmostEqual(result.federal_credit, 750.0)


class TestCRARulingAge65Plus(unittest.TestCase):
    """CRA ruling 2003-0006295: federal credit available to 65+/retired despite Quebec ineligibility."""

    def test_age_65_plus_gets_federal_credit_no_quebec(self):
        """Age 65+ QC resident: federal credit per CRA ruling 2003-0006295."""
        purchase = LSIFPurchase(
            amount=5000,
            birth_year=1960,  # age 66 in 2026
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        # Quebec denies credit to age 65+
        self.assertFalse(result.quebec_eligible)
        self.assertAlmostEqual(result.quebec_credit, 0.0)
        # Federal credit available per CRA ruling 2003-0006295:
        # Quebec's individual restrictions don't constitute suspension of assistance
        self.assertTrue(result.federal_eligible)
        self.assertAlmostEqual(result.federal_credit, 750.0)


class TestDP28ComputedEligibility(unittest.TestCase):
    """DP#28: retirement/pension eligibility computed from year fields.

    retirement_year and pension_start_year compute eligibility on demand,
    avoiding stale stored booleans when the simulation clock ticks.
    When year fields are None, the person is not retired / not receiving pension.
    """

    def test_retirement_year_computes_eligibility(self):
        """retirement_year=2026 makes person retired in 2026+, not before."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1990,
            retirement_year=2026, employment_income=2000,
        )
        # In 2025: not yet retired → eligible (non-retired with any income)
        result_2025 = compute_lsif_credit(purchase, year=2025)
        self.assertTrue(result_2025.quebec_eligible)
        # In 2026: retired with low income → ineligible
        result_2026 = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result_2026.quebec_eligible)

    def test_retirement_year_before_purchase_year_ineligible(self):
        """Person retired before purchase year with low income is ineligible."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1960,
            retirement_year=2024, employment_income=2000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_pension_start_year_computes_eligibility(self):
        """pension_start_year=2027 makes person receiving pension from 2027."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1970,
            pension_start_year=2027, employment_income=2000,
        )
        # In 2026: not yet receiving pension → eligible
        result_2026 = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result_2026.quebec_eligible)
        # In 2027: receiving pension with low income → ineligible
        result_2027 = compute_lsif_credit(purchase, year=2027)
        self.assertFalse(result_2027.quebec_eligible)

    def test_pension_start_year_with_high_income_eligible(self):
        """Receiving pension with employment income > $3,500 is eligible."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1970,
            pension_start_year=2026, employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_both_year_fields_combined_low_income(self):
        """Both retirement_year and pension_start_year with low income."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1990,
            retirement_year=2026, pension_start_year=2026,
            employment_income=2000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_both_year_fields_combined_high_income(self):
        """Both retirement_year and pension_start_year with high income → eligible."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1990,
            retirement_year=2026, pension_start_year=2026,
            employment_income=4000,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_year_field_exact_boundary(self):
        """Year field exactly matching simulation year: eligible at boundary."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1970,
            retirement_year=2026, employment_income=2000,
        )
        # At retirement_year: considered retired (boundary: year >= retirement_year)
        self.assertTrue(purchase.is_retired_in(2026))
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_year_field_year_before_retirement(self):
        """Year before retirement_year: not yet retired."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1970,
            retirement_year=2027, employment_income=2000,
        )
        self.assertFalse(purchase.is_retired_in(2026))
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)

    def test_pension_start_year_exact_boundary(self):
        """pension_start_year exactly matching simulation year: receiving pension."""
        purchase = LSIFPurchase(
            amount=5000, birth_year=1970,
            pension_start_year=2026, employment_income=2000,
        )
        self.assertTrue(purchase.receives_pension_in(2026))
        result = compute_lsif_credit(purchase, year=2026)
        self.assertFalse(result.quebec_eligible)

    def test_from_config_with_year_fields(self):
        """lsif_from_config accepts retirement_year and pension_start_year."""
        cfg = {
            "lsif": {
                "purchase_amount": 5000,
                "birth_year": 1970,
                "employment_income": 4000,
                "retirement_year": 2028,
                "pension_start_year": 2029,
            }
        }
        purchase = lsif_from_config(cfg, birth_year=1970, year=2026)
        self.assertIsNotNone(purchase)
        self.assertEqual(purchase.retirement_year, 2028)
        self.assertEqual(purchase.pension_start_year, 2029)

class TestNegativePurchaseAmount(unittest.TestCase):
    """DP#17: negative purchase amount raises ValueError."""

    def test_negative_amount_raises(self):
        purchase = LSIFPurchase(amount=-1000, birth_year=1990, employment_income=4000)
        with self.assertRaises(ValueError):
            compute_lsif_credit(purchase, year=2026)


class TestZeroPurchaseWithCarryforward(unittest.TestCase):
    """Edge case: amount=0 but carryforward applies."""

    def test_zero_amount_carries_forward(self):
        purchase = LSIFPurchase(
            amount=0,
            birth_year=1990,
            employment_income=4000,
            quebec_carryforward=500,
        )
        result = compute_lsif_credit(purchase, year=2026)
        self.assertTrue(result.quebec_eligible)
        self.assertAlmostEqual(result.current_qc_credit, 0.0)
        self.assertAlmostEqual(result.quebec_carryforward_used, 500.0)
        self.assertAlmostEqual(result.quebec_credit, 500.0)


class TestLSIFHBPCreditRecovery(unittest.TestCase):
    """Test LSIF HBP credit recovery under ITA s.211.8 (issue #217)."""

    def test_basic_8_year_recovery(self):
        """Standard 8-year recovery of $750 LSIF credit."""
        schedule = lsif_hbp_credit_recovery(
            lsif_credit_amount=750,
            hbp_withdrawal_year=2025,
            recovery_start_year=2027,
        )
        self.assertEqual(len(schedule), 8)
        self.assertIn(2027, schedule)
        self.assertIn(2034, schedule)
        # Total must equal original credit
        self.assertAlmostEqual(sum(schedule.values()), 750, places=2)

    def test_zero_credit_returns_empty(self):
        """Zero credit amount returns empty schedule."""
        schedule = lsif_hbp_credit_recovery(
            lsif_credit_amount=0,
            hbp_withdrawal_year=2025,
            recovery_start_year=2027,
        )
        self.assertEqual(schedule, {})

    def test_negative_credit_returns_empty(self):
        """Negative credit amount returns empty schedule."""
        schedule = lsif_hbp_credit_recovery(
            lsif_credit_amount=-100,
            hbp_withdrawal_year=2025,
            recovery_start_year=2027,
        )
        self.assertEqual(schedule, {})

    def test_final_year_gets_remainder(self):
        """Final year gets the remainder to handle rounding."""
        schedule = lsif_hbp_credit_recovery(
            lsif_credit_amount=750,
            hbp_withdrawal_year=2025,
            recovery_start_year=2027,
        )
        # $750 / 8 = $93.75/year, so all years should be $93.75
        for year, amount in schedule.items():
            self.assertAlmostEqual(amount, 93.75, places=2)

    def test_rounding_remainder_in_final_year(self):
        """Non-divisible amount: final year gets the rounding remainder."""
        schedule = lsif_hbp_credit_recovery(
            lsif_credit_amount=100,
            hbp_withdrawal_year=2025,
            recovery_start_year=2027,
        )
        # $100 / 8 = $12.50/year
        self.assertAlmostEqual(sum(schedule.values()), 100, places=2)
        self.assertAlmostEqual(schedule[2027], 12.5, places=2)
        self.assertAlmostEqual(schedule[2034], 12.5, places=2)

    def test_pure_function_same_inputs(self):
        """DP#3: Same inputs produce same outputs."""
        s1 = lsif_hbp_credit_recovery(750, 2025, 2027)
        s2 = lsif_hbp_credit_recovery(750, 2025, 2027)
        self.assertEqual(s1, s2)


class TestFederalC69Phaseout(unittest.TestCase):
    """#319 — Bill C-69 federal credit phase-out for federally-registered LSVCCs."""

    def test_provincial_keeps_full_rate_after_2024(self):
        """Provincially-registered (Quebec) funds keep 15% after 2024."""
        self.assertEqual(federal_lsif_rate(2025, federally_registered=False), FEDERAL_LSIF_RATE)

    def test_federal_keeps_rate_through_2024(self):
        """Federally-registered shares acquired in 2024 still get 15%."""
        self.assertEqual(federal_lsif_rate(2024, federally_registered=True), FEDERAL_LSIF_RATE)

    def test_federal_phased_out_after_2024(self):
        """Federally-registered shares acquired after 2024 get 0%."""
        self.assertEqual(federal_lsif_rate(2025, federally_registered=True), 0.0)

    def test_quebec_fund_federal_credit_unchanged_2025(self):
        """A Quebec (provincial) purchase still yields the full federal credit in 2025."""
        purchase = LSIFPurchase(amount=5000, purchase_year=2025, birth_year=1980,
                                federally_registered=False)
        result = compute_lsif_credit(purchase, 2025)
        self.assertTrue(result.federal_eligible)
        self.assertAlmostEqual(result.federal_credit, 5000 * FEDERAL_LSIF_RATE)

    def test_federally_registered_no_federal_credit_2025(self):
        """A federally-registered purchase in 2025 yields no federal credit."""
        purchase = LSIFPurchase(amount=5000, purchase_year=2025, birth_year=1980,
                                federally_registered=True)
        result = compute_lsif_credit(purchase, 2025)
        self.assertFalse(result.federal_eligible)
        self.assertEqual(result.federal_credit, 0.0)

    def test_federally_registered_phaseout_does_not_block_quebec_credit(self):
        """The federal phase-out leaves the Quebec credit intact."""
        purchase = LSIFPurchase(amount=5000, purchase_year=2025, birth_year=1980,
                                federally_registered=True)
        result = compute_lsif_credit(purchase, 2025)
        self.assertTrue(result.quebec_eligible)
        self.assertAlmostEqual(result.quebec_credit, 5000 * 0.15)

    def test_acquisition_date_year_drives_phaseout(self):
        """acquisition_date year (not taxation year) determines the federal rate."""
        from datetime import date as _date
        purchase = LSIFPurchase(amount=5000, purchase_year=2025, birth_year=1980,
                                federally_registered=True,
                                acquisition_date=_date(2024, 12, 31))
        result = compute_lsif_credit(purchase, 2025)
        self.assertTrue(result.federal_eligible)
        self.assertAlmostEqual(result.federal_credit, 5000 * FEDERAL_LSIF_RATE)

    def test_config_round_trips_federally_registered(self):
        """lsif_from_config wires the federally_registered flag (DP#24)."""
        cfg = {"lsif": {"purchase_amount": 5000, "purchase_year": 2025,
                        "federally_registered": True}}
        purchase = lsif_from_config(cfg, birth_year=1980, year=2025)
        self.assertTrue(purchase.federally_registered)


if __name__ == "__main__":
    unittest.main()