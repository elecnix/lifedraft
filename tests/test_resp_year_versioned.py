#!/usr/bin/env python3
"""Tests for DP#20/DP#12: Year-versioned RESP thresholds and QESI-specific data.

Covers:
- CESG income thresholds are year-versioned (DP#20)
- QESI has its own income thresholds separate from CESG (DP#12)
- CESG annual room is year-versioned ($400 pre-2007, $500 from 2007+)
- CLB income thresholds are year-versioned and more granular (DP#20)
- Boundary conditions at exact thresholds
- CESG lifetime cap interaction with carry-forward

All test data uses round numbers. No personal information.
DP#17: every rule path tested with at least 2 cases.

Run with: python3 -m pytest tests/test_resp_year_versioned.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from countries.canada.resp_rules import (
    RESPChild, RESPCalculator,
    CESG_THRESHOLDS, QESI_THRESHOLDS, CLB_THRESHOLDS,
    CESG_ANNUAL_ROOM_CHANGE_YEAR, CESG_CONTRIBUTION_MAX_CHANGE_YEAR,
    get_cesg_thresholds, get_qesi_thresholds, get_clb_thresholds,
    get_cesg_annual_room, get_cesg_contribution_max,
)


class TestCESGYearVersionedThresholds(unittest.TestCase):
    """DP#20: CESG income thresholds must be year-versioned, not hardcoded constants."""

    def test_2024_cesg_thresholds_exist(self):
        """2024 CESG thresholds must be available."""
        t = get_cesg_thresholds(2024)
        self.assertIn('first_threshold', t)
        self.assertIn('second_threshold', t)
        self.assertGreater(t['first_threshold'], 0)
        self.assertGreater(t['second_threshold'], t['first_threshold'])

    def test_2026_cesg_thresholds_exist(self):
        """2026 CESG thresholds must be available."""
        t = get_cesg_thresholds(2026)
        self.assertEqual(t['first_threshold'], 58523)
        self.assertEqual(t['second_threshold'], 117045)

    def test_cesg_thresholds_differ_by_year(self):
        """Different years must return different thresholds (DP#20)."""
        t2024 = get_cesg_thresholds(2024)
        t2026 = get_cesg_thresholds(2026)
        self.assertNotEqual(t2024['first_threshold'], t2026['first_threshold'])

    def test_cesg_thresholds_data_dict_exists(self):
        """CESG_THRESHOLDS dict must exist with year keys."""
        self.assertIn(2024, CESG_THRESHOLDS)
        self.assertIn(2026, CESG_THRESHOLDS)

    def test_cesg_calculation_uses_year_2024_thresholds(self):
        """calculate_cesg must use 2024 thresholds when year=2024."""
        calc = RESPCalculator()
        child = RESPChild(name="test", birth_year=2012)
        t2024 = get_cesg_thresholds(2024)
        # Income exactly at 2024 first threshold should qualify for additional CESG
        result = calc.calculate_cesg(2500, child, 2024, family_income=t2024['first_threshold'])
        self.assertEqual(result['additional_cesg'], 100)  # 20% × $500

    def test_cesg_calculation_uses_year_2026_thresholds(self):
        """calculate_cesg must use 2026 thresholds when year=2026."""
        calc = RESPCalculator()
        child = RESPChild(name="test", birth_year=2012)
        t2026 = get_cesg_thresholds(2026)
        # Income exactly at 2026 first threshold should qualify for additional CESG
        result = calc.calculate_cesg(2500, child, 2026, family_income=t2026['first_threshold'])
        self.assertEqual(result['additional_cesg'], 100)

    def test_cesg_unknown_year_uses_nearest(self):
        """Unknown year should fall back to nearest available year."""
        t2030 = get_cesg_thresholds(2030)
        self.assertIn('first_threshold', t2030)
        self.assertGreater(t2030['first_threshold'], 0)


class TestQESISpecificThresholds(unittest.TestCase):
    """DP#12: QESI has its own income thresholds separate from CESG."""

    def test_qesi_thresholds_data_exists(self):
        """QESI_THRESHOLDS dict must exist with year keys."""
        self.assertIn(2024, QESI_THRESHOLDS)
        self.assertIn(2026, QESI_THRESHOLDS)

    def test_2024_qesi_thresholds_differ_from_cesg(self):
        """2024 QESI thresholds must be different from CESG thresholds (DP#12)."""
        qesi = get_qesi_thresholds(2024)
        cesg = get_cesg_thresholds(2024)
        # QESI has its own thresholds set by Retraite Québec
        self.assertNotEqual(qesi['first_threshold'], cesg['first_threshold'])

    def test_2024_qesi_known_values(self):
        """2024 QESI thresholds: $51,780 first, $103,545 second (official Retraite Québec)."""
        qesi = get_qesi_thresholds(2024)
        self.assertEqual(qesi['first_threshold'], 51780)
        self.assertEqual(qesi['second_threshold'], 103545)

    def test_qesi_calculation_uses_qesi_thresholds(self):
        """calculate_qesi must use QESI-specific thresholds, not CESG thresholds."""
        calc = RESPCalculator()
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        qesi_t = get_qesi_thresholds(2024)
        # Income at QESI first threshold → low income supplementary rate
        result = calc.calculate_qesi(2500, child, 2024, family_income=qesi_t['first_threshold'])
        self.assertEqual(result['supplementary_qesi'], 50)  # 10% × $500

    def test_qesi_income_between_qesi_and_cesg_thresholds(self):
        """Income between QESI and CESG first thresholds: no QESI supplementary, but CESG additional exists."""
        calc = RESPCalculator()
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        qesi_t = get_qesi_thresholds(2024)
        cesg_t = get_cesg_thresholds(2024)
        # Income above QESI first threshold but below CESG first threshold
        mid_income = (qesi_t['first_threshold'] + cesg_t['first_threshold']) / 2
        qesi_result = calc.calculate_qesi(2500, child, 2024, family_income=mid_income)
        # Should get middle QESI rate (5%), not low rate (10%)
        self.assertEqual(qesi_result['supplementary_qesi'], 25)  # 5% × $500

    def test_qesi_unknown_year_uses_nearest(self):
        """Unknown year should fall back to nearest available year."""
        t2030 = get_qesi_thresholds(2030)
        self.assertIn('first_threshold', t2030)
        self.assertGreater(t2030['first_threshold'], 0)


class TestCESGAnnualRoomYearVersioned(unittest.TestCase):
    """DP#20: CESG annual room is year-versioned ($400 pre-2007, $500 from 2007+)."""

    def test_cesg_room_2004(self):
        """2004: CESG room was $400/year."""
        self.assertEqual(get_cesg_annual_room(2004), 400)

    def test_cesg_room_2005(self):
        """2005: Contribution max changed to $2,500 but annual room still $400."""
        self.assertEqual(get_cesg_annual_room(2005), 400)

    def test_cesg_room_2006(self):
        """2006: Annual room still $400 (room change happens in 2007)."""
        self.assertEqual(get_cesg_annual_room(2006), 400)

    def test_cesg_room_2007(self):
        """2007: Annual room increased to $500."""
        self.assertEqual(get_cesg_annual_room(2007), 500)

    def test_cesg_room_2026(self):
        """2026: $500/year."""
        self.assertEqual(get_cesg_annual_room(2026), 500)

    def test_cesg_room_data_exists(self):
        """CESG annual room change year must be defined."""
        self.assertEqual(CESG_ANNUAL_ROOM_CHANGE_YEAR, 2007)


class TestCESGContributionMaxYearVersioned(unittest.TestCase):
    """DP#20: CESG contribution max is year-versioned ($2000 pre-2005, $2500 from 2005+)."""

    def test_contribution_max_2004(self):
        """2004: contribution max was $2,000."""
        self.assertEqual(get_cesg_contribution_max(2004), 2000)

    def test_contribution_max_2005(self):
        """2005: contribution max increased to $2,500."""
        self.assertEqual(get_cesg_contribution_max(2005), 2500)

    def test_contribution_max_2026(self):
        """2026: $2,500."""
        self.assertEqual(get_cesg_contribution_max(2026), 2500)

    def test_contribution_max_data_exists(self):
        """CESG contribution max change year must be defined."""
        self.assertEqual(CESG_CONTRIBUTION_MAX_CHANGE_YEAR, 2005)


class TestCESGPre2005Calculation(unittest.TestCase):
    """DP#20: CESG calculation with pre-2005 year uses correct contribution max.

    Note: Income thresholds for 2004/2005 fall back to nearest available year (2024).
    These tests suppress the year-range warning since the contribution max
    logic (the actual test target) is year-versioned separately.
    """

    def setUp(self):
        self.calc = RESPCalculator()

    def test_cesg_2004_basic_on_2000(self):
        """2004: 20% on first $2,000 = $400 basic CESG."""
        child = RESPChild(name="test", birth_year=1998)
        result = self.calc.calculate_cesg(2000, child, 2004, family_income=150000)
        self.assertEqual(result['basic_cesg'], 400)
        self.assertEqual(result['total_cesg'], 400)

    def test_cesg_2004_contribution_over_max(self):
        """2004: Contributing $3,000 still only gets CESG on first $2,000."""
        child = RESPChild(name="test", birth_year=1998)
        result = self.calc.calculate_cesg(3000, child, 2004, family_income=150000)
        self.assertEqual(result['basic_cesg'], 400)
        self.assertEqual(result['total_cesg'], 400)

    def test_cesg_2004_low_income(self):
        """2004: Low income gets additional CESG on first $500."""
        child = RESPChild(name="test", birth_year=1998)
        t = get_cesg_thresholds(2004)
        result = self.calc.calculate_cesg(2000, child, 2004, family_income=t['first_threshold'])
        self.assertEqual(result['basic_cesg'], 400)
        self.assertEqual(result['additional_cesg'], 100)  # 20% × $500
        self.assertEqual(result['total_cesg'], 500)

    def test_cesg_2004_mid_income(self):
        """2004: Mid income gets 10% additional on first $500."""
        child = RESPChild(name="test", birth_year=1998)
        t = get_cesg_thresholds(2004)
        result = self.calc.calculate_cesg(2000, child, 2004, family_income=t['first_threshold'] + 1)
        self.assertEqual(result['basic_cesg'], 400)
        self.assertEqual(result['additional_cesg'], 50)  # 10% × $500
        self.assertEqual(result['total_cesg'], 450)

    def test_cesg_2005_transition(self):
        """2005: contribution max is $2,500 but annual room still $400 (room changes in 2007)."""
        child = RESPChild(name="test", birth_year=1998)
        result = self.calc.calculate_cesg(2500, child, 2005, family_income=150000)
        # Contribution max is $2,500 but annual room is still $400
        # so total CESG for high income is $400 (capped by annual room)
        self.assertEqual(result['basic_cesg'], 500)  # 20% × $2,500
        self.assertEqual(result['total_cesg'], 400)  # capped by annual room

    def test_cesg_catchup_2004_contribution_max(self):
        """2004 catch-up: contribution max is $2,000, catchup max is $4,000."""
        child = RESPChild(name="test", birth_year=1998)
        result = self.calc.calculate_cesg_with_catchup(
            4000, child, 2004, family_income=150000, unused_room=2000)
        # Current year: 20% × $2,000 = $400
        self.assertEqual(result['current_year_cesg'], 400)
        # Catch-up: 20% × min($4,000 - $2,000, $2,000, $2,000) = 20% × $2,000 = $400
        self.assertEqual(result['catchup_cesg'], 400)
        # Total: $800 (capped at annual_room × 2 = $800 for high income)
        self.assertEqual(result['total_cesg'], 800)


class TestCLBYearVersionedThresholds(unittest.TestCase):
    """DP#20: CLB income thresholds are year-versioned and more granular."""

    def test_clb_thresholds_data_exists(self):
        """CLB_THRESHOLDS dict must exist with year keys."""
        self.assertIn(2024, CLB_THRESHOLDS)
        self.assertIn(2026, CLB_THRESHOLDS)

    def test_clb_2024_thresholds(self):
        """2024 CLB thresholds must be available for each child count tier."""
        t = get_clb_thresholds(2024)
        self.assertIn(1, t)  # 1-3 children
        self.assertGreater(t[1], 0)

    def test_clb_2026_thresholds(self):
        """2026 CLB thresholds must be available."""
        t = get_clb_thresholds(2026)
        self.assertIn(1, t)
        self.assertGreater(t[1], 0)

    def test_clb_thresholds_differ_by_year(self):
        """Different years must return different thresholds (DP#20)."""
        t2024 = get_clb_thresholds(2024)
        t2026 = get_clb_thresholds(2026)
        self.assertNotEqual(t2024[1], t2026[1])

    def test_clb_calculation_uses_year_thresholds(self):
        """calculate_clb must use year-versioned thresholds."""
        calc = RESPCalculator()
        child = RESPChild(name="test", birth_year=2012)
        t2024 = get_clb_thresholds(2024)
        # Income at threshold should be eligible
        result = calc.calculate_clb(child, 2024, family_income=t2024[1], num_children=1)
        self.assertTrue(result['eligible'])


class TestCESGBoundaryConditions(unittest.TestCase):
    """Test boundary conditions at exact thresholds (DP#17)."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_income_exactly_at_cesg_first_threshold(self):
        """Income exactly at CESG first threshold → qualifies for low additional rate."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_cesg_thresholds(2026)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=t['first_threshold'])
        self.assertEqual(result['additional_cesg'], 100)  # Low rate (20% × $500)

    def test_income_one_dollar_above_cesg_first_threshold(self):
        """$1 above first threshold → mid additional rate."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_cesg_thresholds(2026)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=t['first_threshold'] + 1)
        self.assertEqual(result['additional_cesg'], 50)  # Mid rate (10% × $500)

    def test_income_exactly_at_cesg_second_threshold(self):
        """Income exactly at CESG second threshold → mid additional rate."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_cesg_thresholds(2026)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=t['second_threshold'])
        self.assertEqual(result['additional_cesg'], 50)  # Mid rate (10% × $500)

    def test_income_one_dollar_above_cesg_second_threshold(self):
        """$1 above second threshold → no additional CESG."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_cesg_thresholds(2026)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=t['second_threshold'] + 1)
        self.assertEqual(result['additional_cesg'], 0)

    def test_income_exactly_at_qesi_first_threshold(self):
        """Income exactly at QESI first threshold → low supplementary rate."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        t = get_qesi_thresholds(2024)
        result = self.calc.calculate_qesi(2500, child, 2024, family_income=t['first_threshold'])
        self.assertEqual(result['supplementary_qesi'], 50)  # 10% × $500

    def test_income_one_dollar_above_qesi_first_threshold(self):
        """$1 above QESI first threshold → mid supplementary rate."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        t = get_qesi_thresholds(2024)
        result = self.calc.calculate_qesi(2500, child, 2024, family_income=t['first_threshold'] + 1)
        self.assertEqual(result['supplementary_qesi'], 25)  # 5% × $500

    def test_income_exactly_at_qesi_second_threshold(self):
        """Income exactly at QESI second threshold → mid supplementary rate."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        t = get_qesi_thresholds(2024)
        result = self.calc.calculate_qesi(2500, child, 2024, family_income=t['second_threshold'])
        self.assertEqual(result['supplementary_qesi'], 25)  # 5% × $500

    def test_income_one_dollar_above_qesi_second_threshold(self):
        """$1 above QESI second threshold → no supplementary QESI."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        t = get_qesi_thresholds(2024)
        result = self.calc.calculate_qesi(2500, child, 2024, family_income=t['second_threshold'] + 1)
        self.assertEqual(result['supplementary_qesi'], 0)


class TestCESGLifetimeCapWithCatchup(unittest.TestCase):
    """Test CESG lifetime cap interaction with carry-forward (issue edge case)."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_catchup_capped_by_remaining_lifetime(self):
        """Catch-up CESG must be capped by remaining lifetime room, not just annual max."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_cesg_received = 6700  # Only $500 remaining lifetime
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=2500)
        # Total CESG should be capped at $500 remaining, not $1,000
        self.assertLessEqual(result['total_cesg'], 500)

    def test_catchup_low_income_capped_by_lifetime(self):
        """Low income catch-up: lifetime cap takes priority over annual max."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_cesg_received = 6600  # Only $600 remaining lifetime
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=40000, unused_room=2500)
        # Annual max for low income is $600, but lifetime remaining is also $600
        self.assertLessEqual(result['total_cesg'], 600)

    def test_large_catchup_near_lifetime_cap(self):
        """Contribute $5,000 in one year with $200 lifetime remaining."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_cesg_received = 7000  # Only $200 remaining
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=10000)
        self.assertLessEqual(result['total_cesg'], 200)


class TestCESGCatchupWithYearVersionedRoom(unittest.TestCase):
    """Test that catch-up uses year-versioned CESG annual room."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_cesg_with_catchup_uses_year_2024(self):
        """CESG catch-up in 2024 should use 2024 thresholds."""
        child = RESPChild(name="test", birth_year=2012)
        t2024 = get_cesg_thresholds(2024)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2024, family_income=t2024['first_threshold'], unused_room=2500)
        self.assertGreater(result['total_cesg'], 0)
        self.assertGreater(result['additional_cesg'], 0)

    def test_catchup_high_income_total_cesg_reflects_catchup(self):
        """High income catchup: total_cesg should be ~$500 + catchup, not capped at $500."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=2500)
        # current_year_cesg = 20% × $2,500 = $500
        # catchup_cesg = 20% × $2,500 = $500
        # total should be $1,000, NOT capped at $500
        self.assertEqual(result['current_year_cesg'], 500)
        self.assertEqual(result['catchup_cesg'], 500)
        self.assertEqual(result['total_cesg'], 1000)

    def test_catchup_low_income_total_cesg_reflects_catchup(self):
        """Low income catchup: total_cesg should reflect catchup + additional."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_cesg_thresholds(2026)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=t['first_threshold'], unused_room=2500)
        # current = $500, catchup = $500, additional = $100
        # total = $1,100 (capped at 2× $600 = $1,200)
        self.assertGreater(result['total_cesg'], 500)  # Must exceed normal annual max
        self.assertEqual(result['current_year_cesg'], 500)
        self.assertEqual(result['catchup_cesg'], 500)
        self.assertEqual(result['additional_cesg'], 100)
        self.assertEqual(result['total_cesg'], 1100)

    def test_no_unused_room_no_catchup_cap(self):
        """No unused room → normal annual max applies, not doubled."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=0)
        # Without catchup, max is $500 even though contributing $5,000
        self.assertEqual(result['total_cesg'], 500)

    def test_catchup_mid_income_total_cesg_reflects_catchup(self):
        """Mid income catchup: additional CESG doesn't double, only basic does."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_cesg_thresholds(2026)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=t['second_threshold'], unused_room=2500)
        # current = $500, catchup = $500, additional = $50
        # catchup_annual_max = 500*2 + (550-500) = 1050
        # total = min(500+500+50, 1050) = 1050
        self.assertEqual(result['current_year_cesg'], 500)
        self.assertEqual(result['catchup_cesg'], 500)
        self.assertEqual(result['additional_cesg'], 50)
        self.assertEqual(result['total_cesg'], 1050)

    def test_partial_unused_room_catchup(self):
        """Partial unused_room (< contribution_max) limits catchup grant."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=1000)
        # current = $500, catchup = 20% × min($2500, $2500, $1000) = $200
        self.assertEqual(result['current_year_cesg'], 500)
        self.assertEqual(result['catchup_cesg'], 200)
        self.assertEqual(result['total_cesg'], 700)


class TestQESILifetimeCap(unittest.TestCase):
    """Test QESI lifetime cap interaction."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_qesi_near_lifetime_cap(self):
        """QESI near lifetime cap: grant capped at remaining lifetime."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        child.total_qesi_received = 3500  # Only $100 remaining
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=30000)
        # basic = $250, supplementary = $50, total would be $300 but capped at $100
        self.assertLessEqual(result['total_qesi'], 100)
        self.assertGreater(result['total_qesi'], 0)

    def test_qesi_at_lifetime_cap(self):
        """QESI at lifetime cap: no more grant."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        child.total_qesi_received = 3600
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=30000)
        self.assertEqual(result['total_qesi'], 0)
        self.assertEqual(result['remaining_lifetime_qesi'], 0)


class TestQESINonQuebec(unittest.TestCase):
    """Test QESI for non-Quebec residents — should return 0."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_non_quebec_zero_all_income_levels(self):
        """Non-Quebec resident gets $0 QESI at all income levels."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=False)
        for income in [20000, 50000, 100000, 200000]:
            result = self.calc.calculate_qesi(2500, child, 2026, family_income=income)
            self.assertEqual(result['total_qesi'], 0, f"QESI should be 0 at income {income}")

    def test_non_quebec_returns_reason(self):
        """Non-Quebec QESI result includes reason string."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=False)
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=150000)
        self.assertEqual(result.get('reason'), 'Not a Quebec resident')


class TestCLBGranularThresholds(unittest.TestCase):
    """Test CLB with more granular thresholds."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_clb_no_duplicate_keys(self):
        """CLB_THRESHOLDS should not have duplicate keys 1-3 (S-3)."""
        for year, tiers in CLB_THRESHOLDS.items():
            # Key 2 and 3 should not exist since 1-3 children share key 1
            self.assertNotIn(2, tiers, f"Year {year}: key 2 should not exist (1-3 tier uses key 1)")
            self.assertNotIn(3, tiers, f"Year {year}: key 3 should not exist (1-3 tier uses key 1)")

    def test_clb_2_children_uses_tier_1(self):
        """2 children maps to tier key 1 in calculate_clb."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_clb_thresholds(2026)
        result = self.calc.calculate_clb(child, 2026, family_income=t[1], num_children=2)
        self.assertTrue(result['eligible'])
        # Just above the 1-child tier threshold should be ineligible
        result_above = self.calc.calculate_clb(child, 2026, family_income=t[1] + 1, num_children=2)
        self.assertFalse(result_above['eligible'])

    def test_clb_3_children_uses_tier_1(self):
        """3 children maps to tier key 1 in calculate_clb."""
        child = RESPChild(name="test", birth_year=2012)
        t = get_clb_thresholds(2026)
        result = self.calc.calculate_clb(child, 2026, family_income=t[1], num_children=3)
        self.assertTrue(result['eligible'])

    def test_clb_4_children_higher_threshold(self):
        """4 children gets a higher threshold than 1-3 children."""
        t = get_clb_thresholds(2026)
        if 4 in t:
            self.assertGreater(t[4], t[1])


class TestAnalyzeRESPNonDefaultYear(unittest.TestCase):
    """Test analyze_resp_for_family with non-2026 ref_year (T-3/T-4)."""

    def test_analyze_2024_ref_year(self):
        """analyze_resp_for_family with start_year=2024 uses 2024 thresholds."""
        from countries.canada.resp_rules import analyze_resp_for_family
        cfg = {
            'family': {
                'members': [{'gross_income': 150000}],
                'children': [{'name': 'TestChild', 'age': 10}]
            },
            'accounts': {
                'resp_current_balance': 20000,
                'resp_room_accumulated': 80000,
                'resp_annual_room_per_child': 3000,
            },
            'assumptions': {'start_year': 2024},
        }
        result = analyze_resp_for_family(cfg)
        self.assertTrue(result['TestChild']['cesg_eligible'])
        # income_tier should use 2024 thresholds, not 2026
        t2024 = get_cesg_thresholds(2024)
        self.assertIn(f"{t2024['second_threshold']:,.0f}", result['family_summary']['income_tier'])

    def test_analyze_2004_ref_year_uses_contribution_max_2000(self):
        """analyze_resp_for_family with start_year=2004 uses $2,000 contribution max.

        Note: Income thresholds for 2004 fall back to 2024 data.
        """
        from countries.canada.resp_rules import analyze_resp_for_family
        cfg = {
            'family': {
                'members': [{'gross_income': 100000}],
                'children': [{'name': 'TestChild', 'age': 10}]
            },
            'accounts': {
                'resp_current_balance': 20000,
                'resp_room_accumulated': 80000,
                'resp_annual_room_per_child': 3000,
            },
            'assumptions': {'start_year': 2004},
        }
        result = analyze_resp_for_family(cfg)
        self.assertTrue(result['TestChild']['cesg_eligible'])
        # total_matching_on_max should reference $2,000, not $2,500
        matching_str = result['TestChild']['total_matching_on_max']
        self.assertIn('2,000', matching_str)

    def test_analyze_clb_2024_ref_year(self):
        """CLB with 2024 ref_year uses 2024 thresholds."""
        from countries.canada.resp_rules import analyze_resp_for_family
        cfg = {
            'family': {
                'members': [{'gross_income': 30000}],
                'children': [{'name': 'TestChild', 'age': 10}]
            },
            'accounts': {
                'resp_current_balance': 5000,
                'resp_room_accumulated': 80000,
                'resp_annual_room_per_child': 2000,
            },
            'assumptions': {'start_year': 2024},
        }
        result = analyze_resp_for_family(cfg)
        self.assertTrue(result['TestChild']['clb_eligible'])


class TestGetterReturnsCopies(unittest.TestCase):
    """T3: Getter functions must return copies, not mutable references (C6/D3)."""

    def test_get_cesg_thresholds_returns_copy(self):
        """Mutating returned dict must not affect source data."""
        t1 = get_cesg_thresholds(2026)
        t1['first_threshold'] = 0
        t2 = get_cesg_thresholds(2026)
        self.assertNotEqual(t2['first_threshold'], 0)

    def test_get_qesi_thresholds_returns_copy(self):
        """Mutating returned dict must not affect source data."""
        t1 = get_qesi_thresholds(2026)
        t1['first_threshold'] = 0
        t2 = get_qesi_thresholds(2026)
        self.assertNotEqual(t2['first_threshold'], 0)

    def test_get_clb_thresholds_returns_copy(self):
        """Mutating returned dict must not affect source data."""
        t1 = get_clb_thresholds(2026)
        t1[1] = 0
        t2 = get_clb_thresholds(2026)
        self.assertNotEqual(t2[1], 0)


class TestIncomeDependentImportantNotes(unittest.TestCase):
    """T5: important_notes vary by income tier (C4/G3, C3/S1/D1)."""

    def test_low_income_notes_show_36pct(self):
        """Low income family should see 36% matching rate in important_notes."""
        from countries.canada.resp_rules import analyze_resp_for_family
        cfg = {
            'family': {
                'members': [{'gross_income': 40000}],
                'children': [{'name': 'TestChild', 'age': 10}]
            },
            'accounts': {
                'resp_current_balance': 5000,
            },
        }
        result = analyze_resp_for_family(cfg)
        notes = result['family_summary']['important_notes']
        notes_text = ' '.join(notes)
        self.assertIn('36%', notes_text)

    def test_mid_income_notes_show_33pct(self):
        """Mid income family should see 33% matching rate in important_notes."""
        from countries.canada.resp_rules import analyze_resp_for_family
        cesg_t = get_cesg_thresholds(2026)
        mid_income = (cesg_t['first_threshold'] + cesg_t['second_threshold']) // 2
        cfg = {
            'family': {
                'members': [{'gross_income': mid_income}],
                'children': [{'name': 'TestChild', 'age': 10}]
            },
            'accounts': {
                'resp_current_balance': 5000,
            },
        }
        result = analyze_resp_for_family(cfg)
        notes = result['family_summary']['important_notes']
        notes_text = ' '.join(notes)
        self.assertIn('33%', notes_text)

    def test_high_income_notes_show_30pct(self):
        """High income family should see 30% matching rate in important_notes."""
        from countries.canada.resp_rules import analyze_resp_for_family
        cfg = {
            'family': {
                'members': [{'gross_income': 200000}],
                'children': [{'name': 'TestChild', 'age': 10}]
            },
            'accounts': {
                'resp_current_balance': 5000,
            },
        }
        result = analyze_resp_for_family(cfg)
        notes = result['family_summary']['important_notes']
        notes_text = ' '.join(notes)
        self.assertIn('30%', notes_text)

    def test_dynamic_notes_reference_actual_child_name(self):
        """important_notes should reference actual child names, not hardcoded 'Child 1/2' (C3/S1/D1)."""
        from countries.canada.resp_rules import analyze_resp_for_family
        cfg = {
            'family': {
                'members': [{'gross_income': 200000}],
                'children': [{'name': 'child_a', 'age': 18}, {'name': 'child_b', 'age': 10}]
            },
            'accounts': {
                'resp_current_balance': 5000,
            },
        }
        result = analyze_resp_for_family(cfg)
        notes = result['family_summary']['important_notes']
        notes_text = ' '.join(notes)
        self.assertIn('child_a', notes_text)
        self.assertIn('child_b', notes_text)
        # Should NOT contain hardcoded "Child 1" or "Child 2"
        self.assertNotIn('Child 1', notes_text)
        self.assertNotIn('Child 2', notes_text)


class TestCLBAgeLimit(unittest.TestCase):
    """CLB age limit: available until end of calendar year turning 15 (C2/G2)."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_clb_age_15_eligible(self):
        """Age 15 → still eligible."""
        child = RESPChild(name="test", birth_year=2011)  # Age 15 in 2026
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertTrue(result['eligible'])
        self.assertGreater(result['clb_amount'], 0)

    def test_clb_age_16_not_eligible(self):
        """Age 16 → not eligible (CLB ends at year turning 15)."""
        child = RESPChild(name="test", birth_year=2010)  # Age 16 in 2026
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertFalse(result['eligible'])
        self.assertEqual(result['clb_amount'], 0)


if __name__ == '__main__':
    unittest.main()
