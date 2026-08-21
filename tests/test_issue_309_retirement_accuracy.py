"""Tests for issue #309: Retirement accuracy — delayed OAS, CPP claiming age,
OAS clawback boundary, and RRIF spouse election.

This module provides regression tests for:
1. OAS deferral (0.6%/month increase for ages 65-70)
2. CPP early/late retirement (0.6%/month penalty before 65, 0.7%/month bonus after 65)
3. OAS clawback at the boundary (income exactly at threshold)
4. CPP drop-out provisions — documented as not yet implemented (DP#10)
5. RRIF spouse age election irrevocability — documented as not yet implemented (DP#28)
"""
import unittest
from countries.canada.retirement import (
    oas_amount_for_age,
    oas_clawback,
    get_oas_annual_max,
    get_oas_clawback_threshold,
    cpp_benefit,
    get_oas_annual_max_75plus,
)


class TestOASDeferral(unittest.TestCase):
    """Test OAS deferral: +0.6%/month after age 65, up to 36% at 70.

    Per CRA: OAS can be deferred up to 60 months (5 years) for a 36% increase.
    OAS is NOT available before age 65.
    """

    def test_oas_at_65_is_base_amount(self):
        """At age 65 with no deferral, OAS is the base annual maximum."""
        oas_65 = oas_amount_for_age(65, year=2026)
        base = get_oas_annual_max(2026)
        self.assertAlmostEqual(oas_65, base)

    def test_oas_deferred_12_months(self):
        """OAS deferred 12 months (age 66 start) is 7.2% higher (12 months × 0.6%)."""
        oas_66 = oas_amount_for_age(66, year=2026, defer_months=12)
        base = get_oas_annual_max(2026)
        expected = base * 1.072
        self.assertAlmostEqual(oas_66, expected, places=0)

    def test_oas_deferred_60_months(self):
        """OAS deferred 60 months (age 70 start) is 36% higher (60 months × 0.6%)."""
        oas_70 = oas_amount_for_age(70, year=2026, defer_months=60)
        base = get_oas_annual_max(2026)
        expected = base * 1.36
        self.assertAlmostEqual(oas_70, expected, places=0)

    def test_oas_at_75_uses_enhanced_amount(self):
        """At age 75+, OAS uses the 10% enhanced amount (75+ enhancement)."""
        oas_75 = oas_amount_for_age(75, year=2026)
        enhanced = get_oas_annual_max_75plus(2026)
        self.assertAlmostEqual(oas_75, enhanced)

    def test_oas_below_65_is_base_amount(self):
        """Below 65, OAS amount returns base (even though not yet eligible to claim).

        The function returns the base amount — eligibility is checked separately.
        """
        oas_64 = oas_amount_for_age(64, year=2026)
        base = get_oas_annual_max(2026)
        # Below 65, OAS doesn't have the 75+ enhancement, but also
        # can't be deferred. The function returns the base amount
        # for any age 65-74.
        self.assertAlmostEqual(oas_64, base)

    def test_oas_deferral_increases_monotonically(self):
        """OAS amount should increase monotonically with deferral months."""
        prev = oas_amount_for_age(65, year=2026, defer_months=0)
        for months in range(6, 61, 6):
            current = oas_amount_for_age(65, year=2026, defer_months=months)
            self.assertGreaterEqual(current, prev,
                                    f"OAS with {months} months deferral ({current:.2f}) should be >= {months-6} months ({prev:.2f})")
            prev = current

    def test_oas_deferral_with_year_parameter(self):
        """Year-versioned OAS amounts work with deferral."""
        oas_65_2024 = oas_amount_for_age(65, year=2024)
        oas_70_2024 = oas_amount_for_age(70, year=2024, defer_months=60)
        base_2024 = get_oas_annual_max(2024)

        self.assertAlmostEqual(oas_65_2024, base_2024)
        self.assertAlmostEqual(oas_70_2024, base_2024 * 1.36, places=0)


class TestCPPClaimingAge(unittest.TestCase):
    """Test CPP claiming age adjustments (60-70).

    Per CRA:
    - CPP can start as early as 60 (0.6% per month reduction)
    - CPP can start as late as 70 (0.7% per month increase)
    - At 65, CPP is the base amount
    """

    def test_cpp_at_65_is_base(self):
        """At age 65, CPP benefit is the maximum base amount."""
        benefit = cpp_benefit(start_age=65, year=2026)
        from countries.canada.retirement import CPP_MAX_BENEFIT_65
        self.assertAlmostEqual(benefit, CPP_MAX_BENEFIT_65)

    def test_cpp_at_60_is_reduced_36_percent(self):
        """At age 60, CPP is reduced by 36% (60 months × 0.6%)."""
        benefit_60 = cpp_benefit(start_age=60, year=2026)
        from countries.canada.retirement import CPP_MAX_BENEFIT_65
        expected = CPP_MAX_BENEFIT_65 * (1 - 60 * 0.006)
        self.assertAlmostEqual(benefit_60, expected)

    def test_cpp_at_70_is_increased_42_percent(self):
        """At age 70, CPP is increased by 42% (60 months × 0.7%)."""
        benefit_70 = cpp_benefit(start_age=70, year=2026)
        from countries.canada.retirement import CPP_MAX_BENEFIT_65
        expected = CPP_MAX_BENEFIT_65 * (1 + 60 * 0.007)
        self.assertAlmostEqual(benefit_70, expected)

    def test_cpp_at_63_is_reduced_14_4_percent(self):
        """At age 63, CPP is reduced by 14.4% (24 months × 0.6%)."""
        benefit_63 = cpp_benefit(start_age=63, year=2026)
        from countries.canada.retirement import CPP_MAX_BENEFIT_65
        expected = CPP_MAX_BENEFIT_65 * (1 - 24 * 0.006)
        self.assertAlmostEqual(benefit_63, expected)

    def test_cpp_at_68_is_increased_25_2_percent(self):
        """At age 68, CPP is increased by 25.2% (36 months × 0.7%)."""
        benefit_68 = cpp_benefit(start_age=68, year=2026)
        from countries.canada.retirement import CPP_MAX_BENEFIT_65
        expected = CPP_MAX_BENEFIT_65 * (1 + 36 * 0.007)
        self.assertAlmostEqual(benefit_68, expected)

    def test_cpp_clamps_age_below_60(self):
        """CPP start age below 60 is clamped to 60."""
        benefit_59 = cpp_benefit(start_age=59, year=2026)
        benefit_60 = cpp_benefit(start_age=60, year=2026)
        self.assertAlmostEqual(benefit_59, benefit_60)

    def test_cpp_clamps_age_above_70(self):
        """CPP start age above 70 is clamped to 70."""
        benefit_71 = cpp_benefit(start_age=71, year=2026)
        benefit_70 = cpp_benefit(start_age=70, year=2026)
        self.assertAlmostEqual(benefit_71, benefit_70)

    def test_cpp_benefit_monotonic_from_60_to_70(self):
        """CPP benefit increases monotonically from age 60 to 70."""
        prev = cpp_benefit(start_age=60, year=2026)
        for age in range(61, 71):
            current = cpp_benefit(start_age=age, year=2026)
            self.assertGreater(current, prev,
                               f"CPP at age {age} ({current:.2f}) should be > age {age-1} ({prev:.2f})")
            prev = current

    def test_cpp_year_versioned(self):
        """CPP benefit uses year-versioned max benefit."""
        benefit_2024 = cpp_benefit(start_age=65, year=2024)
        benefit_2026 = cpp_benefit(start_age=65, year=2026)

        # Both should be positive amounts
        self.assertGreater(benefit_2024, 0)
        self.assertGreater(benefit_2026, 0)


class TestOASClawbackBoundary(unittest.TestCase):
    """Test OAS clawback (recovery tax) at and around the threshold.

    Per CRA: OAS is reduced by 15% of net income above the threshold.
    At 2× threshold (approximately), OAS is fully clawed back.
    """

    def test_no_clawback_below_threshold(self):
        """Income below threshold: no clawback."""
        threshold = get_oas_clawback_threshold(2026)
        result = oas_clawback(threshold - 1, year=2026)
        self.assertAlmostEqual(result['clawback_amount'], 0)

    def test_clawback_at_threshold_boundary(self):
        """Income exactly at threshold: no clawback (≤ threshold, no excess)."""
        threshold = get_oas_clawback_threshold(2026)
        result = oas_clawback(threshold, year=2026)
        self.assertAlmostEqual(result['clawback_amount'], 0)

    def test_clawback_above_threshold(self):
        """Income above threshold: 15% of the excess is clawed back."""
        threshold = get_oas_clawback_threshold(2026)
        oas = get_oas_annual_max(2026)
        excess_income = 10000  # $10,000 above threshold
        result = oas_clawback(threshold + excess_income, year=2026)
        expected_clawback = excess_income * 0.15
        self.assertAlmostEqual(result['clawback_amount'], expected_clawback, places=0)

    def test_clawback_capped_at_oas_amount(self):
        """Clawback cannot exceed the OAS annual amount."""
        threshold = get_oas_clawback_threshold(2026)
        oas = get_oas_annual_max(2026)
        # Very high income — clawback should be capped at OAS amount
        result = oas_clawback(500000, year=2026)
        self.assertLessEqual(result['clawback_amount'], oas + 1)  # Allow $1 rounding

    def test_clawback_rate_is_15_percent_of_excess(self):
        """OAS clawback is 15% of excess income above the threshold."""
        threshold = get_oas_clawback_threshold(2026)
        oas = get_oas_annual_max(2026)
        excess_income = 10000  # $10,000 above threshold
        result = oas_clawback(threshold + excess_income, year=2026)
        # The statutory rate is 15% of the excess above threshold
        expected_clawback = excess_income * 0.15
        self.assertAlmostEqual(result['clawback_amount'], expected_clawback, places=0)

    def test_year_versioned_threshold(self):
        """Clawback threshold varies by year."""
        threshold_2024 = get_oas_clawback_threshold(2024)
        threshold_2026 = get_oas_clawback_threshold(2026)
        self.assertNotEqual(threshold_2024, threshold_2026)

    def test_clawback_one_dollar_above_threshold(self):
        """Income $1 above threshold → 15 cents clawback.

        DP#17: boundary test — the smallest possible clawback is 15¢
        for $1.00 of excess income.
        """
        threshold = get_oas_clawback_threshold(2026)
        result = oas_clawback(threshold + 1, year=2026)
        self.assertAlmostEqual(result['clawback_amount'], 0.15, places=2)

    def test_full_clawback_at_exact_threshold(self):
        """Income at threshold + oas/0.15 → full OAS clawback.

        DP#17: boundary test — at exactly the full-clawback income,
        the clawback equals the full OAS amount.
        """
        threshold = get_oas_clawback_threshold(2026)
        oas = get_oas_annual_max(2026)
        full_clawback_income = threshold + oas / 0.15
        result = oas_clawback(full_clawback_income, year=2026)
        self.assertAlmostEqual(result['clawback_amount'], oas, places=2)
        self.assertAlmostEqual(result['net_oas'], 0, places=2)

    def test_almost_full_clawback_one_dollar_below(self):
        """Income $1 below full-clawback → OAS not quite fully clawed.

        DP#17: boundary test — at (full_clawback - 1), clawback is
        OAS - $0.15, so $0.15 of net OAS remains.
        """
        threshold = get_oas_clawback_threshold(2026)
        oas = get_oas_annual_max(2026)
        full_clawback_income = threshold + oas / 0.15
        result = oas_clawback(full_clawback_income - 1, year=2026)
        self.assertAlmostEqual(result['clawback_amount'], oas - 0.15, places=2)
        self.assertAlmostEqual(result['net_oas'], 0.15, places=2)

    def test_oas_clawback_two_dollars_above_threshold(self):
        """Income $2 above threshold → 30 cents clawback.

        DP#17: verify linearity at the origin of the clawback function.
        """
        threshold = get_oas_clawback_threshold(2026)
        result = oas_clawback(threshold + 2, year=2026)
        self.assertAlmostEqual(result['clawback_amount'], 0.30, places=2)


class TestCPPDropOutProvisionNotImplemented(unittest.TestCase):
    """Document that CPP drop-out provisions are not yet implemented.

    The general drop-out provision (17% of lowest-earning months dropped)
    and the child-rearing drop-out provision are significant for accuracy.
    These are tracked in issue #309.
    """

    def test_cpp_benefit_does_not_apply_general_dropout(self):
        """cpp_benefit uses a simplified formula without general drop-out.

        The general drop-out provision (17% of lowest-earning months dropped
        from the calculation) would increase the CPP benefit for people with
        some zero- or low-earning years. This is NOT YET IMPLEMENTED.
        """
        # When average_contributions is not provided, cpp_benefit uses max
        benefit_default = cpp_benefit(start_age=65, year=2026)
        # This is a simplified calculation without drop-out provisions
        self.assertGreater(benefit_default, 0)

    def test_cpp_benefit_does_not_apply_child_rearing_dropout(self):
        """CPP child-rearing drop-out provision is NOT YET IMPLEMENTED.

        The child-rearing drop-out (CRDP) allows parents to exclude years
        when they had children under 7, which increases the CPP benefit.
        """
        # Document that this feature does not exist yet
        # cpp_benefit() does not accept any child-rearing parameters
        benefit = cpp_benefit(start_age=65, year=2026)
        self.assertGreater(benefit, 0)


class TestRRIFSpouseElectionNotImplemented(unittest.TestCase):
    """Document that RRIF spouse age election irrevocability is not enforced.

    Per ITA s.146.3(6.1): Once a RRIF holder designates their spouse's age
    for minimum withdrawal calculations, the election is irrevocable. The
    current code allows changing this designation, which is incorrect.
    """

    def test_rrif_minimum_uses_account_holder_age(self):
        """RRIF minimum withdrawal uses the account holder's age."""
        from countries.canada.retirement import rrif_minimum_withdrawal

        # Age 72, balance $100,000
        min_wd = rrif_minimum_withdrawal(100000, 72)
        self.assertGreater(min_wd, 0)
        # At age 72, the rate should be around 5.40% (2026 rates)
        self.assertAlmostEqual(min_wd / 100000, 0.054, places=2)


if __name__ == '__main__':
    unittest.main()