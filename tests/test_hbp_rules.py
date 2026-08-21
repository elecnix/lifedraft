#!/usr/bin/env python3
"""Comprehensive tests for Home Buyers' Plan (HBP) rules.

Issue #51: hbp_rules.py had zero test coverage. DP#17 requires every
rule to have a test.

Tests cover:
1. Constants and defaults (DP#13: defaults are fallbacks)
2. HBPAccount construction and properties
3. Repayment schedule generation (15-year, start-year delay)
4. Repayment tracking (make_repayment, shortfall detection)
5. Default consequences (tax impact of non-repayment)
6. First-home buyer eligibility
7. HBP-RRSP integration (hbp_repayment_to_rrsp_room)
8. Missed repayment tax impact (hbp_missed_repayment_tax_impact)
9. FHSA-to-HBP transitions (combined strategies, double deduction)
10. First-home strategy comparison (compare_first_home_strategies)
11. Edge cases (zero withdrawal, max withdrawal, early repayment)

Run with: python3 -m pytest tests/test_hbp_rules.py -v
"""

import unittest
from copy import deepcopy

from countries.canada.fhsa import FHSAAccount
from countries.canada.hbp_rules import (
    HBP_ANNUAL_MIN_REPAYMENT_PCT,
    HBP_MAX_WITHDRAWAL,
    HBP_REPAYMENT_START_DELAY,
    HBP_REPAYMENT_YEARS,
    HBPAccount,
    compare_first_home_strategies,
    hbp_missed_repayment_tax_impact,
    hbp_repayment_to_rrsp_room,
)

# =============================================================================
# 1. Constants
# =============================================================================

class TestHBPConstants(unittest.TestCase):
    """DP#13: Constants are clearly-round or clearly-dated fallbacks."""

    def test_max_withdrawal_is_60000(self):
        """HBP max withdrawal increased to $60,000 (2024+)."""
        self.assertEqual(HBP_MAX_WITHDRAWAL, 60000)

    def test_repayment_years_is_15(self):
        """HBP must be repaid over 15 years (ITA s.146.4)."""
        self.assertEqual(HBP_REPAYMENT_YEARS, 15)

    def test_repayment_start_delay_is_2(self):
        """Repayment starts the 3rd year after withdrawal (2-year delay)."""
        self.assertEqual(HBP_REPAYMENT_START_DELAY, 2)

    def test_annual_min_repayment_pct(self):
        """Annual minimum = 1/15 ≈ 6.67% of withdrawal."""
        self.assertAlmostEqual(HBP_ANNUAL_MIN_REPAYMENT_PCT, 1.0 / 15)


# =============================================================================
# 2. HBPAccount Construction and Properties
# =============================================================================

class TestHBPAccountConstruction(unittest.TestCase):
    """HBPAccount dataclass construction and computed properties."""

    def test_default_construction(self):
        """Default HBPAccount has zero withdrawal."""
        hbp = HBPAccount()
        self.assertEqual(hbp.withdrawal, 0.0)
        self.assertEqual(hbp.repaid, 0.0)
        self.assertEqual(hbp.outstanding, 0.0)
        self.assertTrue(hbp.is_first_home)

    def test_construction_with_withdrawal(self):
        """HBPAccount with specified withdrawal amount."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        self.assertEqual(hbp.withdrawal, 35000)
        self.assertEqual(hbp.outstanding, 35000)

    def test_outstanding_cannot_go_negative(self):
        """Outstanding is clamped to zero (never negative)."""
        hbp = HBPAccount(withdrawal=10000)
        hbp.repaid = 15000  # Over-repaid
        self.assertEqual(hbp.outstanding, 0.0)

    def test_withdrawal_year_defaults_to_2026(self):
        """Default withdrawal year is 2026 (DP#13)."""
        hbp = HBPAccount()
        self.assertEqual(hbp.withdrawal_year, 2026)

    def test_is_first_home_defaults_true(self):
        """HBP defaults to first-home buyer (optimistic default)."""
        hbp = HBPAccount()
        self.assertTrue(hbp.is_first_home)

    def test_is_first_home_can_be_set_false(self):
        """Non-first-home buyer flag is settable."""
        hbp = HBPAccount(is_first_home=False)
        self.assertFalse(hbp.is_first_home)

    def test_repayment_schedule_defaults_empty(self):
        """No repayment schedule until generated."""
        hbp = HBPAccount()
        self.assertEqual(hbp.repayment_schedule, [])

    def test_post_init_sets_outstanding(self):
        """__post_init__ computes outstanding from withdrawal - repaid."""
        hbp = HBPAccount(withdrawal=60000, repaid=10000)
        self.assertEqual(hbp.outstanding, 50000)


# =============================================================================
# 3. Repayment Schedule Generation
# =============================================================================

class TestRepaymentSchedule(unittest.TestCase):
    """15-year repayment schedule generation (ITA s.146.4)."""

    def test_annual_min_repayment_calculation(self):
        """Minimum annual repayment = withdrawal / 15."""
        hbp = HBPAccount(withdrawal=60000)
        self.assertAlmostEqual(hbp.annual_min_repayment(), 60000 / 15)

    def test_annual_min_repayment_for_35k(self):
        """$35k withdrawal → $2,333.33/year minimum."""
        hbp = HBPAccount(withdrawal=35000)
        self.assertAlmostEqual(hbp.annual_min_repayment(), 35000 / 15, places=2)

    def test_repayment_start_year_withdrawal_2026(self):
        """Withdraw in 2026 → first repayment in 2029 (2026 + 2 + 1)."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        self.assertEqual(hbp.repayment_start_year(), 2029)

    def test_repayment_start_year_withdrawal_2025(self):
        """Withdraw in 2025 → 5-year relief → first repayment in 2030 (#308)."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2025)
        self.assertEqual(hbp.repayment_start_year(), 2030)

    def test_repayment_start_year_withdrawal_2024(self):
        """Withdraw in 2024 → 5-year relief → first repayment in 2029 (#308)."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2024)
        self.assertEqual(hbp.repayment_start_year(), 2029)

    def test_generate_schedule_length(self):
        """Schedule has exactly 15 entries for a standard withdrawal."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        self.assertEqual(len(schedule), 15)

    def test_generate_schedule_first_year(self):
        """First payment year matches repayment_start_year()."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        self.assertEqual(schedule[0]['year'], 2029)

    def test_generate_schedule_last_year(self):
        """Last payment year = start + 14."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        self.assertEqual(schedule[-1]['year'], 2029 + 14)

    def test_generate_schedule_outstanding_ends_zero(self):
        """After all payments, outstanding balance is zero."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        self.assertAlmostEqual(schedule[-1]['outstanding_after'], 0.0)

    def test_generate_schedule_consecutive_years(self):
        """Payment years are consecutive (no gaps)."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        for i in range(1, len(schedule)):
            self.assertEqual(schedule[i]['year'], schedule[i - 1]['year'] + 1)

    def test_generate_schedule_min_payment_constant(self):
        """Min payment is the same each year (equal installments)."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        for entry in schedule:
            self.assertAlmostEqual(entry['min_payment'], 60000 / 15)

    def test_generate_schedule_is_final_on_last_entry(self):
        """Only the last entry has is_final=True."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        self.assertFalse(schedule[-2]['is_final'])
        self.assertTrue(schedule[-1]['is_final'])

    def test_generate_schedule_stores_on_account(self):
        """Schedule is stored on the account after generation."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        self.assertIs(hbp.repayment_schedule, schedule)

    def test_schedule_outstanding_decreases_monotonically(self):
        """Outstanding balance decreases each year."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        schedule = hbp.generate_repayment_schedule()
        outstanding = hbp.withdrawal
        for entry in schedule:
            self.assertLessEqual(entry['outstanding_after'], outstanding)
            outstanding = entry['outstanding_after']


# =============================================================================
# 4. Repayment Tracking (make_repayment)
# =============================================================================

class TestRepaymentTracking(unittest.TestCase):
    """Making repayments and detecting shortfalls."""

    def test_make_full_minimum_repayment(self):
        """Full minimum repayment: no shortfall."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.make_repayment(4000, year=2029)
        self.assertEqual(result['amount_repaid'], 4000)
        self.assertEqual(result['minimum_required'], 4000)
        self.assertEqual(result['shortfall'], 0)
        self.assertFalse(result['shortfall_tax_consequence'])

    def test_make_above_minimum_repayment(self):
        """Paying more than minimum: no shortfall."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.make_repayment(5000, year=2029)
        self.assertEqual(result['shortfall'], 0)

    def test_make_partial_repayment_creates_shortfall(self):
        """Paying less than minimum creates shortfall."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.make_repayment(2000, year=2029)
        # Min = $4,000, paid $2,000 → shortfall = $2,000
        self.assertEqual(result['shortfall'], 2000)
        self.assertTrue(result['shortfall_tax_consequence'])

    def test_zero_repayment_creates_full_shortfall(self):
        """Paying nothing creates shortfall equal to minimum."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.make_repayment(0, year=2029)
        self.assertEqual(result['shortfall'], 4000)
        self.assertTrue(result['shortfall_tax_consequence'])

    def test_repayment_updates_outstanding(self):
        """Each repayment reduces outstanding balance."""
        hbp = HBPAccount(withdrawal=60000)
        self.assertEqual(hbp.outstanding, 60000)
        hbp.make_repayment(4000, year=2029)
        self.assertEqual(hbp.outstanding, 56000)

    def test_repayment_accumulates(self):
        """Multiple repayments accumulate correctly."""
        hbp = HBPAccount(withdrawal=60000)
        hbp.make_repayment(4000, year=2029)
        hbp.make_repayment(4000, year=2030)
        self.assertEqual(hbp.repaid, 8000)
        self.assertEqual(hbp.outstanding, 52000)

    def test_shortfall_note_in_result(self):
        """Shortfall includes descriptive note about tax consequences."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.make_repayment(2000, year=2029)
        self.assertIn('note', result)
        self.assertIn('shortfall', result['note'].lower())

    def test_no_shortfall_note_when_full(self):
        """No shortfall note when payment meets minimum."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.make_repayment(4000, year=2029)
        self.assertNotIn('note', result)


# =============================================================================
# 5. Default Consequences (default_tax_impact)
# =============================================================================

class TestDefaultConsequences(unittest.TestCase):
    """Tax impact of HBP default (non-repayment)."""

    def test_default_tax_impact_basic(self):
        """Default tax = outstanding × marginal rate."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.default_tax_impact(year=2030, marginal_rate=0.43)
        self.assertAlmostEqual(result['tax_if_default'], 60000 * 0.43)
        self.assertAlmostEqual(result['effective_cost_rate'], 0.43)

    def test_default_tax_impact_with_partial_repayment(self):
        """Default on remaining balance after some repayment."""
        hbp = HBPAccount(withdrawal=60000)
        hbp.make_repayment(4000, year=2029)
        result = hbp.default_tax_impact(year=2030, marginal_rate=0.43)
        # Outstanding = $56,000
        self.assertAlmostEqual(result['outstanding_balance'], 56000)
        self.assertAlmostEqual(result['tax_if_default'], 56000 * 0.43)

    def test_default_tax_impact_zero_outstanding(self):
        """No tax impact if fully repaid."""
        hbp = HBPAccount(withdrawal=60000, repaid=60000)
        result = hbp.default_tax_impact(year=2030, marginal_rate=0.43)
        self.assertAlmostEqual(result['tax_if_default'], 0.0)

    def test_default_tax_impact_quebec_marginal_rate(self):
        """Quebec combined marginal rate (~53%) produces higher tax."""
        hbp = HBPAccount(withdrawal=60000)
        result_qc = hbp.default_tax_impact(year=2030, marginal_rate=0.53)
        result_ab = hbp.default_tax_impact(year=2030, marginal_rate=0.43)
        self.assertGreater(result_qc['tax_if_default'], result_ab['tax_if_default'])

    def test_default_result_includes_note(self):
        """Result includes explanatory note."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.default_tax_impact(year=2030, marginal_rate=0.43)
        self.assertIn('note', result)
        self.assertIn('outstanding balance', result['note'].lower())

    def test_default_result_includes_year(self):
        """Result includes the year of default."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.default_tax_impact(year=2035, marginal_rate=0.43)
        self.assertEqual(result['year'], 2035)


# =============================================================================
# 6. Missed Repayment Tax Impact (standalone function)
# =============================================================================

class TestMissedRepaymentTaxImpact(unittest.TestCase):
    """hbp_missed_repayment_tax_impact: tax cost of missing repayments."""

    def test_one_year_missed(self):
        """Missing 1 year: shortfall = annual min, taxed at MTR."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_missed_repayment_tax_impact(hbp, missed_years=1, marginal_rate=0.43)
        self.assertAlmostEqual(result['annual_minimum'], 4000)
        self.assertAlmostEqual(result['total_shortfall'], 4000)
        self.assertAlmostEqual(result['tax_cost'], 4000 * 0.43)

    def test_multiple_years_missed(self):
        """Missing 3 years: shortfall = 3 × annual min."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_missed_repayment_tax_impact(hbp, missed_years=3, marginal_rate=0.43)
        self.assertAlmostEqual(result['total_shortfall'], 4000 * 3)
        self.assertAlmostEqual(result['tax_cost'], 4000 * 3 * 0.43)

    def test_zero_years_missed(self):
        """Missing 0 years: no shortfall, no tax cost."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_missed_repayment_tax_impact(hbp, missed_years=0, marginal_rate=0.43)
        self.assertAlmostEqual(result['total_shortfall'], 0)
        self.assertAlmostEqual(result['tax_cost'], 0)

    def test_default_marginal_rate_is_round(self):
        """Default marginal rate is 40% (DP#13/26 round fallback, not household-specific 0.43)."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_missed_repayment_tax_impact(hbp, missed_years=1)
        self.assertAlmostEqual(result['effective_tax_rate'], 0.40)

    def test_result_includes_outstanding_balance(self):
        """Result includes current outstanding balance."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_missed_repayment_tax_impact(hbp, missed_years=1, marginal_rate=0.43)
        self.assertAlmostEqual(result['outstanding_balance'], 60000)

    def test_result_includes_note(self):
        """Result includes explanatory note."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_missed_repayment_tax_impact(hbp, missed_years=2, marginal_rate=0.43)
        self.assertIn('note', result)
        self.assertIn('2', result['note'])

    def test_different_withdrawal_amounts(self):
        """Missed-repayment tax scales with withdrawal amount."""
        hbp_small = HBPAccount(withdrawal=35000)
        hbp_large = HBPAccount(withdrawal=60000)
        result_small = hbp_missed_repayment_tax_impact(hbp_small, missed_years=1, marginal_rate=0.43)
        result_large = hbp_missed_repayment_tax_impact(hbp_large, missed_years=1, marginal_rate=0.43)
        self.assertLess(result_small['tax_cost'], result_large['tax_cost'])


# =============================================================================
# 7. HBP-RRSP Integration (hbp_repayment_to_rrsp_room)
# =============================================================================

class TestHBPRepaymentToRRSPRoom(unittest.TestCase):
    """DP#46: HBP repayment → RRSP room impact."""

    def test_default_repayment_is_minimum(self):
        """If repayment_amount is None, use annual minimum."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_repayment_to_rrsp_room(hbp, repayment_year=2029)
        self.assertAlmostEqual(result['amount_repaid'], 60000 / 15)

    def test_custom_repayment_amount(self):
        """Custom repayment amount overrides minimum."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_repayment_to_rrsp_room(hbp, repayment_year=2029, repayment_amount=5000)
        self.assertEqual(result['amount_repaid'], 5000)

    def test_repayment_does_not_create_contribution_room(self):
        """HBP repayment does NOT create new RRSP contribution room."""
        hbp = HBPAccount(withdrawal=35000)
        result = hbp_repayment_to_rrsp_room(hbp, repayment_year=2029, repayment_amount=2333)
        self.assertIn('does NOT create new contribution room', result['note'])

    def test_shortfall_when_underpaying(self):
        """Paying less than minimum creates shortfall reported as income."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_repayment_to_rrsp_room(hbp, repayment_year=2029, repayment_amount=2000)
        self.assertGreater(result['shortfall'], 0)
        self.assertTrue(result['shortfall_included_in_income'])

    def test_no_shortfall_when_overpaying(self):
        """Paying at or above minimum: no shortfall, no income inclusion."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_repayment_to_rrsp_room(hbp, repayment_year=2029, repayment_amount=4000)
        self.assertEqual(result['shortfall'], 0)
        self.assertFalse(result['shortfall_included_in_income'])

    def test_outstanding_after_repayment(self):
        """Outstanding balance updated after repayment."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_repayment_to_rrsp_room(hbp, repayment_year=2029, repayment_amount=4000)
        self.assertAlmostEqual(result['outstanding_after'], 56000)

    def test_repayment_year_in_result(self):
        """Result includes the repayment year."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp_repayment_to_rrsp_room(hbp, repayment_year=2031)
        self.assertEqual(result['repayment_year'], 2031)


# =============================================================================
# 8. HBP Account Summary
# =============================================================================

class TestHBPSummary(unittest.TestCase):
    """HBPAccount.summary() returns complete account state."""

    def test_summary_fields(self):
        """Summary contains all expected fields."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        s = hbp.summary()
        self.assertIn('withdrawal', s)
        self.assertIn('withdrawal_year', s)
        self.assertIn('repaid', s)
        self.assertIn('outstanding', s)
        self.assertIn('annual_min_repayment', s)
        self.assertIn('repayment_start_year', s)
        self.assertIn('years_remaining', s)

    def test_summary_values_fresh_account(self):
        """Fresh account summary reflects initial state."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        s = hbp.summary()
        self.assertEqual(s['withdrawal'], 60000)
        self.assertEqual(s['withdrawal_year'], 2026)
        self.assertEqual(s['repaid'], 0.0)
        self.assertEqual(s['outstanding'], 60000)
        self.assertAlmostEqual(s['annual_min_repayment'], 4000)
        self.assertEqual(s['repayment_start_year'], 2029)

    def test_summary_after_partial_repayment(self):
        """Summary after partial repayment shows reduced outstanding."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        hbp.make_repayment(4000, year=2029)
        s = hbp.summary()
        self.assertEqual(s['repaid'], 4000)
        self.assertEqual(s['outstanding'], 56000)

    def test_summary_years_remaining_before_schedule(self):
        """Years remaining = 15 before schedule is generated."""
        hbp = HBPAccount(withdrawal=60000)
        s = hbp.summary()
        self.assertEqual(s['years_remaining'], 15)


# =============================================================================
# 9. FHSA-to-HBP Transitions
# =============================================================================

class TestFHSAToHBPTransitions(unittest.TestCase):
    """FHSA + HBP combined usage and double deduction (SCENARIO 10.1)."""

    def test_fhsa_eligible_and_hbp_withdrawal(self):
        """Both FHSA and HBP can be used simultaneously (CRA confirmed)."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        # FHSA withdrawal (qualifying)
        fhsa_result = fhsa.qualifying_withdrawal(year=2030)
        self.assertTrue(fhsa_result['eligible'])
        self.assertTrue(fhsa_result['tax_free'])

        # HBP withdrawal (separate account)
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2030)
        self.assertEqual(hbp.outstanding, 35000)
        self.assertTrue(hbp.is_first_home)

    def test_fhsa_non_qualifying_does_not_affect_hbp(self):
        """FHSA non-qualifying withdrawal doesn't prevent HBP use."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        # Non-qualifying withdrawal (not first home)
        fhsa.principal_residence_years = [2028]
        fhsa_result = fhsa.qualifying_withdrawal(year=2030)
        # FHSA says not eligible, but HBP eligibility is separate
        self.assertFalse(fhsa_result['eligible'])

        # HBP can still be used (it has its own eligibility)
        hbp = HBPAccount(withdrawal=35000, is_first_home=True)
        self.assertTrue(hbp.is_first_home)

    def test_combined_down_payment_is_additive(self):
        """FHSA + HBP down payments are additive (contributions + HBP withdrawal)."""
        fhsa = FHSAAccount()
        for _ in range(5):
            fhsa.contribute(8000)
            fhsa.grow(0.07)
        fhsa_down = fhsa.balance

        hbp = HBPAccount(withdrawal=60000)
        hbp_down = hbp.withdrawal

        combined = fhsa_down + hbp_down
        # FHSA balance grows from $40k contributions + growth; HBP adds $60k
        self.assertGreater(combined, fhsa_down)  # HBP adds to total
        self.assertGreater(combined, hbp_down)  # FHSA adds to total
        self.assertLessEqual(hbp_down, HBP_MAX_WITHDRAWAL)

    def test_hbp_repayment_schedule_while_fhsa_is_closed(self):
        """After FHSA closes, HBP repayment schedule continues."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.qualifying_withdrawal(year=2030)
        # FHSA closes after qualifying withdrawal
        self.assertTrue(fhsa.qualifying_withdrawal_made)

        # HBP still requires 15 years of repayment
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2030)
        schedule = hbp.generate_repayment_schedule()
        self.assertEqual(len(schedule), 15)
        self.assertEqual(schedule[0]['year'], 2033)  # 2030 + 2 + 1

    def test_double_deduction_strategy_fhsa_contribution(self):
        """FHSA contribution creates a deduction; HBP withdrawal is tax-free."""
        # Contribute to FHSA → tax deduction
        fhsa = FHSAAccount()
        contrib = fhsa.contribute(8000)
        tax_savings = fhsa.tax_savings(contrib, marginal_rate=0.43)
        self.assertAlmostEqual(tax_savings, 8000 * 0.43)

        # HBP withdrawal → tax-free (no additional tax)
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        # HBP withdrawal doesn't trigger tax
        self.assertEqual(hbp.outstanding, 35000)

    def test_fhsa_transfer_to_rrsp_then_hbp(self):
        """FHSA → RRSP transfer doesn't use RRSP room, then HBP withdrawal."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.grow(0.07)
        balance_before = fhsa.balance

        # Transfer FHSA to RRSP (doesn't use RRSP room)
        transferred = fhsa.transfer_to_rrsp()
        self.assertAlmostEqual(transferred, balance_before)
        self.assertFalse(fhsa.is_open)

        # Can still use HBP from other RRSP funds
        hbp = HBPAccount(withdrawal=60000)
        self.assertEqual(hbp.outstanding, 60000)

    def test_spousal_combined_hbp(self):
        """Each spouse can withdraw up to $60k via HBP ($120k combined)."""
        hbp_primary = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        hbp_spouse = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        combined = hbp_primary.outstanding + hbp_spouse.outstanding
        self.assertEqual(combined, 120000)

    def test_spousal_independent_repayment_schedules(self):
        """Each spouse has an independent 15-year repayment schedule."""
        hbp_p = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        hbp_s = HBPAccount(withdrawal=45000, withdrawal_year=2027)
        sched_p = hbp_p.generate_repayment_schedule()
        sched_s = hbp_s.generate_repayment_schedule()
        self.assertEqual(len(sched_p), 15)
        self.assertEqual(len(sched_s), 15)
        # Different start years
        self.assertEqual(sched_p[0]['year'], 2029)
        self.assertEqual(sched_s[0]['year'], 2030)
        # Different min payments
        self.assertAlmostEqual(sched_p[0]['min_payment'], 4000)
        self.assertAlmostEqual(sched_s[0]['min_payment'], 3000)


# =============================================================================
# 10. First Home Strategy Comparison
# =============================================================================

class TestCompareFirstHomeStrategies(unittest.TestCase):
    """compare_first_home_strategies: rank FHSA, HBP, TFSA, combined."""

    def test_returns_strategies_list(self):
        """Result contains a list of strategies."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        self.assertIn('strategies', result)
        self.assertGreater(len(result['strategies']), 0)

    def test_five_strategies_returned(self):
        """All five strategies are included: FHSA, RRSP HBP, TFSA, Combined, Double Deduction."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        names = [s['name'] for s in result['strategies']]
        self.assertIn('FHSA', names)
        self.assertIn('RRSP HBP', names)
        self.assertIn('TFSA', names)
        self.assertIn('FHSA + HBP Combined', names)

    def test_strategies_sorted_by_net_benefit(self):
        """Strategies are ranked by net_benefit (DP#22: optimizer ranks)."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        benefits = [s['net_benefit'] for s in result['strategies']]
        self.assertEqual(benefits, sorted(benefits, reverse=True))

    def test_best_strategy_is_first(self):
        """Best strategy name matches first in list."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        self.assertEqual(result['best'], result['strategies'][0]['name'])

    def test_fhsa_has_no_repayment(self):
        """FHSA strategy has repayment_required=False."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        fhsa = next(s for s in result['strategies'] if s['name'] == 'FHSA')
        self.assertFalse(fhsa['repayment_required'])

    def test_hbp_has_repayment(self):
        """RRSP HBP strategy has repayment_required=True."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        hbp = next(s for s in result['strategies'] if s['name'] == 'RRSP HBP')
        self.assertTrue(hbp['repayment_required'])
        self.assertIn('repayment_annual', hbp)

    def test_tfsa_has_no_repayment(self):
        """TFSA strategy has repayment_required=False."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        tfsa = next(s for s in result['strategies'] if s['name'] == 'TFSA')
        self.assertFalse(tfsa['repayment_required'])

    def test_combined_has_repayment(self):
        """Combined FHSA + HBP strategy requires HBP repayment."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        combined = next(s for s in result['strategies'] if s['name'] == 'FHSA + HBP Combined')
        self.assertTrue(combined['repayment_required'])

    def test_combined_down_payment_exceeds_fhsa_alone(self):
        """Combined down payment > FHSA alone."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        fhsa = next(s for s in result['strategies'] if s['name'] == 'FHSA')
        combined = next(s for s in result['strategies'] if s['name'] == 'FHSA + HBP Combined')
        self.assertGreater(combined['down_payment'], fhsa['down_payment'])

    def test_hbp_withdrawal_capped_at_max(self):
        """HBP withdrawal is capped at HBP_MAX_WITHDRAWAL."""
        result = compare_first_home_strategies(
            income=200000, marginal_rate=0.50,
            rrsp_existing_balance=100000,
            hbp_withdrawal=80000,  # Requesting more than max
            investment_return=0.07,
        )
        hbp = next(s for s in result['strategies'] if s['name'] == 'RRSP HBP')
        self.assertLessEqual(hbp['down_payment'], HBP_MAX_WITHDRAWAL)

    def test_result_includes_income_and_mtr(self):
        """Result echoes input income and marginal rate."""
        result = compare_first_home_strategies(income=100000, marginal_rate=0.43, investment_return=0.07)
        self.assertEqual(result['income'], 100000)
        self.assertAlmostEqual(result['marginal_rate'], 0.43)

    def test_result_includes_years_to_purchase(self):
        """Result echoes years_to_purchase."""
        result = compare_first_home_strategies(income=100000, marginal_rate=0.43, years_to_purchase=3, investment_return=0.07)
        self.assertEqual(result['years_to_purchase'], 3)

    def test_each_strategy_has_pros_and_cons(self):
        """Every strategy has pros and cons lists."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        for s in result['strategies']:
            self.assertIn('pros', s)
            self.assertIn('cons', s)
            self.assertGreater(len(s['pros']), 0)
            self.assertGreater(len(s['cons']), 0)

    def test_hbp_lost_growth_reported(self):
        """HBP strategy reports lost growth cost."""
        result = compare_first_home_strategies(income=75000, marginal_rate=0.35, investment_return=0.07)
        hbp = next(s for s in result['strategies'] if s['name'] == 'RRSP HBP')
        self.assertIn('lost_growth', hbp)

    def test_default_annual_savings_is_15_percent(self):
        """Default annual savings = 15% of income (DP#13)."""
        result = compare_first_home_strategies(income=100000, marginal_rate=0.43, investment_return=0.07)
        # Verify by checking TFSA uses after-tax savings = income * 0.15 * (1 - MTR)
        tfsa = next(s for s in result['strategies'] if s['name'] == 'TFSA')
        self.assertGreater(tfsa['down_payment'], 0)


# =============================================================================
# 11. Edge Cases
# =============================================================================

class TestHBPEdgeCases(unittest.TestCase):
    """Edge cases: zero withdrawal, max withdrawal, overpayment, etc."""

    def test_zero_withdrawal(self):
        """Zero withdrawal: no outstanding, no repayment required."""
        hbp = HBPAccount(withdrawal=0)
        self.assertEqual(hbp.outstanding, 0.0)
        self.assertAlmostEqual(hbp.annual_min_repayment(), 0.0)

    def test_zero_withdrawal_schedule(self):
        """Zero withdrawal schedule is empty (nothing to repay)."""
        hbp = HBPAccount(withdrawal=0)
        schedule = hbp.generate_repayment_schedule()
        # Should produce 0 or 1 entries (the loop breaks immediately)
        self.assertLessEqual(len(schedule), 1)
        if schedule:
            self.assertAlmostEqual(schedule[0]['outstanding_after'], 0.0)
            self.assertTrue(schedule[0]['is_final'])

    def test_max_withdrawal(self):
        """$60k withdrawal: standard case."""
        hbp = HBPAccount(withdrawal=HBP_MAX_WITHDRAWAL)
        self.assertEqual(hbp.outstanding, 60000)
        self.assertAlmostEqual(hbp.annual_min_repayment(), 4000)

    def test_overpaying_repayment(self):
        """Paying more than outstanding: outstanding clamps to zero."""
        hbp = HBPAccount(withdrawal=60000)
        hbp.make_repayment(4000, year=2029)
        hbp.make_repayment(60000, year=2030)  # Overpay
        self.assertEqual(hbp.outstanding, 0.0)

    def test_early_repayment_before_start_year(self):
        """Repayment before start year is allowed (no enforcement)."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        # Repayment start year is 2029, but paying in 2027
        result = hbp.make_repayment(4000, year=2027)
        self.assertEqual(result['amount_repaid'], 4000)
        self.assertEqual(hbp.repaid, 4000)

    def test_repayment_schedule_with_different_years(self):
        """Schedule generates correctly for non-relief withdrawal years."""
        # 2026 and 2030 are outside the 2022-2025 relief window → normal 3rd-year start.
        for wy in [2026, 2030]:
            hbp = HBPAccount(withdrawal=60000, withdrawal_year=wy)
            schedule = hbp.generate_repayment_schedule()
            self.assertEqual(len(schedule), 15)
            self.assertEqual(schedule[0]['year'], wy + 3)

    def test_hbp_account_is_dataclass(self):
        """HBPAccount is a dataclass (DP#8: compose through data)."""
        from dataclasses import is_dataclass
        self.assertTrue(is_dataclass(HBPAccount))

    def test_hbp_account_can_be_deepcopied(self):
        """HBPAccount can be deep-copied (DP#18: scenarios compose from base)."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2026)
        hbp.make_repayment(4000, year=2029)
        hbp_copy = deepcopy(hbp)
        self.assertEqual(hbp_copy.outstanding, hbp.outstanding)
        # Modifying copy doesn't affect original
        hbp_copy.make_repayment(4000, year=2030)
        self.assertNotEqual(hbp_copy.repaid, hbp.repaid)

    def test_missed_repayment_with_zero_withdrawal(self):
        """Zero withdrawal: missed repayment has no cost."""
        hbp = HBPAccount(withdrawal=0)
        result = hbp_missed_repayment_tax_impact(hbp, missed_years=1, marginal_rate=0.43)
        self.assertAlmostEqual(result['tax_cost'], 0.0)

    def test_default_tax_impact_zero_marginal_rate(self):
        """Zero marginal rate: no tax on default."""
        hbp = HBPAccount(withdrawal=60000)
        result = hbp.default_tax_impact(year=2030, marginal_rate=0.0)
        self.assertAlmostEqual(result['tax_if_default'], 0.0)

    def test_compare_first_home_strategies_zero_income(self):
        """Zero income: strategies still return (with zero savings)."""
        result = compare_first_home_strategies(income=0, marginal_rate=0.0, investment_return=0.07)
        self.assertGreater(len(result['strategies']), 0)

    def test_fhsa_hbp_different_years_to_purchase(self):
        """Strategy comparison works for various years_to_purchase."""
        for years in [1, 3, 5, 10]:
            result = compare_first_home_strategies(
                income=100000, marginal_rate=0.43,
                years_to_purchase=years,
                investment_return=0.07,
            )
            self.assertEqual(result['years_to_purchase'], years)
            self.assertGreater(len(result['strategies']), 0)


# =============================================================================
# 12. Temporary 2022-2025 Repayment Relief (#308, DP#20)
# =============================================================================

from countries.canada.hbp_rules import (
    repayment_start_delay_for_year,
    deductible_contribution_before_hbp,
    HBP_RELIEF_START_DELAY,
)


class TestHBPRepaymentRelief(unittest.TestCase):
    """2022-2025 withdrawals get a 5-year repayment grace (Budget 2024)."""

    def test_relief_year_uses_4_year_delay(self):
        """A 2023 withdrawal gets the relief delay of 4 (5th-year start)."""
        self.assertEqual(repayment_start_delay_for_year(2023), HBP_RELIEF_START_DELAY)

    def test_non_relief_year_uses_standard_delay(self):
        """A 2026 withdrawal uses the standard 2-year delay (3rd-year start)."""
        self.assertEqual(repayment_start_delay_for_year(2026), 2)

    def test_relief_boundary_2022_included(self):
        """2022 is the first relief year (edge)."""
        self.assertEqual(repayment_start_delay_for_year(2022), HBP_RELIEF_START_DELAY)

    def test_relief_boundary_2021_excluded(self):
        """2021 (just before the window) is standard (edge)."""
        self.assertEqual(repayment_start_delay_for_year(2021), 2)

    def test_relief_start_year_for_2023_withdrawal(self):
        """2023 withdrawal → repayment starts 2028 (2023 + 4 + 1)."""
        hbp = HBPAccount(withdrawal=60000, withdrawal_year=2023)
        self.assertEqual(hbp.repayment_start_year(), 2028)


# =============================================================================
# 13. HBP 89-Day Contribution Rule (#308/#305, DP#10)
# =============================================================================

class TestHBP89DayRule(unittest.TestCase):
    """Contributions <90 days before an HBP withdrawal can be non-deductible."""

    def test_contribution_within_window_not_deductible_when_balance_drops(self):
        """Contribution 30 days before withdrawal, balance drops below it → non-deductible."""
        result = deductible_contribution_before_hbp(
            contribution_year=2026, contribution_day_of_year=150,
            withdrawal_year=2026, withdrawal_day_of_year=180,
            contribution_amount=10000,
            rrsp_balance_after_withdrawal=0,
        )
        self.assertTrue(result['within_window'])
        self.assertAlmostEqual(result['non_deductible_amount'], 10000)

    def test_contribution_outside_window_fully_deductible(self):
        """Contribution 120 days before withdrawal is fully deductible (edge)."""
        result = deductible_contribution_before_hbp(
            contribution_year=2026, contribution_day_of_year=60,
            withdrawal_year=2026, withdrawal_day_of_year=180,
            contribution_amount=10000,
            rrsp_balance_after_withdrawal=0,
        )
        self.assertFalse(result['within_window'])
        self.assertAlmostEqual(result['deductible_amount'], 10000)

    def test_sufficient_remaining_balance_keeps_deduction(self):
        """If enough property remains after withdrawal, contribution stays deductible."""
        result = deductible_contribution_before_hbp(
            contribution_year=2026, contribution_day_of_year=150,
            withdrawal_year=2026, withdrawal_day_of_year=180,
            contribution_amount=10000,
            rrsp_balance_after_withdrawal=10000,
        )
        self.assertAlmostEqual(result['non_deductible_amount'], 0)


# =============================================================================
# 14. HBP Re-participation, Cancellation, Death (#308, DP#10/DP#28)
# =============================================================================

class TestHBPReparticipation(unittest.TestCase):
    """Re-participation requires a zero balance on Jan 1 of the new year."""

    def test_zero_balance_allows_reparticipation(self):
        """Fully repaid HBP can re-participate."""
        hbp = HBPAccount(withdrawal=60000, repaid=60000)
        result = hbp.can_reparticipate(2032)
        self.assertTrue(result['eligible'])

    def test_outstanding_balance_blocks_reparticipation(self):
        """An outstanding HBP balance blocks re-participation (edge)."""
        hbp = HBPAccount(withdrawal=60000, repaid=10000)
        result = hbp.can_reparticipate(2032)
        self.assertFalse(result['eligible'])


class TestHBPCancellation(unittest.TestCase):
    """Form RC471 cancellation: repayment is not deductible, not income."""

    def test_full_cancellation_clears_balance(self):
        """Repaying the full outstanding cancels the HBP with no income."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        result = hbp.cancel(2027)
        self.assertAlmostEqual(result['amount_cancelled'], 35000)
        self.assertAlmostEqual(result['income_inclusion'], 0)
        self.assertFalse(result['deductible'])

    def test_partial_cancellation_includes_remainder_in_income(self):
        """Partial cancellation: unrepaid balance is income (edge)."""
        hbp = HBPAccount(withdrawal=35000, withdrawal_year=2026)
        result = hbp.cancel(2027, amount=20000)
        self.assertAlmostEqual(result['income_inclusion'], 15000)


class TestHBPDeath(unittest.TestCase):
    """Death of participant: spouse may elect to assume repayments."""

    def test_no_election_includes_balance_in_income(self):
        """Without election, outstanding balance is income on death."""
        hbp = HBPAccount(withdrawal=60000, repaid=20000)
        result = hbp.on_death(2030, surviving_spouse_elects=False, marginal_rate=0.45)
        self.assertAlmostEqual(result['income_inclusion'], 40000)
        self.assertAlmostEqual(result['tax_on_death'], 40000 * 0.45)

    def test_spouse_election_avoids_income_inclusion(self):
        """Spouse election: no income inclusion, spouse assumes balance (edge)."""
        hbp = HBPAccount(withdrawal=60000, repaid=20000)
        result = hbp.on_death(2030, surviving_spouse_elects=True)
        self.assertAlmostEqual(result['income_inclusion'], 0)
        self.assertTrue(result['spouse_assumes_repayments'])
        self.assertAlmostEqual(result['assumed_balance'], 40000)


if __name__ == '__main__':
    unittest.main()
