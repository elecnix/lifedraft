#!/usr/bin/env python3
"""Tests for issue #48: Year-versioned TFSA/RRSP limits, withdrawal room recovery, and TFSA penalties.

DP#20: TFSA and RRSP limits should come from tax_data.py, not hardcoded constants.
DP#10: TFSA withdrawal room recovery should be tracked.
DP#12: RRSP cap should be populated from year-versioned tax data.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/test_issue_48.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from countries.canada.account_models import RRSPAccount, TSFAccount, NonRegAccount
from tax_data import TaxDataProvider


class TestTFSAYearVersioned(unittest.TestCase):
    """Test that TFSA limits come from year-versioned tax_data (DP#20)."""

    def test_tfsa_limit_2023(self):
        """2023 TFSA limit is $6,500."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_tfsa_limit(2023), 6500)

    def test_tfsa_limit_2024(self):
        """2024 TFSA limit is $7,000."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_tfsa_limit(2024), 7000)

    def test_tfsa_limit_2025(self):
        """2025 TFSA limit is $7,000."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_tfsa_limit(2025), 7000)

    def test_tfsa_limit_2026(self):
        """2026 TFSA limit is $7,000."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_tfsa_limit(2026), 7000)

    def test_tfsa_add_annual_room_with_year(self):
        """TSFAccount.add_annual_room can accept year-specific limit from TaxDataProvider."""
        provider = TaxDataProvider()
        tfsa = TSFAccount()
        # 2023: $6,500 limit
        added = tfsa.add_annual_room(year=2023, annual_limit=provider.get_tfsa_limit(2023))
        self.assertAlmostEqual(added, 6500)
        self.assertAlmostEqual(tfsa.contribution_room, 6500)

    def test_tfsa_limits_vary_across_years(self):
        """TFSA limits are not a constant — they change across years."""
        provider = TaxDataProvider()
        limit_2023 = provider.get_tfsa_limit(2023)
        limit_2024 = provider.get_tfsa_limit(2024)
        # 2023 was $6,500, 2024+ is $7,000
        self.assertNotEqual(limit_2023, limit_2024,
                            "TFSA limits should differ across years (DP#20)")


class TestTFSAWithdrawalRoomRecovery(unittest.TestCase):
    """Test TFSA withdrawal room recovery (DP#10).

    CRA rule: withdrawals from TFSA add to contribution room
    at the beginning of the NEXT calendar year.
    """

    def test_withdrawal_tracked_for_recovery(self):
        """TSFAccount tracks withdrawals for next-year room recovery."""
        tfsa = TSFAccount(balance=10000, contribution_room=0)
        tfsa.withdraw(5000)
        # The withdrawal should be tracked for next-year room recovery
        self.assertGreater(tfsa.withdrawals_pending_recovery, 0,
                          "Withdrawals should be tracked for room recovery")
        self.assertAlmostEqual(tfsa.withdrawals_pending_recovery, 5000)

    def test_recovery_applied_next_year(self):
        """Withdrawals add to room at the start of the next year."""
        tfsa = TSFAccount(balance=10000, contribution_room=0)
        tfsa.withdraw(5000)
        # Before recovery: room = 0
        self.assertAlmostEqual(tfsa.contribution_room, 0)
        # Apply annual room (new year) — should include both new limit AND recovery
        provider = TaxDataProvider()
        tfsa.add_annual_room(year=2024, annual_limit=provider.get_tfsa_limit(2024))
        # Room should be $7,000 (2024 limit) + $5,000 (recovery) = $12,000
        self.assertAlmostEqual(tfsa.contribution_room, 12000)

    def test_no_recovery_when_no_withdrawal(self):
        """No room recovery when no withdrawals occurred."""
        tfsa = TSFAccount(balance=10000, contribution_room=0)
        # No withdrawal
        provider = TaxDataProvider()
        tfsa.add_annual_room(year=2024, annual_limit=provider.get_tfsa_limit(2024))
        # Room should be just the annual limit
        self.assertAlmostEqual(tfsa.contribution_room, 7000)

    def test_multiple_withdrawals_accumulate(self):
        """Multiple withdrawals accumulate for recovery."""
        tfsa = TSFAccount(balance=20000, contribution_room=0)
        tfsa.withdraw(5000)
        tfsa.withdraw(3000)
        self.assertAlmostEqual(tfsa.withdrawals_pending_recovery, 8000)

    def test_withdrawal_recovery_resets_after_new_year(self):
        """After recovery is applied, the pending recovery resets."""
        tfsa = TSFAccount(balance=15000, contribution_room=0)
        tfsa.withdraw(5000)
        provider = TaxDataProvider()
        tfsa.add_annual_room(year=2024, annual_limit=provider.get_tfsa_limit(2024))
        # Recovery applied, should reset
        self.assertAlmostEqual(tfsa.withdrawals_pending_recovery, 0)
        # Room = 7000 (new limit) + 5000 (recovery)
        self.assertAlmostEqual(tfsa.contribution_room, 12000)


class TestTFSAOvercontributionPenalty(unittest.TestCase):
    """Test TFSA over-contribution penalty (1%/month on excess)."""

    def test_overcontribution_penalty(self):
        """TSFAccount can calculate over-contribution penalty."""
        tfsa = TSFAccount(balance=0, contribution_room=5000)
        excess = 2000  # Contributing $7,000 when room is $5,000
        penalty = tfsa.overcontribution_penalty(excess, months=1)
        # CRA: 1% per month on the full excess (no grace amount like RRSP)
        self.assertAlmostEqual(penalty, 2000 * 0.01 * 1)

    def test_no_penalty_within_room(self):
        """No penalty when contribution is within room."""
        tfsa = TSFAccount(balance=0, contribution_room=5000)
        penalty = tfsa.overcontribution_penalty(0, months=1)
        self.assertAlmostEqual(penalty, 0)

    def test_penalty_multiple_months(self):
        """Penalty compounds for multiple months of excess."""
        tfsa = TSFAccount()
        penalty = tfsa.overcontribution_penalty(3000, months=6)
        self.assertAlmostEqual(penalty, 3000 * 0.01 * 6)


class TestRRSPYearVersioned(unittest.TestCase):
    """Test that RRSP cap comes from year-versioned tax_data (DP#12)."""

    def test_rrsp_limit_2023(self):
        """2023 RRSP dollar limit is $30,450."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_rrsp_limit(2023), 30450)

    def test_rrsp_limit_2024(self):
        """2024 RRSP dollar limit is $31,546."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_rrsp_limit(2024), 31546)

    def test_rrsp_limit_2025(self):
        """2025 RRSP dollar limit is $32,783."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_rrsp_limit(2025), 32783)

    def test_rrsp_limit_2026(self):
        """2026 RRSP dollar limit is $33,810."""
        provider = TaxDataProvider()
        self.assertAlmostEqual(provider.get_rrsp_limit(2026), 33810)

    def test_rrsp_limits_vary_across_years(self):
        """RRSP limits change across years (not a constant)."""
        provider = TaxDataProvider()
        v2023 = provider.get_rrsp_limit(2023)
        v2026 = provider.get_rrsp_limit(2026)
        self.assertNotEqual(v2023, v2026,
                            "RRSP limits should differ across years (DP#20)")

    def test_rrsp_add_annual_room_with_tax_data_cap(self):
        """RRSPAccount.add_annual_room uses year-versioned cap from TaxDataProvider."""
        provider = TaxDataProvider()
        rrsp = RRSPAccount()
        # 2026: cap = $33,810, earned_income = $200,000
        new_room = rrsp.add_annual_room(
            earned_income=200000, year=2026,
            annual_cap=provider.get_rrsp_limit(2026)
        )
        # 18% of $200k = $36,000, capped at $33,810
        self.assertAlmostEqual(new_room, 33810)

    def test_rrsp_add_annual_room_uncapped_when_no_cap(self):
        """Without a cap, RRSP room is 18% of earned income."""
        rrsp = RRSPAccount()
        new_room = rrsp.add_annual_room(earned_income=200000)
        # 18% of $200k = $36,000 (no cap)
        self.assertAlmostEqual(new_room, 36000)


if __name__ == '__main__':
    unittest.main()