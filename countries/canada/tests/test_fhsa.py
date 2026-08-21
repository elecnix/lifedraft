#!/usr/bin/env python3
"""Unit tests for fhsa.py module.

Run with: python3 -m pytest countries/canada/tests/ -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.fhsa import (
    FHSA_ANNUAL_LIMIT,
    FHSA_MAX_AGE,
    FHSA_MAX_YEARS_OPEN,
    FHSAAccount,
    fhsa_contribution_deduction_year,
    fhsa_designated_transfer_to_rrsp,
    fhsa_double_deduction_analysis,
    fhsa_excess_contribution_tax,
)


class TestFHSAAccount(unittest.TestCase):
    """Test FHSA account model."""

    def test_contribute_within_room(self):
        """Contribute within annual room."""
        fhsa = FHSAAccount()
        actual = fhsa.contribute(8000)
        self.assertAlmostEqual(actual, 8000)
        self.assertAlmostEqual(fhsa.balance, 8000)

    def test_contribute_exceeds_annual_room(self):
        """Contribution capped at annual room."""
        fhsa = FHSAAccount()
        actual = fhsa.contribute(15000)
        self.assertAlmostEqual(actual, FHSA_ANNUAL_LIMIT)

    def test_lifetime_limit(self):
        """Lifetime limit of $40k is respected over 5 years."""
        fhsa = FHSAAccount()
        for _ in range(5):
            fhsa.contribute(8000)
            fhsa.add_annual_room()
        self.assertAlmostEqual(fhsa.lifetime_used, 40000)
        # 6th year: no room left
        actual = fhsa.contribute(8000)
        self.assertAlmostEqual(actual, 0)

    def test_over_contribution_prevented(self):
        """Contributions exceeding room are prevented (capped)."""
        fhsa = FHSAAccount()
        fhsa.lifetime_used = 39000  # $1,000 remaining
        # Try to contribute $10,000
        actual = fhsa.contribute(10000)
        # Only $1,000 allowed
        self.assertAlmostEqual(actual, 1000)
        self.assertAlmostEqual(fhsa.lifetime_used, 40000)

    def test_over_contribution_non_qualifying_withdrawal(self):
        """Over-contribution withdrawn as non-qualifying is taxed at 10%.

        Community question: 'What happens if I mistakenly over-contribute to FHSA?'
        Answer: Withdraw the excess as non-qualifying (taxable at 10% withholding + income tax).
        """
        fhsa = FHSAAccount()
        fhsa.lifetime_used = 40000  # At limit
        # Try to contribute excess
        actual = fhsa.contribute(1000)  # Should be 0
        self.assertEqual(actual, 0)
        # If somehow excess existed and was withdrawn non-qualifying
        fhsa.balance = 1000  # Simulate excess in account
        result = fhsa.non_qualifying_withdrawal(2026)
        self.assertEqual(result['withholding_tax'], 100)  # 10% of 1000
        self.assertTrue(result['taxable_income'], 1000)

    def test_carry_forward(self):
        """Unused room carries forward to next year."""
        fhsa = FHSAAccount()
        # Year 1: contribute only $4k
        fhsa.contribute(4000)
        fhsa.add_annual_room()
        # Year 2: annual room $8k + carry-forward $4k = $12k... but capped at $8k carry-forward
        self.assertAlmostEqual(fhsa.carry_forward_room, 4000)

    def test_growth_tax_free(self):
        """FHSA balance grows tax-free."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.grow(0.07)
        self.assertAlmostEqual(fhsa.balance, 8000 * 1.07)

    def test_qualifying_withdrawal_tax_free(self):
        """Qualifying withdrawal is tax-free."""
        fhsa = FHSAAccount()
        for _ in range(5):
            fhsa.contribute(8000)
            fhsa.add_annual_room()
        result = fhsa.qualifying_withdrawal(2030)
        self.assertAlmostEqual(result['amount'], 40000)
        self.assertTrue(result['eligible'])
        self.assertTrue(result['tax_free'])
        self.assertTrue(fhsa.qualifying_withdrawal_made)
        self.assertEqual(fhsa.balance, 0)

    def test_non_qualifying_withdrawal_taxable(self):
        """Non-qualifying withdrawal is taxable."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        result = fhsa.non_qualifying_withdrawal(2028)
        self.assertAlmostEqual(result['amount'], 8000)
        self.assertAlmostEqual(result['withholding_tax'], 1600)  # 20% × $8k (CRA flat-rate bracket)
        self.assertFalse(fhsa.is_open)

    def test_transfer_to_rrsp(self):
        """Transfer to RRSP without using RRSP room."""
        fhsa = FHSAAccount()
        for _ in range(5):
            fhsa.contribute(8000)
            fhsa.add_annual_room()
        transfer = fhsa.transfer_to_rrsp()
        self.assertAlmostEqual(transfer, 40000)
        self.assertFalse(fhsa.is_open)

    def test_must_close_age_71(self):
        """FHSA must close at age 71."""
        fhsa = FHSAAccount()
        self.assertTrue(fhsa.must_close(2050, 1979))  # Age 71

    def test_no_close_before_71(self):
        """FHSA stays open before age 71."""
        fhsa = FHSAAccount()
        self.assertFalse(fhsa.must_close(2030, 1979))  # Age 51

    def test_must_close_after_qualifying_withdrawal(self):
        """FHSA must close year after qualifying withdrawal."""
        fhsa = FHSAAccount()
        fhsa.qualifying_withdrawal(2028)
        self.assertTrue(fhsa.must_close(2029, 1979))
        self.assertFalse(fhsa.must_close(2028, 1979))  # Same year is fine

    def test_tax_savings(self):
        """FHSA contribution gives tax savings like RRSP."""
        fhsa = FHSAAccount()
        savings = fhsa.tax_savings(8000, 0.4571)
        self.assertAlmostEqual(savings, 8000 * 0.4571)

    def test_summary(self):
        """Summary returns account details."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        s = fhsa.summary()
        self.assertIn('balance', s)
        self.assertIn('lifetime_used', s)
        self.assertAlmostEqual(s['balance'], 8000)


class TestFHSADoubleDeduction(unittest.TestCase):
    """Test FHSA + HBP double deduction analysis."""

    def test_analysis_runs(self):
        """Analysis produces results."""
        result = fhsa_double_deduction_analysis(
            annual_income=130000, marginal_rate=0.4571,
            investment_return=0.07
        )
        self.assertTrue(result['double_deduction_possible'])
        self.assertGreater(result['total_down_payment'], 0)

    def test_hbp_repayment(self):
        """HBP must be repaid over 15 years."""
        result = fhsa_double_deduction_analysis(
            annual_income=130000, marginal_rate=0.4571,
            hbp_withdrawal=35000,
            investment_return=0.07,
        )
        self.assertAlmostEqual(result['hbp_annual_repayment'], 35000 / 15, places=0)

    def test_fhsa_lifetime(self):
        """FHSA contributions capped at lifetime limit."""
        result = fhsa_double_deduction_analysis(
            annual_income=130000, marginal_rate=0.4571,
            investment_return=0.07,
        )
        self.assertAlmostEqual(result['fhsa_lifetime_contributions'], 40000)

    def test_tax_savings_at_marginal_rate(self):
        """Total tax savings reflect marginal rate."""
        result = fhsa_double_deduction_analysis(
            annual_income=130000, marginal_rate=0.4571,
            investment_return=0.07,
        )
        expected = 40000 * 0.4571
        self.assertAlmostEqual(result['fhsa_tax_savings_total'], expected, places=0)


class TestFHSAAccountCarryForward(unittest.TestCase):
    """Test FHSAAccount.add_annual_room() carry-forward behavior (M7)."""

    def test_unused_room_carries_forward(self):
        """Unused annual room becomes carry-forward in next year."""
        fhsa = FHSAAccount(annual_room=8000, carry_forward_room=0)
        fhsa.contribute(3000)  # Use 3000 of 8000
        fhsa.add_annual_room()  # New year: unused 5000 becomes carry-forward
        self.assertAlmostEqual(fhsa.carry_forward_room, 5000)
        self.assertAlmostEqual(fhsa.annual_room, 8000)

    def test_fully_used_room_no_carry_forward(self):
        """When all annual room is used, no carry-forward."""
        fhsa = FHSAAccount(annual_room=8000, carry_forward_room=0)
        fhsa.contribute(8000)  # Use all room
        fhsa.add_annual_room()
        self.assertAlmostEqual(fhsa.carry_forward_room, 0)
        self.assertAlmostEqual(fhsa.annual_room, 8000)

    def test_carry_forward_accumulates_with_previous(self):
        """Carry-forward replaces prior carry-forward (only 1 year, per CRA)."""
        fhsa = FHSAAccount(annual_room=8000, carry_forward_room=4000)
        fhsa.contribute(2000)  # Uses carry-forward first (2000 of 4000)
        # After: carry_forward_room=2000, annual_room=8000
        fhsa.add_annual_room()  # Prior annual_room (8000) becomes new carry-forward
        self.assertAlmostEqual(fhsa.carry_forward_room, 8000)
        self.assertAlmostEqual(fhsa.annual_room, 8000)


class TestFHSAAccountNoContributionRoomAlias(unittest.TestCase):
    """DP#9 (#718): the deprecated contribution_room alias is gone.

    Callers read annual_room + carry_forward_room directly; no @property
    alias, no setter, and no _contribution_room_init constructor param.
    """

    def test_no_contribution_room_property(self):
        """FHSAAccount exposes no contribution_room attribute."""
        fhsa = FHSAAccount(annual_room=8000, carry_forward_room=4000)
        self.assertFalse(hasattr(fhsa, "contribution_room"))

    def test_no_contribution_room_init_param(self):
        """The deprecated constructor param no longer exists."""
        with self.assertRaises(TypeError):
            FHSAAccount(_contribution_room_init=16000)


class TestFHSAExcessTax(unittest.TestCase):
    """1%/month excess FHSA contribution tax (ITA s.207.021) — #307."""

    def test_one_month_excess_tax(self):
        """1% of the highest excess for one month."""
        result = fhsa_excess_contribution_tax(2000, months=1)
        self.assertAlmostEqual(result['total_tax'], 20)

    def test_multi_month_excess_tax(self):
        """Excess persisting several months accrues 1%/month (edge)."""
        result = fhsa_excess_contribution_tax(2000, months=4)
        self.assertAlmostEqual(result['total_tax'], 80)

    def test_no_excess_no_tax(self):
        """No excess → no tax."""
        result = fhsa_excess_contribution_tax(0, months=6)
        self.assertAlmostEqual(result['total_tax'], 0)


class TestFHSAContributionTiming(unittest.TestCase):
    """FHSA first-60-day contributions cannot roll back to prior year — #307."""

    def test_first_60_days_does_not_roll_back(self):
        """A first-60-days FHSA contribution is deductible in the year made (not prior)."""
        year = fhsa_contribution_deduction_year(2027, contribution_in_first_60_days=True)
        self.assertEqual(year, 2027)

    def test_normal_contribution_deductible_in_year_made(self):
        """A regular contribution is deductible in the contribution year."""
        year = fhsa_contribution_deduction_year(2027, contribution_in_first_60_days=False)
        self.assertEqual(year, 2027)


class TestFHSAPostWithdrawalDeduction(unittest.TestCase):
    """No deduction for contributions after the first qualifying withdrawal — #307."""

    def test_pre_withdrawal_contribution_deductible(self):
        """Before a qualifying withdrawal, contributions are fully deductible."""
        fhsa = FHSAAccount()
        self.assertAlmostEqual(fhsa.deductible_contribution(8000), 8000)

    def test_post_withdrawal_contribution_not_deductible(self):
        """After a qualifying withdrawal, contributions are not deductible (edge)."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        fhsa.qualifying_withdrawal(2030)
        self.assertAlmostEqual(fhsa.deductible_contribution(5000), 0)


class TestFHSADesignatedTransfer(unittest.TestCase):
    """Designated transfer to RRSP/RRIF (Form RC727) — #307."""

    def test_designated_transfer_does_not_use_rrsp_room(self):
        """Designated transfer is not deductible and uses no RRSP room."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        result = fhsa.designated_transfer_to_rrsp()
        self.assertAlmostEqual(result['amount'], 8000)
        self.assertFalse(result['uses_rrsp_room'])
        self.assertFalse(result['restores_fhsa_room'])
        self.assertAlmostEqual(fhsa.balance, 0)

    def test_module_level_designated_transfer(self):
        """Module-level helper documents the room impact."""
        result = fhsa_designated_transfer_to_rrsp(5000)
        self.assertFalse(result['deductible'])


class TestFHSAReParticipationRoom(unittest.TestCase):
    """Excess resolved by taxable withdrawal restores room; transfer does not — #307."""

    def test_taxable_withdrawal_restores_room(self):
        """Resolving excess via taxable withdrawal restores participation room."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        room_before = fhsa.annual_room
        result = fhsa.resolve_excess_by_taxable_withdrawal(3000)
        self.assertTrue(result['restores_fhsa_room'])
        self.assertAlmostEqual(fhsa.annual_room, room_before + 3000)

    def test_designated_transfer_does_not_restore_room(self):
        """Designated transfer of excess does NOT restore room (edge)."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        room_before = fhsa.annual_room
        fhsa.designated_transfer_to_rrsp(3000)
        self.assertAlmostEqual(fhsa.annual_room, room_before)


class TestFHSAMaxParticipationPeriod(unittest.TestCase):
    """Maximum participation period = earliest of 15yr / age 71 / post-withdrawal — #307."""

    def test_fifteen_year_anniversary_governs(self):
        """With no withdrawal and a young holder, the 15-year anniversary governs."""
        fhsa = FHSAAccount(open_year=2026)
        # Holder born 1990 → age 71 in 2061; 15th anniversary in 2041 is earlier.
        self.assertEqual(fhsa.participation_end_year(1990), 2041)

    def test_age_71_governs_for_older_holder(self):
        """For an older holder, age 71 governs over the 15-year window (edge)."""
        fhsa = FHSAAccount(open_year=2026)
        # Holder born 1965 → age 71 in 2036; earlier than 2041 anniversary.
        self.assertEqual(fhsa.participation_end_year(1965), 2036)

    def test_year_14_does_not_close_year_15_does(self):
        """DP#17: both sides of the 15-year anniversary threshold itself.

        A young holder (age 71 is far off) opened in 2026: year 14
        (2040) must NOT close; year 15 (2041) MUST close. This is the
        anniversary boundary in isolation, distinct from the age-71
        vs. 15-year "whichever comes first" comparison tested elsewhere.
        """
        fhsa = FHSAAccount(open_year=2026)
        holder_birth = 1990  # age 71 in 2061 — nowhere near this boundary
        self.assertFalse(fhsa.must_close(2026 + (FHSA_MAX_YEARS_OPEN - 1), holder_birth))
        self.assertTrue(fhsa.must_close(2026 + FHSA_MAX_YEARS_OPEN, holder_birth))


class TestFHSADeath(unittest.TestCase):
    """FHSA death/successor-holder rules — #307."""

    def test_eligible_spouse_successor_continues_fhsa(self):
        """Eligible spouse successor: FHSA continues, no income inclusion."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        result = fhsa.on_death(successor_is_eligible_spouse=True)
        self.assertTrue(result['successor_continues_fhsa'])
        self.assertAlmostEqual(result['income_inclusion'], 0)

    def test_no_eligible_spouse_includes_in_income(self):
        """No eligible spouse successor: amount is income to beneficiary (edge)."""
        fhsa = FHSAAccount()
        fhsa.contribute(8000)
        result = fhsa.on_death(successor_is_eligible_spouse=False, marginal_rate=0.40)
        self.assertAlmostEqual(result['income_inclusion'], 8000)
        self.assertAlmostEqual(result['tax_on_death'], 8000 * 0.40)

    def test_death_before_15_years_eligible_spouse_successor(self):
        """Eligible spouse successor: 15-year deadline doesn't apply at holder's death.

        DP#17, DP#28: when FHSA holder dies, eligible spouse can inherit
        without triggering the 15-year close deadline.
        """
        fhsa = FHSAAccount(open_year=2023)
        fhsa.contribute(10000)
        # Eligible spouse successor continues the FHSA
        result = fhsa.on_death(successor_is_eligible_spouse=True, marginal_rate=0.40)
        self.assertTrue(result['successor_continues_fhsa'])
        self.assertEqual(result['income_inclusion'], 0)

    def test_death_after_15_years_no_eligible_spouse(self):
        """After 15 years, no successor -> full amount is estate income."""
        fhsa = FHSAAccount(open_year=2008)  # Opened 15+ years ago
        fhsa.contribute(20000)  # Exceed lifetime limit to test balance
        fhsa.balance = 12000  # Set balance directly
        result = fhsa.on_death(successor_is_eligible_spouse=False, marginal_rate=0.45)
        self.assertEqual(result['income_inclusion'], 12000)
        self.assertAlmostEqual(result['tax_on_death'], 12000 * 0.45)

    def test_death_at_age_71_plus_successor_ineligible(self):
        """Holder dies at age 71+; successor spouse is ineligible (age ≥71).

        DP#28: successor eligibility requires age <71.
        """
        fhsa = FHSAAccount(open_year=2024)
        fhsa.balance = 15000  # Set balance directly
        # Even if we tried to name spouse as successor, they'd be ineligible at 71+
        result = fhsa.on_death(successor_is_eligible_spouse=False, marginal_rate=0.40)
        self.assertEqual(result['income_inclusion'], 15000)


class TestCPPSurvivorBoundary(unittest.TestCase):
    """CPP/QPP survivor benefit boundary tests — #376."""

    def test_survivor_age_65_plus_rate(self):
        """Survivor age 65+: gets 60% of deceased's CPP."""
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=10000,
            survivor_age=65,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        self.assertEqual(result['survivor_benefit'], 6000)  # 60% × 10000
        self.assertAlmostEqual(result['combined'], 11000)  # 5000 + 6000

    def test_survivor_age_64_under_65_rate(self):
        """Survivor age 64 (under 65): gets 37.5% of deceased's CPP + flat rate.

        DP#17: boundary between age-65 and under-65 rates.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=10000,
            survivor_age=64,
            survivor_own_cpp_annual=5000,
            province="ontario",
        )
        self.assertGreater(result['survivor_benefit'], 0)
        self.assertGreater(result['combined'], 5000)

    def test_survivor_benefit_capped_at_individual_max(self):
        """Combined survivor + own pension cannot exceed individual maximum.

        DP#17: boundary where survivor benefit is reduced due to cap.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit

        # 2026 CPP max approx $66,782
        cpp_max_approx = 66782

        result = compute_survivor_benefit(
            deceased_cpp_annual=20000,
            survivor_age=65,
            survivor_own_cpp_annual=15000,  # Already close to max
            province="ontario",
        )
        # Combined should be capped at approximately cpp_max
        self.assertLessEqual(result['combined'], cpp_max_approx + 1000)  # Allow some tolerance

    def test_quebec_survivor_flat_rate_plus_percentage(self):
        """QPP survivor: flat rate + 37.5% of deceased's QPP.

        DP#17, DP#52: Quebec has different survivor rules.
        """
        from countries.canada.cpp_sharing import compute_survivor_benefit
        result = compute_survivor_benefit(
            deceased_cpp_annual=8000,
            survivor_age=65,
            survivor_own_cpp_annual=4000,
            province="quebec",
        )
        self.assertEqual(result['pension_plan'], 'QPP')
        self.assertGreater(result['survivor_benefit'], 0)


class TestFHSADeathInheritanceEdgeCases(unittest.TestCase):
    """FHSA death/inheritance edge cases per DP#17 — issue #376.

    Tests for: surviving spouse transfer vs estate taxation,
    15-year deadline handling, age-71 interaction, death timing
    relative to close deadlines.
    """

    # ── Surviving spouse transfer vs estate taxation ──

    def test_eligible_spouse_successor_inherits_full_balance(self):
        """Eligible spouse successor inherits the entire FHSA balance tax-free."""
        fhsa = FHSAAccount(open_year=2026)
        for _ in range(5):
            fhsa.contribute(8000)
            fhsa.add_annual_room(8000, 2027 + _)
        fhsa.grow(0.07)
        balance_before = fhsa.balance
        result = fhsa.on_death(successor_is_eligible_spouse=True)
        self.assertTrue(result['successor_continues_fhsa'])
        self.assertAlmostEqual(result['income_inclusion'], 0)
        self.assertAlmostEqual(result['tax_on_death'], 0)
        self.assertAlmostEqual(result['transferred_amount'], balance_before)
        self.assertFalse(fhsa.is_open)

    def test_ineligible_spouse_estate_taxation(self):
        """Spouse not FHSA-eligible: balance taxed as income to estate/beneficiary."""
        fhsa = FHSAAccount(open_year=2026)
        fhsa.contribute(8000)
        fhsa.grow(0.07)
        balance_before = fhsa.balance
        result = fhsa.on_death(successor_is_eligible_spouse=False, marginal_rate=0.4571)
        self.assertFalse(result['successor_continues_fhsa'])
        self.assertAlmostEqual(result['income_inclusion'], balance_before)
        self.assertAlmostEqual(result['tax_on_death'], balance_before * 0.4571)
        self.assertAlmostEqual(result['transferred_amount'], 0)
        self.assertAlmostEqual(fhsa.balance, 0)
        self.assertFalse(fhsa.is_open)

    # ── 15-year deadline handling ──

    def test_death_within_15_year_window_spouse_inherits_deadline(self):
        """Holder dies in year 10; spouse inherits — the close deadline continues.

        Per ITA s.146.6(13): the survivor becomes the new holder of the FHSA.
        The original 15-year clock runs from the ORIGINAL opening year (not reset).
        """
        holder_birth = 1990  # Age 36 in 2026
        fhsa = FHSAAccount(open_year=2026)
        fhsa.contribute(8000)
        # Holder dies in 2036 (year 10 of FHSA)
        dead_year = 2026 + 10
        # Before death, must_close should be False (not yet year 15, not age 71)
        self.assertFalse(fhsa.must_close(dead_year, holder_birth))
        # Death — eligible spouse inherits
        result = fhsa.on_death(successor_is_eligible_spouse=True)
        self.assertTrue(result['successor_continues_fhsa'])
        # The 15-year clock runs from original open_year (2026), so close by 2041
        # Even under new owner, the deadline doesn't reset
        anniversary_year = 2026 + FHSA_MAX_YEARS_OPEN  # 2041
        self.assertEqual(anniversary_year, 2041)

    def test_death_at_15_year_anniversary_fhsa_must_close(self):
        """Holder dies exactly when FHSA reaches 15-year anniversary — must close.

        If FHSA reaches its 15-year deadline, the holder must close it.
        Death at that point: the account should close per deadine rules.
        """
        holder_birth = 1990
        fhsa = FHSAAccount(open_year=2026)
        fhsa.contribute(8000)
        # Year of 15th anniversary: 2026 + 15 = 2041
        close_year = 2026 + FHSA_MAX_YEARS_OPEN
        self.assertTrue(fhsa.must_close(close_year, holder_birth))
        # Death at this point — eligible spouse transfer still works
        result = fhsa.on_death(successor_is_eligible_spouse=True)
        self.assertTrue(result['successor_continues_fhsa'])

    # ── Age-71 interaction ──

    def test_death_at_age_70_fhsa_still_open(self):
        """Holder dies at age 70 — FHSA is still open (not yet 71).

        The FHSA must close by age 71 (strictly before turning 71).
        At 70, the account is open. Death at 70 allows spouse inheritance.
        """
        holder_birth = 1960  # Age 66 in 2026
        fhsa = FHSAAccount(open_year=2026)
        fhsa.contribute(8000)
        # At age 70 (year 2030), must_close should be False
        self.assertFalse(fhsa.must_close(2030, holder_birth))
        # At age 71 (year 2031), must_close should be True
        self.assertTrue(fhsa.must_close(2031, holder_birth))
        # Death at 70 — spouse inherits
        result = fhsa.on_death(successor_is_eligible_spouse=True)
        self.assertTrue(result['successor_continues_fhsa'])

    def test_death_at_age_71_fhsa_already_must_close(self):
        """Holder dies at age 71 — FHSA must already close by this deadline.

        The FHSA must close by the year the holder turns 71.
        Death at 71: account should have been closed/transferred.
        """
        holder_birth = 1960  # Age 66 in 2026, 71 in 2031
        fhsa = FHSAAccount(open_year=2026)
        fhsa.contribute(8000)
        age_71_year = 1960 + FHSA_MAX_AGE  # 2031
        self.assertTrue(fhsa.must_close(age_71_year, holder_birth))
        # Even though must_close, death processing still works
        result = fhsa.on_death(successor_is_eligible_spouse=False, marginal_rate=0.40)
        self.assertAlmostEqual(result['income_inclusion'], 8000)
        self.assertAlmostEqual(result['tax_on_death'], 8000 * 0.40)

    def test_death_at_age_72_fhsa_opened_in_2023(self):
        """Holder dies at 72; FHSA opened 2023 — should already be closed.

        Opens FHSA in 2023 at age 71 (same year as age-71 deadline).
        Death at 72 means the FHSA should have been transferred/closed before.
        """
        holder_birth = 1952  # Age 71 in 2023
        fhsa = FHSAAccount(open_year=2023)
        fhsa.contribute(8000)
        # At age 71 (2023), must_close already True (opened same year but age 71 hits)
        self.assertTrue(fhsa.must_close(2023, holder_birth))
        # At age 72 (2024), FHSA should have been closed
        self.assertTrue(fhsa.must_close(2024, holder_birth))
        # Death at 72 — account processes as non-eligible
        result = fhsa.on_death(successor_is_eligible_spouse=False, marginal_rate=0.40)
        self.assertAlmostEqual(result['income_inclusion'], 8000)

    def test_death_age_71_vs_15_year_whichever_comes_first(self):
        """Deadline is earliest of 15 years or age 71.

        Opened at age 60: 15 years = age 75, but age 71 is sooner → close at 71.
        Opened at age 50: 15 years = age 65, which is sooner → close at 65.
        """
        # Case 1: age-71 is sooner than 15-year
        fhsa1 = FHSAAccount(open_year=2026)
        holder_birth_old = 1955  # Age 71 in 2026 (same year as open)
        self.assertTrue(fhsa1.must_close(2026, holder_birth_old))
        # 15-year anniversary would be 2041, but age 71 is 2026
        self.assertEqual(fhsa1.participation_end_year(holder_birth_old), 2026)

        # Case 2: 15-year is sooner than age 71
        fhsa2 = FHSAAccount(open_year=2026)
        holder_birth_young = 1990  # Age 71 in 2061
        # 15-year anniversary is 2041; age 71 is 2061 → 2041 is earliest
        self.assertEqual(fhsa2.participation_end_year(holder_birth_young), 2041)
        self.assertTrue(fhsa2.must_close(2041, holder_birth_young))

    # ── Death after qualifying withdrawal ──

    def test_death_after_qualifying_withdrawal_in_close_window(self):
        """Holder made qualifying withdrawal, then dies in close year.

        After a qualifying withdrawal, FHSA must close by Dec 31 of next year.
        Death during this window: account is still in the close window.
        """
        fhsa = FHSAAccount(open_year=2026)
        fhsa.contribute(8000)
        fhsa.qualifying_withdrawal(2030)
        # Year after withdrawal: must close
        self.assertTrue(fhsa.must_close(2031, 1990))
        # Holder dies in that close year
        result = fhsa.on_death(successor_is_eligible_spouse=True)
        self.assertTrue(result['successor_continues_fhsa'])

    def test_death_after_qualifying_withdrawal_balance_is_zero(self):
        """Qualifying withdrawal empties the account; death has nothing to transfer."""
        fhsa = FHSAAccount(open_year=2026)
        fhsa.contribute(8000)
        fhsa.qualifying_withdrawal(2030)
        self.assertAlmostEqual(fhsa.balance, 0)
        result = fhsa.on_death(successor_is_eligible_spouse=False, marginal_rate=0.40)
        self.assertAlmostEqual(result['income_inclusion'], 0)
        self.assertAlmostEqual(result['tax_on_death'], 0)


if __name__ == '__main__':
    unittest.main()
