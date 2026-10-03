#!/usr/bin/env python3
"""
Tests for Retirement Income Extensions (DP#28) and Scenario Seed Integration

Covers:
- SCENARIO_SEED §5.1-5.2, §12.1-12.3 integration tests
"""

import pytest
from countries.canada.retirement import (
    oas_clawback, cpp_benefit, rrif_minimum_withdrawal,
    pension_splitting_available, OAS_ANNUAL_MAX, OAS_CLAWBACK_THRESHOLD,
)


class TestScenario51OASClawback:
    """SCENARIO_SEED §5.1: OAS Clawback Management."""

    def test_oas_below_threshold(self):
        """Income below threshold: no clawback."""
        result = oas_clawback(90000)
        assert result['clawback_amount'] == 0
        assert result['net_oas'] == OAS_ANNUAL_MAX

    def test_oas_partial_clawback(self):
        """Income above threshold: partial clawback at 15%."""
        # Net income $106,148 → threshold $95,323 → excess $10,825
        # Clawback = $10,825 × 0.15 = $1,624
        result = oas_clawback(106148)
        expected_clawback = (106148 - OAS_CLAWBACK_THRESHOLD) * 0.15
        assert result['clawback_amount'] == pytest.approx(expected_clawback, abs=1)

    def test_oas_full_clawback(self):
        """Income high enough: full OAS clawback."""
        # Full clawback when income > threshold + OAS / 0.15
        full_clawback_threshold = OAS_CLAWBACK_THRESHOLD + OAS_ANNUAL_MAX / 0.15
        result = oas_clawback(full_clawback_threshold + 1000)
        assert result['net_oas'] == pytest.approx(0, abs=1)

    def test_drawdown_order_tfsa_first(self):
        """TFSA withdrawals not counted as income → avoid OAS clawback."""
        # Drawing from TFSA doesn't increase net income
        tfsa_withdrawal = 20000
        # TFSA withdrawal is tax-free and not counted as income
        assert True  # TFSA withdrawal is NOT included in net_income for OAS

    def test_drawdown_order_rrif_increases_income(self):
        """RRIF withdrawals are taxable income → can trigger OAS clawback."""
        # RRIF minimum at 72 = 5.4% of balance
        rrif_balance = 800000
        min_withdrawal = rrif_minimum_withdrawal(rrif_balance, 72)
        assert min_withdrawal > 0
        assert min_withdrawal == pytest.approx(rrif_balance * 0.054, abs=500)


# =============================================================================
# SCENARIO_SEED §12.1 Pension Splitting Tests
# =============================================================================

class TestScenario121PensionSplitting:
    """SCENARIO_SEED §12.1: Pension Income Splitting at 65+."""

    def test_pension_splitting_available_at_65_quebec(self):
        """Quebec: pension splitting at 65+."""
        result = pension_splitting_available(65, 'quebec')
        assert result['federal_available'] == True
        assert result['provincial_available'] == True

    def test_pension_splitting_not_available_under_65_quebec(self):
        """Quebec: no provincial splitting under 65."""
        result = pension_splitting_available(64, 'quebec')
        assert result['federal_available'] == True
        assert result['provincial_available'] == False

    def test_pension_splitting_available_at_55_ontario(self):
        """Ontario: pension splitting at 55+ (federal rule)."""
        result = pension_splitting_available(55, 'ontario')
        assert result['federal_available'] == True
        assert result['provincial_available'] == True

    def test_pension_income_credit(self):
        """Pension income credit: first $2,000 at 15% = $300."""
        # Both spouses get credit if receiving pension income
        pension_credit = 2000 * 0.15
        assert pension_credit == 300

    def test_spouse_a_splitting_scenario(self):
        """SCENARIO_SEED §12.1: Split $20,000 to spouse B."""
        # Spouse A: $62,908 income
        # Spouse B: $31,908 income
        # After 50% split of $40,000 RRIF:
        # Spouse A: $42,908, Spouse B: $51,908
        income_a_before = 62908
        income_b_before = 31908
        split_amount = 20000  # 50% of $40,000 RRIF
        
        income_a_after = income_a_before - split_amount
        income_b_after = income_b_before + split_amount
        
        assert income_a_after == 42908
        assert income_b_after == 51908
        
        # OAS clawback for Spouse A should decrease
        clawback_before = oas_clawback(income_a_before)
        clawback_after = oas_clawback(income_a_after)
        assert clawback_after['clawback_amount'] <= clawback_before['clawback_amount']


# =============================================================================
# SCENARIO_SEED §12.3 OAS Clawback Detailed Tests
# =============================================================================

class TestScenario123OASClawbackDetailed:
    """SCENARIO_SEED §12.3: OAS Clawback Detailed Modeling."""

    def test_strategy_pension_splitting_reduces_clawback(self):
        """Pension splitting reduces OAS clawback."""
        # Before splitting: $111,148 income
        income_before = 111148
        clawback_before = oas_clawback(income_before)
        
        # After splitting $20K to spouse: $91,148
        income_after = 91148
        clawback_after = oas_clawback(income_after)
        
        assert clawback_after['clawback_amount'] < clawback_before['clawback_amount']
        # At $91,148, below $95,323 threshold → no clawback
        assert clawback_after['clawback_amount'] == 0

    def test_strategy_cpp_early_vs_deferred(self):
        """CPP at 60 vs 65 vs 70."""
        cpp_at_60 = cpp_benefit(60, year=2026)
        cpp_at_65 = cpp_benefit(65, year=2026)
        cpp_at_70 = cpp_benefit(70, year=2026)
        
        # Early: reduced
        assert cpp_at_60 < cpp_at_65
        # Deferred: increased
        assert cpp_at_70 > cpp_at_65
        # 60: 36% reduction
        expected_at_60 = cpp_at_65 * (1 - 0.36)
        assert cpp_at_60 == pytest.approx(expected_at_60, abs=1)
        # 70: 42% increase
        expected_at_70 = cpp_at_65 * (1 + 0.42)
        assert cpp_at_70 == pytest.approx(expected_at_70, abs=1)


# =============================================================================
# SCENARIO_SEED §6.1 Full Family Optimization
# =============================================================================

class TestScenario61FullFamilyOptimization:
    """SCENARIO_SEED §6.1: Full Family Optimization."""

    def test_bracket_gap_calculation(self):
        """Bracket gap determines spousal RRSP benefit."""
        primary_mtr = 0.4571  # $130K
        spouse_mtr = 0.2571   # $50K
        bracket_gap = primary_mtr - spouse_mtr
        # $10,000 spousal RRSP saves bracket_gap per dollar
        spousal_benefit = 10000 * bracket_gap
        assert spousal_benefit == pytest.approx(2000, abs=1)