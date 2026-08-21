#!/usr/bin/env python3
"""Tests for FHSA non-qualifying withdrawal tax, first-home eligibility, and carry-forward.

Issue #50: FHSA non-qualifying withdrawal tax modeled as 10% withholding only,
but the actual cost is MTR (withholding is a pre-payment, not additional).
First-home eligibility is computed from dates (DP#28), not a boolean flag.
Carry-forward tracking records which year's room is carried forward (DP#20).

Tests cover:
1. Non-qualifying withdrawal: income tax (MTR) + withholding (pre-payment, flat-rate brackets)
2. First-home buyer eligibility from principal residence history (DP#28)
3. FHSA opening eligibility (checks account holder AND spouse)
4. Qualifying withdrawal: checks eligibility BEFORE modifying state
5. Carry-forward tracking with year information (DP#20)
6. CRA flat-rate withholding brackets (10%/20%/30%, QC 19%/29%/34%)
7. FHSA closure timing (age 71 vs 15th anniversary)

Run with: python3 -m pytest countries/canada/tests/test_fhsa_issue_50.py -v
"""

import unittest

from countries.canada.fhsa import (
    FHSAAccount, fhsa_double_deduction_analysis,
    FHSA_ANNUAL_LIMIT, FHSA_LIFETIME_LIMIT,
    compute_withholding_tax, FHSA_WITHHOLDING_BRACKETS, FHSA_WITHHOLDING_BRACKETS_QC,
)


class TestWithholdingTaxCalculation(unittest.TestCase):
    """CRA flat-rate withholding brackets for non-qualifying withdrawals.

    CRA applies a single flat rate to the ENTIRE withdrawal amount based
    on which bracket the total falls into. This is NOT a marginal/tiered
    calculation — the rate applies to the full amount.
    """

    def test_withholding_under_5000(self):
        """10% flat rate on amounts up to $5,000."""
        self.assertAlmostEqual(compute_withholding_tax(3000), 300)
        self.assertAlmostEqual(compute_withholding_tax(5000), 500)

    def test_withholding_between_5000_and_15000(self):
        """20% flat rate on the entire amount for $5,001–$15,000."""
        # $10k falls in the $5,001-$15,000 bracket → 20% × $10k = $2,000
        self.assertAlmostEqual(compute_withholding_tax(10000), 2000)
        # $5,001 falls in the 20% bracket → 20% × $5,001 = $1,000.20
        self.assertAlmostEqual(compute_withholding_tax(5001), 1000.20)

    def test_withholding_over_15000(self):
        """30% flat rate on the entire amount for amounts over $15,000."""
        # $20k falls in the >$15,000 bracket → 30% × $20k = $6,000
        self.assertAlmostEqual(compute_withholding_tax(20000), 6000)
        # $15,001 falls in the 30% bracket → 30% × $15,001 = $4,500.30
        self.assertAlmostEqual(compute_withholding_tax(15001), 4500.30)

    def test_withholding_zero(self):
        """Zero withholding on zero amount."""
        self.assertAlmostEqual(compute_withholding_tax(0), 0)

    def test_withholding_negative(self):
        """Zero withholding on negative amount."""
        self.assertAlmostEqual(compute_withholding_tax(-1000), 0)

    def test_quebec_withholding_under_5000(self):
        """19% Quebec flat rate on amounts up to $5,000."""
        self.assertAlmostEqual(compute_withholding_tax(3000, quebec=True), 570)
        self.assertAlmostEqual(compute_withholding_tax(5000, quebec=True), 950)

    def test_quebec_withholding_between_5000_and_15000(self):
        """29% Quebec flat rate on the entire amount for $5,001–$15,000."""
        # $10k → 29% × $10k = $2,900
        self.assertAlmostEqual(compute_withholding_tax(10000, quebec=True), 2900)

    def test_quebec_withholding_over_15000(self):
        """34% Quebec flat rate on the entire amount for >$15,000."""
        # $20k → 34% × $20k = $6,800
        self.assertAlmostEqual(compute_withholding_tax(20000, quebec=True), 6800)


class TestFHSANonQualifyingWithdrawalTax(unittest.TestCase):
    """DP#50: Non-qualifying withdrawal tax = MTR (withholding is pre-payment)."""

    def test_income_tax_is_mtr_times_amount(self):
        """Income tax on non-qualifying withdrawal = MTR × amount."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        result = fhsa.non_qualifying_withdrawal(2028, marginal_rate=0.4571)
        # Income tax is the actual tax burden
        self.assertAlmostEqual(result['income_tax'], 8000 * 0.4571, places=0)

    def test_withholding_is_prepayment_not_additional(self):
        """Withholding is a pre-payment of income tax, not an additional tax."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        result = fhsa.non_qualifying_withdrawal(2028, marginal_rate=0.50)
        # Net tax owing = MTR × amount - withholding (pre-payment)
        # With $8k withdrawal: income_tax = $4,000
        # Withholding = 20% × $8k = $1,600 (falls in $5,001-$15k bracket)
        # Net owing = $4,000 - $1,600 = $2,400
        expected_income = 8000 * 0.50
        expected_withholding = compute_withholding_tax(8000)  # 20% × $8k = $1,600
        expected_net = expected_income - expected_withholding
        self.assertAlmostEqual(result['net_tax_owing'], expected_net)
        self.assertAlmostEqual(result['income_tax'], expected_income)
        self.assertAlmostEqual(result['withholding_tax'], expected_withholding)

    def test_effective_tax_rate_is_mtr_not_mtr_plus_withholding(self):
        """Effective tax rate is MTR, not MTR + withholding rate."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        result = fhsa.non_qualifying_withdrawal(2028, marginal_rate=0.50)
        # The actual tax rate you pay is just your MTR (50%)
        # The withholding is a pre-payment, not additional
        self.assertAlmostEqual(result['effective_tax_rate'], 0.50)

    def test_withholding_greater_than_income_tax_yields_refund(self):
        """When withholding > income tax, net_owing is 0 (you get a refund)."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        # Very low MTR means withholding exceeds income tax
        result = fhsa.non_qualifying_withdrawal(2028, marginal_rate=0.05)
        # Income tax = $400, withholding = 20% × $8k = $1,600
        # Net owing = max(0, $400 - $1,600) = $0 (you get a refund)
        self.assertAlmostEqual(result['net_tax_owing'], 0)
        self.assertAlmostEqual(result['income_tax'], 400)

    def test_zero_balance_no_tax(self):
        """Zero balance: no tax on non-qualifying withdrawal."""
        fhsa = FHSAAccount()
        result = fhsa.non_qualifying_withdrawal(2028, marginal_rate=0.50)
        self.assertAlmostEqual(result['income_tax'], 0)
        self.assertAlmostEqual(result['withholding_tax'], 0)

    def test_quebec_withholding_rates(self):
        """Quebec uses 19%/29%/34% flat-rate withholding brackets."""
        fhsa = FHSAAccount()
        # Build up $20k balance over multiple years
        for _ in range(5):
            fhsa.contribute(8000)
            fhsa.grow(0.0)
            fhsa.add_annual_room()
        # Override balance for withholding bracket test
        fhsa.balance = 20000
        result = fhsa.non_qualifying_withdrawal(2028, marginal_rate=0.50, quebec=True)
        # Quebec: $20k falls in >$15k bracket → 34% × $20k = $6,800
        self.assertAlmostEqual(result['withholding_tax'], 6800)
        # Income tax is still MTR × amount = $10,000
        self.assertAlmostEqual(result['income_tax'], 10000)
        # Net owing = $10,000 - $6,800 = $3,200
        self.assertAlmostEqual(result['net_tax_owing'], 3200)

    def test_withholding_varies_by_amount_bracket(self):
        """Withholding varies by amount per CRA flat-rate brackets."""
        fhsa_small = FHSAAccount()
        fhsa_small.contribute(3000)
        result_small = fhsa_small.non_qualifying_withdrawal(2028, marginal_rate=0.50)
        # $3k: 10% × $3k = $300
        self.assertAlmostEqual(result_small['withholding_tax'], 300)

        fhsa_mid = FHSAAccount()
        fhsa_mid.contribute(8000)
        fhsa_mid.add_annual_room()
        fhsa_mid.contribute(8000)
        fhsa_mid.balance = 10000  # Override for bracket test
        result_mid = fhsa_mid.non_qualifying_withdrawal(2028, marginal_rate=0.50)
        # $10k: 20% × $10k = $2,000
        self.assertAlmostEqual(result_mid['withholding_tax'], 2000)


class TestFirstHomeBuyerEligibility(unittest.TestCase):
    """DP#28: First-home buyer eligibility computed from principal residence history."""

    def test_no_residence_history_eligible(self):
        """No principal residence history: eligible as first-home buyer."""
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = []
        self.assertTrue(fhsa.is_first_home_buyer(2030))

    def test_residence_5_years_ago_still_eligible(self):
        """Lived in principal residence 5+ years ago: eligible (4-year lookback)."""
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = [2020]  # 10 years before withdrawal
        self.assertTrue(fhsa.is_first_home_buyer(2030))

    def test_residence_within_4_years_ineligible(self):
        """Lived in principal residence within 4 years: NOT eligible."""
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = [2027]  # 3 years before withdrawal in 2030
        self.assertFalse(fhsa.is_first_home_buyer(2030))

    def test_residence_in_withdrawal_year_ineligible(self):
        """Lived in principal residence in the withdrawal year: NOT eligible."""
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = [2030]
        self.assertFalse(fhsa.is_first_home_buyer(2030))

    def test_withdrawal_does_not_check_spouse(self):
        """DP#28: Qualifying withdrawal only checks account holder, NOT spouse.

        Per CRA, spouse ownership only matters for opening an FHSA,
        not for making a qualifying withdrawal.
        """
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = []
        # is_first_home_buyer does NOT accept spouse parameter
        self.assertTrue(fhsa.is_first_home_buyer(2030))


class TestFHSALOpeningEligibility(unittest.TestCase):
    """FHSA opening eligibility checks both account holder AND spouse."""

    def test_holder_no_residence_eligible(self):
        """Account holder with no residence history: eligible to open."""
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = []
        self.assertTrue(fhsa.is_eligible_for_opening(2030))

    def test_spouse_residence_makes_ineligible(self):
        """Spouse's principal residence also matters for opening."""
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = []
        spouse_years = [2028]
        self.assertFalse(fhsa.is_eligible_for_opening(2030, spouse_residence_years=spouse_years))

    def test_spouse_residence_5_years_ago_eligible(self):
        """Spouse's residence 5+ years ago: still eligible to open."""
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = []
        spouse_years = [2024]  # 6 years before 2030
        self.assertTrue(fhsa.is_eligible_for_opening(2030, spouse_residence_years=spouse_years))

    def test_investment_property_does_not_disqualify(self):
        """Owning an investment property you don't live in does NOT disqualify.

        The field tracks principal_residence_years, not just ownership.
        """
        fhsa = FHSAAccount()
        fhsa.principal_residence_years = []  # Never lived in a home you owned
        self.assertTrue(fhsa.is_eligible_for_opening(2030))


class TestQualifyingWithdrawalStateIntegrity(unittest.TestCase):
    """qualifying_withdrawal must not corrupt state when ineligible."""

    def test_eligible_withdrawal_modifies_state(self):
        """Eligible qualifying withdrawal modifies state correctly."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.principal_residence_years = []
        result = fhsa.qualifying_withdrawal(2030)
        self.assertTrue(result['eligible'])
        self.assertTrue(result['tax_free'])
        self.assertAlmostEqual(result['amount'], 8000)
        self.assertTrue(fhsa.qualifying_withdrawal_made)
        self.assertAlmostEqual(fhsa.balance, 0)

    def test_ineligible_withdrawal_preserves_state(self):
        """Ineligible qualifying withdrawal does NOT modify account state.

        DP#28: Eligibility is checked BEFORE state modification.
        """
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.principal_residence_years = [2028]  # Recent principal residence
        original_balance = fhsa.balance
        original_qw_made = fhsa.qualifying_withdrawal_made

        result = fhsa.qualifying_withdrawal(2030, marginal_rate=0.50)

        self.assertFalse(result['eligible'])
        # State must NOT be modified
        self.assertAlmostEqual(fhsa.balance, original_balance)
        self.assertEqual(fhsa.qualifying_withdrawal_made, original_qw_made)
        # Non-qualifying cost info should be provided
        self.assertIn('non_qualifying_cost', result)

    def test_ineligible_withdrawal_shows_non_qualifying_cost(self):
        """Ineligible withdrawal shows non-qualifying cost estimates."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.principal_residence_years = [2028]
        result = fhsa.qualifying_withdrawal(2030, marginal_rate=0.50)

        self.assertIn('non_qualifying_cost', result)
        cost = result['non_qualifying_cost']
        self.assertIn('withholding_tax', cost)
        self.assertIn('income_tax', cost)
        self.assertIn('net_tax_owing', cost)
        # Effective rate should be MTR (not MTR + withholding)
        self.assertAlmostEqual(cost['effective_tax_rate'], 0.50)

    def test_ineligible_withdrawal_quebec_rates(self):
        """Ineligible withdrawal with Quebec rates uses QC withholding."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.principal_residence_years = [2028]
        result = fhsa.qualifying_withdrawal(2030, marginal_rate=0.50, quebec=True)
        cost = result['non_qualifying_cost']
        # Quebec: $8k falls in $5,001-$15k bracket → 29% × $8k = $2,320
        self.assertAlmostEqual(cost['withholding_tax'], 2320)


class TestFHSACarryForwardTracking(unittest.TestCase):
    """DP#20: Carry-forward tracking with year information."""

    def test_carry_forward_tracks_year(self):
        """Carry-forward room tracks the year it came from."""
        fhsa = FHSAAccount()
        fhsa.contribute(4000)  # Use only half
        fhsa.add_annual_room(current_year=2027)  # 2027 rolls over 2026 room
        # Carry-forward came from 2026 (current_year - 1)
        self.assertEqual(fhsa.carry_forward_year, 2026)
        self.assertAlmostEqual(fhsa.carry_forward_room, 4000)

    def test_carry_forward_year_without_current_year(self):
        """Without current_year, carry_forward_year is not set."""
        fhsa = FHSAAccount()
        fhsa.contribute(4000)
        fhsa.add_annual_room()  # No current_year provided
        # carry_forward_year should remain None
        self.assertIsNone(fhsa.carry_forward_year)
        self.assertAlmostEqual(fhsa.carry_forward_room, 4000)

    def test_carry_forward_used_first(self):
        """Carry-forward room is used before annual room."""
        fhsa = FHSAAccount()
        fhsa.contribute(4000)  # Use half
        fhsa.add_annual_room(current_year=2028)  # Unused 4k becomes carry-forward
        # Year 2: $4k carry-forward + $8k annual = $12k available
        actual = fhsa.contribute(12000)
        self.assertAlmostEqual(actual, 12000)
        self.assertAlmostEqual(fhsa.carry_forward_room, 0)

    def test_carry_forward_capped_at_annual_limit(self):
        """Carry-forward is capped at $8k (one year's room)."""
        fhsa = FHSAAccount()
        fhsa.contribute(0)  # Skip a year
        fhsa.add_annual_room()
        self.assertAlmostEqual(fhsa.carry_forward_room, 8000)

    def test_summary_includes_carry_forward_year(self):
        """Summary includes carry-forward year information."""
        fhsa = FHSAAccount()
        fhsa.contribute(4000)
        fhsa.add_annual_room(current_year=2027)
        s = fhsa.summary()
        self.assertIn('carry_forward_year', s)
        self.assertEqual(s['carry_forward_year'], 2026)
        self.assertIn('principal_residence_years', s)


class TestFHSAClosureTiming(unittest.TestCase):
    """FHSA closure timing: age 71 vs 15th anniversary vs withdrawal + 1 year."""

    def test_close_at_age_71(self):
        """FHSA must close when owner turns 71."""
        fhsa = FHSAAccount(open_year=2020)
        self.assertTrue(fhsa.must_close(2026, 1955))  # Born 1955, turns 71 in 2026

    def test_close_at_15th_anniversary(self):
        """FHSA must close at 15th anniversary of opening."""
        fhsa = FHSAAccount(open_year=2020)
        self.assertTrue(fhsa.must_close(2035, 1990))  # 2020 + 15 = 2035

    def test_whichever_comes_first(self):
        """FHSA closes at whichever comes first: age 71 or 15th anniversary."""
        fhsa = FHSAAccount(open_year=2020)
        # Born 1955: age 71 in 2026, 15th anniversary in 2035
        self.assertTrue(fhsa.must_close(2026, 1955))  # Age 71 comes first
        self.assertFalse(fhsa.must_close(2025, 1955))  # Age 70, still open

    def test_no_close_before_deadline(self):
        """FHSA stays open before any deadline."""
        fhsa = FHSAAccount(open_year=2020)
        self.assertFalse(fhsa.must_close(2026, 1990))
        self.assertFalse(fhsa.must_close(2030, 1990))

    def test_close_year_after_qualifying_withdrawal(self):
        """FHSA closes year after qualifying withdrawal."""
        fhsa = FHSAAccount(open_year=2020)
        fhsa.principal_residence_years = []
        fhsa.contribute(8000)
        fhsa.qualifying_withdrawal(2026)
        self.assertTrue(fhsa.must_close(2027, 1990))  # Year after withdrawal
        self.assertFalse(fhsa.must_close(2026, 1990))  # Withdrawal year


if __name__ == '__main__':
    unittest.main()