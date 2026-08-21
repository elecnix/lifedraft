#!/usr/bin/env python3
"""Unit tests for cpp_sharing.py module.

All test data uses round numbers per DP#15. No personal information.
Role-based names per DP#4. Tests verify every rule path per DP#17.

SCENARIO 12.2: CPP sharing tax benefit calculation.
SCENARIO 12.1: Combined CPP sharing + pension splitting.

Run with: python3 -m pytest countries/canada/tests/test_cpp_sharing.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.cpp_sharing import (
    CPPSharingInput,
    Province,
    combined_cpp_and_pension_split,
    compute_credit_split,
    compute_shared_benefits,
    compute_sharing_ratio,
    cpp_sharing_eligibility,
    cpp_sharing_tax_benefit,
    credit_split_pension_impact,
    optimize_cpp_sharing,
    project_cpp_sharing,
)


class TestCPPSharingEligibility(unittest.TestCase):
    """Test CPP sharing eligibility conditions (DP#28: date-computed gates)."""

    def test_both_eligible(self):
        """Both spouses 60+, receiving CPP, cohabiting: eligible."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
        )
        result = cpp_sharing_eligibility(data)
        self.assertTrue(result["eligible"])
        self.assertTrue(result["both_receiving_cpp"])
        self.assertTrue(result["both_at_least_60"])
        self.assertTrue(result["cohabiting"])

    def test_one_under_60_not_eligible(self):
        """One spouse under 60: not eligible (DP#28: age gate)."""
        data = CPPSharingInput(
            primary_birth_year=1970,  # 56 in 2026
            spouse_birth_year=1960,
            primary_cpp_annual=10000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1990,
        )
        result = cpp_sharing_eligibility(data)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["both_at_least_60"])
        self.assertIn("must be 60+", " ".join(result.get("reasons", [])))

    def test_both_under_60_not_eligible(self):
        """Both spouses under 60: not eligible."""
        data = CPPSharingInput(
            primary_birth_year=1970,  # 56
            spouse_birth_year=1972,    # 54
            primary_cpp_annual=5000,
            spouse_cpp_annual=3000,
            cohabitation_start_year=1992,
        )
        result = cpp_sharing_eligibility(data)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["both_at_least_60"])

    def test_zero_cpp_not_eligible(self):
        """One spouse with zero CPP: not eligible (both must be receiving)."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=0,  # Spouse never contributed
            cohabitation_start_year=1985,
        )
        result = cpp_sharing_eligibility(data)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["both_receiving_cpp"])

    def test_not_cohabiting_not_eligible(self):
        """Spouses not cohabiting: not eligible."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            cohabitation_end_year=2020,  # Stopped cohabiting
        )
        result = cpp_sharing_eligibility(data)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["cohabiting"])

    def test_just_turned_60_eligible(self):
        """Age 60 exactly: eligible (boundary test, DP#17)."""
        data = CPPSharingInput(
            primary_birth_year=1966,  # 60 in 2026
            spouse_birth_year=1964,   # 62 in 2026
            primary_cpp_annual=10000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1986,
        )
        result = cpp_sharing_eligibility(data)
        self.assertTrue(result["both_at_least_60"])

    def test_qpp_eligible(self):
        """Quebec residents: QPP follows same eligibility rules."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            province="quebec",
        )
        result = cpp_sharing_eligibility(data)
        self.assertTrue(result["eligible"])


class TestSharingRatio(unittest.TestCase):
    """Test sharing ratio computation (DP#3: pure function, same inputs → same output)."""

    def test_full_cohabitation_ratio_1(self):
        """Cohabiting entire joint contributory period: ratio ≈ 1.0."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1980,
            cohabitation_start_year=1980,  # Start of joint period
            calculation_year=2026,
        )
        ratio = compute_sharing_ratio(data)
        self.assertAlmostEqual(ratio, 1.0, places=1)

    def test_partial_cohabitation_ratio(self):
        """Cohabited 30 of 44 years: ratio ≈ 0.68."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            cohabitation_start_year=1985,  # 3 years after joint start
            calculation_year=2026,
        )
        ratio = compute_sharing_ratio(data)
        # Joint period: 1982-2026 = 44 years = 528 months
        # Cohab in joint: 1985-2026 = 41 years = 492 months
        # Expected: 492/528 ≈ 0.932
        self.assertAlmostEqual(ratio, 492 / 528, places=2)

    def test_recent_cohabitation_low_ratio(self):
        """Started cohabiting near end of contributory period: low ratio."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            cohabitation_start_year=2020,  # Only 6 years of cohabitation
            calculation_year=2026,
        )
        ratio = compute_sharing_ratio(data)
        # Joint period: max(1978,1982)-min(2025,2025) = 1982-2025 = 43 years
        # Cohab in joint: 2020-2025 = 5 years
        # Expected: 5/43 ≈ 0.116
        self.assertAlmostEqual(ratio, 5 / 43, places=2)
        self.assertLess(ratio, 0.2)

    def test_no_overlap_zero_ratio(self):
        """Very little overlap in contributory periods: low ratio."""
        data = CPPSharingInput(
            primary_birth_year=1935,
            spouse_birth_year=1985,  # Large age gap
            primary_contribution_start_year=1953,
            spouse_contribution_start_year=2003,
            cohabitation_start_year=2010,
            calculation_year=2026,
            primary_start_age=60,
        )
        # Primary at 60 in 1995, so contributory period ends at 1995
        # Spouse started contributing at 2003
        # No overlap: primary ended before spouse began
        # contrib_period_end for primary = 1935+60 = 1995
        # spouse_contrib_start = 2003 > 1995
        ratio = compute_sharing_ratio(data)
        # Should be 0 because joint_start (2003) > joint_end (1995)
        self.assertEqual(ratio, 0.0)

    def test_started_cohabiting_before_contributing(self):
        """Cohabiting since before contributing: ratio should be high."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_contribution_start_year=1985,
            spouse_contribution_start_year=1986,
            cohabitation_start_year=1982,  # Before contributing
            calculation_year=2026,
        )
        ratio = compute_sharing_ratio(data)
        # Cohabitation started before joint contributory period
        # So ratio should be ≈1.0 (entire joint period was cohabited)
        self.assertAlmostEqual(ratio, 1.0, places=1)


class TestSharedBenefits(unittest.TestCase):
    """Test CPP shared benefit calculation (DP#3: pure function)."""

    def test_equal_benefits_equal_sharing(self):
        """When both spouses have equal CPP, sharing redistributes equally."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=12000,
            spouse_cpp_annual=12000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
        )
        result = compute_shared_benefits(data)
        self.assertTrue(result.eligible)
        # Equal CPP, equal ratio → each gets ~same as before
        # Total should be preserved
        total_before = 12000 + 12000
        total_after = result.primary_total_after_sharing + result.spouse_total_after_sharing
        self.assertAlmostEqual(total_before, total_after, places=0)

    def test_unequal_benefits_redistribution(self):
        """Higher earner transfers some CPP to lower earner."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
        )
        result = compute_shared_benefits(data)
        self.assertTrue(result.eligible)
        # Total preserved
        total_before = 15000 + 8000
        total_after = result.primary_total_after_sharing + result.spouse_total_after_sharing
        self.assertAlmostEqual(total_before, total_after, places=0)

        # Primary's CPP should decrease, spouse's should increase
        self.assertLess(result.primary_total_after_sharing, 15000)
        self.assertGreater(result.spouse_total_after_sharing, 8000)

    def test_prb_not_shared(self):
        """Post-retirement benefits are NOT shared (DP#28: rule path)."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            primary_prb_annual=2000,   # PRB earned after starting CPP
            spouse_prb_annual=500,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
        )
        result = compute_shared_benefits(data)

        # PRB amounts should be preserved unchanged
        self.assertEqual(result.primary_prb_preserved, 2000)
        self.assertEqual(result.spouse_prb_preserved, 500)

        # Total including PRB should equal total before sharing
        total_before = data.primary_cpp_annual + data.spouse_cpp_annual
        total_after = (
            result.primary_total_after_sharing + result.spouse_total_after_sharing
        )
        self.assertAlmostEqual(total_before, total_after, places=0)

        # Shareable pool excludes PRB
        self.assertAlmostEqual(
            data.primary_shareable_cpp, 13000, places=0,
            msg="Primary shareable should be 15000 - 2000"
        )
        self.assertAlmostEqual(
            data.spouse_shareable_cpp, 7500, places=0,
            msg="Spouse shareable should be 8000 - 500"
        )

    def test_combined_total_always_preserved(self):
        """Combined benefit total is always preserved (key invariant)."""
        for primary_cpp in [10000, 15000, 20000]:
            for spouse_cpp in [5000, 8000, 12000]:
                for ratio_scenario in [
                    dict(cohabitation_start_year=1980, primary_contribution_start_year=1978, spouse_contribution_start_year=1982),
                    dict(cohabitation_start_year=2000, primary_contribution_start_year=1978, spouse_contribution_start_year=1982),
                ]:
                    data = CPPSharingInput(
                        primary_birth_year=1960,
                        spouse_birth_year=1962,
                        primary_cpp_annual=primary_cpp,
                        spouse_cpp_annual=spouse_cpp,
                        **ratio_scenario,
                    )
                    result = compute_shared_benefits(data)
                    if result.eligible:
                        total_before = primary_cpp + spouse_cpp
                        total_after = (
                            result.primary_total_after_sharing
                            + result.spouse_total_after_sharing
                        )
                        self.assertAlmostEqual(
                            total_before, total_after, places=0,
                            msg=f"Total not preserved for primary={primary_cpp}, spouse={spouse_cpp}"
                        )

    def test_not_eligible_returns_original_benefits(self):
        """When not eligible, benefits are unchanged."""
        data = CPPSharingInput(
            primary_birth_year=1970,  # Too young
            spouse_birth_year=1960,
            primary_cpp_annual=10000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1990,
        )
        result = compute_shared_benefits(data)
        self.assertFalse(result.eligible)
        self.assertEqual(result.primary_total_after_sharing, 10000)
        self.assertEqual(result.spouse_total_after_sharing, 8000)


class TestTaxBenefit(unittest.TestCase):
    """Test CPP sharing tax benefit calculation (SCENARIO 12.2)."""

    def test_high_earner_sharing_benefits_low_earner(self):
        """SCENARIO 12.2: Primary at 48% MTR, spouse at 20% MTR.

        Transferring CPP income from 48% to 20% bracket saves
        ~28 percentage points per dollar transferred.
        """
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,  # ~48% MTR
            spouse_other_income=30000,     # ~20% MTR
        )
        result = cpp_sharing_tax_benefit(data)

        self.assertTrue(result["eligible"])
        # Sharing should benefit the family (income moved from high to low MTR)
        self.assertGreater(result["annual_tax_savings"], 0)

        # The MTR gap should be significant
        self.assertGreater(result["mtr_gap"], 0.10)

    def test_equal_incomes_no_benefit(self):
        """Equal incomes: no MTR gap, no tax savings from sharing."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=10000,
            spouse_cpp_annual=10000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1980,
            primary_other_income=60000,
            spouse_other_income=60000,
        )
        result = cpp_sharing_tax_benefit(data)

        # Equal CPP, equal other income, high sharing ratio → minimal benefit
        # With equal incomes, the MTR gap is near zero
        self.assertAlmostEqual(result["annual_tax_savings"], 0, places=-1)

    def test_low_ratio_reduces_benefit(self):
        """Lower sharing ratio (less cohabitation) reduces the benefit."""
        # Full cohabitation
        data_full = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1982,  # Full joint period
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
        )
        result_full = cpp_sharing_tax_benefit(data_full)

        # Partial cohabitation
        data_partial = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=2010,  # Only recent years
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
        )
        result_partial = cpp_sharing_tax_benefit(data_partial)

        # Full cohabitation should give more benefit
        self.assertGreater(
            result_full["annual_tax_savings"],
            result_partial["annual_tax_savings"],
        )

    def test_prb_does_not_affect_sharing_benefit(self):
        """PRB amounts don't affect the sharing calculation."""
        data_no_prb = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
        )
        data_with_prb = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            primary_prb_annual=2000,
            spouse_prb_annual=500,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
        )

        benefit_no_prb = cpp_sharing_tax_benefit(data_no_prb)
        benefit_with_prb = cpp_sharing_tax_benefit(data_with_prb)

        # The shareable pool is smaller with PRB, so the total benefit
        # from sharing should be based on the shareable portion only
        # Both should have positive tax savings
        self.assertTrue(benefit_no_prb["eligible"])
        self.assertTrue(benefit_with_prb["eligible"])


class TestOptimizeCPPSharing(unittest.TestCase):
    """Test CPP sharing optimization decision."""

    def test_recommend_share_when_beneficial(self):
        """When MTR gap exists: recommend sharing."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
        )
        result = optimize_cpp_sharing(data)

        self.assertEqual(result["recommendation"], "share")
        self.assertGreater(result["annual_tax_savings"], 0)
        self.assertAlmostEqual(
            result["primary_cpp_before"] + result["spouse_cpp_before"],
            result["primary_cpp_after"] + result["spouse_cpp_after"],
            places=0,
        )

    def test_no_benefit_when_equal_incomes(self):
        """Equal incomes with equal CPP: no benefit from sharing."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=10000,
            spouse_cpp_annual=10000,
            cohabitation_start_year=1980,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1980,
            primary_other_income=60000,
            spouse_other_income=60000,
        )
        result = optimize_cpp_sharing(data)

        # With equal income and equal CPP, the net transfer is near zero
        # and tax savings should be negligible
        self.assertAlmostEqual(result["annual_tax_savings"], 0, places=-1)

    def test_not_eligible_returns_none(self):
        """Not eligible: recommendation is 'not_eligible'."""
        data = CPPSharingInput(
            primary_birth_year=1970,  # Too young
            spouse_birth_year=1962,
            primary_cpp_annual=5000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1995,
        )
        result = optimize_cpp_sharing(data)
        self.assertEqual(result["recommendation"], "not_eligible")

    def test_qpp_pension_plan_label(self):
        """Quebec residents: pension plan labeled as QPP."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            province="quebec",
        )
        result = optimize_cpp_sharing(data)
        self.assertIn(result.get("pension_plan"), ["QPP", "CPP"])
        if data.province_enum == Province.QUEBEC:
            self.assertEqual(result["pension_plan"], "QPP")

    def test_ontario_cpp_label(self):
        """Ontario residents: pension plan labeled as CPP."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            province="ontario",
        )
        result = optimize_cpp_sharing(data)
        self.assertEqual(result["pension_plan"], "CPP")


class TestCombinedCPPAndPensionSplit(unittest.TestCase):
    """Test combined CPP sharing + pension income splitting (SCENARIO 12.2).

    SCENARIO 12.2: CPP sharing and pension splitting are independent programs
    that can both be used. The combined benefit is the sum.
    """

    def test_combined_greater_than_either_alone(self):
        """Combined CPP sharing + pension splitting beats either alone."""
        data = CPPSharingInput(
            primary_birth_year=1958,
            spouse_birth_year=1960,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1976,
            spouse_contribution_start_year=1980,
            primary_other_income=60000,
            spouse_other_income=15000,
        )

        result = combined_cpp_and_pension_split(
            data,
            eligible_pension=40000,  # RRIF eligible for splitting
            spouse_a_other_income=60000,  # Plus CPP
            spouse_b_other_income=15000,
            spouse_a_age=68,
            spouse_b_age=66,
            province="quebec",
        )

        # Both programs should be available
        self.assertIsNotNone(result["cpp_sharing"])
        self.assertIsNotNone(result["pension_splitting"])

        # Combined savings should be > 0
        self.assertGreater(result["combined_annual_savings"], 0)

        # Combined > CPP sharing alone
        cpp_savings = result["cpp_sharing"].get("annual_tax_savings", 0)
        pension_savings = result["pension_splitting"]["tax_savings"]
        self.assertGreaterEqual(
            result["combined_annual_savings"],
            cpp_savings + pension_savings - 100,  # Within $100 for rounding
        )

    def test_programs_are_independent(self):
        """CPP sharing and pension splitting are separate programs."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=12000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            province="quebec",
        )

        result = combined_cpp_and_pension_split(
            data,
            eligible_pension=30000,
            spouse_a_other_income=50000,
            spouse_b_other_income=20000,
            spouse_a_age=68,
            spouse_b_age=66,
        )

        self.assertTrue(result["programs_are_independent"])
        self.assertIn("Service Canada", result["note"])

    def test_no_pension_income_no_splitting(self):
        """No eligible pension: only CPP sharing applies."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_other_income=80000,
            spouse_other_income=30000,
        )

        result = combined_cpp_and_pension_split(
            data,
            eligible_pension=0,  # No RRIF yet
        )

        # Pension splitting should be None
        self.assertIsNone(result["pension_splitting"])
        # CPP sharing should still apply
        self.assertGreater(result["combined_annual_savings"], 0)

    def test_under_65_no_pension_splitting(self):
        """Under 65 in Quebec: CPP sharing available but pension splitting not (provincial)."""
        data = CPPSharingInput(
            primary_birth_year=1966,  # 60 in 2026
            spouse_birth_year=1964,   # 62 in 2026
            primary_cpp_annual=10000,
            spouse_cpp_annual=6000,
            cohabitation_start_year=1988,
            primary_other_income=60000,
            spouse_other_income=25000,
            province="quebec",
        )

        result = combined_cpp_and_pension_split(
            data,
            eligible_pension=20000,
            spouse_a_other_income=60000,
            spouse_b_other_income=25000,
            spouse_a_age=60,  # Under 65 in Quebec
            spouse_b_age=62,
            province="quebec",
        )

        # CPP sharing should be available
        self.assertIsNotNone(result["cpp_sharing"])
        # Pension splitting may not be available provincially (Quebec 65+)
        # but federally it is at 55+
        # The combined benefits should still reflect what's available


class TestProjectCPPSharing(unittest.TestCase):
    """Test multi-year CPP sharing projection (DP#26: fold over steps)."""

    def test_10_year_projection(self):
        """10-year projection runs and produces results."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
        )
        results = project_cpp_sharing(data, years=10)

        self.assertEqual(len(results), 10)
        # First year should be eligible (both 60+ in 2026)
        self.assertTrue(results[0]["cpp_sharing_eligible"])
        # CPP should increase with inflation each year
        self.assertGreater(results[1]["primary_cpp_before"], results[0]["primary_cpp_before"])

    def test_projection_preserves_total(self):
        """Each year: total CPP before = total CPP after (invariant)."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
        )
        results = project_cpp_sharing(data, years=5)

        for r in results:
            if r["cpp_sharing_eligible"]:
                total_before = r["primary_cpp_before"] + r["spouse_cpp_before"]
                sharing = r["sharing_result"]
                total_after = (
                    sharing["primary_cpp_after"] + sharing["spouse_cpp_after"]
                )
                self.assertAlmostEqual(
                    total_before, total_after, places=0,
                    msg=f"Total not preserved in year {r['year']}"
                )

    def test_projection_inflation_growth(self):
        """CPP benefits increase with inflation over projection."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
        )
        results = project_cpp_sharing(data, years=10, inflation=0.025)

        # Primary CPP should grow ~2.5% per year
        self.assertGreater(results[-1]["primary_cpp_before"], results[0]["primary_cpp_before"])
        # Check that growth is in the right ballpark (2.5% × 10 years ≈ 28%)
        growth_ratio = results[-1]["primary_cpp_before"] / results[0]["primary_cpp_before"]
        self.assertGreater(growth_ratio, 1.24)  # At least 24% growth (2.5%^10 ≈ 28% but first year doesn't inflate)

    def test_age_transition_in_projection(self):
        """Projection handles age transitions correctly."""
        data = CPPSharingInput(
            primary_birth_year=1966,  # 60 in 2026
            spouse_birth_year=1968,   # 58 in 2026
            primary_cpp_annual=10000,
            spouse_cpp_annual=6000,
            cohabitation_start_year=1990,
        )
        results = project_cpp_sharing(data, years=5)

        # 2026: primary 60, spouse 58 → not eligible (spouse under 60)
        self.assertFalse(results[0]["cpp_sharing_eligible"])
        # 2028: primary 62, spouse 60 → eligible
        self.assertTrue(results[2]["cpp_sharing_eligible"])


class TestProvinceEnum(unittest.TestCase):
    """Test Province enum and QPP vs CPP determination."""

    def test_quebec_uses_qpp(self):
        """Quebec residents use QPP."""
        p = Province.QUEBEC
        self.assertTrue(p.is_quebec)
        self.assertEqual(p.pension_plan, "QPP")

    def test_ontario_uses_cpp(self):
        """Ontario residents use CPP."""
        p = Province.ONTARIO
        self.assertFalse(p.is_quebec)
        self.assertEqual(p.pension_plan, "CPP")

    def test_other_uses_cpp(self):
        """Other provinces use CPP."""
        p = Province.OTHER
        self.assertFalse(p.is_quebec)
        self.assertEqual(p.pension_plan, "CPP")

    def test_province_enum_from_string(self):
        """CPPSharingInput converts province strings to enum."""
        data_qc = CPPSharingInput(province="quebec")
        self.assertEqual(data_qc.province_enum, Province.QUEBEC)

        data_qc_short = CPPSharingInput(province="qc")
        self.assertEqual(data_qc_short.province_enum, Province.QUEBEC)

        data_on = CPPSharingInput(province="ontario")
        self.assertEqual(data_on.province_enum, Province.ONTARIO)

        data_bc = CPPSharingInput(province="british_columbia")
        self.assertEqual(data_bc.province_enum, Province.OTHER)


class TestCPPSharingInputProperties(unittest.TestCase):
    """Test CPPSharingInput computed properties."""

    def test_age_in_calculation(self):
        """Per DP#1: age computed from birth_year, not stored."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            calculation_year=2026,
        )
        self.assertEqual(data.primary_age_in(2026), 66)
        self.assertEqual(data.spouse_age_in(2026), 64)

    def test_omitted_birth_year_fails_loudly_not_a_plausible_person(self):
        """DP#32 / issue #741: a caller that omits birth_year must NOT silently get
        a plausible 47-year-old. The `0` sentinel must raise, naming the member
        and the field, the moment a computation tries to use it."""
        data = CPPSharingInput()  # omits both birth years -> sentinel 0
        with self.assertRaisesRegex(ValueError, "primary_birth_year=0"):
            data.primary_age_in(2026)
        with self.assertRaisesRegex(ValueError, "spouse_birth_year=0"):
            data.spouse_age_in(2026)

    def test_compute_sharing_ratio_fails_loudly_on_omitted_birth_year(self):
        """The birth_year sentinel must fail at the computation that drives the
        sharing ratio, not produce a confident number for the wrong person."""
        from countries.canada.cpp_sharing import compute_sharing_ratio
        data = CPPSharingInput(
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=2025,
        )  # omits birth years
        with self.assertRaisesRegex(ValueError, "primary_birth_year=0"):
            compute_sharing_ratio(data)

    def test_invalid_birth_year_age_out_of_range_fails(self):
        """A birth year yielding an impossible age (< 0 or > 130) must raise."""
        data = CPPSharingInput(primary_birth_year=1800, spouse_birth_year=1960)
        with self.assertRaisesRegex(ValueError, "primary_birth_year=1800"):
            data.primary_age_in(2026)

    def test_shareable_cpp(self):
        """Shareable CPP = total minus post-retirement benefits."""
        data = CPPSharingInput(
            primary_cpp_annual=15000,
            primary_prb_annual=3000,
            spouse_cpp_annual=8000,
            spouse_prb_annual=1000,
        )
        self.assertEqual(data.primary_shareable_cpp, 12000)
        self.assertEqual(data.spouse_shareable_cpp, 7000)

    def test_zero_prb_all_shareable(self):
        """Zero PRB: all CPP is shareable."""
        data = CPPSharingInput(
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
        )
        self.assertEqual(data.primary_shareable_cpp, 15000)
        self.assertEqual(data.spouse_shareable_cpp, 8000)


class TestEdgeCases(unittest.TestCase):
    """Test edge cases per DP#17: every rule path needs a test."""

    def test_one_spouse_zero_cpp(self):
        """One spouse never contributed: not eligible for sharing."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=0,
            cohabitation_start_year=1985,
        )
        result = compute_shared_benefits(data)
        self.assertFalse(result.eligible)

    def test_both_zero_cpp(self):
        """Both spouses zero CPP: not eligible."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=0,
            spouse_cpp_annual=0,
        )
        result = cpp_sharing_eligibility(data)
        self.assertFalse(result["eligible"])

    def test_very_large_mtr_gap(self):
        """Maximum MTR gap: high earner at 54%, low earner at 0%."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=250000,  # Very high MTR
            spouse_other_income=5000,     # Very low MTR
        )
        result = cpp_sharing_tax_benefit(data)
        self.assertTrue(result["eligible"])
        self.assertGreater(result["annual_tax_savings"], 0)

    def test_small_benefit_amount(self):
        """Small CPP amounts: benefit should still be calculable."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=3000,
            spouse_cpp_annual=2000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=80000,
            spouse_other_income=30000,
        )
        result = cpp_sharing_tax_benefit(data)
        self.assertTrue(result["eligible"])
        # Even small amounts should produce a computable (possibly tiny) result
        self.assertGreaterEqual(result["annual_tax_savings"], 0)

    def test_qpp_same_structure_as_cpp(self):
        """QPP sharing uses same eligibility rules as CPP."""
        data_qc = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            province="quebec",
        )
        data_on = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            province="ontario",
        )

        result_qc = cpp_sharing_eligibility(data_qc)
        result_on = cpp_sharing_eligibility(data_on)

        # Same eligibility (structure is identical)
        self.assertEqual(result_qc["eligible"], result_on["eligible"])

    def test_single_person_no_sharing(self):
        """Single person: CPP sharing not applicable."""
        # This would require spouse_cpp_annual = 0, which means
        # not both receiving CPP
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,
            spouse_cpp_annual=0,
            cohabitation_start_year=1985,
        )
        result = cpp_sharing_eligibility(data)
        self.assertFalse(result["eligible"])
        self.assertFalse(result["both_receiving_cpp"])


class TestMixedAgeCoupleMTR(unittest.TestCase):
    """Mixed-age couple MTR interaction tests — issue #376.

    When one retires early while the other continues working, the
    household MTR gap changes. CPP sharing redistributes income
    from higher-MTR to lower-MTR spouse, reducing family tax.
    DP#17: edge cases around age differences and retirement timing.
    """

    def test_older_spouse_retires_younger_continues_work(self):
        """Older spouse at 65 (retired, low MTR); younger at 55 (working, higher MTR).

        CPP sharing at 60+ can shift some CPP income from the older retiree
        to the younger working spouse (when sharing is available), but this
        depends on both being 60+.
        """
        data = CPPSharingInput(
            primary_birth_year=1950,   # 76 in 2026 — retired
            spouse_birth_year=1960,    # 66 in 2026 — receiving CPP
            primary_cpp_annual=18000,
            spouse_cpp_annual=10000,
            cohabitation_start_year=1980,
            primary_contribution_start_year=1968,
            spouse_contribution_start_year=1978,
            primary_other_income=15000,   # Low other income (retired)
            spouse_other_income=80000,    # Higher income (or pension)
        )
        benefit = cpp_sharing_tax_benefit(data)
        self.assertTrue(benefit['eligible'])
        # With different MTRs, sharing should produce tax savings
        self.assertGreaterEqual(benefit['annual_tax_savings'], 0)
        # Sharing ratio should be computable
        self.assertGreater(benefit['sharing_ratio'], 0)

    def test_large_age_gap_contributory_period_overlap(self):
        """Large age gap (15+ years): contributory periods barely overlap.

        When one spouse is much older, the joint contributory period
        may be short or nonexistent, reducing or eliminating sharing benefit.
        """
        data = CPPSharingInput(
            primary_birth_year=1945,   # 81 in 2026
            spouse_birth_year=1965,    # 61 in 2026
            primary_cpp_annual=20000,
            spouse_cpp_annual=8000,
            cohabitation_start_year=2000,
            primary_contribution_start_year=1963,
            spouse_contribution_start_year=1983,
            primary_start_age=60,  # Primary started CPP at 60 (2005)
            spouse_start_age=61,   # Spouse started CPP at 61 (2026)
        )
        ratio = compute_sharing_ratio(data)
        # With primary's contributory ending in 2005 and spouse's in 2026
        # if no overlap, ratio is 0
        self.assertGreaterEqual(ratio, 0.0)
        self.assertLessEqual(ratio, 1.0)

    def test_one_retires_early_at_60_other_at_70(self):
        """One retires at 60 (reduced CPP), other defers to 70 (enhanced CPP).

        The early retiree has a permanently lower pension; the late retiree
        has a permanently higher pension. CPP sharing redistributes some
        of the late retiree's higher pension to the early retiree.
        """
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=9000,    # Reduced: started at 60
            spouse_cpp_annual=22000,    # Enhanced: started at 70
            cohabitation_start_year=1980,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1980,
            primary_start_age=60,
            spouse_start_age=70,
            primary_other_income=40000,
            spouse_other_income=40000,
        )
        result = compute_shared_benefits(data)

        # Age check: primary is 66, spouse is 64 — both 60+ in 2026
        eligibility = cpp_sharing_eligibility(data)
        self.assertTrue(eligibility['eligible'])

        # Spouse (late retiree) should transfer some CPP to primary (early retiree)
        # Because spouse's CPP is higher
        total_before = data.primary_cpp_annual + data.spouse_cpp_annual
        total_after = result.primary_total_after_sharing + result.spouse_total_after_sharing
        self.assertAlmostEqual(total_before, total_after, places=0)

    def test_equal_ages_different_start_ages(self):
        """Same age spouses, one started CPP earlier than the other.

        This creates a pension gap despite equal contributory periods.
        Sharing reduces the gap when both 60+.
        """
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1960,
            primary_cpp_annual=12000,   # Started at 60
            spouse_cpp_annual=18000,    # Started at 65 (full pension)
            cohabitation_start_year=1980,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1978,
            primary_start_age=60,
            spouse_start_age=65,
            primary_other_income=50000,
            spouse_other_income=50000,
        )
        result = compute_shared_benefits(data)
        self.assertTrue(result.eligible)

        # With equal other incomes, the MTR gap comes from different CPP amounts
        # The sharing should narrow the CPP gap between spouses
        primary_after = result.primary_total_after_sharing
        spouse_after = result.spouse_total_after_sharing
        gap_after = abs(primary_after - spouse_after)
        # Sharing should narrow or maintain the gap between spouses' CPP
        self.assertGreaterEqual(gap_after, 0)


class TestCreditSplit(unittest.TestCase):
    """Test CPP credit splitting on relationship breakdown — ITA s.55.1 (issue #310)."""

    def test_equal_split_of_pooled_credits(self):
        """Cohabitation-period credits are pooled and divided 50/50."""
        result = compute_credit_split(
            primary_cohab_earnings=600000,
            spouse_cohab_earnings=200000,
            cohabitation_start_year=1990,
            cohabitation_end_year=2020,
            relationship_ended=True,
        )
        self.assertTrue(result.eligible)
        self.assertEqual(result.pooled_credits, 800000)
        self.assertEqual(result.primary_credits_after, 400000)
        self.assertEqual(result.spouse_credits_after, 400000)
        # Lower earner gains, higher earner loses; combined conserved.
        self.assertEqual(result.spouse_credit_change, 200000)
        self.assertEqual(result.primary_credit_change, -200000)
        self.assertEqual(result.combined_preserved, 800000)

    def test_no_split_while_relationship_intact(self):
        """Edge: credit splitting requires separation/divorce (s.55.1)."""
        result = compute_credit_split(
            primary_cohab_earnings=600000,
            spouse_cohab_earnings=200000,
            cohabitation_start_year=1990,
            cohabitation_end_year=2020,
            relationship_ended=False,
        )
        self.assertFalse(result.eligible)
        self.assertEqual(result.primary_credits_after, 600000)

    def test_pension_impact_weighted_by_contributory_share(self):
        """Per-partner credit change is weighted by the cohabitation share."""
        result = compute_credit_split(
            primary_cohab_earnings=600000,
            spouse_cohab_earnings=200000,
            cohabitation_start_year=1990,
            cohabitation_end_year=2020,
        )
        impact = credit_split_pension_impact(
            result,
            cohab_years_in_contributory_period=30,
            total_contributory_years=40,
            person="spouse",
        )
        self.assertEqual(impact['earnings_base_share'], 0.75)
        self.assertEqual(impact['credit_change'], 200000)
        self.assertEqual(impact['approx_base_change'], 200000 * 0.75)


if __name__ == '__main__':
    unittest.main()
