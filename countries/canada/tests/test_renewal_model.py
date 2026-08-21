"""Tests for mortgage renewal model (DP#17: every rule path tested).

Covers:
- MortgageTerm: construction, properties, contains_year, contains_month
- RenewalEvent: properties, rate_change, is_rate_increase
- monthly_payment: standard amortization, edge cases
- simulate_renewal_path: single term, multi-term, renewal events
- compare_rate_term_options: fixed vs variable comparison
- rate_sensitivity_analysis: renewal rate sensitivity

References:
    countries/canada/renewal_model.py
    Issue #62: zero test coverage
"""

import pytest
import math
from countries.canada.renewal_model import (
    MortgageTerm, RenewalEvent, RenewalPathResult,
    monthly_payment, simulate_renewal_path,
    compare_rate_term_options, rate_sensitivity_analysis,
)


class TestMortgageTerm:
    """Test MortgageTerm data class."""

    def test_term_construction(self):
        """Basic term construction."""
        term = MortgageTerm(rate=0.04, start_year=2026, term_years=5)
        assert term.rate == 0.04
        assert term.start_year == 2026
        assert term.term_years == 5

    def test_end_year(self):
        """end_year = start_year + term_years."""
        term = MortgageTerm(rate=0.04, start_year=2026, term_years=5)
        assert term.end_year == 2031

    def test_total_months(self):
        """total_months = term_years * 12."""
        term = MortgageTerm(rate=0.04, start_year=2026, term_years=5)
        assert term.total_months == 60

    def test_contains_year_inside(self):
        """Year inside the term."""
        term = MortgageTerm(rate=0.04, start_year=2026, term_years=5)
        assert term.contains_year(2026) is True
        assert term.contains_year(2028) is True
        assert term.contains_year(2030) is True

    def test_contains_year_outside(self):
        """Year outside the term."""
        term = MortgageTerm(rate=0.04, start_year=2026, term_years=5)
        assert term.contains_year(2025) is False
        assert term.contains_year(2031) is False

    def test_contains_month_inside(self):
        """Month inside the term."""
        term = MortgageTerm(rate=0.04, start_year=2026, term_years=5)
        # Month 0 = Jan 2026, should be inside
        assert term.contains_month(0, start_year=2026) is True
        # Month 59 = Dec 2030, should be inside
        assert term.contains_month(59, start_year=2026) is True

    def test_contains_month_outside(self):
        """Month outside the term."""
        term = MortgageTerm(rate=0.04, start_year=2026, term_years=5)
        # Month 60 = Jan 2031, should be outside
        assert term.contains_month(60, start_year=2026) is False
        # Month -1 = before start
        assert term.contains_month(-1, start_year=2026) is False

    def test_default_lender(self):
        """Default lender is empty string."""
        term = MortgageTerm(rate=0.04, start_year=2026)
        assert term.lender == ""


class TestRenewalEvent:
    """Test RenewalEvent data class."""

    def test_rate_change_increase(self):
        """Rate change when new rate is higher."""
        prev = MortgageTerm(rate=0.04, start_year=2026, term_years=3)
        new = MortgageTerm(rate=0.05, start_year=2029, term_years=5)
        event = RenewalEvent(year=2029, previous_term=prev, new_term=new)
        assert event.rate_change == pytest.approx(0.01)
        assert event.is_rate_increase is True

    def test_rate_change_decrease(self):
        """Rate change when new rate is lower."""
        prev = MortgageTerm(rate=0.05, start_year=2026, term_years=3)
        new = MortgageTerm(rate=0.04, start_year=2029, term_years=5)
        event = RenewalEvent(year=2029, previous_term=prev, new_term=new)
        assert event.rate_change == pytest.approx(-0.01)
        assert event.is_rate_increase is False

    def test_rate_change_same(self):
        """Rate change when new rate is same."""
        prev = MortgageTerm(rate=0.04, start_year=2026, term_years=3)
        new = MortgageTerm(rate=0.04, start_year=2029, term_years=5)
        event = RenewalEvent(year=2029, previous_term=prev, new_term=new)
        assert event.rate_change == pytest.approx(0.0)
        assert event.is_rate_increase is False

    def test_discharge_fee_and_cash_out(self):
        """Discharge fee and cash-out at renewal."""
        prev = MortgageTerm(rate=0.04, start_year=2026, term_years=3)
        new = MortgageTerm(rate=0.05, start_year=2029, term_years=5)
        event = RenewalEvent(
            year=2029, previous_term=prev, new_term=new,
            discharge_fee=500, cash_out=20000
        )
        assert event.discharge_fee == 500
        assert event.cash_out == 20000


class TestMonthlyPayment:
    """Test monthly payment calculation."""

    def test_standard_mortgage(self):
        """Standard 25-year mortgage at 5%."""
        pmt = monthly_payment(400000, 0.05, 25)
        # Expected: approximately $2,338/month
        assert 2300 < pmt < 2400

    def test_zero_balance(self):
        """Zero balance = zero payment."""
        assert monthly_payment(0, 0.05, 25) == 0.0

    def test_zero_rate(self):
        """Zero interest rate = simple division."""
        pmt = monthly_payment(300000, 0.0, 25)
        assert pmt == pytest.approx(300000 / (25 * 12))

    def test_high_rate(self):
        """High interest rate produces higher payment."""
        pmt_low = monthly_payment(400000, 0.03, 25)
        pmt_high = monthly_payment(400000, 0.08, 25)
        assert pmt_high > pmt_low

    def test_longer_amortization_lower_payment(self):
        """Longer amortization = lower payment."""
        pmt_25 = monthly_payment(400000, 0.05, 25)
        pmt_30 = monthly_payment(400000, 0.05, 30)
        assert pmt_30 < pmt_25

    def test_negative_balance(self):
        """Negative balance = zero payment."""
        assert monthly_payment(-100000, 0.05, 25) == 0.0

    def test_negative_amortization(self):
        """Negative amortization = zero payment."""
        assert monthly_payment(400000, 0.05, -1) == 0.0

    def test_one_year_amortization(self):
        """1-year amortization = nearly full balance per month."""
        pmt = monthly_payment(120000, 0.05, 1)
        # Principal alone is $10k/month, plus interest
        assert pmt > 10000


class TestSimulateRenewalPath:
    """Test simulate_renewal_path — the core simulation."""

    def test_single_term(self):
        """Single term: balance decreases over time."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=5)]
        result = simulate_renewal_path(
            initial_balance=400000, terms=terms,
            amortization_years=25, projection_years=5,
        )
        assert len(result.annual_summaries) == 5
        assert result.final_balance < 400000
        assert result.total_interest_paid > 0
        assert result.total_principal_paid > 0

    def test_two_terms_renewal(self):
        """Two terms: renewal event detected."""
        terms = [
            MortgageTerm(rate=0.04, start_year=2026, term_years=3),
            MortgageTerm(rate=0.05, start_year=2029, term_years=7),
        ]
        result = simulate_renewal_path(
            initial_balance=400000, terms=terms,
            amortization_years=25, projection_years=10,
        )
        assert len(result.renewal_events) == 1
        assert result.renewal_events[0].year == 2029
        assert result.renewal_events[0].rate_change == pytest.approx(0.01)

    def test_payment_recalculates_at_renewal(self):
        """Payment increases when rate increases at renewal."""
        terms_low = [
            MortgageTerm(rate=0.04, start_year=2026, term_years=5),
        ]
        terms_high = [
            MortgageTerm(rate=0.04, start_year=2026, term_years=3),
            MortgageTerm(rate=0.06, start_year=2029, term_years=7),
        ]
        result_low = simulate_renewal_path(400000, terms_low, 25, 10)
        result_high = simulate_renewal_path(400000, terms_high, 25, 10)
        # Higher renewal rate should mean more total interest
        assert result_high.total_interest_paid > result_low.total_interest_paid

    def test_empty_terms(self):
        """Empty terms list returns empty result."""
        result = simulate_renewal_path(400000, [], 25, 10)
        assert len(result.annual_summaries) == 0
        assert result.final_balance == 0.0

    def test_balance_decreases_each_year(self):
        """Balance should decrease each year."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=10)]
        result = simulate_renewal_path(400000, terms, 25, 10)
        for i in range(1, len(result.annual_summaries)):
            prev_balance = result.annual_summaries[i-1]['end_balance']
            curr_balance = result.annual_summaries[i]['end_balance']
            assert curr_balance < prev_balance

    def test_extra_monthly_payment(self):
        """Extra monthly payment reduces balance faster."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=10)]
        result_no_extra = simulate_renewal_path(400000, terms, 25, 10, extra_monthly=0)
        result_with_extra = simulate_renewal_path(400000, terms, 25, 10, extra_monthly=200)
        assert result_with_extra.final_balance < result_no_extra.final_balance
        assert result_with_extra.total_interest_paid < result_no_extra.total_interest_paid

    def test_projection_longer_than_term(self):
        """Projection extends beyond last term: use last known rate."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=3)]
        result = simulate_renewal_path(400000, terms, 25, 10)
        # Should use 5% rate for years after 2028
        assert len(result.annual_summaries) == 10
        assert result.annual_summaries[-1]['end_balance'] < 400000

    def test_terms_sorted_by_start_year(self):
        """Terms should be sorted by start_year regardless of input order."""
        terms = [
            MortgageTerm(rate=0.05, start_year=2029, term_years=7),
            MortgageTerm(rate=0.04, start_year=2026, term_years=3),
        ]
        result = simulate_renewal_path(400000, terms, 25, 10)
        assert result.terms[0].start_year == 2026
        assert result.terms[1].start_year == 2029

    def test_annual_summaries_have_required_fields(self):
        """Each annual summary has all required fields."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=5)]
        result = simulate_renewal_path(400000, terms, 25, 5)
        for summary in result.annual_summaries:
            assert 'year' in summary
            assert 'total_payment' in summary
            assert 'total_interest' in summary
            assert 'total_principal' in summary
            assert 'end_balance' in summary
            assert 'rate' in summary


class TestCompareRateTermOptions:
    """Test compare_rate_term_options — scenario comparison."""

    def test_two_options(self):
        """Compare two rate/term options."""
        options = [
            {'name': '3yr fixed', 'rate': 0.0404, 'term_years': 3, 'rate_type': 'fixed'},
            {'name': '5yr variable', 'rate': 0.0375, 'term_years': 5, 'rate_type': 'variable'},
        ]
        result = compare_rate_term_options(
            mortgage_balance=400000,
            amortization_years=25,
            options=options,
            projection_years=10,
        )
        assert 'options' in result
        assert 'best' in result
        assert len(result['options']) == 2
        # One option should be ranked best
        assert result['best'] is not None

    def test_total_cost_spread(self):
        """Total cost spread is non-negative when multiple options."""
        options = [
            {'name': 'low rate', 'rate': 0.03, 'term_years': 5, 'rate_type': 'fixed'},
            {'name': 'high rate', 'rate': 0.06, 'term_years': 5, 'rate_type': 'fixed'},
        ]
        result = compare_rate_term_options(400000, 25, options, 10)
        assert result['total_cost_spread'] >= 0

    def test_options_sorted_by_total_interest(self):
        """Results sorted by total interest (lowest first)."""
        options = [
            {'name': 'mid rate', 'rate': 0.045, 'term_years': 5, 'rate_type': 'fixed'},
            {'name': 'low rate', 'rate': 0.035, 'term_years': 5, 'rate_type': 'fixed'},
            {'name': 'high rate', 'rate': 0.055, 'term_years': 5, 'rate_type': 'fixed'},
        ]
        result = compare_rate_term_options(400000, 25, options, 10)
        interests = [opt['total_interest'] for opt in result['options']]
        assert interests == sorted(interests)

    def test_renewal_assumptions_custom(self):
        """Custom renewal assumptions."""
        options = [
            {'name': 'fixed', 'rate': 0.04, 'term_years': 5, 'rate_type': 'fixed'},
        ]
        result = compare_rate_term_options(
            400000, 25, options, 10,
            renewal_assumptions={'fixed': 0.06, 'variable': 0.05},
        )
        assert result['options'][0]['initial_rate'] == 0.04


class TestRateSensitivityAnalysis:
    """Test rate_sensitivity_analysis."""

    def test_default_range(self):
        """Default renewal rate range."""
        result = rate_sensitivity_analysis(
            mortgage_balance=400000,
            amortization_years=25,
            base_rate=0.04,
            term_years=5,
            projection_years=10,
        )
        assert 'sensitivity_results' in result
        assert len(result['sensitivity_results']) == 7  # Default 7 rates
        assert 'base_rate' in result
        assert 'current_monthly_payment' in result

    def test_custom_range(self):
        """Custom renewal rate range."""
        result = rate_sensitivity_analysis(
            mortgage_balance=400000,
            amortization_years=25,
            base_rate=0.04,
            term_years=5,
            projection_years=10,
            renewal_rate_range=[0.03, 0.04, 0.05],
        )
        assert len(result['sensitivity_results']) == 3

    def test_higher_rate_more_interest(self):
        """Higher renewal rate = more total interest."""
        result = rate_sensitivity_analysis(
            400000, 25, 0.04, 5, 10,
            renewal_rate_range=[0.03, 0.05, 0.07],
        )
        interests = [r['total_interest'] for r in result['sensitivity_results']]
        assert interests[0] < interests[1] < interests[2]

    def test_current_monthly_payment(self):
        """Current monthly payment is computed correctly."""
        result = rate_sensitivity_analysis(400000, 25, 0.05, 5, 10)
        expected_pmt = monthly_payment(400000, 0.05, 25)
        assert result['current_monthly_payment'] == pytest.approx(expected_pmt)


class TestRenewalPathResult:
    """Test RenewalPathResult data class."""

    def test_default_values(self):
        """Default values are correct."""
        result = RenewalPathResult()
        assert result.terms == []
        assert result.annual_summaries == []
        assert result.total_interest_paid == 0.0
        assert result.total_principal_paid == 0.0
        assert result.final_balance == 0.0
        assert result.renewal_events == []

    def test_populated_result(self):
        """Result can be populated with values."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=5)]
        result = simulate_renewal_path(400000, terms, 25, 5)
        assert len(result.terms) == 1
        assert result.total_interest_paid > 0
        assert result.total_principal_paid > 0
        assert result.final_balance < 400000


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_very_short_term(self):
        """Very short term (1 year)."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=1)]
        result = simulate_renewal_path(400000, terms, 25, 3)
        assert len(result.annual_summaries) == 3

    def test_zero_projection_years(self):
        """Zero projection years returns empty result."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=5)]
        result = simulate_renewal_path(400000, terms, 25, 0)
        # Should return empty or minimal result
        assert result.total_interest_paid == 0.0

    def test_three_term_scenario(self):
        """Three-term scenario with rate changes."""
        terms = [
            MortgageTerm(rate=0.04, start_year=2026, term_years=3),
            MortgageTerm(rate=0.05, start_year=2029, term_years=3),
            MortgageTerm(rate=0.04, start_year=2032, term_years=4),
        ]
        result = simulate_renewal_path(400000, terms, 25, 10)
        assert len(result.renewal_events) == 2
        # First renewal: rate goes up
        assert result.renewal_events[0].rate_change == pytest.approx(0.01)
        # Second renewal: rate goes down
        assert result.renewal_events[1].rate_change == pytest.approx(-0.01)

    def test_mortgage_nearly_paid_off(self):
        """Small balance nearly paid off."""
        terms = [MortgageTerm(rate=0.05, start_year=2026, term_years=5)]
        result = simulate_renewal_path(10000, terms, 25, 5)
        # Should have low total interest relative to balance
        assert result.total_interest_paid < 10000

    def test_monthly_payment_consistency(self):
        """Monthly payment should be consistent with amortization formula."""
        # P = L * r(1+r)^n / ((1+r)^n - 1)
        balance = 400000
        rate = 0.05
        years = 25
        pmt = monthly_payment(balance, rate, years)
        monthly_rate = rate / 12
        n = years * 12
        factor = (1 + monthly_rate) ** n
        expected = balance * monthly_rate * factor / (factor - 1)
        assert pmt == pytest.approx(expected)