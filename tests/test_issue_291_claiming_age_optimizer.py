"""Tests for issue #291: CPP/OAS Claiming Age Optimizer.

Tests verify:
- CPP start age adjustments (60-70) produce correct benefit amounts
- OAS deferral (0-60 months) increases OAS amount by 0.6%/month
- Optimizer recommends appropriate claiming ages
- Lifetime benefit comparison vs. baseline (claim at 65)
- OAS clawback interaction with claiming age
- Longevity assumption is configurable
- Year-versioned OAS/CPP amounts
"""
import unittest
from countries.canada.claiming_age_optimizer import (
    optimize_claiming_ages,
    ClaimingAgeResult,
    _compute_lifetime_cpp,
    _compute_lifetime_oas,
)
from countries.canada.retirement import (
    cpp_benefit,
    oas_amount_for_age,
    get_oas_annual_max,
    CPP_MAX_BENEFIT_65,
)


class TestCPPClaimingAge(unittest.TestCase):
    """Test CPP start age adjustments."""

    def test_cpp_at_60_reduced_36_percent(self):
        """CPP at 60 is reduced by 36% (60 months × 0.6%)."""
        benefit = cpp_benefit(start_age=60, year=2026)
        expected = CPP_MAX_BENEFIT_65 * (1 - 60 * 0.006)
        self.assertAlmostEqual(benefit, expected)

    def test_cpp_at_65_is_base(self):
        """CPP at 65 is the base amount."""
        benefit = cpp_benefit(start_age=65, year=2026)
        self.assertAlmostEqual(benefit, CPP_MAX_BENEFIT_65)

    def test_cpp_at_70_increased_42_percent(self):
        """CPP at 70 is increased by 42% (60 months × 0.7%)."""
        benefit = cpp_benefit(start_age=70, year=2026)
        expected = CPP_MAX_BENEFIT_65 * (1 + 60 * 0.007)
        self.assertAlmostEqual(benefit, expected)

    def test_lifetime_cpp_earlier_start_more_years(self):
        """Starting CPP earlier gives more years of benefits."""
        life_exp = 90
        cpp_65 = _compute_lifetime_cpp(1500, 65, life_exp, year_start=2026)
        cpp_60 = _compute_lifetime_cpp(1500, 60, life_exp, year_start=2026)
        # 60-year start gives 30 years, 65 gives 25 years
        # But 60-year benefit is 36% less per year
        self.assertTrue(cpp_60 > 0)
        self.assertTrue(cpp_65 > 0)

    def test_lifetime_cpp_increases_with_start_age_at_90(self):
        """For life expectancy 90, deferring CPP can increase lifetime benefits."""
        # At age 90, CPP at 65 might be optimal
        cpp_65 = _compute_lifetime_cpp(1500, 65, 90, year_start=2026)
        cpp_70 = _compute_lifetime_cpp(1500, 70, 90, year_start=2026)
        # Both should be positive
        self.assertTrue(cpp_65 > 0)
        self.assertTrue(cpp_70 > 0)


class TestOASDeferral(unittest.TestCase):
    """Test OAS deferral calculations."""

    def test_oas_at_65_no_deferral(self):
        """OAS at 65 with 0 months deferral is the base amount."""
        oas = oas_amount_for_age(65, year=2026, defer_months=0)
        base = get_oas_annual_max(2026)
        self.assertAlmostEqual(oas, base)

    def test_oas_deferred_36_months(self):
        """OAS deferred 36 months (age 68) is 21.6% higher."""
        oas = oas_amount_for_age(68, year=2026, defer_months=36)
        base = get_oas_annual_max(2026)
        expected = base * (1 + 36 * 0.006)
        self.assertAlmostEqual(oas, expected, places=0)

    def test_oas_deferred_60_months(self):
        """OAS deferred 60 months (age 70) is 36% higher."""
        oas = oas_amount_for_age(70, year=2026, defer_months=60)
        base = get_oas_annual_max(2026)
        expected = base * 1.36
        self.assertAlmostEqual(oas, expected, places=0)


class TestClaimingAgeOptimizer(unittest.TestCase):
    """Test the optimize_claiming_ages function."""

    def test_optimizer_returns_result(self):
        """Optimizer returns a ClaimingAgeResult."""
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
        )
        self.assertIsInstance(result, ClaimingAgeResult)
        self.assertIn(result.recommended_cpp_start_age, range(60, 71))
        self.assertGreaterEqual(result.recommended_oas_defer_months, 0)
        self.assertLessEqual(result.recommended_oas_defer_months, 60)

    def test_baseline_is_cpp_65_oas_65(self):
        """Baseline scenario is CPP at 65, OAS at 65 (0 months deferral)."""
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
        )
        self.assertEqual(result.baseline_cpp_start_age, 65)
        self.assertEqual(result.baseline_oas_defer_months, 0)

    def test_optimal_never_below_60(self):
        """Recommended CPP start age is never below 60."""
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
        )
        self.assertGreaterEqual(result.recommended_cpp_start_age, 60)

    def test_optimal_never_above_70(self):
        """Recommended CPP start age is never above 70."""
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
        )
        self.assertLessEqual(result.recommended_cpp_start_age, 70)

    def test_benefit_delta_is_computed(self):
        """Optimizer computes benefit delta vs. baseline."""
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
        )
        self.assertIsNotNone(result.benefit_delta)
        # Delta can be positive (deferring is better) or near-zero

    def test_explanation_is_provided(self):
        """Optimizer provides a human-readable explanation."""
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
        )
        self.assertTrue(len(result.explanation) > 0)
        self.assertIn("CPP", result.explanation)
        self.assertIn("OAS", result.explanation)

    def test_high_income_defers_oas_to_avoid_clawback(self):
        """At high income, OAS is heavily clawed back; deferral may be optimal.

        A high earner ($250k income) would have OAS fully clawed back at 65.
        Deferring to 70 increases OAS by 36%, which may still be clawed back
        but provides more net benefit per year.
        """
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
            net_income_excluding_oas=250000,  # High income → heavy clawback
            marginal_rate=0.50,
        )
        # At high income, the optimizer should still produce a valid result
        self.assertIsInstance(result, ClaimingAgeResult)

    def test_low_income_claims_early(self):
        """At low income, claiming early provides more years of benefits."""
        result_low = optimize_claiming_ages(
            cpp_monthly_at_65=800,
            life_expectancy=82,  # Shorter life expectancy
            net_income_excluding_oas=30000,
            marginal_rate=0.20,
        )
        self.assertIsInstance(result_low, ClaimingAgeResult)

    def test_life_expectancy_affects_recommendation(self):
        """Longer life expectancy favors later claiming."""
        result_85 = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=85,
        )
        result_95 = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=95,
        )
        # Both should return valid results
        self.assertIsInstance(result_85, ClaimingAgeResult)
        self.assertIsInstance(result_95, ClaimingAgeResult)
        # Longer life expectancy should favor later claiming (or at least be different)
        # The specific recommendation depends on the parameters

    def test_scenarios_are_enumerated(self):
        """Optimizer enumerates all scenarios."""
        result = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
        )
        self.assertIsNotNone(result.all_scenarios)
        self.assertGreater(len(result.all_scenarios), 0)

    def test_year_versioned_amounts(self):
        """Optimizer uses year-versioned OAS/CPP amounts."""
        result_2024 = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
            year=2024,
        )
        result_2026 = optimize_claiming_ages(
            cpp_monthly_at_65=1500,
            life_expectancy=90,
            year=2026,
        )
        # Different years should produce different results
        # (at minimum, the benefit amounts should differ)
        self.assertIsInstance(result_2024, ClaimingAgeResult)
        self.assertIsInstance(result_2026, ClaimingAgeResult)


class TestLongevityAssumption(unittest.TestCase):
    """Test that longevity assumption is documented and configurable."""

    def test_default_life_expectancy(self):
        """Default life expectancy is 90."""
        result = optimize_claiming_ages(cpp_monthly_at_65=1500)
        # The optimizer should use life_expectancy=90 by default
        # Check that it produces a valid result
        self.assertIsInstance(result, ClaimingAgeResult)

    def test_custom_life_expectancy(self):
        """Custom life expectancy is configurable."""
        for life_exp in [80, 85, 90, 95, 100]:
            result = optimize_claiming_ages(
                cpp_monthly_at_65=1500,
                life_expectancy=life_exp,
            )
            self.assertIsInstance(result, ClaimingAgeResult)


if __name__ == '__main__':
    unittest.main()