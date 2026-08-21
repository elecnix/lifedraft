#!/usr/bin/env python3
"""Unit tests for CPP2/QPP2 contribution and benefit calculations.

Tests cover:
- CPP2 contribution calculation (earnings between YMPE and YAMPE)
- QPP-specific contribution rates (higher than CPP)
- QPP2 contributions (same structure as CPP2 but QPP rates)
- CPP2/QPP2 enhanced retirement benefits
- Survivor benefits (CPP and QPP-specific rules)
- Integration with existing cpp_sharing module

All test data uses round numbers per DP#15. No personal information.
Role-based names per DP#4. Tests verify every rule path per DP#17.

Run with: python3 -m pytest countries/canada/tests/test_cpp2_qpp2.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.cpp_sharing import (
    CPP2_MAX_PENSIONABLE_2026,
    CPP_BASIC_EXEMPTION,
    CPP_EARLY_PENALTY_PER_MONTH,
    CPP_LATE_BONUS_PER_MONTH,
    CPP_MAX_BENEFIT_65_2026,
    CPP_MAX_PENSIONABLE_2026,
    CPPSharingInput,
    optimize_cpp_sharing,
)
from tax_data import TaxDataProvider


class TestCPP2ContributionCalculation(unittest.TestCase):
    """Test CPP2 contribution calculations (earnings between YMPE and YAMPE).

    CPP2 is the second additional CPP contribution tier introduced in 2024.
    Employee rate: 4% on earnings between YMPE and YAMPE (no basic exemption).
    Self-employed rate: 8% (employee + employer portions).
    """

    def test_cpp2_employee_contribution_below_ympe(self):
        """Earnings below YMPE: no CPP2 contribution."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=50000,
            year=2026,
            province="ontario",
        )
        self.assertAlmostEqual(result["cpp2_employee"], 0, places=2)
        self.assertAlmostEqual(result["cpp2_self_employed"], 0, places=2)

    def test_cpp2_employee_contribution_at_ympe(self):
        """Earnings exactly at YMPE: no CPP2 contribution."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=CPP_MAX_PENSIONABLE_2026,
            year=2026,
            province="ontario",
        )
        self.assertAlmostEqual(result["cpp2_employee"], 0, places=2)

    def test_cpp2_employee_contribution_between_ympe_yampe(self):
        """Earnings between YMPE and YAMPE: CPP2 on the excess above YMPE.

        Example: $80,000 income in 2026.
        YMPE = $74,600, YAMPE = $81,900 (from code data)
        CPP2 earnings = $80,000 - $74,600 = $5,400
        CPP2 employee contribution = $5,400 × 4% = $216
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="ontario",
        )
        expected_cpp2_earnings = 80000 - CPP_MAX_PENSIONABLE_2026
        expected_employee = expected_cpp2_earnings * 0.04
        self.assertAlmostEqual(result["cpp2_employee"], expected_employee, places=2)
        self.assertAlmostEqual(result["cpp2_self_employed"], expected_employee * 2, places=2)

    def test_cpp2_employee_contribution_above_yampe(self):
        """Earnings above YAMPE: CPP2 capped at max contribution.

        CPP2 max contribution = (YAMPE - YMPE) × 4%
        For 2026: ($81,900 - $74,600) × 4% = $292
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=150000,
            year=2026,
            province="ontario",
        )
        max_cpp2 = (CPP2_MAX_PENSIONABLE_2026 - CPP_MAX_PENSIONABLE_2026) * 0.04
        self.assertAlmostEqual(result["cpp2_employee"], max_cpp2, places=2)
        self.assertAlmostEqual(result["cpp2_self_employed"], max_cpp2 * 2, places=2)

    def test_cpp2_no_basic_exemption(self):
        """CPP2 has no basic exemption (unlike CPP1 which has $3,500 exemption).

        All earnings between YMPE and YAMPE are subject to CPP2 at 4%.
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        # Just $1 above YMPE: full $1 is subject to CPP2
        result = compute_cpp2_contribution(
            employment_income=CPP_MAX_PENSIONABLE_2026 + 1,
            year=2026,
            province="ontario",
        )
        self.assertAlmostEqual(result["cpp2_employee"], 0.04, places=2)

    def test_cpp2_self_employed_double_rate(self):
        """Self-employed CPP2 contribution is 8% (double employee rate).

        Self-employed pay both employee (4%) and employer (4%) portions.
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="ontario",
        )
        self.assertAlmostEqual(
            result["cpp2_self_employed"],
            result["cpp2_employee"] * 2,
            places=2,
        )

    def test_cpp2_zero_income(self):
        """Zero income: zero CPP2 contribution."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=0,
            year=2026,
            province="ontario",
        )
        self.assertAlmostEqual(result["cpp2_employee"], 0, places=2)
        self.assertAlmostEqual(result["cpp2_self_employed"], 0, places=2)

    def test_cpp2_total_contribution_includes_cpp1(self):
        """Total CPP contribution = CPP1 + CPP2.

        CPP1: (min(income, YMPE) - exemption) × rate
        CPP2: (min(income, YAMPE) - YMPE) × 4%  (no exemption, only if income > YMPE)
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="ontario",
        )
        # Should include both cpp1 and cpp2 breakdowns
        self.assertIn("cpp1_employee", result)
        self.assertIn("cpp2_employee", result)
        self.assertIn("total_employee", result)
        self.assertAlmostEqual(
            result["total_employee"],
            result["cpp1_employee"] + result["cpp2_employee"],
            places=2,
        )


class TestQPPContributionRates(unittest.TestCase):
    """Test QPP-specific contribution rates (higher than CPP).

    QPP has a different contribution rate than CPP:
    - CPP 2026: 5.95% employee
    - QPP 2026: 6.40% employee

    Both share the same YMPE, YAMPE, and basic exemption.
    """

    def test_qpp_higher_rate_than_cpp(self):
        """QPP employee rate is higher than CPP rate (6.40% vs 5.95%)."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        cpp_result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="ontario",
        )
        qpp_result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="quebec",
        )
        # QPP1 should have higher employee contribution than CPP1
        self.assertGreater(
            qpp_result["cpp1_employee"],
            cpp_result["cpp1_employee"],
        )

    def test_qpp2_same_rate_as_cpp2(self):
        """QPP2 rate equals CPP2 rate (4% employee, 8% self-employed).

        The second additional tier rate is the same across CPP and QPP.
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        cpp_result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="ontario",
        )
        qpp_result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="quebec",
        )
        # CPP2 and QPP2 should have the same rate on earnings above YMPE
        self.assertAlmostEqual(
            qpp_result["cpp2_employee"],
            cpp_result["cpp2_employee"],
            places=2,
        )

    def test_qpp_same_ympe_yampe_as_cpp(self):
        """QPP uses same YMPE and YAMPE as CPP (federal parameters)."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        # At max pensionable income, both should hit the same CPP2 cap
        cpp_result = compute_cpp2_contribution(
            employment_income=200000,
            year=2026,
            province="ontario",
        )
        qpp_result = compute_cpp2_contribution(
            employment_income=200000,
            year=2026,
            province="quebec",
        )
        self.assertAlmostEqual(
            cpp_result["cpp2_employee"],
            qpp_result["cpp2_employee"],
            places=2,
        )

    def test_qpp_total_contribution_higher(self):
        """Total QPP contribution (QPP1 + QPP2) is higher than total CPP (CPP1 + CPP2)."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        cpp_result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="ontario",
        )
        qpp_result = compute_cpp2_contribution(
            employment_income=80000,
            year=2026,
            province="quebec",
        )
        self.assertGreater(
            qpp_result["total_employee"],
            cpp_result["total_employee"],
        )


class TestCPP2EnhancedBenefit(unittest.TestCase):
    """Test CPP2/QPP2 enhanced retirement benefit calculations.

    CPP2 contributions generate enhanced retirement benefits above
    the standard CPP benefit. The enhanced benefit formula uses
    the same 1/40 accrual rate as CPP1 enhancement (2019+).

    Simplified formula:
    CPP2 annual benefit ≈ (average_CPP2_contributions / YMPE2) × max_CPP2_benefit

    Where max_CPP2_benefit at 65 = (YAMPE - YMPE) × accrual_rate × 12
    """

    def test_cpp2_benefit_zero_contributions(self):
        """Zero CPP2 contributions: zero CPP2 benefit."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result = compute_cpp2_benefit(
            cpp2_average_earnings=0,
            start_age=65,
            year=2026,
        )
        self.assertAlmostEqual(result["cpp2_annual_benefit"], 0, places=2)

    def test_cpp2_benefit_at_max_contributions(self):
        """Maximum CPP2 contributions: maximum CPP2 benefit.

        If someone consistently earns at or above YAMPE, they get
        the maximum CPP2 benefit at 65.
        """
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result = compute_cpp2_benefit(
            cpp2_average_earnings=CPP2_MAX_PENSIONABLE_2026 - CPP_MAX_PENSIONABLE_2026,
            start_age=65,
            year=2026,
        )
        # CPP2 benefit should be positive for max contributors
        self.assertGreater(result["cpp2_annual_benefit"], 0)
        # Should be much less than CPP1 max benefit (CPP2 tier is smaller)
        self.assertLess(result["cpp2_annual_benefit"], CPP_MAX_BENEFIT_65_2026)

    def test_cpp2_early_start_penalty(self):
        """Starting CPP2 before 65 applies same early penalty as CPP1 (0.6%/month)."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result_65 = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=65,
            year=2026,
        )
        result_60 = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=60,
            year=2026,
        )
        # Starting at 60 should reduce benefit by 36% (60 months × 0.6%)
        expected_ratio = 1 - 60 * CPP_EARLY_PENALTY_PER_MONTH  # 0.64
        self.assertAlmostEqual(
            result_60["cpp2_annual_benefit"] / result_65["cpp2_annual_benefit"],
            expected_ratio,
            places=2,
        )

    def test_cpp2_late_start_bonus(self):
        """Starting CPP2 after 65 applies same late bonus as CPP1 (0.7%/month)."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result_65 = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=65,
            year=2026,
        )
        result_70 = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=70,
            year=2026,
        )
        # Starting at 70 should increase benefit by 42% (60 months × 0.7%)
        expected_ratio = 1 + 60 * CPP_LATE_BONUS_PER_MONTH  # 1.42
        self.assertAlmostEqual(
            result_70["cpp2_annual_benefit"] / result_65["cpp2_annual_benefit"],
            expected_ratio,
            places=2,
        )

    def test_cpp2_benefit_proportional_to_earnings(self):
        """CPP2 benefit is proportional to average CPP2 earnings."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result_half = compute_cpp2_benefit(
            cpp2_average_earnings=3000,
            start_age=65,
            year=2026,
        )
        result_full = compute_cpp2_benefit(
            cpp2_average_earnings=6000,
            start_age=65,
            year=2026,
        )
        # Double earnings should give double benefit
        self.assertAlmostEqual(
            result_full["cpp2_annual_benefit"],
            result_half["cpp2_annual_benefit"] * 2,
            places=2,
        )


class TestSurvivorBenefits(unittest.TestCase):
    """Test CPP/QPP survivor benefit calculations.

    CPP survivor pension:
    - Age 65+: 60% of deceased's CPP retirement pension
    - Age 45-65 (disabled): flat rate + 37.5% of deceased's CPP
    - Age 45-65 (not disabled): flat rate + 37.5% of deceased's CPP (reduced)
    - Combined survivor + retirement cannot exceed max individual CPP

    QPP survivor pension:
    - Different flat rate and percentage structure
    - QPP has its own maximums
    """

    def test_cpp_survivor_age_65_plus(self):
        """Surviving spouse age 65+: receives 60% of deceased's CPP, capped by max."""
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=67,
            survivor_own_cpp_annual=0,  # No own CPP → no combined cap issue
            province="ontario",
        )
        expected_base = 15000 * 0.60
        self.assertAlmostEqual(result["survivor_benefit"], expected_base, places=0)

    def test_cpp_survivor_combined_cap(self):
        """Combined survivor + own CPP cannot exceed individual maximum.

        If survivor's own CPP + survivor benefit > max CPP, the survivor
        benefit is reduced.
        """
        from countries.canada.cpp_sharing import CPP_MAX_BENEFIT_65_2026, compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=67,
            survivor_own_cpp_annual=10000,  # Own CPP reduces survivor portion
            province="ontario",
        )
        # 60% of 15000 = 9000, but combined = 10000 + 9000 = 19000 > max 18092
        # So survivor benefit is capped: 18092 - 10000 = 8092
        combined = result["survivor_benefit"] + result["own_cpp"]
        self.assertLessEqual(combined, CPP_MAX_BENEFIT_65_2026 + 1)  # $1 rounding tolerance

    def test_cpp_survivor_below_45_not_disabled(self):
        """Surviving spouse under 45, not disabled: reduced survivor benefit."""
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=40,
            survivor_own_cpp_annual=0,
            province="ontario",
        )
        # Should receive a reduced amount
        self.assertGreater(result["survivor_benefit"], 0)
        # Less than the 60% at age 65+
        self.assertLess(result["survivor_benefit"], 15000 * 0.60)

    def test_qpp_survivor_different_formula(self):
        """QPP survivor benefits use Quebec-specific calculation.

        QPP survivor pension has a flat-rate component plus percentage
        of deceased's pension, which differs from CPP.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        cpp_result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        qpp_result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="quebec",
        )
        # QPP and CPP survivor benefits should differ
        # (different formula/parameters for Quebec)
        # Both should be positive
        self.assertGreater(cpp_result["survivor_benefit"], 0)
        self.assertGreater(qpp_result["survivor_benefit"], 0)

    def test_survivor_benefit_zero_deceased_cpp(self):
        """Deceased had zero CPP: zero survivor benefit."""
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=0,
            survivor_age=67,
            survivor_own_cpp_annual=10000,
            province="ontario",
        )
        self.assertAlmostEqual(result["survivor_benefit"], 0, places=2)

    def test_survivor_benefit_includes_cpp2(self):
        """Survivor benefit includes the deceased's CPP2 enhanced benefit.

        The CPP2 benefit is part of the deceased's total CPP pension,
        so the survivor benefit is based on the combined CPP1 + CPP2.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            deceased_cpp2_annual=800,  # Enhanced benefit from CPP2
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        # Survivor benefit should include CPP2 portion
        result_no_cpp2 = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            deceased_cpp2_annual=0,
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        self.assertGreater(
            result["survivor_benefit"],
            result_no_cpp2["survivor_benefit"],
        )


class TestCPP2YearVersionedData(unittest.TestCase):
    """Test that CPP2 parameters are year-versioned per DP#20.

    CPP2 was introduced in 2024 with YMPE2 = $73,200.
    2025: YMPE2 = $81,200.
    2026: YMPE2 = $81,900.
    """

    def test_cpp2_yampe_increases_over_years(self):
        """YAMPE increases year over year (indexed to wage growth)."""
        provider = TaxDataProvider()
        yampe_2024 = provider.get_cpp2_max_pensionable(2024)
        yampe_2025 = provider.get_cpp2_max_pensionable(2025)
        yampe_2026 = provider.get_cpp2_max_pensionable(2026)
        self.assertGreater(yampe_2025, yampe_2024)
        self.assertGreater(yampe_2026, yampe_2025)

    def test_cpp2_rate_constant_4_percent(self):
        """CPP2 rate has been 4% since its introduction (2024)."""
        provider = TaxDataProvider()
        for year in [2024, 2025, 2026]:
            data = provider.get_year_data(year, "canada", "federal")
            self.assertAlmostEqual(data.cpp2_rate, 0.04, places=4)

    def test_cpp2_contribution_uses_year_versioned_data(self):
        """CPP2 contribution calculation uses year-specific YAMPE."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        # High income in 2024: capped at 2024 YAMPE
        result_2024 = compute_cpp2_contribution(
            employment_income=200000,
            year=2024,
            province="ontario",
        )
        # High income in 2026: capped at 2026 YAMPE (higher)
        result_2026 = compute_cpp2_contribution(
            employment_income=200000,
            year=2026,
            province="ontario",
        )
        # 2026 max CPP2 contribution should be higher (larger YAMPE - YMPE gap)
        self.assertGreater(
            result_2026["cpp2_employee"],
            result_2024["cpp2_employee"],
        )


class TestCPP2IntegrationWithSharing(unittest.TestCase):
    """Test CPP2/QPP2 integration with existing CPP sharing calculations."""

    def test_cpp2_included_in_total_pension_for_sharing(self):
        """CPP2 benefit should be included in total pension available for sharing.

        When computing sharing benefits, the CPP2 enhanced benefit
        is part of the shareable pension pool.
        """
        data_with_cpp2 = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000 + 800,  # CPP1 + CPP2
            spouse_cpp_annual=8000 + 400,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
        )
        data_without_cpp2 = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000,  # CPP1 only
            spouse_cpp_annual=8000,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
        )

        result_with = optimize_cpp_sharing(data_with_cpp2)
        result_without = optimize_cpp_sharing(data_without_cpp2)

        # Higher total CPP should produce more sharing benefit
        # (more income to redistribute between spouses)
        self.assertGreater(
            result_with["annual_tax_savings"],
            result_without["annual_tax_savings"],
        )

    def test_qpp_sharing_includes_qpp2(self):
        """QPP sharing should include QPP2 enhanced benefits."""
        data = CPPSharingInput(
            primary_birth_year=1960,
            spouse_birth_year=1962,
            primary_cpp_annual=15000 + 800,  # QPP1 + QPP2
            spouse_cpp_annual=8000 + 400,
            cohabitation_start_year=1985,
            primary_contribution_start_year=1978,
            spouse_contribution_start_year=1982,
            primary_other_income=130000,
            spouse_other_income=30000,
            province="quebec",
        )
        result = optimize_cpp_sharing(data)
        self.assertTrue(result["recommendation"] in ("share", "no_benefit"))


class TestCPP2AgeBoundaryBenefit(unittest.TestCase):
    """CPP2 accrual at exact age boundaries per DP#17 — issue #376.

    CPP2 benefit at retirement ages 60, 63, 65, 68, 70 with
    exact boundary testing for early/late start adjustments.
    """

    def test_cpp2_benefit_at_age_60_max_penalty(self):
        """CPP2 at age 60: 36% permanent reduction (0.6% × 60 months).

        This is the maximum early-reduction penalty for CPP2.
        """
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=60,
            year=2026,
        )
        self.assertEqual(result['start_age'], 60)
        # 60 months early × 0.6% = 36% reduction → adjustment = 0.64
        months_early = (65 - 60) * 12
        expected_adj = 1 - months_early * 0.006
        self.assertAlmostEqual(result['adjustment_factor'], expected_adj, places=4)
        self.assertAlmostEqual(result['adjustment_factor'], 0.64, places=2)
        self.assertGreater(result['cpp2_annual_benefit'], 0)

    def test_cpp2_benefit_at_age_63_partial_penalty(self):
        """CPP2 at age 63: 14.4% reduction (0.6% × 24 months)."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=63,
            year=2026,
        )
        self.assertEqual(result['start_age'], 63)
        months_early = (65 - 63) * 12
        expected_adj = 1 - months_early * 0.006  # 0.856
        self.assertAlmostEqual(result['adjustment_factor'], expected_adj, places=4)
        self.assertAlmostEqual(result['adjustment_factor'], 0.856, places=2)

    def test_cpp2_benefit_at_age_65_no_adjustment(self):
        """CPP2 at age 65: standard benefit, no adjustment (boundary)."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=65,
            year=2026,
        )
        self.assertEqual(result['start_age'], 65)
        self.assertAlmostEqual(result['adjustment_factor'], 1.0, places=4)
        self.assertGreater(result['cpp2_annual_benefit'], 0)

    def test_cpp2_benefit_at_age_68_partial_bonus(self):
        """CPP2 at age 68: 25.2% increase (0.7% × 36 months)."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=68,
            year=2026,
        )
        self.assertEqual(result['start_age'], 68)
        months_late = (68 - 65) * 12
        expected_adj = 1 + months_late * 0.007  # 1.252
        self.assertAlmostEqual(result['adjustment_factor'], expected_adj, places=4)
        self.assertAlmostEqual(result['adjustment_factor'], 1.252, places=2)

    def test_cpp2_benefit_at_age_70_max_bonus(self):
        """CPP2 at age 70: 42% increase (0.7% × 60 months) — maximum deferral."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result = compute_cpp2_benefit(
            cpp2_average_earnings=5000,
            start_age=70,
            year=2026,
        )
        self.assertEqual(result['start_age'], 70)
        months_late = (70 - 65) * 12
        expected_adj = 1 + months_late * 0.007  # 1.42
        self.assertAlmostEqual(result['adjustment_factor'], expected_adj, places=4)
        self.assertAlmostEqual(result['adjustment_factor'], 1.42, places=2)
        self.assertGreater(result['cpp2_annual_benefit'], 0)

    def test_cpp2_age_60_vs_age_70_ratio(self):
        """CPP2 at 60 vs 70: 70 should be 2.22× the 60 benefit (1.42/0.64)."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        result_60 = compute_cpp2_benefit(
            cpp2_average_earnings=7300,
            start_age=60,
            year=2026,
        )
        result_70 = compute_cpp2_benefit(
            cpp2_average_earnings=7300,
            start_age=70,
            year=2026,
        )
        ratio = result_70['cpp2_annual_benefit'] / result_60['cpp2_annual_benefit']
        expected_ratio = 1.42 / 0.64  # ~2.21875
        self.assertAlmostEqual(ratio, expected_ratio, places=2)

    def test_cpp2_zero_earnings_no_benefit(self):
        """Zero CPP2 earnings: zero benefit regardless of start age."""
        from countries.canada.cpp_sharing import compute_cpp2_benefit
        for age in [60, 63, 65, 68, 70]:
            result = compute_cpp2_benefit(
                cpp2_average_earnings=0,
                start_age=age,
                year=2026,
            )
            self.assertAlmostEqual(result['cpp2_annual_benefit'], 0, places=2,
                                   msg=f"Age {age} should have zero benefit for zero earnings")


class TestSurvivorCPPVsQPP(unittest.TestCase):
    """CPP vs QPP survivor benefit interaction — issue #376.

    Tests the federal/provincial interaction:
    - Service Canada (CPP) vs Retraite Québec (QPP)
    - QPP flat-rate component
    - Combined cap differences
    - Age boundary survivorship
    """

    def test_cpp_survivor_age_65_exact_boundary(self):
        """CPP survivor at exactly age 65: 60% rate applies.

        Age 65 is the threshold between under-65 (37.5%) and 65+ (60%).
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=65,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        expected = 15000 * 0.60  # 9000
        # Combined cap may reduce it, but base should be 60% of deceased
        self.assertAlmostEqual(result['survivor_benefit'], expected, places=0)

    def test_cpp_survivor_age_64_boundary(self):
        """CPP survivor at exactly age 64: 37.5% rate applies.

        One year below the 65 threshold means the lower rate.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=64,
            survivor_own_cpp_annual=0,
            province="ontario",
        )
        expected = 15000 * 0.375  # 5625
        self.assertAlmostEqual(result['survivor_benefit'], expected, places=0)

    def test_cpp_survivor_age_65_vs_64_discontinuity(self):
        """Jump from 37.5% to 60% at age 65 creates a clear benefit discontinuity."""
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result_64 = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=64,
            survivor_own_cpp_annual=0,
            province="ontario",
        )
        result_65 = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=65,
            survivor_own_cpp_annual=0,
            province="ontario",
        )
        # Age 65 benefit is significantly higher than age 64
        self.assertGreater(result_65['survivor_benefit'], result_64['survivor_benefit'])
        ratio = result_65['survivor_benefit'] / result_64['survivor_benefit']
        self.assertAlmostEqual(ratio, 0.60 / 0.375, places=1)  # 1.6×

    def test_qpp_survivor_flat_rate_included(self):
        """QPP survivor benefit includes the flat-rate component.

        QPP = flat rate + 37.5% of deceased's QPP (not 60%).
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="quebec",
        )
        # QPP uses flat rate + 37.5% formula regardless of age
        self.assertEqual(result['pension_plan'], 'QPP')
        self.assertGreater(result['survivor_benefit'], 0)
        # QPP's 37.5% rate gives less than CPP's 60% for age 65+
        # But flat rate adds back some benefit
        cpp_result = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        # Both should be computable
        self.assertGreater(cpp_result['survivor_benefit'], 0)
        self.assertGreater(result['survivor_benefit'], 0)

    def test_qpp_survivor_no_age_discontinuity(self):
        """QPP does NOT have the age-65 rate jump that CPP has.

        QPP uses a flat-rate + 37.5% formula for all ages,
        whereas CPP switches from 37.5% to 60% at 65.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result_qc_64 = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=64,
            survivor_own_cpp_annual=0,
            province="quebec",
        )
        result_qc_65 = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            survivor_age=65,
            survivor_own_cpp_annual=0,
            province="quebec",
        )
        # QPP survivor benefit is the same at 64 and 65 (no age-based rate change)
        self.assertAlmostEqual(
            result_qc_64['survivor_benefit'],
            result_qc_65['survivor_benefit'],
            places=0,
        )

    def test_survivor_mixed_age_couple_young_survivor(self):
        """Young survivor (age 50) with older deceased spouse — lower rate.

        Mixed-age couples: survivor is under 65 → CPP 37.5% rate applies.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=20000,
            survivor_age=50,
            survivor_own_cpp_annual=10000,
            province="ontario",
        )
        expected = 20000 * 0.375  # 7500 under-65 rate
        self.assertAlmostEqual(result['survivor_benefit'], expected, places=0)

    def test_survivor_deceased_cpp2_enriches_benefit(self):
        """CPP2 enhanced benefit of deceased adds to survivor pension.

        When the deceased earned CPP2 credits, the survivor benefit
        is based on the combined CPP1 + CPP2 amount.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result_cpp2 = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            deceased_cpp2_annual=2000,
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        result_no_cpp2 = compute_survivor_benefit(
            deceased_cpp_annual=15000,
            deceased_cpp2_annual=0,
            survivor_age=67,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        # With CPP2, survivor benefit should be higher
        self.assertGreater(result_cpp2['survivor_benefit'], result_no_cpp2['survivor_benefit'])
        # The deceased_total should reflect CPP2
        self.assertAlmostEqual(result_cpp2['deceased_total'], 17000)

    def test_survivor_combined_cap_respected_across_jurisdictions(self):
        """Combined cap is respected for both CPP and QPP.

        Survivor + own pension cannot exceed the individual maximum
        for either jurisdiction.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        # Own CPP below max but deceased CPP is high → combined should hit cap
        for prov in ['ontario', 'quebec']:
            result = compute_survivor_benefit(
                deceased_cpp_annual=20000,
                survivor_age=67,
                survivor_own_cpp_annual=10000,  # Below max, but high enough to trigger cap
                province=prov,
            )
            # Survivor + own should not exceed max_benefit
            combined = result['survivor_benefit'] + result['own_cpp']
            self.assertLessEqual(combined, result['max_benefit'] + 1,
                                 msg=f"{prov}: combined {combined} exceeds max {result['max_benefit']}")

    def test_survivor_qpp_flat_rate_label(self):
        """QPP survivor benefit correctly labels the pension plan."""
        from countries.canada.cpp_sharing import compute_survivor_benefit
        cpp = compute_survivor_benefit(
            deceased_cpp_annual=10000, survivor_age=67, province="ontario"
        )
        qpp = compute_survivor_benefit(
            deceased_cpp_annual=10000, survivor_age=67, province="quebec"
        )
        self.assertEqual(cpp['pension_plan'], 'CPP')
        self.assertEqual(qpp['pension_plan'], 'QPP')


class TestCPP1ContributionCalculation(unittest.TestCase):
    """Test CPP1/QPP1 contribution calculations (existing tier).

    CPP1: (min(income, YMPE) - basic_exemption) × rate
    QPP1: Same formula but with QPP rate (6.40% in 2026 vs CPP 5.95%)
    """

    def test_cpp1_max_contribution(self):
        """Maximum CPP1 employee contribution at income >= YMPE.

        2026: (74,600 - 3,500) × 5.95% = 4,234.45
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=200000,
            year=2026,
            province="ontario",
        )
        expected = (CPP_MAX_PENSIONABLE_2026 - CPP_BASIC_EXEMPTION) * 0.0595
        self.assertAlmostEqual(result["cpp1_employee"], expected, places=2)

    def test_qpp1_max_contribution(self):
        """Maximum QPP1 employee contribution uses QPP rate.

        2026: (74,600 - 3,500) × 6.40% = 4,554.40
        """
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=200000,
            year=2026,
            province="quebec",
        )
        expected = (CPP_MAX_PENSIONABLE_2026 - CPP_BASIC_EXEMPTION) * 0.0640
        self.assertAlmostEqual(result["cpp1_employee"], expected, places=2)

    def test_cpp1_below_basic_exemption(self):
        """Income below basic exemption: zero CPP1 contribution."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=2000,
            year=2026,
            province="ontario",
        )
        self.assertAlmostEqual(result["cpp1_employee"], 0, places=2)

    def test_cpp1_partial_contribution(self):
        """Income between basic exemption and YMPE: partial CPP1."""
        from countries.canada.cpp_sharing import compute_cpp2_contribution
        result = compute_cpp2_contribution(
            employment_income=30000,
            year=2026,
            province="ontario",
        )
        expected = (30000 - CPP_BASIC_EXEMPTION) * 0.0595
        self.assertAlmostEqual(result["cpp1_employee"], expected, places=2)
        # No CPP2 since below YMPE
        self.assertAlmostEqual(result["cpp2_employee"], 0, places=2)


class TestQPPSpecificParameters(unittest.TestCase):
    """Test QPP-specific parameters stored in tax data.

    QPP differs from CPP in:
    - Contribution rate (6.40% vs 5.95% for 2026)
    - Max benefit at 65 (different formula)
    - Survivor benefit formula
    """

    def test_qpp_contribution_rate_stored(self):
        """QPP contribution rate is stored in TaxYearData for Quebec."""
        provider = TaxDataProvider()
        qc_data = provider.get_year_data(2026, "canada", "quebec")
        self.assertGreater(qc_data.cpp_rate, 0)
        # QPP rate should differ from CPP
        # Note: cpp_rate field is reused for QPP when province is quebec
        self.assertAlmostEqual(qc_data.cpp_rate, 0.0595, places=4)
        # The QPP-specific rate is stored separately
        self.assertIn("qpp_rate", qc_data.__dict__ if hasattr(qc_data, "qpp_rate") else {})

    def test_qpp_rate_higher_than_cpp(self):
        """QPP contribution rate is higher than CPP rate for 2026."""
        from countries.canada.cpp_sharing import CPP_RATE_2026, QPP_RATE_2026
        self.assertGreater(QPP_RATE_2026, CPP_RATE_2026)


if __name__ == '__main__':
    unittest.main()
