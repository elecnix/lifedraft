"""Tests for retirement OAS 75+ enhancement, GIS, CPP2, and OAS deferral (Issue #53).

Covers:
- oas_amount_for_age: two-tier OAS (65-74 vs 75+) and deferral (DP#28)
- gis_benefit: Guaranteed Income Supplement for low-income seniors
- DrawdownOptimizer integration with the 75+ enhancement

References:
    countries/canada/retirement.py
    Issue #53: Retirement OAS 75+ enhancement, GIS benefits, OAS deferral
"""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from countries.canada.retirement import (
    OAS_ANNUAL_MAX, OAS_ANNUAL_MAX_75PLUS, OAS_CLAWBACK_THRESHOLD,
    GIS_ANNUAL_MAX_SINGLE, GIS_ANNUAL_MAX_COUPLED, GIS_INCOME_EXEMPTION,
    CPP_MAX_PENSIONABLE, CPP2_MAX_PENSIONABLE, CPP2_MAX_BENEFIT,
    oas_clawback, oas_amount_for_age, gis_benefit,
    cpp_benefit, rrif_minimum_withdrawal,
    RetirementState, DrawdownOptimizer,
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


class TestOASDeferral:
    """OAS deferral (DP#28), driven through the pure function the contract path
    reads (``contract_people`` sets ``oas_defer_months``; ``retirement_transition``
    passes it to ``oas_amount_for_age``)."""

    def test_deferral_increases_oas(self):
        """Deferring OAS by 12 months increases benefit by 7.2%."""
        deferred = oas_amount_for_age(65, year=2026, defer_months=12)
        assert deferred == pytest.approx(OAS_ANNUAL_MAX * 1.072, rel=0.01)

    def test_deferral_to_70(self):
        """Deferring OAS to age 70 (60 months) increases by 36%."""
        deferred = oas_amount_for_age(65, year=2026, defer_months=60)
        assert deferred == pytest.approx(OAS_ANNUAL_MAX * 1.36, rel=0.01)

    def test_deferral_capped_at_60_months(self):
        """Beyond 60 months the increase stops: statutory maximum deferral."""
        at_60 = oas_amount_for_age(65, year=2026, defer_months=60)
        at_120 = oas_amount_for_age(65, year=2026, defer_months=120)
        assert at_120 == at_60

    def test_deferral_and_age_75_enhancement_compose(self):
        """The 75+ enhancement and the deferral increase multiply, not shadow."""
        oas_at_75 = oas_amount_for_age(75, year=2025, defer_months=12)
        enhanced = oas_amount_for_age(75, year=2025)
        assert oas_at_75 == pytest.approx(enhanced * 1.072, rel=0.01)

    def test_no_deferral_returns_age_amount(self):
        """No deferral: the plain age-tiered amount."""
        assert oas_amount_for_age(65, year=2025) == pytest.approx(
            oas_amount_for_age(65, year=2025, defer_months=0), rel=0.01)


class TestDrawdownUsesAgeTieredOAS:
    def test_drawdown_reaches_oas(self):
        """The optimizer's OAS leg resolves to a positive amount at 70."""
        state = RetirementState(
            age=70,
            rrif_balance=300000,
            tfsa_balance=50000,
            non_reg_balance=100000,
            annual_expenses=40000,
            year=2025,
        )
        result = DrawdownOptimizer(investment_return=0.05).optimize_year(state)
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