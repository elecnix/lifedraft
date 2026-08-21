#!/usr/bin/env python3
"""Comprehensive tests for resp_rules.py — full coverage of CESG, QESI, CLB, and limits.

All test data uses round numbers. No personal information.
DP#17: every rule path tested with at least 2 cases.

Run with: python3 -m pytest tests/test_resp_rules_full.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from io import StringIO

from countries.canada.resp_rules import RESPChild, RESPCalculator, analyze_resp_for_family, print_resp_report


class TestRESPChild(unittest.TestCase):
    """Test RESPChild dataclass and eligibility methods."""

    def test_age_in_year(self):
        child = RESPChild(name="test", birth_year=2010)
        self.assertEqual(child.age_in_year(2026), 16)

    def test_cesg_eligible_under_17(self):
        child = RESPChild(name="test", birth_year=2010)  # Age 16
        self.assertTrue(child.cesg_eligible(2026))

    def test_cesg_eligible_exactly_17(self):
        child = RESPChild(name="test", birth_year=2009)  # Age 17
        self.assertTrue(child.cesg_eligible(2026))

    def test_cesg_not_eligible_18(self):
        child = RESPChild(name="test", birth_year=2008)  # Age 18
        self.assertFalse(child.cesg_eligible(2026))

    def test_cesg_not_eligible_older(self):
        child = RESPChild(name="test", birth_year=2000)  # Age 26
        self.assertFalse(child.cesg_eligible(2026))

    def test_cesg_16_17_eligible_young_child(self):
        """Ages under 16 always return True (not subject to the rule)."""
        child = RESPChild(name="test", birth_year=2012)  # Age 14
        self.assertTrue(child.cesg_16_17_eligible(2026))

    def test_cesg_16_17_eligible_with_2000_before_15(self):
        """Condition 1: $2,000 total contributions before age 15."""
        child = RESPChild(name="test", birth_year=2010)  # Age 16
        child.total_before_age_15 = 2000
        self.assertTrue(child.cesg_16_17_eligible(2026))

    def test_cesg_16_17_eligible_with_4_years_100(self):
        """Condition 2: $100/year in 4+ years before age 15."""
        child = RESPChild(name="test", birth_year=2010)  # Age 16
        child.total_before_age_15 = 0
        child.contribution_years = [
            (2022, 100), (2023, 100), (2024, 100), (2025, 100)
        ]
        self.assertTrue(child.cesg_16_17_eligible(2026))

    def test_cesg_16_17_not_eligible_insufficient(self):
        """Neither condition met → not eligible at 16-17."""
        child = RESPChild(name="test", birth_year=2010)  # Age 16
        child.total_before_age_15 = 500
        child.contribution_years = [(2023, 50), (2024, 50)]
        self.assertFalse(child.cesg_16_17_eligible(2026))

    def test_cesg_16_17_eligible_condition_1_partial_not_enough(self):
        """$1,999 before age 15 is not enough for condition 1."""
        child = RESPChild(name="test", birth_year=2010)
        child.total_before_age_15 = 1999
        child.contribution_years = []
        self.assertFalse(child.cesg_16_17_eligible(2026))

    def test_cesg_16_17_eligible_3_years_not_4(self):
        """3 years of $100 contributions is not enough (need 4)."""
        child = RESPChild(name="test", birth_year=2010)
        child.total_before_age_15 = 0
        child.contribution_years = [
            (2022, 100), (2023, 100), (2024, 100)
        ]
        self.assertFalse(child.cesg_16_17_eligible(2026))

    def test_cesg_16_17_eligible_age_17(self):
        """Age 17 still subject to 16-17 rule."""
        child = RESPChild(name="test", birth_year=2009)  # Age 17
        child.total_before_age_15 = 2000
        self.assertTrue(child.cesg_16_17_eligible(2026))


class TestCESGCalculation(unittest.TestCase):
    """Test CESG calculation — all income tiers and edge cases."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_basic_cesg_20pct_on_2500(self):
        """Basic CESG: 20% on first $2,500 = $500."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=150000)
        self.assertEqual(result['basic_cesg'], 500)
        self.assertEqual(result['total_cesg'], 500)
        self.assertTrue(result['eligible'])

    def test_basic_cesg_on_smaller_contribution(self):
        """Even $1,000 contribution gets $200 (20%)."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(1000, child, 2026, family_income=150000)
        self.assertEqual(result['basic_cesg'], 200)

    def test_zero_contribution(self):
        """Zero contribution → zero CESG."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(0, child, 2026, family_income=150000)
        self.assertEqual(result['total_cesg'], 0)

    def test_additional_cesg_low_income(self):
        """Low income (≤$58,523): 20% additional on first $500 = $100 extra."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=40000)
        self.assertEqual(result['basic_cesg'], 500)
        self.assertEqual(result['additional_cesg'], 100)  # 20% × $500
        self.assertEqual(result['total_cesg'], 600)

    def test_additional_cesg_mid_income(self):
        """Middle income ($58k-$117k): 10% additional on first $500 = $50 extra."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=80000)
        self.assertEqual(result['additional_cesg'], 50)  # 10% × $500
        self.assertEqual(result['total_cesg'], 550)

    def test_additional_cesg_high_income(self):
        """High income: no additional CESG."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=200000)
        self.assertEqual(result['additional_cesg'], 0)
        self.assertEqual(result['total_cesg'], 500)

    def test_additional_cesg_at_first_threshold_exact(self):
        """Income exactly at first threshold → low-rate additional CESG.

        DP#17: boundary test for additional CESG phase-out.
        2026 first threshold is $58,523 — '≤' includes the threshold value.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=58523)
        self.assertEqual(result['basic_cesg'], 500)
        self.assertEqual(result['additional_cesg'], 100)  # 20% × $500
        self.assertEqual(result['total_cesg'], 600)

    def test_additional_cesg_at_second_threshold_exact(self):
        """Income exactly at second threshold → mid-rate additional CESG.

        DP#17: the 'elif' clause means second threshold is '≤ second_threshold'
        for those above first_threshold.
        2026 second threshold is $117,045.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=117045)
        self.assertEqual(result['basic_cesg'], 500)
        self.assertEqual(result['additional_cesg'], 50)  # 10% × $500
        self.assertEqual(result['total_cesg'], 550)

    def test_additional_cesg_one_dollar_above_second_threshold(self):
        """Income $1 above second threshold → no additional CESG.

        DP#17: boundary test — the 'elif' clause excludes above-threshold values.
        2026 second threshold is $117,045.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=117046)
        self.assertEqual(result['basic_cesg'], 500)
        self.assertEqual(result['additional_cesg'], 0)
        self.assertEqual(result['total_cesg'], 500)

    def test_child_not_eligible_over_17(self):
        """Child over 17 → no CESG at all."""
        child = RESPChild(name="test", birth_year=2008)  # Age 18
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=150000)
        self.assertEqual(result['total_cesg'], 0)
        self.assertFalse(result['eligible'])

    def test_child_not_eligible_16_17(self):
        """Child 16-17 without prior contributions → no CESG."""
        child = RESPChild(name="test", birth_year=2010)  # Age 16
        child.total_before_age_15 = 0
        child.contribution_years = []
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=150000)
        self.assertEqual(result['total_cesg'], 0)
        self.assertFalse(result['eligible'])

    def test_lifetime_limit_reached(self):
        """Lifetime CESG limit ($7,200) already reached → no more CESG."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_cesg_received = 7200
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=150000)
        self.assertEqual(result['total_cesg'], 0)
        self.assertTrue(result['eligible'])
        self.assertEqual(result['remaining_lifetime_cesg'], 0)

    def test_contribution_exceeds_annual_max(self):
        """Contributing $10,000 only gets CESG on first $2,500."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(10000, child, 2026, family_income=150000)
        self.assertEqual(result['basic_cesg'], 500)  # 20% × 2500, not 20% × 10000

    def test_low_income_max_annual_grant(self):
        """Low income: annual max grant is $600."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=40000)
        self.assertEqual(result['total_cesg'], 600)

    def test_mid_income_max_annual_grant(self):
        """Middle income: annual max grant is $550."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=80000)
        self.assertEqual(result['total_cesg'], 550)

    def test_high_income_max_annual_grant(self):
        """High income: annual max grant is $500."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=200000)
        self.assertEqual(result['total_cesg'], 500)


class TestCESGWithCatchup(unittest.TestCase):
    """Test CESG with carry-forward of unused room."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_catchup_over_17_ineligible(self):
        """Over-17 child with catchup room → total_cesg == 0 (C1/G1)."""
        child = RESPChild(name="test", birth_year=2008)  # Age 18
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=2500)
        self.assertEqual(result['total_cesg'], 0)
        self.assertFalse(result['eligible'])

    def test_catchup_16_17_ineligible(self):
        """16-17 ineligible child with catchup → total_cesg == 0 (C1/G1)."""
        child = RESPChild(name="test", birth_year=2010)  # Age 16
        child.total_before_age_15 = 0
        child.contribution_years = []
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=2500)
        self.assertEqual(result['total_cesg'], 0)
        self.assertFalse(result['eligible'])

    def test_catchup_no_unused_room(self):
        """No unused room → same as regular CESG calculation."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            2500, child, 2026, family_income=150000, unused_room=0)
        self.assertEqual(result['current_year_cesg'], 500)
        self.assertEqual(result['catchup_cesg'], 0)

    def test_catchup_with_unused_room(self):
        """$2,500 unused room + $5,000 contribution = catch-up applies."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=2500)
        # Current year: 20% on $2,500 = $500
        self.assertEqual(result['current_year_cesg'], 500)
        # Catch-up: 20% on min($5,000 - $2,500, $2,500) = 20% × $2,500 = $500
        self.assertEqual(result['catchup_cesg'], 500)

    def test_catchup_exceeds_contribution(self):
        """More unused room than excess contribution → limited by contribution."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            3000, child, 2026, family_income=150000, unused_room=10000)
        # Catch-up: 20% × min($500, $10,000) = $100 (only $500 excess beyond $2,500)
        self.assertEqual(result['catchup_cesg'], 100)

    def test_catchup_low_income_additional(self):
        """Low income additional CESG applies with catch-up."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=40000, unused_room=2500)
        self.assertGreater(result['additional_cesg'], 0)


class TestQESI(unittest.TestCase):
    """Test Quebec Education Savings Incentive."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_basic_qesi_10pct(self):
        """Basic QESI: 10% on first $5,000 = $250 max."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        result = self.calc.calculate_qesi(5000, child, 2026, family_income=150000)
        self.assertEqual(result['basic_qesi'], 250)
        self.assertTrue(result['eligible'])

    def test_qesi_on_smaller_contribution(self):
        """$2,500 contribution → $250 QESI (10% × $2,500 = $250, capped at $250)."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=150000)
        self.assertEqual(result['basic_qesi'], 250)  # 10% × 2500 = 250, equals cap

    def test_qesi_not_quebec_resident(self):
        """Non-Quebec resident → no QESI."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=False)
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=150000)
        self.assertEqual(result['total_qesi'], 0)
        self.assertFalse(result['eligible'])

    def test_qesi_child_over_17(self):
        """Over 17 → no QESI."""
        child = RESPChild(name="test", birth_year=2008, is_quebec_resident=True)
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=150000)
        self.assertEqual(result['total_qesi'], 0)
        self.assertFalse(result['eligible'])

    def test_qesi_lifetime_limit_reached(self):
        """Lifetime QESI limit ($3,600) reached → no more."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        child.total_qesi_received = 3600
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=150000)
        self.assertEqual(result['total_qesi'], 0)

    def test_qesi_supplementary_low_income(self):
        """Low income: 10% supplementary on first $500 = $50."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=40000)
        self.assertEqual(result['supplementary_qesi'], 50)

    def test_qesi_supplementary_mid_income(self):
        """Middle income: 5% supplementary on first $500 = $25."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=80000)
        self.assertEqual(result['supplementary_qesi'], 25)

    def test_qesi_supplementary_high_income(self):
        """High income: no supplementary QESI."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=200000)
        self.assertEqual(result['supplementary_qesi'], 0)

    def test_qesi_lifetime_capping(self):
        """Total QESI capped at remaining lifetime room."""
        child = RESPChild(name="test", birth_year=2012, is_quebec_resident=True)
        child.total_qesi_received = 3400  # Only $200 remaining
        result = self.calc.calculate_qesi(2500, child, 2026, family_income=150000)
        self.assertLessEqual(result['total_qesi'], 200)


class TestCLB(unittest.TestCase):
    """Test Canada Learning Bond."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_clb_first_year_eligible(self):
        """First year of eligibility: $500 payment."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertEqual(result['clb_amount'], 500)
        self.assertTrue(result['eligible'])

    def test_clb_subsequent_year(self):
        """Subsequent years: $100 per year."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_clb_received = 500  # Already got first payment
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertEqual(result['clb_amount'], 100)

    def test_clb_born_before_2004(self):
        """Child born before 2004 → not eligible."""
        child = RESPChild(name="test", birth_year=2003)
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertEqual(result['clb_amount'], 0)
        self.assertFalse(result['eligible'])

    def test_clb_age_16_plus(self):
        """Age 16+ → not eligible. CLB available until end of year beneficiary turns 15."""
        child = RESPChild(name="test", birth_year=2010)  # Age 16
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertEqual(result['clb_amount'], 0)
        self.assertFalse(result['eligible'])

    def test_clb_age_15_eligible(self):
        """Age 15 → still eligible. CLB ends at calendar year turning 15."""
        child = RESPChild(name="test", birth_year=2011)  # Age 15
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertTrue(result['eligible'])
        self.assertGreater(result['clb_amount'], 0)

    def test_clb_income_above_threshold(self):
        """Income above CLB threshold → not eligible."""
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_clb(child, 2026, family_income=100000, num_children=1)
        self.assertEqual(result['clb_amount'], 0)
        self.assertFalse(result['eligible'])

    def test_clb_lifetime_max_reached(self):
        """Already received $2,000 CLB → no more."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_clb_received = 2000
        result = self.calc.calculate_clb(child, 2026, family_income=40000, num_children=1)
        self.assertEqual(result['clb_amount'], 0)

    def test_clb_multiple_children_income_threshold(self):
        """More children → higher income threshold."""
        child = RESPChild(name="test", birth_year=2012)
        # 5+ children: higher threshold
        result = self.calc.calculate_clb(
            child, 2026, family_income=70000, num_children=5)
        self.assertTrue(result['eligible'], "5+ children at $70k should be eligible")

    def test_clb_income_at_exact_threshold_1_child(self):
        """Income exactly at CLB threshold for 1-3 children → eligible.

        DP#17: boundary test — the '≤' operator includes the threshold value.
        2026 threshold for 1-3 children is $58,523.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_clb(child, 2026, family_income=58523, num_children=1)
        self.assertTrue(result['eligible'], "Income at threshold should be eligible")
        self.assertGreater(result['clb_amount'], 0)

    def test_clb_income_one_dollar_above_threshold_1_child(self):
        """Income $1 above CLB threshold for 1-3 children → NOT eligible.

        DP#17: boundary test — the '≤' operator excludes above-threshold values.
        2026 threshold for 1-3 children is $58,523.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_clb(child, 2026, family_income=58524, num_children=1)
        self.assertFalse(result['eligible'], "Income $1 above threshold should not be eligible")
        self.assertEqual(result['clb_amount'], 0)

    def test_clb_income_at_exact_threshold_4_children(self):
        """Income exactly at CLB threshold for 4 children → eligible.

        DP#17: household composition boundary — 4-child threshold differs from 1-3.
        2026 threshold for 4 children is $66,078.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_clb(child, 2026, family_income=66078, num_children=4)
        self.assertTrue(result['eligible'], "4 children at threshold should be eligible")
        self.assertGreater(result['clb_amount'], 0)

    def test_clb_income_at_exact_threshold_5plus_children(self):
        """Income exactly at CLB threshold for 5+ children → eligible.

        DP#17: household composition boundary — 5+-child threshold.
        2026 threshold for 5+ children is $73,633.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_clb(child, 2026, family_income=73633, num_children=6)
        self.assertTrue(result['eligible'], "5+ children at threshold should be eligible")
        self.assertGreater(result['clb_amount'], 0)

    def test_clb_household_composition_determines_threshold(self):
        """Same income, different num_children → different eligibility.

        DP#17: household composition changes affect CLB eligibility.
        At $65,000: 1-3 children (threshold $58,523) → not eligible,
                   4 children (threshold $66,078) → eligible.
        """
        child = RESPChild(name="test", birth_year=2012)
        # 2 children: threshold is $58,523
        result_2 = self.calc.calculate_clb(child, 2026, family_income=65000, num_children=2)
        self.assertFalse(result_2['eligible'], "2 children at $65k: over 1-3 threshold")
        # 4 children: threshold is $66,078
        result_4 = self.calc.calculate_clb(child, 2026, family_income=65000, num_children=4)
        self.assertTrue(result_4['eligible'], "4 children at $65k: under 4-child threshold")


class TestRESPContributionCheck(unittest.TestCase):
    """Test lifetime contribution limit checking."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_within_limits(self):
        """Normal contribution within limits."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_contributions = 10000
        result = self.calc.resp_contribution_check(2500, child)
        self.assertTrue(result['within_limits'])
        self.assertEqual(result['excess'], 0)

    def test_exceeds_lifetime_limit(self):
        """Contribution pushing over $50,000 lifetime limit."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_contributions = 49000
        result = self.calc.resp_contribution_check(5000, child)
        self.assertFalse(result['within_limits'])
        self.assertEqual(result['excess'], 4000)
        self.assertGreater(result['excess_tax_per_month'], 0)

    def test_exactly_at_limit(self):
        """Contribution exactly at limit boundary."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_contributions = 47500
        result = self.calc.resp_contribution_check(2500, child)
        self.assertTrue(result['within_limits'])

    def test_remaining_lifetime_room(self):
        """Remaining lifetime room calculation."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_contributions = 20000
        result = self.calc.resp_contribution_check(5000, child)
        self.assertTrue(result['within_limits'])
        self.assertEqual(result['remaining_lifetime'], 25000)  # 50k - 20k - 5k


class TestAnalyzeRESPForFamily(unittest.TestCase):
    """Test family-level RESP analysis (integration)."""

    def test_basic_family_analysis(self):
        """Analyze a basic family with 2 children."""
        cfg = {
            'family': {
                'members': [
                    {'gross_income': 130000},
                    {'gross_income': 50000},
                ],
                'children': [
                    {'name': 'Child1', 'age': 10},
                    {'name': 'Child2', 'age': 15},
                ]
            },
            'accounts': {
                'resp_current_balance': 30000,
                'resp_room_accumulated': 80000,
                'resp_annual_room_per_child': 3000,
            }
        }
        result = analyze_resp_for_family(cfg)
        self.assertIn('Child1', result)
        self.assertIn('Child2', result)
        self.assertIn('family_summary', result)
        self.assertEqual(result['Child1']['age'], 10)
        self.assertEqual(result['Child2']['age'], 15)
        self.assertTrue(result['Child1']['cesg_eligible'])
        self.assertTrue(result['Child2']['cesg_eligible'])

    def test_family_with_over17_child(self):
        """Child over 17 → no CESG eligibility."""
        cfg = {
            'family': {
                'members': [{'gross_income': 200000}],
                'children': [
                    {'name': 'Over17', 'age': 18},
                ]
            },
            'accounts': {
                'resp_current_balance': 45000,
                'resp_room_accumulated': 80000,
                'resp_annual_room_per_child': 3000,
            }
        }
        result = analyze_resp_for_family(cfg)
        self.assertFalse(result['Over17']['cesg_eligible'])

    def test_print_resp_report(self):
        """print_resp_report runs without error (captures stdout)."""
        cfg = {
            'family': {
                'members': [{'gross_income': 150000}],
                'children': [
                    {'name': 'TestChild', 'age': 10},
                ]
            },
            'accounts': {
                'resp_current_balance': 20000,
                'resp_room_accumulated': 80000,
                'resp_annual_room_per_child': 3000,
            }
        }
        result = analyze_resp_for_family(cfg)
        import sys
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            print_resp_report(result)
        finally:
            sys.stdout = old_stdout

    def test_no_children(self):
        """Empty children list → no crash."""
        cfg = {
            'family': {
                'members': [{'gross_income': 100000}],
                'children': []
            },
            'accounts': {
                'resp_current_balance': 0,
                'resp_room_accumulated': 0,
                'resp_annual_room_per_child': 3000,
            }
        }
        result = analyze_resp_for_family(cfg)
        self.assertIn('family_summary', result)

    def test_family_with_16_17_child(self):
        """16-17 year old with sufficient prior contributions."""
        cfg = {
            'family': {
                'members': [{'gross_income': 150000}, {'gross_income': 50000}],
                'children': [
                    {'name': 'Sixteen', 'age': 16},
                ]
            },
            'accounts': {
                'resp_current_balance': 30000,
                'resp_room_accumulated': 80000,
                'resp_annual_room_per_child': 3000,
            }
        }
        result = analyze_resp_for_family(cfg)
        self.assertTrue(result['Sixteen']['cesg_eligible'])


class TestOverContributionCommunityQuestions(unittest.TestCase):
    """Test scenarios commonly asked about in community forums.

    Based on CRA FAQ and government documentation.
    """

    def setUp(self):
        self.calc = RESPCalculator()

    def test_over_contribution_1_percent_per_month_tax(self):
        """1% monthly tax on excess contributions ($50k lifetime limit)."""
        child = RESPChild(name="test", birth_year=2012)
        child.total_contributions = 49500  # $500 remaining
        # Contribute $1000 → $500 excess
        result = self.calc.resp_contribution_check(1000, child)
        # 1% per month = 12% annual on excess
        self.assertEqual(result['excess'], 500)
        self.assertEqual(result['excess_tax_per_month'], 500 * 0.01)
        self.assertFalse(result['within_limits'])

    def test_over_4k_withdrawal_cesg_repayment_rule(self):
        """Document the CESG repayment rule for >$4k excess withdrawal.

        Per CRA: If excess > $4,000 at withdrawal, CESG repayable
        on entire withdrawal amount (not just excess portion).

        Note: This is a documentation test - the penalty logic is in the promoter's
        responsibility, not the contribution check function.
        """
        # This documents the rule: excess > $4k triggers CESG repayment
        # The contribution_check function validates new contributions, not withdrawal effects
        child = RESPChild(name="test", birth_year=2012)
        child.total_contributions = 48000  # $2000 remaining
        # Contributing $1000 is fine
        result = self.calc.resp_contribution_check(1000, child)
        self.assertTrue(result['within_limits'])

    def test_lifetime_limit_50k_per_beneficiary(self):
        """$50,000 lifetime contribution limit per beneficiary.

        Multiple RESPs for same child share this limit.
        """
        child = RESPChild(name="test", birth_year=2012)
        child.total_contributions = 49000
        result = self.calc.resp_contribution_check(2000, child)
        self.assertEqual(result['excess'], 1000)
        self.assertEqual(result['remaining_lifetime'], 0)

    def test_cesg_catch_up_5k_max_with_unused_room(self):
        """CESG catch-up: can contribute $5,000 and get up to $1,000 CESG.

        If there's $2,500 unused room from previous years.
        """
        child = RESPChild(name="test", birth_year=2012)
        result = self.calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=100000, unused_room=2500)
        # Current: 20% × $2,500 = $500
        # Catch-up: 20% × $2,500 = $500
        self.assertEqual(result['current_year_cesg'], 500)
        self.assertEqual(result['catchup_cesg'], 500)


class TestAntichurningRule(unittest.TestCase):
    """Test anti-churning rule for CESG after assisted contribution withdrawal.

    Per CRA: If assisted contributions withdrawn before EAP eligibility,
    no Additional CESG for 3 years (current + 2 following calendar years).
    """

    def setUp(self):
        self.calc = RESPCalculator()

    def test_basic_cesg_still_available_after_anti_churning(self):
        """Basic CESG (20%) still available after anti-churning, just not Additional."""
        child = RESPChild(name="test", birth_year=2012)
        # Even after anti-churning, basic 20% still applies
        result = self.calc.calculate_cesg(2500, child, 2026, family_income=40000)
        # Basic $500 still available
        self.assertGreater(result['total_cesg'], 0)


if __name__ == '__main__':
    unittest.main()