#!/usr/bin/env python3
"""Tests for issue #54: Pension Split Optimizer should verify income type eligibility.

Federal rules (ITA s.60.03):
- Age 55-64: Can split LIF, RPP, and life annuity income only
- Age 65+: Can also split RRIF income and RRSP annuity income
- RRSP lump-sum withdrawals NEVER qualify for splitting

Quebec rules (TP-1 Schedule L):
- Age 65+: Life annuity, LIF, RRIF income qualifies
- No splitting before age 65

DP#28: Eligibility is a date-computed gate from age and income type.
DP#27: Investment income has distinct tax treatments.
"""

import pytest
from countries.canada.retirement import (
    pension_splitting_available,
    PensionIncomeType,
    _is_income_type_eligible,
)


class TestIncomeTypeEligibility:
    """Test _is_income_type_eligible for pension splitting (DP#54, DP#28)."""

    # ── Federal rules (non-Quebec) ──

    def test_lif_eligible_at_55_federal(self):
        """LIF income is eligible for splitting at age 55+ (federal)."""
        assert _is_income_type_eligible(PensionIncomeType.LIF_INCOME, 55, 'ontario') is True

    def test_lif_not_eligible_under_55_federal(self):
        """LIF income is NOT eligible for splitting under age 55 (federal)."""
        assert _is_income_type_eligible(PensionIncomeType.LIF_INCOME, 54, 'ontario') is False

    def test_rpp_eligible_at_55_federal(self):
        """RPP pension income is eligible for splitting at age 55+."""
        assert _is_income_type_eligible(PensionIncomeType.RPP_PENSION, 55, 'ontario') is True

    def test_life_annuity_eligible_at_55_federal(self):
        """Life annuity income is eligible for splitting at age 55+."""
        assert _is_income_type_eligible(PensionIncomeType.LIFE_ANNUITY, 60, 'ontario') is True

    def test_rrif_not_eligible_at_55_federal(self):
        """RRIF income is NOT eligible for splitting at age 55-64 (federal).
        
        Only eligible at 65+ per ITA s.60.03.
        """
        assert _is_income_type_eligible(PensionIncomeType.RRIF_INCOME, 55, 'ontario') is False

    def test_rrif_eligible_at_65_federal(self):
        """RRIF income IS eligible for splitting at age 65+ (federal)."""
        assert _is_income_type_eligible(PensionIncomeType.RRIF_INCOME, 65, 'ontario') is True

    def test_rrsp_annuity_not_eligible_at_55_federal(self):
        """RRSP annuity is NOT eligible at age 55-64 (federal)."""
        assert _is_income_type_eligible(PensionIncomeType.RRSP_ANNUITY, 55, 'ontario') is False

    def test_rrsp_annuity_eligible_at_65_federal(self):
        """RRSP annuity IS eligible at age 65+ (federal)."""
        assert _is_income_type_eligible(PensionIncomeType.RRSP_ANNUITY, 65, 'ontario') is True

    def test_rrsp_withdrawal_never_eligible(self):
        """RRSP lump-sum withdrawals NEVER qualify for splitting (any age)."""
        assert _is_income_type_eligible(PensionIncomeType.RRSP_WITHDRAWAL, 65, 'ontario') is False
        assert _is_income_type_eligible(PensionIncomeType.RRSP_WITHDRAWAL, 55, 'ontario') is False

    def test_non_qualifying_never_eligible(self):
        """Non-qualifying income type never qualifies for splitting."""
        assert _is_income_type_eligible(PensionIncomeType.NON_QUALIFYING, 65, 'ontario') is False

    # ── Quebec rules ──

    def test_lif_eligible_at_65_quebec(self):
        """LIF income is eligible for splitting at age 65+ (Quebec)."""
        assert _is_income_type_eligible(PensionIncomeType.LIF_INCOME, 65, 'quebec') is True

    def test_lif_not_eligible_under_65_quebec(self):
        """LIF income is NOT eligible for splitting under age 65 (Quebec).
        
        Quebec has a higher age threshold (65) than federal (55).
        """
        assert _is_income_type_eligible(PensionIncomeType.LIF_INCOME, 64, 'quebec') is False

    def test_rrif_eligible_at_65_quebec(self):
        """RRIF income is eligible at age 65+ (Quebec)."""
        assert _is_income_type_eligible(PensionIncomeType.RRIF_INCOME, 65, 'quebec') is True

    def test_rrif_not_eligible_at_60_quebec(self):
        """RRIF income is NOT eligible at age 60 in Quebec (need 65+)."""
        assert _is_income_type_eligible(PensionIncomeType.RRIF_INCOME, 60, 'quebec') is False

    def test_life_annuity_eligible_at_65_quebec(self):
        """Life annuity is eligible at age 65+ (Quebec)."""
        assert _is_income_type_eligible(PensionIncomeType.LIFE_ANNUITY, 65, 'quebec') is True

    def test_no_splitting_before_65_quebec(self):
        """No pension splitting at all before age 65 in Quebec."""
        for itype in PensionIncomeType:
            if itype != PensionIncomeType.NON_QUALIFYING:
                assert _is_income_type_eligible(itype, 64, 'quebec') is False, \
                    f"{itype.value} should not be eligible at 64 in Quebec"

    def test_rrsp_withdrawal_never_eligible_quebec(self):
        """RRSP withdrawals never qualify for splitting in Quebec."""
        assert _is_income_type_eligible(PensionIncomeType.RRSP_WITHDRAWAL, 65, 'quebec') is False


class TestPensionSplittingAvailableWithIncomeType:
    """Test pension_splitting_available with income_type parameter (DP#54)."""

    def test_rrif_at_55_with_income_type_not_available_federal(self):
        """RRIF at age 55: splitting NOT available because income type not eligible."""
        result = pension_splitting_available(
            age=55, province='ontario', income_type=PensionIncomeType.RRIF_INCOME
        )
        assert result['federal_available'] is False

    def test_rrif_at_65_with_income_type_available_federal(self):
        """RRIF at age 65: splitting IS available because age and type both eligible."""
        result = pension_splitting_available(
            age=65, province='ontario', income_type=PensionIncomeType.RRIF_INCOME
        )
        assert result['federal_available'] is True

    def test_lif_at_55_with_income_type_available_federal(self):
        """LIF at age 55: splitting IS available (eligible type, eligible age)."""
        result = pension_splitting_available(
            age=55, province='ontario', income_type=PensionIncomeType.LIF_INCOME
        )
        assert result['federal_available'] is True

    def test_rrsp_withdrawal_never_available(self):
        """RRSP withdrawal: splitting NEVER available regardless of age."""
        result = pension_splitting_available(
            age=65, province='ontario', income_type=PensionIncomeType.RRSP_WITHDRAWAL
        )
        assert result['federal_available'] is False

    def test_quebec_65_with_rrif_available(self):
        """Quebec age 65 with RRIF: splitting available."""
        result = pension_splitting_available(
            age=65, province='quebec', income_type=PensionIncomeType.RRIF_INCOME
        )
        assert result['provincial_available'] is True

    def test_quebec_64_with_lif_not_available(self):
        """Quebec age 64 with LIF: splitting NOT available (need 65+)."""
        result = pension_splitting_available(
            age=64, province='quebec', income_type=PensionIncomeType.LIF_INCOME
        )
        assert result['provincial_available'] is False

    def test_backward_compat_has_qualifying_income(self):
        """Backward compat: has_qualifying_income=True still works (DP#9)."""
        result = pension_splitting_available(
            age=65, province='quebec', has_qualifying_income=True
        )
        assert result['provincial_available'] is True

    def test_backward_compat_no_income_type(self):
        """Backward compat: no income_type parameter still works."""
        result = pension_splitting_available(age=55, province='ontario')
        assert result['federal_available'] is True

    def test_income_type_overrides_has_qualifying_income(self):
        """When both income_type and has_qualifying_income are provided,
        income_type takes precedence (because it's more specific)."""
        # RRSP withdrawal: income_type says NOT eligible, but has_qualifying_income=True
        result = pension_splitting_available(
            age=65, province='ontario',
            income_type=PensionIncomeType.RRSP_WITHDRAWAL,
            has_qualifying_income=True,  # Should NOT override the type check
        )
        assert result['federal_available'] is False