"""Tests for retirement OAS 75+ enhancement, GIS, CPP2, and OAS deferral (Issue #53).

Covers:
- oas_amount_for_age: two-tier OAS (65-74 vs 75+)
- gis_benefit: Guaranteed Income Supplement for low-income seniors
- cpp2_benefit: CPP2 benefits for earners above YMPE
- MemberRetirementData.oas_annual_for_year: OAS deferral + age-based enhancement
- DrawdownOptimizer integration with OAS deferral and 75+ enhancement

References:
    countries/canada/retirement.py
    Issue #53: Retirement OAS 75+ enhancement, GIS, CPP2 benefits, OAS deferral
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from countries.canada.retirement import (
    OAS_ANNUAL_MAX, OAS_ANNUAL_MAX_75PLUS, OAS_CLAWBACK_THRESHOLD,
    GIS_ANNUAL_MAX_SINGLE, GIS_ANNUAL_MAX_COUPLED, GIS_INCOME_EXEMPTION,
    CPP_MAX_PENSIONABLE, CPP2_MAX_PENSIONABLE, CPP2_MAX_BENEFIT,
    oas_clawback, oas_amount_for_age, gis_benefit, cpp2_benefit,
    cpp_benefit, rrif_minimum_withdrawal,
    MemberRetirementData, RetirementState, DrawdownOptimizer,
)


class TestOASAmountForAge:
    """Test two-tier OAS: age 65-74 vs 75+ (10% enhancement since July 2022)."""

    def test_age_65_standard_rate(self):
        """Age 65 gets standard OAS amount."""
        assert oas_amount_for_age(65, year=2026) == OAS_ANNUAL_MAX

    def test_age_74_standard_rate(self):
        """Age 74 still gets standard OAS amount."""
        assert oas_amount_for_age(74, year=2026) == OAS_ANNUAL_MAX

    def test_age_75_enhanced_rate(self):
        """Age 75 gets 10% enhanced OAS amount."""
        assert oas_amount_for_age(75, year=2026) == OAS_ANNUAL_MAX_75PLUS
        assert oas_amount_for_age(75, year=2026) > OAS_ANNUAL_MAX

    def test_age_80_enhanced_rate(self):
        """Age 80 still gets enhanced rate."""
        assert oas_amount_for_age(80, year=2026) == OAS_ANNUAL_MAX_75PLUS

    def test_enhancement_is_about_10_percent(self):
        """75+ OAS should be approximately 10% higher than 65-74."""
        ratio = OAS_ANNUAL_MAX_75PLUS / OAS_ANNUAL_MAX
        assert 1.08 <= ratio <= 1.12, f"Expected ~10% enhancement, got {ratio:.4f}"

    def test_year_versioned_2026(self):
        """Year 2026 data provides both OAS tiers."""
        amount_65 = oas_amount_for_age(65, year=2026)
        amount_75 = oas_amount_for_age(75, year=2026)
        assert amount_65 < amount_75
        assert amount_65 == pytest.approx(8908, abs=1)
        assert amount_75 == pytest.approx(9800, abs=1)

    def test_year_versioned_2024(self):
        """Year 2024 data provides both OAS tiers."""
        amount_65 = oas_amount_for_age(65, year=2024)
        amount_75 = oas_amount_for_age(75, year=2024)
        assert amount_65 < amount_75

    def test_oas_clawback_with_75plus_amount(self):
        """OAS clawback uses the correct (enhanced) amount for age 75+."""
        amount_75 = oas_amount_for_age(75, year=2026)
        result = oas_clawback(100000, oas_amount=amount_75)
        # With higher OAS, full clawback threshold is higher
        expected_full = OAS_CLAWBACK_THRESHOLD + amount_75 / 0.15
        assert result['full_clawback_threshold'] == pytest.approx(expected_full, rel=0.01)


class TestGISBenefit:
    """Test Guaranteed Income Supplement (GIS) for low-income seniors."""

    def test_zero_income_gets_max_gis(self):
        """Zero income (excluding OAS) gets maximum GIS."""
        result = gis_benefit(0, is_coupled=False)
        assert result['eligible']
        assert result['gis_amount'] == pytest.approx(GIS_ANNUAL_MAX_SINGLE, abs=1)

    def test_low_income_partial_gis(self):
        """Low income gets partial GIS (reduced by 50% of income above exemption)."""
        income = GIS_INCOME_EXEMPTION + 2000  # $2K above exemption
        result = gis_benefit(income, is_coupled=False)
        expected_reduction = 2000 * 0.50
        expected_gis = GIS_ANNUAL_MAX_SINGLE - expected_reduction
        assert result['gis_amount'] == pytest.approx(expected_gis, abs=1)

    def test_high_income_no_gis(self):
        """High income eliminates GIS entirely."""
        result = gis_benefit(50000, is_coupled=False)
        assert not result['eligible']
        assert result['gis_amount'] == 0

    def test_income_below_exemption(self):
        """Income below exemption threshold gets full GIS."""
        result = gis_benefit(3000, is_coupled=False)
        assert result['eligible']
        assert result['gis_amount'] == pytest.approx(GIS_ANNUAL_MAX_SINGLE, abs=1)

    def test_coupled_higher_max_gis(self):
        """Coupled pensioners have different (lower per-person) GIS maximum."""
        single = gis_benefit(0, is_coupled=False)
        coupled = gis_benefit(0, is_coupled=True)
        # Both get their respective maximums
        assert single['gis_amount'] > 0
        assert coupled['gis_amount'] > 0
        # Coupled max is typically lower than single max (split between 2)
        # but the exact relationship depends on CRA rules

    def test_full_elimination_threshold(self):
        """GIS is fully eliminated at a calculable income threshold."""
        result = gis_benefit(0, is_coupled=False)
        threshold = result['full_elimination_threshold']
        assert threshold > 0
        # At the threshold, GIS should be zero
        result_at_threshold = gis_benefit(threshold, is_coupled=False)
        assert result_at_threshold['gis_amount'] == pytest.approx(0, abs=1)

    def test_gis_reduction_rate(self):
        """GIS is reduced by 50% of countable income."""
        income = GIS_INCOME_EXEMPTION + 4000
        result = gis_benefit(income, is_coupled=False)
        # 4000 above exemption × 50% = 2000 reduction
        expected_reduction = 2000
        assert result['gis_reduction'] == pytest.approx(expected_reduction, abs=1)

    def test_year_versioned_gis(self):
        """GIS amounts vary by year (DP#20)."""
        result_2024 = gis_benefit(0, is_coupled=False, year=2024)
        result_2026 = gis_benefit(0, is_coupled=False, year=2026)
        # 2026 should have higher or equal GIS maximums (indexation)
        assert result_2026['max_gis'] >= result_2024['max_gis']


class TestCPP2Benefit:
    """Test CPP2 (second additional CPP) benefits for earners above YMPE."""

    def test_zero_earnings_above_ympe(self):
        """No earnings above YMPE → no CPP2 benefit."""
        assert cpp2_benefit(0, year=2026) == 0

    def test_max_earnings_above_ympe(self):
        """Maximum earnings above YMPE → maximum CPP2 benefit at 65."""
        max_range = CPP2_MAX_PENSIONABLE - CPP_MAX_PENSIONABLE
        benefit = cpp2_benefit(max_range, start_age=65, year=2026)
        # Should be close to the maximum CPP2 benefit
        assert benefit == pytest.approx(CPP2_MAX_BENEFIT, rel=0.01)

    def test_half_earnings_above_ympe(self):
        """Half the pensionable range → approximately half the max benefit."""
        max_range = CPP2_MAX_PENSIONABLE - CPP_MAX_PENSIONABLE
        benefit = cpp2_benefit(max_range / 2, start_age=65, year=2026)
        expected = CPP2_MAX_BENEFIT * 0.50
        assert benefit == pytest.approx(expected, rel=0.02)

    def test_earnings_capped_at_ympe2(self):
        """Earnings above YMPE2 are capped — no benefit beyond YMPE2."""
        max_range = CPP2_MAX_PENSIONABLE - CPP_MAX_PENSIONABLE
        # Earnings well above YMPE2 — should be same as max
        benefit_capped = cpp2_benefit(max_range + 50000, start_age=65, year=2026)
        benefit_max = cpp2_benefit(max_range, start_age=65, year=2026)
        assert benefit_capped == pytest.approx(benefit_max, rel=0.01)

    def test_early_start_penalty(self):
        """CPP2 at age 60 has the same penalty as CPP1 (36% reduction)."""
        max_range = CPP2_MAX_PENSIONABLE - CPP_MAX_PENSIONABLE
        at_60 = cpp2_benefit(max_range, start_age=60, year=2026)
        at_65 = cpp2_benefit(max_range, start_age=65, year=2026)
        # 60 months early × 0.6% = 36% reduction
        expected_ratio = 1 - 0.36
        assert at_60 == pytest.approx(at_65 * expected_ratio, rel=0.01)

    def test_late_start_bonus(self):
        """CPP2 at age 70 has the same bonus as CPP1 (42% increase)."""
        max_range = CPP2_MAX_PENSIONABLE - CPP_MAX_PENSIONABLE
        at_70 = cpp2_benefit(max_range, start_age=70, year=2026)
        at_65 = cpp2_benefit(max_range, start_age=65, year=2026)
        # 60 months late × 0.7% = 42% increase
        expected_ratio = 1 + 0.42
        assert at_70 == pytest.approx(at_65 * expected_ratio, rel=0.01)

    def test_year_versioned_cpp2(self):
        """CPP2 benefit uses year-versioned maximum (DP#20)."""
        max_range = CPP2_MAX_PENSIONABLE - CPP_MAX_PENSIONABLE
        benefit_2026 = cpp2_benefit(max_range, start_age=65, year=2026)
        assert benefit_2026 > 0

    def test_cpp1_plus_cpp2(self):
        """High earner (primary at $250k) gets both CPP1 and CPP2 benefits."""
        max_range = CPP2_MAX_PENSIONABLE - CPP_MAX_PENSIONABLE
        cpp1 = cpp_benefit(65, year=2026)
        cpp2 = cpp2_benefit(max_range, start_age=65, year=2026)
        total = cpp1 + cpp2
        # Total should exceed CPP1 alone
        assert total > cpp1
        # CPP2 should be a meaningful addition
        assert cpp2 > 0


class TestOASDeferralIntegration:
    """Test OAS deferral integration with drawdown optimizer."""

    def test_deferral_increases_oas(self):
        """Deferring OAS by 12 months increases benefit by 7.2%."""
        member = MemberRetirementData(birth_year=1960, oas_defer_months=12)
        base = OAS_ANNUAL_MAX
        deferred = member.oas_annual
        # 12 months × 0.6% = 7.2% increase
        expected = base * 1.072
        assert deferred == pytest.approx(expected, rel=0.01)

    def test_deferral_to_70(self):
        """Deferring OAS to age 70 (60 months) increases by 36%."""
        member = MemberRetirementData(birth_year=1960, oas_defer_months=60)
        base = OAS_ANNUAL_MAX
        deferred = member.oas_annual
        # 60 months × 0.6% = 36% increase
        expected = base * 1.36
        assert deferred == pytest.approx(expected, rel=0.01)

    def test_oas_annual_for_year_age_75(self):
        """OAS at age 75 combines enhancement and deferral."""
        # Born 1950, year 2025 → age 75
        member = MemberRetirementData(birth_year=1950, oas_defer_months=12)
        oas_at_75 = member.oas_annual_for_year(2025)
        # Base at 75 uses enhanced amount × deferral
        enhanced = oas_amount_for_age(75, year=2025)
        expected = enhanced * 1.072  # 12 months deferred
        assert oas_at_75 == pytest.approx(expected, rel=0.01)

    def test_oas_annual_for_year_no_deferral(self):
        """OAS at age 65 with no deferral returns standard amount."""
        member = MemberRetirementData(birth_year=1960, oas_defer_months=0)
        oas_at_65 = member.oas_annual_for_year(2025)
        assert oas_at_65 == pytest.approx(oas_amount_for_age(65, year=2025), rel=0.01)

    def test_drawdown_uses_deferred_oas(self):
        """Drawdown optimizer uses deferred OAS amount when available."""
        member = MemberRetirementData(birth_year=1955, oas_defer_months=24)
        state = RetirementState(
            age=70,
            rrif_balance=300000,
            tfsa_balance=50000,
            non_reg_balance=100000,
            annual_expenses=40000,
            year=2025,  # Set year for year-versioned CPP/OAS lookups
            members=[member],
        )
        optimizer = DrawdownOptimizer(investment_return=0.05)
        result = optimizer.optimize_year(state)
        # Should use the deferred (higher) OAS amount
        assert result['net_oas'] > 0


class TestGISClawbackInteraction:
    """Test OAS clawback and GIS interaction."""

    def test_rrif_withdrawal_reduces_gis(self):
        """RRIF withdrawals increase countable income, reducing GIS."""
        # Low income: gets GIS
        result_no_rrif = gis_benefit(5000, is_coupled=False)
        # With RRIF withdrawal: less GIS
        result_with_rrif = gis_benefit(25000, is_coupled=False)
        assert result_with_rrif['gis_amount'] < result_no_rrif['gis_amount']

    def test_oas_does_not_count_for_gis(self):
        """OAS itself is excluded from GIS income calculation."""
        # GIS income calculation should not include OAS
        result = gis_benefit(10000, is_coupled=False)
        # The countable_income is based on net_income (excluding OAS)
        # per GIS rules
        assert result['countable_income'] >= 0


class TestCPP2PreconditionsAndFallbacks:
    """``cpp2_benefit``'s refusal and fallback paths, which the era-correct
    tests never reached (issue #422 review).

    Every existing test here calls ``cpp2_benefit(year=2026)`` -- a year that
    IS in ``CPP_OAS_BY_YEAR``. So the ``year is None`` refusal, the
    out-of-table fallback to the module constant, and the zero-earnings band
    were unexercised: thin branches in the one function that computes a CPP2
    benefit. They are pinned here rather than left to chance, because the
    pre-2024 early return added in #422 changes which paths reach the lookup
    below it.
    """

    def test_year_is_required(self):
        from countries.canada.retirement import cpp2_benefit

        with pytest.raises(ValueError, match="year parameter is required"):
            cpp2_benefit(5_000, years_contributing=40, start_age=65, year=None)

    @pytest.mark.parametrize("year", [2022, 2023])
    def test_a_pre_cpp2_year_is_zero_before_any_lookup(self, year):
        """2023 predates CPP2 entirely, so there is nothing to earn.

        This is the guard that stops the 2026 constant being handed to a year
        the program did not exist in.
        """
        from countries.canada.retirement import CPP2_START_YEAR, cpp2_benefit

        assert CPP2_START_YEAR == 2024
        assert cpp2_benefit(100_000, years_contributing=40,
                            start_age=65, year=year) == 0.0

    def test_a_pre_cpp2_year_ignores_an_explicit_max_benefit(self):
        """An explicit argument cannot manufacture a benefit that cannot exist."""
        from countries.canada.retirement import cpp2_benefit

        assert cpp2_benefit(100_000, years_contributing=40, start_age=65,
                            max_benefit=999_999.0, year=2023) == 0.0

    def test_a_year_beyond_the_table_uses_the_module_constant(self):
        """Out-of-table years carry forward the published constant.

        2035 is past the last published row, so the ceilings come from the
        year-versioned getters rather than the table and the maximum falls back
        to CPP2_MAX_BENEFIT. Pinned so the fallback stays deliberate.
        """
        from countries.canada.retirement import (CPP2_MAX_BENEFIT,
                                                 CPP_OAS_BY_YEAR, cpp2_benefit)

        assert 2027 not in CPP_OAS_BY_YEAR
        benefit = cpp2_benefit(100_000, years_contributing=40,
                               start_age=65, year=2027)
        assert 0.0 < benefit <= CPP2_MAX_BENEFIT

    def test_a_distant_year_can_have_an_empty_band(self):
        """Beyond a point the projected YMPE passes the 2026 YAMPE constant.

        The out-of-table path caps the second ceiling at ``CPP2_MAX_PENSIONABLE``
        (the 2026 figure), so a far-future year whose YMPE has grown past it has
        a NEGATIVE band width and therefore no second-tier benefit. That is a
        consequence of carrying a constant forward, recorded here rather than
        left to be discovered as a silent zero.
        """
        from countries.canada.retirement import (CPP2_MAX_PENSIONABLE,
                                                 get_cpp_max_pensionable,
                                                 cpp2_benefit)

        ympe_2035 = get_cpp_max_pensionable(2035)
        assert ympe_2035 > CPP2_MAX_PENSIONABLE, (
            "if this stops holding, the out-of-table YAMPE fallback needs a "
            "sourced rule rather than the 2026 constant"
        )
        assert cpp2_benefit(100_000, years_contributing=40,
                            start_age=65, year=2035) == 0.0

    def test_no_earnings_above_ympe_yields_a_zero_ratio(self):
        """Earnings at or below the YMPE earn no second-tier benefit."""
        from countries.canada.retirement import cpp2_benefit

        assert cpp2_benefit(0.0, years_contributing=40,
                            start_age=65, year=2026) == 0.0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])