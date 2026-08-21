#!/usr/bin/env python3
"""Tests for RRSP room cap, over-contribution penalty, AMT risk, and HBP integration.

Issue #46: RRSP contribution room cap, AMT risk detection, over-contribution
penalty, and HBP repayment tracking.

Tests cover:
1. RRSP room cap enforcement with year-versioned data (DP#20, DP#12)
2. Over-contribution penalty ($2,000 grace + 1%/month)
3. AMT risk detection for large RRSP deductions
4. HBP repayment → RRSP room impact
5. HBP missed repayment tax consequences

Run with: python3 -m pytest tests/test_issue_46_rrsp_room_amt_hbp.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.account_models import RRSPAccount
from countries.canada.hbp_rules import (
    HBPAccount, hbp_repayment_to_rrsp_room, hbp_missed_repayment_tax_impact,
)
from countries.canada.amt import AMTParameters, compute_amt, total_tax_with_amt, amt_adjusted_income
from tax_data import TaxDataProvider


class TestRRSPRoomCap(unittest.TestCase):
    """DP#20/DP#12: RRSP room cap should come from year-versioned data."""

    def test_default_cap_is_zero_uncapped(self):
        """Default annual_room_cap = 0 means uncapped (18% of income)."""
        rrsp = RRSPAccount()
        self.assertEqual(rrsp.annual_room_cap, 0.0)
        # With cap=0, room = 18% of income (uncapped)
        room = rrsp.add_annual_room(200000, year=2026)
        self.assertAlmostEqual(room, 200000 * 0.18)  # $36,000

    def test_cap_enforced_from_tax_provider(self):
        """RRSP room cap comes from TaxDataProvider.get_rrsp_limit(year)."""
        provider = TaxDataProvider()
        limit_2026 = provider.get_rrsp_limit(2026)
        self.assertGreater(limit_2026, 0, "RRSP limit for 2026 should be positive")

        rrsp = RRSPAccount()
        room = rrsp.add_annual_room(300000, year=2026, annual_cap=limit_2026)
        # 300k * 18% = 54k, but cap limits it
        self.assertAlmostEqual(room, min(300000 * 0.18, limit_2026))

    def test_cap_limits_high_income(self):
        """High income earners are capped at the annual RRSP limit."""
        rrsp = RRSPAccount(annual_room_cap=33810)  # 2026 RRSP limit
        # $500k income * 18% = $90k, but cap = $33,810
        room = rrsp.add_annual_room(500000, year=2026)
        self.assertAlmostEqual(room, 33810)

    def test_pension_adjustment_reduces_room(self):
        """Pension adjustment reduces available RRSP room."""
        rrsp = RRSPAccount(annual_room_cap=33810, pension_adjustment=5000)
        # $200k * 18% = $36k, cap = $33,810, minus PA = $28,810
        room = rrsp.add_annual_room(200000, year=2026)
        self.assertAlmostEqual(room, 33810 - 5000)

    def test_pension_adjustment_cannot_make_room_negative(self):
        """Room cannot go below zero even with large pension adjustment."""
        rrsp = RRSPAccount(annual_room_cap=33810, pension_adjustment=50000)
        room = rrsp.add_annual_room(200000, year=2026)
        self.assertGreaterEqual(room, 0)

    def test_year_versioned_cap_from_provider(self):
        """RRSP cap is year-versioned — different years have different caps."""
        provider = TaxDataProvider()
        cap_2024 = provider.get_rrsp_limit(2024)
        cap_2025 = provider.get_rrsp_limit(2025)
        cap_2026 = provider.get_rrsp_limit(2026)
        # All should be positive
        self.assertGreater(cap_2024, 0)
        self.assertGreater(cap_2025, 0)
        self.assertGreater(cap_2026, 0)

    def test_cap_accumulates_room(self):
        """Room accumulates year over year if not used."""
        rrsp = RRSPAccount(annual_room_cap=33810)
        rrsp.add_annual_room(150000, year=2026)  # $27,000
        rrsp.add_annual_room(160000, year=2027)  # min($28,800, $33,810) = $28,800
        # Total accumulated room should be $27,000 + $28,800 = $55,800
        self.assertAlmostEqual(rrsp.contribution_room, 27000 + 28800)


class TestRRSPOvercontributionPenalty(unittest.TestCase):
    """DP#2: Over-contribution penalty ($2,000 grace + 1%/month)."""

    def test_no_penalty_within_grace(self):
        """$2,000 over-contribution has no penalty (within grace amount)."""
        rrsp = RRSPAccount()
        penalty = rrsp.overcontribution_penalty(excess_amount=2000)
        self.assertAlmostEqual(penalty, 0.0)

    def test_no_penalty_when_under_grace(self):
        """Less than $2,000 over-contribution has no penalty."""
        rrsp = RRSPAccount()
        penalty = rrsp.overcontribution_penalty(excess_amount=1500)
        self.assertAlmostEqual(penalty, 0.0)

    def test_penalty_on_excess_above_grace(self):
        """Penalty = 1%/month on amount above $2,000 grace."""
        rrsp = RRSPAccount()
        # $5,000 excess - $2,000 grace = $3,000 penalty base
        # 1%/month = $30/month
        penalty = rrsp.overcontribution_penalty(excess_amount=5000, months=1)
        self.assertAlmostEqual(penalty, 3000 * 0.01 * 1)  # $30

    def test_penalty_accumulates_over_months(self):
        """Penalty accumulates for each month the excess exists."""
        rrsp = RRSPAccount()
        # $10,200 excess - $2,000 grace = $8,200 penalty base
        # 1%/month * 3 months = $246
        penalty = rrsp.overcontribution_penalty(excess_amount=10200, months=3)
        self.assertAlmostEqual(penalty, 8200 * 0.01 * 3)

    def test_zero_excess_no_penalty(self):
        """Zero excess: no penalty."""
        rrsp = RRSPAccount()
        penalty = rrsp.overcontribution_penalty(excess_amount=0)
        self.assertAlmostEqual(penalty, 0.0)

    def test_large_overcontribution_penalty(self):
        """Large over-contribution generates significant penalty."""
        rrsp = RRSPAccount()
        # $50,000 excess (way beyond $2k grace)
        # $48,000 * 1% * 12 months = $5,760/year
        penalty = rrsp.overcontribution_penalty(excess_amount=50000, months=12)
        self.assertAlmostEqual(penalty, 48000 * 0.01 * 12)  # $5,760


class TestAMTRiskDetection(unittest.TestCase):
    """AMT risk for large RRSP deductions (DP#27)."""

    def test_amt_calculation_basic(self):
        """Basic AMT calculation: compute AMT liability."""
        params = AMTParameters.for_year(2026)
        # Large RRSP deduction might trigger AMT
        result = compute_amt(
            regular_tax=25000,
            adjusted_income=500000,
            params=params,
        )
        self.assertIn('amt_owing', result)
        self.assertIn('amt_surcharge', result)

    def test_no_amt_below_exemption(self):
        """AMTI below exemption: no AMT surcharge."""
        params = AMTParameters.for_year(2026)
        result = compute_amt(
            regular_tax=15000,
            adjusted_income=100000,  # Below $180k exemption
            params=params,
        )
        # AMT base = max(0, AMTI - exemption) = 0 or small
        # If regular tax > tentative AMT, no surcharge
        self.assertGreaterEqual(result['amt_base'], 0)

    def test_a_large_rrsp_deduction_does_NOT_trigger_amt(self):
        """Was `test_large_rrsp_deduction_triggers_amt`, asserting the opposite.

        An RRSP deduction is not an AMT preference item: it appears nowhere in
        ITA s.127.52(1)'s closed list of add-backs. A $100k deduction on $250k of
        income leaves $150k of taxable income, and THAT is the AMT base — below
        the $181,440 exemption, so the minimum amount is zero.

        The old expectation passed only because `amt_adjusted_income` invented an
        RRSP add-back (#710/#754). See tests/test_issue_710_amt_base.py.
        """
        params = AMTParameters.for_year(2026)
        result = total_tax_with_amt(
            regular_tax=30000,
            taxable_income=150000,   # after the $100k deduction from $250k
            params=params,
        )
        self.assertAlmostEqual(result['adjusted_income'], 150000)
        self.assertAlmostEqual(result['amt_surcharge'], 0)

    def test_amt_parameters_year_versioned(self):
        """AMT parameters are year-versioned (DP#20)."""
        params_2023 = AMTParameters.for_year(2023)
        params_2026 = AMTParameters.for_year(2026)
        # 2024+ has much higher exemption ($173k+ vs $40k)
        self.assertLess(params_2023.exemption, params_2026.exemption)

    def test_amt_adjusted_income_does_not_add_back_rrsp(self):
        """Was `test_amt_adjusted_income_adds_back_rrsp`, asserting AMTI = 150k.

        Taxable income is already net of the RRSP deduction, and AMT leaves it
        that way. The parameter is gone entirely (DP#9: no shims), so passing it
        raises rather than silently reviving the fabrication.
        """
        self.assertAlmostEqual(amt_adjusted_income(taxable_income=120000), 120000)

        with self.assertRaises(TypeError):
            amt_adjusted_income(taxable_income=120000, rrsp_deduction=30000)


class TestHBPRepaymentRRSPIntegration(unittest.TestCase):
    """DP#46: HBP repayment tracking and RRSP room impact."""

    def test_hbp_repayment_does_not_create_room(self):
        """HBP repayment goes back to RRSP balance but does NOT create room."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        result = hbp_repayment_to_rrsp_room(
            hbp_account=hbp,
            repayment_year=2029,
            repayment_amount=2333,
        )
        self.assertAlmostEqual(result['amount_repaid'], 2333)
        self.assertIn('does NOT create new contribution room', result['note'])

    def test_hbp_missed_repayment_tax_consequence(self):
        """Missed HBP repayment is included in income and taxed."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        result = hbp_missed_repayment_tax_impact(
            hbp_account=hbp,
            missed_years=1,
            marginal_rate=0.43,
        )
        # Annual min = $60k / 15 = $4,000
        self.assertAlmostEqual(result['annual_minimum'], 4000)
        self.assertAlmostEqual(result['total_shortfall'], 4000)
        self.assertAlmostEqual(result['tax_cost'], 4000 * 0.43)

    def test_hbp_shortfall_reported(self):
        """Shortfall in repayment is reported in integration result."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        result = hbp_repayment_to_rrsp_room(
            hbp_account=hbp,
            repayment_year=2029,
            repayment_amount=2000,  # Less than minimum of $2,333
        )
        self.assertGreater(result['shortfall'], 0)
        self.assertTrue(result['shortfall_included_in_income'])

    def test_hbp_full_repayment_no_shortfall(self):
        """Full repayment: no shortfall, no income inclusion."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        result = hbp_repayment_to_rrsp_room(
            hbp_account=hbp,
            repayment_year=2029,
            repayment_amount=2500,  # Above minimum
        )
        self.assertEqual(result['shortfall'], 0)
        self.assertFalse(result['shortfall_included_in_income'])


if __name__ == '__main__':
    unittest.main()