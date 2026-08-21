#!/usr/bin/env python3
"""Issue #304: RESP/CESG/QESI/CLB excess penalty, EAP/AIP, CLB incidental expense, and edge cases.

Tests for:
- CLB $25 incidental expense payment
- EAP payment limits ($8,000 qualifying / $4,000 specified)
- AIP 20% additional tax (already in resp_collapse_proceeds, documented here)
- CESG carry-forward across years with income threshold changes
- QESI supplementary rate with income just above/below second threshold
- Child turning 17 mid-year (eligibility ends in calendar year they turn 17)

Run: uv run pytest tests/test_resp_issue_304.py -v
"""

import unittest
from countries.canada.resp_rules import (
    RESPCalculator, RESPChild,
    get_cesg_thresholds, get_qesi_thresholds,
)


class TestCLBIncidentalExpense(unittest.TestCase):
    """Issue #304: CLB includes a $25 incidental expense payment when first paid."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_first_clb_payment_includes_incidental(self):
        """First CLB payment includes $500 + $25 incidental expense."""
        child = RESPChild(name="test", birth_year=2020)
        result = self.calc.calculate_clb(child, 2026, family_income=30000, num_children=1)
        self.assertEqual(result['clb_amount'], 500)
        self.assertEqual(result['incidental_expense'], 25)
        self.assertEqual(result['total_clb_deposit'], 525)

    def test_subsequent_clb_no_incidental(self):
        """Subsequent annual CLB payments do NOT include the incidental expense."""
        child = RESPChild(name="test", birth_year=2020)
        child.total_clb_received = 500  # Already received initial payment
        result = self.calc.calculate_clb(child, 2027, family_income=30000, num_children=1)
        self.assertEqual(result['clb_amount'], 100)
        self.assertEqual(result['incidental_expense'], 0)
        self.assertEqual(result['total_clb_deposit'], 100)

    def test_clb_incidental_expense_constant(self):
        """CLB incidental expense is $25 per CRA policy."""
        self.assertEqual(self.calc.CLB_INCIDENTAL_EXPENSE, 25)

    def test_clb_not_eligible_no_incidental(self):
        """Non-eligible CLB returns no incidental expense."""
        child = RESPChild(name="test", birth_year=2000)  # Born before 2004
        result = self.calc.calculate_clb(child, 2026, family_income=30000, num_children=1)
        self.assertEqual(result['clb_amount'], 0)
        self.assertNotIn('incidental_expense', result)


class TestEAPPaymentLimits(unittest.TestCase):
    """Issue #304: EAP payment limits per ITA s.146.1."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_qualifying_program_limit_8000(self):
        """Qualifying educational program: $8,000 limit for 13-week term."""
        result = self.calc.eap_payment_limit(qualifying_program=True, weeks_of_program=13)
        self.assertEqual(result['eap_limit'], 8000)
        self.assertTrue(result['qualifying_program'])

    def test_specified_program_limit_4000(self):
        """Specified educational program: $4,000 limit for 13-week term."""
        result = self.calc.eap_payment_limit(qualifying_program=False, weeks_of_program=13)
        self.assertEqual(result['eap_limit'], 4000)
        self.assertFalse(result['qualifying_program'])

    def test_short_program_pro_rated(self):
        """Shorter programs have pro-rated EAP limits."""
        # 6-week qualifying program: $8,000 × (6/13) ≈ $3,692
        result = self.calc.eap_payment_limit(qualifying_program=True, weeks_of_program=6)
        self.assertAlmostEqual(result['eap_limit'], 8000 * 6 / 13, places=2)

    def test_year_long_program_has_normal_limit(self):
        """Year-long (52-week) program: EAP limit is still $8,000 per 13-week term."""
        result = self.calc.eap_payment_limit(qualifying_program=True, weeks_of_program=13)
        # Each 13-week term has its own $8,000 limit
        self.assertEqual(result['eap_limit'], 8000)

    def test_specified_short_program_pro_rated(self):
        """6-week specified program: $4,000 × (6/13) ≈ $1,846."""
        result = self.calc.eap_payment_limit(qualifying_program=False, weeks_of_program=6)
        self.assertAlmostEqual(result['eap_limit'], 4000 * 6 / 13, places=2)


class TestAIPPenaltyRate(unittest.TestCase):
    """Issue #304: AIP (Accumulated Income Payment) includes 20% additional tax."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_aip_penalty_rate_constant(self):
        """AIP penalty rate is 20% (ITA s.146.1)."""
        self.assertEqual(self.calc.AIP_PENALTY_RATE, 0.20)

    def test_aip_total_rate_includes_mtr(self):
        """AIP total tax = MTR + 20% penalty."""
        base_cfg = {
            'accounts': {
                'resp_current_balance': 100000,
                'resp_composition': {
                    'total_contributions': 50000,
                    'total_cesg_received': 5000,
                    'total_qesi_received': 2500,
                    'investment_earnings': 42500,
                },
            },
        }
        n_mtr = 0.30
        result = self.calc.resp_collapse_proceeds(base_cfg, n_mtr)
        # Total AIP rate = 0.30 + 0.20 = 0.50
        expected_tax = 42500 * 0.50
        self.assertAlmostEqual(result['tax_cost'], expected_tax, places=0)
        self.assertAlmostEqual(result['effective_tax_rate'], 0.50, places=2)


class TestCESGCarryForwardWithThresholdChanges(unittest.TestCase):
    """Issue #304: CESG carry-forward across years with income threshold changes."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_cesg_thresholds_change_between_years(self):
        """Income thresholds for additional CESG change between 2024 and 2026."""
        thresholds_2024 = get_cesg_thresholds(2024)
        thresholds_2026 = get_cesg_thresholds(2026)
        # 2026 thresholds should be higher (indexed)
        self.assertGreater(thresholds_2026['first_threshold'], thresholds_2024['first_threshold'])
        self.assertGreater(thresholds_2026['second_threshold'], thresholds_2024['second_threshold'])

    def test_family_income_near_threshold_boundary(self):
        """Family income just below first CESG threshold gets additional CESG."""
        child = RESPChild(name="test", birth_year=2018)
        thresholds_2026 = get_cesg_thresholds(2026)
        # Income just below first threshold → additional CESG applies
        income = thresholds_2026['first_threshold'] - 1
        result = self.calc.calculate_cesg(2500, child, 2026, income)
        self.assertGreater(result['additional_cesg'], 0)

    def test_family_income_just_above_first_threshold_no_additional(self):
        """Family income above second CESG threshold: no additional CESG."""
        child = RESPChild(name="test", birth_year=2018)
        thresholds_2026 = get_cesg_thresholds(2026)
        # Income above second threshold → no additional CESG
        income = thresholds_2026['second_threshold'] + 1
        result = self.calc.calculate_cesg(2500, child, 2026, income)
        self.assertEqual(result['additional_cesg'], 0)

    def test_carry_forward_grants_accumulate(self):
        """Unused CESG room carries forward from previous years."""
        child = RESPChild(name="test", birth_year=2020)
        # Year 1: Contribute $1000, get $200 basic CESG
        result1 = self.calc.calculate_cesg(1000, child, 2026, 150000)
        self.assertEqual(result1['basic_cesg'], 200)  # 20% of $1000
        # The remaining room carries forward to next year


class TestQESISupplementaryRateThreshold(unittest.TestCase):
    """Issue #304: QESI supplementary rate with income at threshold boundaries."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_qesi_thresholds_change_between_years(self):
        """QESI income thresholds change between years."""
        thresholds_2024 = get_qesi_thresholds(2024)
        thresholds_2026 = get_qesi_thresholds(2026)
        self.assertGreater(thresholds_2026['first_threshold'], thresholds_2024['first_threshold'])

    def test_income_below_first_qesi_threshold_gets_supplementary(self):
        """Income below QESI first threshold: supplementary QESI applies."""
        child = RESPChild(name="test", birth_year=2018, province='quebec')
        thresholds_2026 = get_qesi_thresholds(2026)
        income = thresholds_2026['first_threshold'] - 1
        result = self.calc.calculate_qesi(2500, child, 2026, income)
        # Should get basic (10%) + supplementary QESI
        self.assertGreater(result['total_qesi'], 0)
        self.assertTrue(result['eligible'])

    def test_income_above_second_qesi_threshold_no_supplementary(self):
        """Income above QESI second threshold: basic rate only, no supplementary."""
        child = RESPChild(name="test", birth_year=2018, province='quebec')
        thresholds_2026 = get_qesi_thresholds(2026)
        income = thresholds_2026['second_threshold'] + 1
        result = self.calc.calculate_qesi(2500, child, 2026, income)
        # Should get basic QESI only (10%), no supplementary
        self.assertGreater(result['total_qesi'], 0)
        # Supplementary QESI should be 0 for high-income families


class TestChildAge17EligibilityEdgeCase(unittest.TestCase):
    """Issue #304: Child turning 17 mid-year — eligibility ends in calendar year they turn 17."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_cesg_eligible_at_16(self):
        """Child aged 16 can be eligible for CESG if 16-17 contribution conditions met."""
        child = RESPChild(name="test", birth_year=2010)  # 16 in 2026
        # Condition: total_before_age_15 >= $2000
        child.total_before_age_15 = 2000
        self.assertTrue(child.cesg_16_17_eligible(2026))

    def test_cesg_not_eligible_at_18(self):
        """Child aged 18 is NOT eligible for CESG."""
        child = RESPChild(name="test", birth_year=2008)  # 18 in 2026
        self.assertFalse(child.cesg_eligible(2026))

    def test_cesg_eligible_at_17_with_contributions(self):
        """Child aged 17 is eligible for CESG if 16-17 contribution conditions met."""
        child = RESPChild(name="test", birth_year=2009)  # 17 in 2026
        child.total_contributions = 2000
        self.assertTrue(child.cesg_eligible(2026))

    def test_cesg_not_eligible_at_17_without_contributions(self):
        """Child aged 17 without 16-17 contribution conditions is NOT eligible."""
        child = RESPChild(name="test", birth_year=2009)  # 17 in 2026
        child.total_contributions = 100  # Too low
        self.assertFalse(child.cesg_16_17_eligible(2026))

    def test_clb_not_eligible_at_16(self):
        """CLB eligibility ends at age 15 (not available at 16+)."""
        child = RESPChild(name="test", birth_year=2010)  # 16 in 2026
        result = self.calc.calculate_clb(child, 2026, family_income=30000, num_children=1)
        self.assertFalse(result['eligible'])

    def test_clb_eligible_at_15(self):
        """CLB is still available at age 15."""
        child = RESPChild(name="test", birth_year=2011)  # 15 in 2026
        result = self.calc.calculate_clb(child, 2026, family_income=30000, num_children=1)
        self.assertTrue(result['eligible'])


class TestRESPExcessPenalty(unittest.TestCase):
    """Issue #304: 1%/month penalty on excess contributions over $50k lifetime."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_within_lifetime_limit(self):
        """Contribution within $50k lifetime limit: no excess, no penalty."""
        child = RESPChild(name="test", birth_year=2018)
        child.total_contributions = 40000
        result = self.calc.resp_contribution_check(10000, child)
        self.assertTrue(result['within_limits'])
        self.assertEqual(result['excess'], 0)
        self.assertEqual(result['excess_tax_per_month'], 0)

    def test_exactly_at_lifetime_limit(self):
        """Contribution that exactly reaches $50k: no excess."""
        child = RESPChild(name="test", birth_year=2018)
        child.total_contributions = 45000
        result = self.calc.resp_contribution_check(5000, child)
        self.assertTrue(result['within_limits'])
        self.assertEqual(result['excess'], 0)

    def test_over_lifetime_limit_by_1000(self):
        """$1,000 excess over $50k: 1%/month = $10/month penalty."""
        child = RESPChild(name="test", birth_year=2018)
        child.total_contributions = 49500
        result = self.calc.resp_contribution_check(1500, child)
        # 49500 + 1500 = 51000, excess = 1000
        # Contribution allowed: 500
        # Excess: 1000
        # Penalty: 1000 × 0.01 = $10/month
        self.assertFalse(result['within_limits'])
        self.assertEqual(result['excess'], 1000)
        self.assertEqual(result['excess_tax_per_month'], 10.0)
        self.assertEqual(result['contribution_allowed'], 500)


if __name__ == '__main__':
    unittest.main()