"""Tests for Quebec Interest Deduction module.

Per DP#17: Tests exercise every rule path, not just every module.
Per DP#11: Unit tests verify Quebec jurisdiction modules in isolation.
This test file is dedicated to quebec_deduction.py and lives in the
jurisdiction package per DP#10.
"""
import pytest

from countries.canada.provinces.quebec.quebec_deduction import (
    QuebecDeductionTracker,
    compute_sm_qc_benefit,
    quebec_interest_deduction,
    quebec_sm_portfolio_optimization,
)

# =============================================================================
# Pure Function Tests: quebec_interest_deduction
# =============================================================================

class TestQuebecInterestDeduction:
    """Test the pure function for Quebec interest deduction limit."""

    def test_federal_full_deduction(self):
        """Federal: full interest is deductible."""
        result = quebec_interest_deduction(10000, 3000)
        assert result['federal_deductible'] == 10000

    def test_quebec_limited_by_income(self):
        """Quebec: deduction limited to investment income."""
        result = quebec_interest_deduction(10000, 3000)
        assert result['qc_deductible'] == 3000
        assert result['new_carry_forward'] == 7000

    def test_quebec_income_exceeds_interest(self):
        """Quebec: full deduction when income exceeds interest."""
        result = quebec_interest_deduction(5000, 10000)
        assert result['qc_deductible'] == 5000
        assert result['new_carry_forward'] == 0

    def test_carry_forward_increases_limit(self):
        """Quebec: carry-forward from prior year increases this year's limit."""
        result = quebec_interest_deduction(10000, 3000, carry_forward_prior=5000)
        assert result['qc_deductible'] == 8000
        assert result['new_carry_forward'] == 2000

    def test_capital_gains_count_toward_limit(self):
        """Quebec: realized capital gains count toward deduction limit."""
        result = quebec_interest_deduction(10000, 3000, capital_gains_realized=10000)
        assert result['qc_deductible'] == 8000
        assert result['new_carry_forward'] == 2000

    def test_zero_interest_zero_deduction(self):
        """Zero interest: zero deduction."""
        result = quebec_interest_deduction(0, 3000)
        assert result['federal_deductible'] == 0
        assert result['qc_deductible'] == 0

    def test_zero_income_no_carry_max_carry_forward(self):
        """Zero income and no carry-forward: Quebec deducts nothing."""
        result = quebec_interest_deduction(10000, 0)
        assert result['qc_deductible'] == 0
        assert result['new_carry_forward'] == 10000


# =============================================================================
# Year-by-Year Tracker Tests: QuebecDeductionTracker
# =============================================================================

class TestQuebecDeductionTracker:
    """Test year-by-year Quebec deduction tracking."""

    def test_year1_creates_carry_forward(self):
        """Year 1: income deficit creates carry-forward."""
        tracker = QuebecDeductionTracker()
        result = tracker.process_year(2026, 10000, 3000)
        assert result['qc_deductible'] == 3000
        assert tracker.carry_forward == 7000

    def test_year2_uses_carry_forward(self):
        """Year 2: carry-forward from year 1 is used."""
        tracker = QuebecDeductionTracker()
        tracker.process_year(2026, 10000, 3000)
        result = tracker.process_year(2027, 10000, 10000)
        assert result['qc_deductible'] == 10000
        assert tracker.carry_forward == 0

    def test_multi_year_shortfall_tracking(self):
        """Multi-year tracking of shortfall."""
        tracker = QuebecDeductionTracker()
        tracker.process_year(2026, 8000, 2000)
        tracker.process_year(2027, 8000, 3000)
        assert tracker.total_qc_deduction_shortfall() > 0

    def test_summary_output(self):
        """Summary produces formatted output."""
        tracker = QuebecDeductionTracker()
        tracker.process_year(2026, 10000, 3000)
        summary = tracker.summary()
        assert 'QUEBEC' in summary
        assert '2026' in summary


# =============================================================================
# Strategy Optimization Tests: quebec_sm_portfolio_optimization
# =============================================================================

class TestQuebecSMPortfolioOptimization:
    """Test SM portfolio optimization for Quebec deduction."""

    def test_income_covers_interest_no_change(self):
        """When income covers interest, no change needed."""
        result = quebec_sm_portfolio_optimization(
            heloc_interest=5000,
            current_dividend_income=5000,
        )
        assert result['qc_deduction_fully_covered'] is True
        assert result['income_gap'] == 0

    def test_income_gap_triggers_recommendations(self):
        """Income gap: dividend ETF recommendation."""
        result = quebec_sm_portfolio_optimization(
            heloc_interest=10000,
            current_dividend_income=2000,
            current_interest_income=1000,
        )
        assert result['qc_deduction_fully_covered'] is False
        assert result['income_gap'] > 0
        assert len(result['options']) >= 1

    def test_zero_current_income_full_gap(self):
        """Zero current income: full gap to fill."""
        result = quebec_sm_portfolio_optimization(
            heloc_interest=8000,
        )
        assert result['qc_deduction_fully_covered'] is False
        assert result['income_gap'] == 8000


# =============================================================================
# Schedule L Line-by-Line Tests
# =============================================================================

class TestQuebecScheduleL:
    """Test Schedule L line-by-line mapping (DP#66)."""

    def test_schedule_l_lines_present(self):
        """Schedule L line items are included in the result."""
        result = quebec_interest_deduction(
            heloc_interest=10000, investment_income=3000,
            eligible_dividend_income=2000,
            interest_income=1000,
        )
        sl = result['schedule_l']
        assert sl['line_1_interest_on_borrowed_money'] == 10000
        assert sl['line_2_eligible_dividends'] == 2000
        assert sl['line_3_non_eligible_dividends'] == 0
        assert sl['line_4_interest_and_other'] == 1000
        assert sl['line_5_taxable_capital_gains'] == 0
        assert sl['line_6_net_rental_income'] == 0
        assert sl['line_7_total_investment_income'] == 3000
        assert sl['line_8_deductible_interest'] == 3000
        assert sl['line_9_carry_forward'] == 7000

    def test_schedule_l_with_capital_gains(self):
        """Schedule L correctly maps capital gains to line 5."""
        result = quebec_interest_deduction(
            heloc_interest=10000, investment_income=3000,
            capital_gains_realized=8000,
        )
        sl = result['schedule_l']
        assert sl['line_5_taxable_capital_gains'] == 4000
        assert sl['line_7_total_investment_income'] == 7000

    def test_schedule_l_with_detailed_income(self):
        """Schedule L correctly separates income types when detailed breakdown provided."""
        result = quebec_interest_deduction(
            heloc_interest=15000, investment_income=0,
            eligible_dividend_income=4000,
            non_eligible_dividend_income=1000,
            interest_income=2000,
            rental_income_net=3000,
            capital_gains_realized=10000,
        )
        sl = result['schedule_l']
        assert sl['line_2_eligible_dividends'] == 4000
        assert sl['line_3_non_eligible_dividends'] == 1000
        assert sl['line_4_interest_and_other'] == 2000
        assert sl['line_5_taxable_capital_gains'] == 5000
        assert sl['line_6_net_rental_income'] == 3000
        assert sl['line_7_total_investment_income'] == 15000
        assert sl['line_8_deductible_interest'] == 15000
        assert sl['line_9_carry_forward'] == 0


# =============================================================================
# compute_sm_qc_benefit Tests (DP#17 - ALL rule paths)
# =============================================================================

class TestComputeSMQCBenefit:
    """Test compute_sm_qc_benefit with all rule paths (DP#17)."""

    def test_basic_calculation_no_portfolio_data(self):
        """Basic calculation using default yield rate when no portfolio_data."""
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.02,
            portfolio_data=None,
        )
        assert result['readvance_interest'] == pytest.approx(5000, abs=1)
        assert result['qc_deductible'] == pytest.approx(2000, abs=1)
        assert result['readvance_tax_savings'] == pytest.approx(900, abs=1)

    def test_portfolio_data_overrides_default_yield(self):
        """portfolio_data provided uses composition yields, not default rate."""
        portfolio_data = {
            'eligible_dividends': 0.03,
            'interest': 0.01,
            'capital_gains': 0.02,
        }
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.02,
            portfolio_data=portfolio_data,
        )
        assert result['qc_deductible'] == pytest.approx(4000, abs=1)

    def test_partial_deductible_proportion(self):
        """Only investment-purpose advances are deductible."""
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=0.5,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
        )
        assert result['deductible_interest'] == pytest.approx(2500, abs=1)
        assert result['qc_deductible'] == pytest.approx(2000, abs=1)

    def test_carry_forward_consumed(self):
        """Carry-forward from prior years increases deduction limit."""
        portfolio_data = {
            'eligible_dividends': 0.02,
            'interest': 0.01,
        }
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=3000,
            marginal_rate=0.45,
            sim_year=2026,
            portfolio_data=portfolio_data,
        )
        assert result['qc_deductible'] == pytest.approx(5000, abs=1)
        assert result['qc_carry_forward'] == 0

    def test_zero_deductible_proportion(self):
        """Zero deductible proportion: no deduction even with HELOC balance."""
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=0.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
        )
        assert result['deductible_interest'] == 0
        assert result['qc_deductible'] == 0
        assert result['readvance_tax_savings'] == 0

    def test_high_yield_covers_full_interest(self):
        """High yield rate fully covers HELOC interest deduction."""
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.10,
        )
        assert result['qc_deductible'] == pytest.approx(5000, abs=1)
        assert result['qc_carry_forward'] == 0


# =============================================================================
# Carry-forward Edge Cases (DP#17)
# =============================================================================

class TestCarryForwardEdgeCases:
    """Test carry-forward edge cases required by DP#17."""

    def test_carry_forward_two_step_consumption(self):
        """Carry-forward in year 1 is fully consumed in year 2."""
        tracker = QuebecDeductionTracker()
        r1 = tracker.process_year(2026, 10000, 3000)
        assert r1['qc_deductible'] == 3000
        assert tracker.carry_forward == 7000
        r2 = tracker.process_year(2027, 10000, 10000)
        assert r2['qc_deductible'] == 10000
        assert tracker.carry_forward == 0

    def test_zero_income_maximum_carry_forward(self):
        """Zero investment income: all HELOC interest becomes carry-forward."""
        result = quebec_interest_deduction(
            heloc_interest=15000,
            investment_income=0,
            carry_forward_prior=0,
        )
        assert result['qc_deductible'] == 0
        assert result['new_carry_forward'] == 15000

    def test_rental_income_eligible(self):
        """Net rental income counts toward deduction limit (Schedule L line 6)."""
        result = quebec_interest_deduction(
            heloc_interest=10000, investment_income=0,
            rental_income_net=8000,
        )
        assert result['qc_deductible'] == 8000
        assert result['new_carry_forward'] == 2000

    def test_mixed_income_types(self):
        """Mixed income types: dividends + interest + CG + rental."""
        result = quebec_interest_deduction(
            heloc_interest=20000, investment_income=0,
            eligible_dividend_income=3000,
            non_eligible_dividend_income=1000,
            interest_income=2000,
            rental_income_net=1500,
            capital_gains_realized=10000,
        )
        sl = result['schedule_l']
        total_income = 3000 + 1000 + 2000 + 5000 + 1500
        assert sl['line_7_total_investment_income'] == total_income
        assert sl['line_8_deductible_interest'] == total_income
        assert sl['line_9_carry_forward'] == 20000 - total_income
