#!/usr/bin/env python3
"""Tests for negative amortization: when interest exceeds payment, balance grows.

Issue #68: The amortization_schedule function must model balance growth
when monthly interest exceeds the payment amount.

Canadian variable-rate mortgages can have contractually fixed payments
that don't cover interest after rate increases ("trigger rate" scenario).

Run with: python3 -m pytest tests/test_negative_amortization.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch

from countries.canada.rate_model import (
    build_rate_path, monthly_payment, amortization_schedule,
)


class TestNegativeAmortizationBalanceGrowth(unittest.TestCase):
    """When interest > payment, balance must increase each month."""

    def test_balance_grows_when_interest_exceeds_payment(self):
        """Balance $200k at 10% → monthly interest ~$1,667 > payment $500.
        Balance must grow, not stay flat."""
        path = build_rate_path("high rate", initial_rate=0.10, term_years=10,
                               rate_type="fixed", projection_years=10)
        with patch('countries.canada.rate_model.monthly_payment', return_value=500.0):
            sched = amortization_schedule(200000, path, amortization_years=25,
                                           projection_months=12)
            self.assertGreater(sched[0]['balance'], 200000,
                               "Balance should grow when interest exceeds payment")

    def test_negative_principal_reported(self):
        """When interest > payment, principal portion should be negative."""
        path = build_rate_path("neg amort", initial_rate=0.10, term_years=10,
                               rate_type="fixed", projection_years=10)
        with patch('countries.canada.rate_model.monthly_payment', return_value=500.0):
            sched = amortization_schedule(200000, path, amortization_years=25,
                                           projection_months=3)
            for entry in sched:
                self.assertLess(entry['principal'], 0,
                                 "Principal must be negative when interest > payment")

    def test_balance_increases_by_shortfall(self):
        """Balance should increase by (interest - payment) each month."""
        path = build_rate_path("neg amort", initial_rate=0.10, term_years=10,
                               rate_type="fixed", projection_years=10)
        with patch('countries.canada.rate_model.monthly_payment', return_value=500.0):
            sched = amortization_schedule(200000, path, amortization_years=25,
                                           projection_months=3)
            prev_balance = 200000
            for entry in sched:
                shortfall = entry['interest'] - entry['payment']
                expected_balance = prev_balance + shortfall
                self.assertAlmostEqual(entry['balance'], expected_balance, places=0,
                                       msg="Balance should grow by the shortfall amount")
                prev_balance = entry['balance']


class TestNegativeAmortizationRealWorldScenario(unittest.TestCase):
    """Real-world scenario from issue #68: $587k at 4.95%, $1,040/mo payment.

    Monthly interest = $587,000 × 4.95% / 12 = $2,421.63
    Monthly payment = $1,040 (fixed, doesn't cover interest)
    Shortfall = ~$1,381/month → balance grows by this each month.
    """

    def test_587k_at_495_pct_with_1040_payment(self):
        """$587k at 4.95% variable, $1,040/mo fixed payment.
        Balance should grow each month."""
        path = build_rate_path("issue68", initial_rate=0.0495, term_years=10,
                               rate_type="variable", projection_years=10)
        with patch('countries.canada.rate_model.monthly_payment', return_value=1040.0):
            sched = amortization_schedule(587000, path, amortization_years=25,
                                           projection_months=6)
            prev_balance = 587000
            for entry in sched:
                self.assertGreater(entry['interest'], entry['payment'],
                                   "Interest should exceed the fixed $1,040 payment")
                self.assertGreater(entry['balance'], prev_balance,
                                   "Balance must grow when interest > payment")
                prev_balance = entry['balance']

    def test_587k_monthly_shortfall_amount(self):
        """Monthly shortfall ≈ $1,381.63 for the issue #68 scenario."""
        path = build_rate_path("issue68", initial_rate=0.0495, term_years=10,
                               rate_type="variable", projection_years=10)
        with patch('countries.canada.rate_model.monthly_payment', return_value=1040.0):
            sched = amortization_schedule(587000, path, amortization_years=25,
                                           projection_months=3)
            entry = sched[0]
            monthly_interest = 587000 * 0.0495 / 12  # ≈ $2,421.63
            expected_shortfall = monthly_interest - 1040.0  # ≈ $1,381.63
            expected_balance = 587000 + expected_shortfall
            self.assertAlmostEqual(entry['balance'], expected_balance, places=0,
                                   msg="Balance should grow by shortfall each month")


class TestNegativeAmortizationScheduleTracking(unittest.TestCase):
    """The schedule must correctly report negative amortization months."""

    def test_principal_negative_in_schedule(self):
        """Principal column should be negative during negative amortization months."""
        path = build_rate_path("neg amort", initial_rate=0.10, term_years=10,
                               rate_type="fixed", projection_years=10)
        with patch('countries.canada.rate_model.monthly_payment', return_value=500.0):
            sched = amortization_schedule(200000, path, amortization_years=25,
                                           projection_months=3)
            for entry in sched:
                self.assertLess(entry['principal'], 0,
                                 "Principal must be negative when interest > payment")

    def test_cumulative_principal_can_be_negative(self):
        """Cumulative principal should decrease (go negative) during neg amortization."""
        path = build_rate_path("neg amort", initial_rate=0.10, term_years=10,
                               rate_type="fixed", projection_years=10)
        with patch('countries.canada.rate_model.monthly_payment', return_value=500.0):
            sched = amortization_schedule(200000, path, amortization_years=25,
                                           projection_months=6)
            self.assertLess(sched[-1]['cumulative_principal'], 0,
                            "Cumulative principal should be negative after "
                            "months of balance growth")


if __name__ == '__main__':
    unittest.main()