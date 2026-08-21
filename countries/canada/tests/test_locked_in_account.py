#!/usr/bin/env python3
"""Unit tests for locked_in_account.py module.

Per DP#17: every rule path needs tests.
- Conversion gate (age 71)
- LIF minimum withdrawal (same as RRIF)
- LIF maximum withdrawal (C/F actuarial formula, OSFI-published factors)
- Quebec no-maximum for ages 55+ (effective 2025)
- Small-balance unlock (age 55+ gate, jurisdiction thresholds)
- Financial hardship withdrawal (eligibility validation)
- No-contribution invariant
- Creditor protection
- Late conversion past deadline
- Immutable dataclasses (DP#3, DP#26)

Run with: python3 -m pytest countries/canada/tests/test_locked_in_account.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.locked_in_account import (
    LockedInAccount,
    LIFFund,
    lif_minimum_withdrawal,
    lif_maximum_withdrawal,
    must_convert_by_year,
    small_balance_unlock_eligible,
    hardship_withdrawal_eligible,
    lira_to_rrsp_transfer_eligible,
    death_benefit_disposition,
    compute_lif_withdrawal_range,
    get_ympe,
    YMPE_BY_YEAR,
    LIF_MAX_WITHDRAWAL_FACTORS,
    HARDSHIP_CATEGORIES,
    JURISDICTION_UNLOCK_CATEGORIES,
    QUEBEC_PRESCRIBED_RATES,
)
from countries.canada.retirement import _get_rrif_rates


class TestYMPEYearVersioned(unittest.TestCase):
    """DP#12, DP#20: YMPE is year-versioned data."""

    def test_ympe_2026(self):
        """2026 YMPE is $74,600 per canada.ca."""
        self.assertEqual(get_ympe(2026), 74600)

    def test_ympe_2025(self):
        """2025 YMPE is $71,300 per canada.ca."""
        self.assertEqual(get_ympe(2025), 71300)

    def test_ympe_2024(self):
        self.assertEqual(get_ympe(2024), 68500)

    def test_ympe_fallback_nearest(self):
        """Years outside the table use nearest available year."""
        ympe = get_ympe(2027)
        # Should use 2026 as nearest
        self.assertEqual(ympe, 74600)


class TestConversionGate(unittest.TestCase):
    """DP#28: Conversion to LIF is date-computed from birth_year.

    Must convert by December 31 of the year the owner turns 71.
    """

    def test_must_convert_at_age_71(self):
        birth_year = 1955
        self.assertEqual(must_convert_by_year(birth_year), 1955 + 71)

    def test_no_convert_before_71(self):
        birth_year = 1955
        convert_year = must_convert_by_year(birth_year)
        self.assertFalse(birth_year + 70 >= convert_year)

    def test_conversion_gate_71_not_70(self):
        birth_year = 1960
        convert_year = must_convert_by_year(birth_year)
        self.assertEqual(convert_year, 2031)

    def test_late_birth_year(self):
        birth_year = 2000
        self.assertEqual(must_convert_by_year(birth_year), 2071)

    def test_locked_in_account_auto_convert(self):
        account = LockedInAccount(balance=100000, birth_year=1955)
        self.assertFalse(account.must_convert(2025))
        self.assertTrue(account.must_convert(2026))


class TestLIFMinimumWithdrawal(unittest.TestCase):
    """LIF minimum withdrawal uses the same factors as RRIF."""

    def test_minimum_equals_rrif(self):
        rrif_rates = _get_rrif_rates()
        for age, rate in rrif_rates.items():
            balance = 100000
            lif_min = lif_minimum_withdrawal(balance, age)
            self.assertAlmostEqual(lif_min, balance * rate, places=2,
                                   msg=f"Age {age}: LIF min should equal RRIF min")

    def test_minimum_at_71(self):
        min_w = lif_minimum_withdrawal(200000, 71)
        self.assertAlmostEqual(min_w, 200000 * 0.0528, places=2)

    def test_minimum_at_65(self):
        min_w = lif_minimum_withdrawal(500000, 65)
        self.assertAlmostEqual(min_w, 500000 * 0.04, places=2)

    def test_minimum_at_95(self):
        min_w = lif_minimum_withdrawal(100000, 95)
        self.assertAlmostEqual(min_w, 100000 * 0.1129, places=2)

    def test_minimum_above_table_uses_highest_rate(self):
        """Ages above the table use the highest age rate (0.1129 at 95)."""
        min_w = lif_minimum_withdrawal(100000, 100)
        self.assertAlmostEqual(min_w, 100000 * 0.1129, places=2)

    def test_zero_balance_zero_minimum(self):
        self.assertAlmostEqual(lif_minimum_withdrawal(0, 71), 0)

    def test_lif_fund_minimum_withdrawal(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955)
        min_w = lif.minimum_withdrawal(2026)  # OSFI age=70 (Dec 31 of prev year)
        expected = lif_minimum_withdrawal(300000, 70)
        self.assertAlmostEqual(min_w, expected, places=2)

    def test_minimum_below_table_uses_inverse_formula(self):
        """Ages < 55 use 1/(90-age) per ITA Reg. 7301 (DP#17)."""
        balance = 200000
        # Age 54: 1/(90-54) = 1/36 ≈ 0.02778
        min_54 = lif_minimum_withdrawal(balance, 54)
        self.assertAlmostEqual(min_54, balance / (90 - 54), places=2)
        # Age 50: 1/(90-50) = 1/40 = 0.025
        min_50 = lif_minimum_withdrawal(balance, 50)
        self.assertAlmostEqual(min_50, balance / (90 - 50), places=2)

    def test_lif_fund_birth_year_zero_raises(self):
        """birth_year=0 (sentinel) raises ValueError (DP#13, DP#17)."""
        lif = LIFFund(balance=200000, owner_birth_year=0, jurisdiction='quebec')
        with self.assertRaises(ValueError):
            lif.age_in(2026)
        with self.assertRaises(ValueError):
            lif.minimum_withdrawal(2026)

    def test_locked_in_account_birth_year_zero_raises(self):
        """LockedInAccount birth_year=0 raises ValueError on age-dependent methods."""
        account = LockedInAccount(balance=50000, birth_year=0)
        with self.assertRaises(ValueError):
            account.age_in(2026)
        with self.assertRaises(ValueError):
            account.must_convert(2026)
        with self.assertRaises(ValueError):
            account.small_balance_unlock_with_jurisdiction(74600, 2026)
        with self.assertRaises(ValueError):
            account.convert_to_lif(2026)

    def test_locked_in_account_from_dict_birth_year_none(self):
        """from_dict with birth_year=None coerces to 0 (sentinel), then age_in raises."""
        account = LockedInAccount.from_dict({'balance': 50000, 'birth_year': None})
        self.assertEqual(account.birth_year, 0)  # None coerced to sentinel
        with self.assertRaises(ValueError):
            account.must_convert(2026)

    def test_small_balance_unlock_eligible_birth_year_zero_raises(self):
        """Standalone function also guards birth_year=0."""
        with self.assertRaises(ValueError):
            small_balance_unlock_eligible(50000, 74600, 0, 2026)

    def test_must_convert_by_year_zero_raises(self):
        """must_convert_by_year(0) raises ValueError (DP#13 sentinel)."""
        with self.assertRaises(ValueError):
            must_convert_by_year(0)

    def test_lif_fund_from_dict_owner_birth_year_none(self):
        """LIFFund.from_dict coerces owner_birth_year=None to 0, then age_in raises."""
        lif = LIFFund.from_dict({'balance': 200000, 'owner_birth_year': None})
        self.assertEqual(lif.owner_birth_year, 0)  # None coerced to sentinel
        with self.assertRaises(ValueError):
            lif.age_in(2026)

    def test_age_for_factor_lookup_off_by_one(self):
        """age_for_factor_lookup returns age on Dec 31 of previous year (OSFI convention)."""
        lif = LIFFund(balance=300000, owner_birth_year=1955)
        # age_in(2026) = 71, but age_for_factor_lookup(2026) = 70 (OSFI convention)
        self.assertEqual(lif.age_in(2026), 71)
        self.assertEqual(lif.age_for_factor_lookup(2026), 70)


class TestLIFMaximumWithdrawalOSFI(unittest.TestCase):
    """LIF maximum withdrawal uses C/F actuarial formula (OSFI-published factors).

    Maximum = C / F where F is present value of $1/year annuity
    payable in advance until age 90, using CANSIM V122487 rate
    for first 15 years and 6% thereafter (PBSR s.20.1).
    """

    def test_maximum_greater_than_minimum(self):
        balance = 300000
        age = 71
        year = 2026
        min_w = lif_minimum_withdrawal(balance, age)
        max_w = lif_maximum_withdrawal(balance, age, year)
        self.assertGreater(max_w, min_w)

    def test_maximum_uses_osfi_factor_2026(self):
        """At age 71 in 2026, OSFI factor is 7.0804%."""
        balance = 200000
        age = 71
        year = 2026
        max_w = lif_maximum_withdrawal(balance, age, year)
        self.assertAlmostEqual(max_w, balance * 0.070804, places=0)

    def test_maximum_uses_osfi_factor_2026_age_65(self):
        """At age 65 in 2026, OSFI factor is 6.0272%."""
        balance = 500000
        age = 65
        year = 2026
        max_w = lif_maximum_withdrawal(balance, age, year)
        self.assertAlmostEqual(max_w, balance * 0.060272, places=0)

    def test_maximum_uses_osfi_factor_2026_age_80(self):
        """At age 80 in 2026, OSFI factor is 11.6128%."""
        balance = 300000
        age = 80
        year = 2026
        max_w = lif_maximum_withdrawal(balance, age, year)
        self.assertAlmostEqual(max_w, balance * 0.116128, places=0)

    def test_maximum_year_versioned(self):
        """Different years have different factors (DP#20)."""
        balance = 200000
        age = 71
        max_2025 = lif_maximum_withdrawal(balance, age, year=2025)
        max_2026 = lif_maximum_withdrawal(balance, age, year=2026)
        # 2025 and 2026 factors differ (different CANSIM rates)
        self.assertNotAlmostEqual(max_2025, max_2026, places=0)
        self.assertGreater(max_2025, 0)
        self.assertGreater(max_2026, 0)

    def test_maximum_bounded_by_balance(self):
        balance = 100000
        max_w = lif_maximum_withdrawal(balance, 89, year=2026)
        self.assertLessEqual(max_w, balance)

    def test_maximum_at_zero_balance(self):
        self.assertAlmostEqual(lif_maximum_withdrawal(0, 71, year=2026), 0)

    def test_maximum_age_89_withdraw_all(self):
        """At age 89, OSFI factor is 100% — can withdraw entire balance."""
        balance = 100000
        max_w = lif_maximum_withdrawal(balance, 89, year=2026)
        self.assertAlmostEqual(max_w, balance, places=2)

    def test_maximum_age_90_withdraw_all(self):
        """Ages 90+ (outside OSFI table) can withdraw entire balance."""
        balance = 100000
        max_w = lif_maximum_withdrawal(balance, 90, year=2026)
        self.assertAlmostEqual(max_w, balance, places=2)

    def test_quebec_no_maximum_age_55_plus(self):
        """Quebec removed LIF maximum for ages 55+ effective 2025."""
        balance = 300000
        age = 55
        max_w = lif_maximum_withdrawal(balance, age, year=2025,
                                        jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_has_maximum_before_2025(self):
        """Quebec LIF still had maximum for ages 55+ before 2025."""
        balance = 300000
        age = 65
        max_w = lif_maximum_withdrawal(balance, age, year=2024,
                                        jurisdiction='quebec')
        self.assertLess(max_w, balance)

    def test_quebec_no_maximum_age_65(self):
        """Quebec: age 65 → no maximum, full balance withdrawable."""
        balance = 500000
        max_w = lif_maximum_withdrawal(balance, 65, year=2026,
                                        jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_no_maximum_age_80(self):
        """Quebec: age 80 → no maximum."""
        balance = 200000
        max_w = lif_maximum_withdrawal(balance, 80, year=2026,
                                        jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_age_54_uses_prescribed_rate_2025(self):
        """Quebec: age 54 in 2025 → prescribed rate × balance (NOT no-maximum).

        DP#17, DP#28: age-54-to-55 transition boundary.
        Under 55 uses prescribed_rate × balance formula.
        2025 prescribed rate is 6.00%.
        """
        balance = 200000
        max_w = lif_maximum_withdrawal(balance, 54, year=2025,
                                        jurisdiction='quebec')
        expected = balance * 0.06
        self.assertAlmostEqual(max_w, expected, places=2)
        self.assertLess(max_w, balance, "Age 54 should have a maximum, not full balance")

    def test_quebec_age_54_uses_prescribed_rate_2026(self):
        """Quebec: age 54 in 2026 → prescribed rate × balance.

        DP#17: verify that the year-versioned prescribed rate applies.
        2026 prescribed rate is 6.25%.
        """
        balance = 200000
        max_w = lif_maximum_withdrawal(balance, 54, year=2026,
                                        jurisdiction='quebec')
        expected = balance * 0.0625
        self.assertAlmostEqual(max_w, expected, places=2)
        self.assertLess(max_w, balance, "Age 54 should have a maximum, not full balance")

    def test_quebec_age_54_to_55_transition_2025(self):
        """Quebec: age 54 has maximum, age 55 (next year) has no maximum.

        DP#17, DP#28: the transition happens at the exact age-55 boundary,
        computed from birth_year, not a stored boolean.
        """
        balance = 200000
        max_54 = lif_maximum_withdrawal(balance, 54, year=2025,
                                         jurisdiction='quebec')
        max_55 = lif_maximum_withdrawal(balance, 55, year=2025,
                                         jurisdiction='quebec')
        self.assertLess(max_54, balance, "Age 54 should have a maximum")
        self.assertAlmostEqual(max_55, balance, "Age 55 should get full balance")

    def test_quebec_lif_age_computed_from_birth_year(self):
        """DP#28: LIF maximum is computed from birth_year, not stored age.

        Same LIFFund at different years produces different ages → different
        withdrawal rules apply. The transition from prescribed-rate (under 55)
        to no-maximum (55+) is determined by the computed age.

        LIFFund uses age_for_factor_lookup (= age_in(year) - 1).
        Born 1970: age_for_factor_lookup=54 in 2025, =55 in 2026.
        """
        # Owner born 1970: OSFI-age 54 in 2025, OSFI-age 55 in 2026
        lif = LIFFund(balance=200000, owner_birth_year=1970,
                      reference_rate=0.06, jurisdiction='quebec')
        # Age 54 (year 2025): has maximum
        max_2025 = lif.maximum_withdrawal(2025)
        self.assertLess(max_2025, 200000, "Age 54 should have maximum")
        # Age 55 (year 2026): no maximum
        max_2026 = lif.maximum_withdrawal(2026)
        self.assertAlmostEqual(max_2026, 200000,
                               msg="Age 55+ should get full balance")

    def test_quebec_lif_birth_year_stays_under_55(self):
        """DP#28: age computed from birth_year — two consecutive years under 55.

        Both years use prescribed rate; the rate may differ by year.
        """
        # Owner born 1980: age 45 in 2025, age 46 in 2026
        lif = LIFFund(balance=100000, owner_birth_year=1980,
                      reference_rate=0.06, jurisdiction='quebec')
        max_2025 = lif.maximum_withdrawal(2025)
        max_2026 = lif.maximum_withdrawal(2026)
        # Both under 55: both should have a maximum (not full balance)
        self.assertLess(max_2025, 100000)
        self.assertLess(max_2026, 100000)

    def test_birth_year_1971_turns_55_in_2026_three_year_progression(self):
        """DP#17, DP#28: three consecutive years straddling the age-55 transition.

        Born 1971, Quebec, OSFI convention (age_for_factor_lookup = age_in - 1):
        2025 -> OSFI age 53 (prescribed rate), 2026 -> OSFI age 54 (still
        prescribed rate, one year short of the no-maximum rule), 2027 ->
        OSFI age 55 (no maximum). The no-maximum benefit under LIFFund's
        OSFI-age lookup arrives one calendar year later than the raw
        age_in(year) == 55 threshold would suggest.
        """
        lif = LIFFund(balance=300000, owner_birth_year=1971,
                      reference_rate=0.06, jurisdiction='quebec')
        self.assertEqual(lif.age_for_factor_lookup(2025), 53)
        self.assertEqual(lif.age_for_factor_lookup(2026), 54)
        self.assertEqual(lif.age_for_factor_lookup(2027), 55)
        max_2025 = lif.maximum_withdrawal(2025)
        max_2026 = lif.maximum_withdrawal(2026)
        max_2027 = lif.maximum_withdrawal(2027)
        self.assertLess(max_2025, 300000)
        self.assertLess(max_2026, 300000)
        self.assertAlmostEqual(max_2027, 300000)

    def test_dec31_and_jan1_birth_same_osfi_age_convention(self):
        """DP#1, DP#28: OSFI age convention only uses birth YEAR, not exact date.

        A holder born Dec 31 1970 and one born Jan 1 1970 both reach the
        same age_for_factor_lookup in a given year, because the model
        stores dates as calendar years (DP#1) and the OSFI convention is
        defined relative to Dec 31 of the prior year, not an exact
        birthdate. Both boundary dates within the same birth year must
        resolve identically.
        """
        lif_dec31 = LIFFund(balance=300000, owner_birth_year=1970,
                             reference_rate=0.06, jurisdiction='federal')
        lif_jan1 = LIFFund(balance=300000, owner_birth_year=1970,
                            reference_rate=0.06, jurisdiction='federal')
        self.assertEqual(lif_dec31.age_in(2026), 56)
        self.assertEqual(lif_dec31.age_for_factor_lookup(2026), 55)
        self.assertEqual(lif_jan1.age_for_factor_lookup(2026),
                          lif_dec31.age_for_factor_lookup(2026))

    def test_federal_lif_fund_maximum_uses_osfi_convention_cross_check(self):
        """LIFFund.maximum_withdrawal(year) must equal the module-level
        lif_maximum_withdrawal() called with the OSFI-convention age, not
        the raw age_in(year) — cross-checks the class wrapper against the
        pure function it delegates to (DP#28).
        """
        lif = LIFFund(balance=300000, owner_birth_year=1955,
                      reference_rate=0.06, jurisdiction='federal')
        self.assertEqual(lif.age_in(2026), 71)
        self.assertEqual(lif.age_for_factor_lookup(2026), 70)
        max_w = lif.maximum_withdrawal(2026)
        expected = lif_maximum_withdrawal(300000, 70, 2026, jurisdiction='federal')
        self.assertAlmostEqual(max_w, expected)
        # It must NOT match the raw age_in(2026)=71 factor (regression guard).
        wrong = lif_maximum_withdrawal(300000, 71, 2026, jurisdiction='federal')
        self.assertNotAlmostEqual(max_w, wrong)

    def test_federal_lif_fund_minimum_uses_osfi_convention_cross_check(self):
        """LIFFund.minimum_withdrawal also uses the OSFI age convention."""
        lif = LIFFund(balance=300000, owner_birth_year=1955,
                      reference_rate=0.06, jurisdiction='federal')
        min_w = lif.minimum_withdrawal(2026)
        expected = lif_minimum_withdrawal(300000, 70)  # OSFI age = 70
        self.assertAlmostEqual(min_w, expected)

    def test_federal_has_maximum_at_55(self):
        """Federal LIF still has maximum at age 55."""
        balance = 300000
        age = 55
        year = 2026
        max_w = lif_maximum_withdrawal(balance, age, year, jurisdiction='federal')
        self.assertAlmostEqual(max_w, balance * 0.052096, places=0)
        self.assertLess(max_w, balance)

    def test_lif_fund_maximum_withdrawal(self):
        """LIFFund.maximum_withdrawal uses OSFI factors with OSFI age convention."""
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        # OSFI uses "age on Dec 31 of previous year" convention
        max_w = lif.maximum_withdrawal(2026)  # OSFI age=70 (born 1955, Dec 31 2025)
        expected = lif_maximum_withdrawal(300000, 70, 2026)  # OSFI age=70
        self.assertAlmostEqual(max_w, expected, places=2)

    def test_lif_fund_quebec_no_maximum(self):
        """Quebec LIFFund has no maximum at age 55+."""
        lif = LIFFund(balance=300000, owner_birth_year=1960,
                      reference_rate=0.06, jurisdiction='quebec')
        max_w = lif.maximum_withdrawal(2026)  # age 66
        self.assertAlmostEqual(max_w, 300000)

    def test_custom_factors_override(self):
        """Custom factor table takes precedence."""
        custom = {2026: {71: 0.10}}
        max_w = lif_maximum_withdrawal(200000, 71, 2026, factors=custom)
        self.assertAlmostEqual(max_w, 200000 * 0.10, places=2)

    def test_fallback_nearest_year(self):
        """Years outside the factor table use nearest available year."""
        max_w = lif_maximum_withdrawal(200000, 71, year=2030)
        # Should use 2026 as nearest
        self.assertGreater(max_w, 0)


class TestLIFWithdrawalRange(unittest.TestCase):
    """compute_lif_withdrawal_range returns both min and max."""

    def test_range_valid(self):
        result = compute_lif_withdrawal_range(300000, 71, year=2026)
        self.assertLessEqual(result['minimum'], result['maximum'])

    def test_range_includes_both(self):
        result = compute_lif_withdrawal_range(300000, 71, year=2026)
        self.assertIn('minimum', result)
        self.assertIn('maximum', result)
        self.assertIn('balance', result)

    def test_withdrawal_within_range(self):
        result = compute_lif_withdrawal_range(300000, 71, year=2026)
        valid_withdrawal = (result['minimum'] + result['maximum']) / 2
        self.assertGreaterEqual(valid_withdrawal, result['minimum'])
        self.assertLessEqual(valid_withdrawal, result['maximum'])

    def test_quebec_maximum_equals_balance(self):
        """Quebec range: maximum = balance for ages 55+."""
        result = compute_lif_withdrawal_range(300000, 65, year=2026,
                                                jurisdiction='quebec')
        self.assertAlmostEqual(result['maximum'], 300000)


class TestSmallBalanceUnlock(unittest.TestCase):
    """Small-balance unlock: balance < threshold% YMPE AND age >= 55."""

    def test_small_balance_eligible_federal(self):
        """Balance below 50% YMPE, age >= 55 → eligible."""
        balance = 20000
        ympe = get_ympe(2026)  # 74600
        self.assertTrue(small_balance_unlock_eligible(
            balance, ympe, birth_year=1960, current_year=2026))

    def test_large_balance_not_eligible(self):
        """Balance at or above 50% YMPE → NOT eligible."""
        balance = 40000
        ympe = get_ympe(2026)  # 74600, 50% = 37300
        self.assertFalse(small_balance_unlock_eligible(
            balance, ympe, birth_year=1960, current_year=2026))

    def test_exact_threshold_not_eligible(self):
        """Balance exactly at 50% YMPE → NOT eligible (strictly below)."""
        ympe = get_ympe(2026)
        balance = ympe * 0.50
        self.assertFalse(small_balance_unlock_eligible(
            balance, ympe, birth_year=1960, current_year=2026))

    def test_under_55_not_eligible(self):
        """Age < 55 → NOT eligible regardless of balance."""
        balance = 100
        ympe = get_ympe(2026)
        self.assertFalse(small_balance_unlock_eligible(
            balance, ympe, birth_year=1980, current_year=2026))

    def test_age_55_eligible(self):
        """Age exactly 55 → eligible if balance small enough."""
        balance = 1000
        ympe = get_ympe(2026)
        self.assertTrue(small_balance_unlock_eligible(
            balance, ympe, birth_year=1971, current_year=2026))

    def test_ontario_40_percent_threshold(self):
        """Ontario uses 40% YMPE threshold, not 50%."""
        balance = 34000
        ympe = get_ympe(2026)  # 74600, 40% = 29840, 50% = 37300
        # Under federal 50% but over Ontario 40%
        self.assertTrue(small_balance_unlock_eligible(
            balance, ympe, birth_year=1960, current_year=2026, jurisdiction='federal'))
        self.assertFalse(small_balance_unlock_eligible(
            balance, ympe, birth_year=1960, current_year=2026, jurisdiction='ontario'))

    def test_locked_in_account_small_balance_unlock(self):
        """LockedInAccount can unlock small balance (federal threshold)."""
        account = LockedInAccount(balance=10000, birth_year=1960)
        unlocked, new_account = account.small_balance_unlock_with_jurisdiction(
            get_ympe(2026), current_year=2026, jurisdiction='federal')
        self.assertAlmostEqual(unlocked, 10000)
        self.assertAlmostEqual(new_account.balance, 0)

    def test_locked_in_account_no_small_balance_unlock(self):
        """LockedInAccount rejects unlock when balance too large."""
        account = LockedInAccount(balance=50000, birth_year=1960)
        unlocked, new_account = account.small_balance_unlock_with_jurisdiction(
            get_ympe(2026), current_year=2026, jurisdiction='federal')
        self.assertAlmostEqual(unlocked, 0)
        self.assertAlmostEqual(new_account.balance, 50000)

    def test_original_account_unchanged(self):
        """DP#3: original account is immutable."""
        account = LockedInAccount(balance=10000, birth_year=1960)
        unlocked, new_account = account.small_balance_unlock_with_jurisdiction(
            get_ympe(2026), current_year=2026, jurisdiction='federal')
        self.assertAlmostEqual(account.balance, 10000)  # original unchanged


class TestHardshipWithdrawalEligibility(unittest.TestCase):
    """Hardship withdrawal eligibility validation."""

    def test_low_income_category(self):
        self.assertTrue(hardship_withdrawal_eligible('low_income'))

    def test_medical_category(self):
        self.assertTrue(hardship_withdrawal_eligible('medical'))

    def test_eviction_category(self):
        self.assertTrue(hardship_withdrawal_eligible('eviction'))

    def test_non_resident_category(self):
        self.assertTrue(hardship_withdrawal_eligible('non_resident'))

    def test_shortened_life_category(self):
        self.assertTrue(hardship_withdrawal_eligible('shortened_life'))

    def test_invalid_category(self):
        self.assertFalse(hardship_withdrawal_eligible('general'))

    def test_empty_category(self):
        self.assertFalse(hardship_withdrawal_eligible(''))

    def test_hardship_withdrawal_with_valid_category(self):
        # Medical hardship is an Ontario unlocking ground (#318).
        account = LockedInAccount(balance=100000, birth_year=1960,
                                  jurisdiction='ontario')
        withdrawn, new_account = account.hardship_withdrawal(30000, category='medical')
        self.assertAlmostEqual(withdrawn, 30000)
        self.assertAlmostEqual(new_account.balance, 70000)

    def test_hardship_withdrawal_invalid_category(self):
        """Invalid category → no withdrawal."""
        account = LockedInAccount(balance=100000, birth_year=1960)
        withdrawn, new_account = account.hardship_withdrawal(30000, category='general')
        self.assertAlmostEqual(withdrawn, 0)
        self.assertAlmostEqual(new_account.balance, 100000)

    def test_hardship_no_category_allows_withdrawal(self):
        """No category (None) → withdrawal proceeds (caller validates)."""
        account = LockedInAccount(balance=100000, birth_year=1960)
        withdrawn, new_account = account.hardship_withdrawal(30000)
        self.assertAlmostEqual(withdrawn, 30000)
        self.assertAlmostEqual(new_account.balance, 70000)


class TestJurisdictionUnlockCategories(unittest.TestCase):
    """#318: unlocking grounds differ by jurisdiction (federal/Quebec/Ontario)."""

    def test_ontario_allows_financial_hardship(self):
        # Ontario recognizes low-income financial hardship.
        self.assertTrue(
            hardship_withdrawal_eligible('low_income', jurisdiction='ontario'))

    def test_federal_rejects_financial_hardship(self):
        # Federal PBSR has no general financial-hardship ground.
        self.assertFalse(
            hardship_withdrawal_eligible('low_income', jurisdiction='federal'))

    def test_quebec_rejects_financial_hardship(self):
        # Quebec has no general low-income/eviction hardship unlocking.
        self.assertFalse(
            hardship_withdrawal_eligible('eviction', jurisdiction='quebec'))

    def test_shortened_life_recognized_in_every_jurisdiction(self):
        for j in JURISDICTION_UNLOCK_CATEGORIES:
            self.assertTrue(
                hardship_withdrawal_eligible('shortened_life', jurisdiction=j))


class TestLiraToRrspTransfer(unittest.TestCase):
    """#318: small-balance / shortened-life transfer to an unlocked RRSP/RRIF."""

    def test_small_balance_age_55_transfers(self):
        # Below 50% of 2026 YMPE ($37,300) and age 55+ → eligible.
        self.assertTrue(lira_to_rrsp_transfer_eligible(
            balance=30000, ympe=get_ympe(2026), birth_year=1968,
            current_year=2026, jurisdiction='federal'))

    def test_under_55_large_balance_not_eligible(self):
        # Age 50, full-size balance, no shortened life → not eligible.
        self.assertFalse(lira_to_rrsp_transfer_eligible(
            balance=200000, ympe=get_ympe(2026), birth_year=1976,
            current_year=2026, jurisdiction='federal'))

    def test_shortened_life_transfers_at_any_age(self):
        # Shortened life expectancy unlocks regardless of age or balance.
        self.assertTrue(lira_to_rrsp_transfer_eligible(
            balance=200000, ympe=get_ympe(2026), birth_year=1980,
            current_year=2026, jurisdiction='federal', shortened_life=True))

    def test_account_transfer_depletes_balance(self):
        account = LockedInAccount(balance=30000, birth_year=1968)
        amount, new_account = account.transfer_to_rrsp(
            ympe=get_ympe(2026), current_year=2026)
        self.assertAlmostEqual(amount, 30000)
        self.assertAlmostEqual(new_account.balance, 0)


class TestDeathDisposition(unittest.TestCase):
    """#318: death payout — spouse rollover vs taxable estate lump sum."""

    def test_surviving_spouse_rolls_over_tax_deferred(self):
        d = death_benefit_disposition(balance=100000, has_spouse=True)
        self.assertEqual(d['recipient'], 'spouse')
        self.assertTrue(d['tax_deferred'])
        self.assertAlmostEqual(d['taxable_lump_sum'], 0)

    def test_no_spouse_pays_taxable_lump_sum_to_estate(self):
        d = death_benefit_disposition(balance=100000, has_spouse=False)
        self.assertEqual(d['recipient'], 'estate')
        self.assertFalse(d['tax_deferred'])
        self.assertAlmostEqual(d['taxable_lump_sum'], 100000)

    def test_spouse_waiver_routes_to_estate(self):
        d = death_benefit_disposition(
            balance=100000, has_spouse=True, spouse_waived=True)
        self.assertEqual(d['recipient'], 'estate')

    def test_account_death_depletes_balance(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        disposition, new_account = account.death_disposition(has_spouse=True)
        self.assertEqual(disposition['recipient'], 'spouse')
        self.assertAlmostEqual(new_account.balance, 0)


class TestNoContributionInvariant(unittest.TestCase):
    """CRI/LIRA accepts no new contributions — balance grows from returns only."""

    def test_contribute_returns_zero(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        result, new_account = account.contribute(5000)
        self.assertAlmostEqual(result, 0)
        self.assertAlmostEqual(new_account.balance, 100000)

    def test_balance_grows_from_returns_only(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        growth, new_account = account.grow(0.07)
        self.assertAlmostEqual(growth, 7000)
        self.assertAlmostEqual(new_account.balance, 107000)
        self.assertAlmostEqual(account.balance, 100000)  # original unchanged


class TestHardshipWithdrawal(unittest.TestCase):
    """Financial hardship permits exceptional access to locked-in funds."""

    def test_hardship_withdrawal_capped_at_balance(self):
        account = LockedInAccount(balance=10000, birth_year=1960)
        withdrawn, new_account = account.hardship_withdrawal(50000)
        self.assertAlmostEqual(withdrawn, 10000)
        self.assertAlmostEqual(new_account.balance, 0)

    def test_normal_withdrawal_blocked(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        result, new_account = account.withdraw(10000)
        self.assertAlmostEqual(result, 0)
        self.assertAlmostEqual(new_account.balance, 100000)


class TestCreditorProtection(unittest.TestCase):
    """CRI/LIF balances are generally not seizable by creditors."""

    def test_creditor_protected(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        self.assertTrue(account.creditor_protected)

    def test_seizable_balance_excluded(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        self.assertAlmostEqual(account.seizable_amount(), 0)


class TestLIFConversion(unittest.TestCase):
    """Converting LockedInAccount to LIFFund at the mandatory age."""

    def test_convert_to_lif(self):
        account = LockedInAccount(balance=150000, birth_year=1955)
        lif, depleted = account.convert_to_lif(year=2026)
        self.assertIsInstance(lif, LIFFund)
        self.assertAlmostEqual(lif.balance, 150000)
        self.assertAlmostEqual(depleted.balance, 0)

    def test_convert_preserves_birth_year(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        lif, _ = account.convert_to_lif(year=2026)
        self.assertEqual(lif.owner_birth_year, 1960)

    def test_convert_after_deadline(self):
        account = LockedInAccount(balance=100000, birth_year=1955)
        lif, _ = account.convert_to_lif(year=2027)
        self.assertTrue(lif.converted_late)

    def test_convert_does_not_mutate_original(self):
        """DP#3: conversion returns new instances, original unchanged."""
        account = LockedInAccount(balance=100000, birth_year=1955)
        lif, depleted = account.convert_to_lif(year=2026)
        self.assertAlmostEqual(account.balance, 100000)  # original unchanged

    def test_convert_requires_year(self):
        """Year parameter is required (no default)."""
        account = LockedInAccount(balance=100000, birth_year=1955)
        # year is now required - calling without it is a TypeError
        with self.assertRaises(TypeError):
            account.convert_to_lif()

    def test_convert_quebec_cri_preserves_jurisdiction(self):
        """Quebec CRI converts to Quebec LIF (FRV), preserving jurisdiction.

        DP#1: jurisdiction is stored, not derived. A Quebec CRI must
        produce a Quebec LIF so withdrawal rules apply correctly.
        """
        account = LockedInAccount(balance=200000, birth_year=1955,
                                   jurisdiction='quebec')
        lif, depleted = account.convert_to_lif(year=2026)
        self.assertEqual(lif.jurisdiction, 'quebec')
        self.assertAlmostEqual(lif.balance, 200000)
        self.assertAlmostEqual(depleted.balance, 0)

    def test_convert_federal_lira_preserves_jurisdiction(self):
        """Federal LIRA converts to federal LIF, preserving jurisdiction."""
        account = LockedInAccount(balance=150000, birth_year=1960,
                                   jurisdiction='federal')
        lif, _ = account.convert_to_lif(year=2026)
        self.assertEqual(lif.jurisdiction, 'federal')

    def test_convert_ontario_lira_preserves_jurisdiction(self):
        """Ontario LIRA converts to Ontario LIF, preserving jurisdiction."""
        account = LockedInAccount(balance=100000, birth_year=1960,
                                   jurisdiction='ontario')
        lif, _ = account.convert_to_lif(year=2026)
        self.assertEqual(lif.jurisdiction, 'ontario')


class TestLIFFundWithdrawal(unittest.TestCase):
    """LIF withdrawal rules — between min and max."""

    def test_withdraw_at_minimum(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        min_w = lif.minimum_withdrawal(2026)  # age 71
        actual, new_lif = lif.withdraw(min_w, year=2026)
        self.assertAlmostEqual(actual, min_w)

    def test_withdraw_above_minimum_below_maximum(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        min_w = lif.minimum_withdrawal(2026)
        max_w = lif.maximum_withdrawal(2026)
        mid = (min_w + max_w) / 2
        actual, new_lif = lif.withdraw(mid, year=2026)
        self.assertAlmostEqual(actual, mid)

    def test_withdraw_above_maximum_capped(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        max_w = lif.maximum_withdrawal(2026)
        actual, new_lif = lif.withdraw(999999, year=2026)
        self.assertAlmostEqual(actual, max_w)

    def test_withdraw_below_minimum_forced(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        min_w = lif.minimum_withdrawal(2026)
        actual, new_lif = lif.withdraw(0, year=2026)
        self.assertAlmostEqual(actual, min_w)

    def test_withdrawal_reduces_balance_in_new_fund(self):
        """DP#3: withdrawal returns new fund with reduced balance."""
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        min_w = lif.minimum_withdrawal(2026)
        actual, new_lif = lif.withdraw(min_w, year=2026)
        self.assertAlmostEqual(new_lif.balance, 300000 - actual)
        self.assertAlmostEqual(lif.balance, 300000)  # original unchanged

    def test_lif_grows_from_returns(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        growth, new_lif = lif.grow(0.07)
        self.assertAlmostEqual(growth, 300000 * 0.07)
        self.assertAlmostEqual(new_lif.balance, 300000 * 1.07)
        self.assertAlmostEqual(lif.balance, 300000)  # original unchanged

    def test_lif_withdrawal_is_taxable(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955, reference_rate=0.06)
        actual, new_lif = lif.withdraw(lif.minimum_withdrawal(2026), year=2026)
        tax = lif.withdrawal_tax(actual, marginal_rate=0.45)
        self.assertGreater(tax, 0)
        self.assertAlmostEqual(tax, actual * 0.45)

    def test_quebec_lif_no_maximum(self):
        """Quebec LIFFund: ages 55+ can withdraw entire balance."""
        lif = LIFFund(balance=300000, owner_birth_year=1955,
                      reference_rate=0.06, jurisdiction='quebec')
        max_w = lif.maximum_withdrawal(2026)  # age 71
        self.assertAlmostEqual(max_w, 300000)


class TestImmutability(unittest.TestCase):
    """DP#3, DP#26: dataclasses are frozen — no mutation."""

    def test_locked_in_account_frozen(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        with self.assertRaises(AttributeError):
            account.balance = 50000

    def test_lif_fund_frozen(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955)
        with self.assertRaises(AttributeError):
            lif.balance = 100000

    def test_grow_returns_new_instance(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        growth, new_account = account.grow(0.05)
        self.assertIsNot(new_account, account)
        self.assertAlmostEqual(account.balance, 100000)
        self.assertAlmostEqual(new_account.balance, 105000)

    def test_withdraw_returns_new_instance(self):
        lif = LIFFund(balance=300000, owner_birth_year=1955)
        actual, new_lif = lif.withdraw(20000, year=2026)
        self.assertIsNot(new_lif, lif)


class TestLockedInAccountGrowAndProject(unittest.TestCase):
    """Multi-year growth projection for CRI/LIRA before conversion."""

    def test_grow_no_withdrawals(self):
        account = LockedInAccount(balance=100000, birth_year=1960)
        for year in range(5):
            _, account = account.grow(0.05)
        expected = 100000 * (1.05 ** 5)
        self.assertAlmostEqual(account.balance, expected, places=0)

    def test_convert_then_lif_withdrawal(self):
        account = LockedInAccount(balance=100000, birth_year=1955)
        for year in range(3):
            _, account = account.grow(0.05)
        lif, _ = account.convert_to_lif(year=2026)
        min_w = lif.minimum_withdrawal(2026)
        actual, _ = lif.withdraw(min_w, year=2026)
        self.assertAlmostEqual(actual, min_w)

    def test_from_dict_roundtrip(self):
        data = {
            'balance': 50000,
            'birth_year': 1975,
            'transfer_date': '2020-06-15',
            'source_pension_plan': 'rpp',
            'creditor_protected': True,
            'jurisdiction': 'quebec',
        }
        account = LockedInAccount.from_dict(data)
        self.assertEqual(account.jurisdiction, 'quebec')
        exported = account.to_dict()
        self.assertEqual(exported['jurisdiction'], 'quebec')
        restored = LockedInAccount.from_dict(exported)
        self.assertAlmostEqual(restored.balance, account.balance)
        self.assertEqual(restored.birth_year, account.birth_year)
        self.assertEqual(restored.creditor_protected, account.creditor_protected)
        self.assertEqual(restored.jurisdiction, account.jurisdiction)

    def test_from_dict_default_creditor_protected(self):
        """DP#24: from_dict defaults creditor_protected to True."""
        data = {'balance': 50000, 'birth_year': 1975}
        account = LockedInAccount.from_dict(data)
        self.assertTrue(account.creditor_protected)

    def test_from_dict_default_jurisdiction(self):
        """DP#24: from_dict defaults jurisdiction to 'federal'."""
        data = {'balance': 50000, 'birth_year': 1975}
        account = LockedInAccount.from_dict(data)
        self.assertEqual(account.jurisdiction, 'federal')

    def test_from_dict_quebec_jurisdiction(self):
        """Quebec jurisdiction round-trips through from_dict/to_dict."""
        data = {'balance': 50000, 'birth_year': 1975, 'jurisdiction': 'quebec'}
        account = LockedInAccount.from_dict(data)
        self.assertEqual(account.jurisdiction, 'quebec')
        exported = account.to_dict()
        self.assertEqual(exported['jurisdiction'], 'quebec')

    def test_to_dict_includes_jurisdiction(self):
        """DP#24: to_dict includes jurisdiction field."""
        account = LockedInAccount(balance=100000, birth_year=1960, jurisdiction='ontario')
        d = account.to_dict()
        self.assertEqual(d['jurisdiction'], 'ontario')


class TestLIFNoContributionInvariant(unittest.TestCase):
    """LIF also does not accept new contributions."""

    def test_lif_contribute_returns_zero(self):
        lif = LIFFund(balance=200000, owner_birth_year=1955, reference_rate=0.06)
        result, new_lif = lif.contribute(5000)
        self.assertAlmostEqual(result, 0)
        self.assertAlmostEqual(new_lif.balance, 200000)

    def test_lif_to_dict_includes_jurisdiction(self):
        lif = LIFFund(balance=200000, owner_birth_year=1955,
                      reference_rate=0.06, jurisdiction='quebec')
        d = lif.to_dict()
        self.assertEqual(d['jurisdiction'], 'quebec')


class TestLIFFundFromDict(unittest.TestCase):
    """DP#24: LIFFund.from_dict round-trip."""

    def test_from_dict_roundtrip(self):
        data = {
            'balance': 500000,
            'owner_birth_year': 1955,
            'reference_rate': 0.06,
            'converted_late': False,
            'jurisdiction': 'federal',
        }
        lif = LIFFund.from_dict(data)
        self.assertAlmostEqual(lif.balance, 500000)
        self.assertEqual(lif.owner_birth_year, 1955)
        self.assertAlmostEqual(lif.reference_rate, 0.06)
        self.assertFalse(lif.converted_late)
        self.assertEqual(lif.jurisdiction, 'federal')
        # Round-trip
        exported = lif.to_dict()
        restored = LIFFund.from_dict(exported)
        self.assertAlmostEqual(restored.balance, lif.balance)
        self.assertEqual(restored.owner_birth_year, lif.owner_birth_year)

    def test_from_dict_defaults(self):
        """DP#24: from_dict uses sensible defaults."""
        data = {'balance': 100000}
        lif = LIFFund.from_dict(data)
        self.assertAlmostEqual(lif.balance, 100000)
        self.assertEqual(lif.owner_birth_year, 0)
        self.assertAlmostEqual(lif.reference_rate, 0.06)
        self.assertFalse(lif.converted_late)
        self.assertEqual(lif.jurisdiction, 'federal')

    def test_from_dict_quebec(self):
        data = {
            'balance': 300000,
            'owner_birth_year': 1960,
            'jurisdiction': 'quebec',
        }
        lif = LIFFund.from_dict(data)
        self.assertEqual(lif.jurisdiction, 'quebec')


class TestQuebecUnder55PrescribedRate(unittest.TestCase):
    """Quebec LIF under-55 maximum uses prescribed-rate formula.

    Per Retraite Québec, for FRV holders under age 55, the maximum annual
    withdrawal is prescribed_rate × balance (not the C/F actuarial factors).
    Source: https://www.retraitequebec.gouv.qc.ca/en/services/cri-frv/Pages/retrait-maximal-frv.aspx
    """

    def test_quebec_under_55_prescribed_rate_2024(self):
        """Quebec under-55 in 2024: max = 6.1% × balance (Schedule 0.6 factor)."""
        balance = 200000
        max_w = lif_maximum_withdrawal(balance, age=54, year=2024,
                                       jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance * 0.061, places=2)

    def test_quebec_under_55_prescribed_rate_2025(self):
        """Quebec under-55 in 2025: max = 6% × balance (Retraite Québec prescribed rate)."""
        balance = 200000
        max_w = lif_maximum_withdrawal(balance, age=54, year=2025,
                                       jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance * 0.06, places=2)

    def test_quebec_under_55_prescribed_rate_2026(self):
        """Quebec under-55 in 2026: max = 6.25% × balance (Retraite Québec prescribed rate)."""
        balance = 200000
        max_w = lif_maximum_withdrawal(balance, age=54, year=2026,
                                       jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance * 0.0625, places=2)

    def test_federal_under_55_uses_annuity_formula(self):
        """Federal under-55 uses annuity formula for maximum withdrawal."""
        balance = 100000
        max_w = lif_maximum_withdrawal(balance, 54, year=2026,
                                       jurisdiction='federal')
        # Age 54 at 3.49% CANSIM rate: factor ~4.76%
        self.assertGreater(max_w, 0)
        self.assertLess(max_w, balance)
        # Maximum should be higher than minimum withdrawal at same age
        min_w = lif_minimum_withdrawal(balance, 54)
        self.assertGreater(max_w, min_w)

    def test_quebec_under_50_prescribed_rate(self):
        """Quebec under-55 at age 50: still uses prescribed rate."""
        balance = 150000
        max_w = lif_maximum_withdrawal(balance, age=50, year=2026,
                                       jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance * 0.0625, places=2)

    def test_quebec_prescribed_rate_zero_balance(self):
        """Prescribed rate with zero balance returns zero."""
        max_w = lif_maximum_withdrawal(0, age=54, year=2026,
                                      jurisdiction='quebec')
        self.assertAlmostEqual(max_w, 0)

    def test_quebec_prescribed_rates_are_reasonable(self):
        """Prescribed rates should be between 0 and 1."""
        for year, rate in QUEBEC_PRESCRIBED_RATES.items():
            self.assertGreater(rate, 0)
            self.assertLess(rate, 1)

    def test_quebec_under_55_with_temporary_income(self):
        """Quebec under-55 FRV max = prescribed_rate × balance - temporary_income (Article 20)."""
        balance = 200000
        # Without temporary income: 6% × 200000 = 12000
        max_no_ti = lif_maximum_withdrawal(balance, age=54, year=2025,
                                           jurisdiction='quebec')
        self.assertAlmostEqual(max_no_ti, 12000)
        # With $3000 temporary income: 12000 - 3000 = 9000
        max_with_ti = lif_maximum_withdrawal(balance, age=54, year=2025,
                                             jurisdiction='quebec',
                                             temporary_income=3000)
        self.assertAlmostEqual(max_with_ti, 9000)

    def test_quebec_under_55_temporary_income_exceeds_prescribed(self):
        """If temporary_income > prescribed_rate × balance, result is 0 (floored)."""
        balance = 200000
        # 6% × 200000 = 12000; temporary_income = 15000 > 12000
        max_w = lif_maximum_withdrawal(balance, age=54, year=2025,
                                       jurisdiction='quebec',
                                       temporary_income=15000)
        self.assertAlmostEqual(max_w, 0)


class TestQuebecYearBoundary(unittest.TestCase):
    """Quebec no-maximum rule only applies from 2025 onward."""

    def test_quebec_has_maximum_before_2025(self):
        """Quebec LIF has maximum for ages 55+ before 2025."""
        balance = 300000
        age = 65
        max_w = lif_maximum_withdrawal(balance, age, year=2024,
                                        jurisdiction='quebec')
        self.assertLess(max_w, balance)

    def test_quebec_no_maximum_in_2025(self):
        """Quebec LIF no maximum for ages 55+ starting 2025."""
        balance = 300000
        age = 65
        max_w = lif_maximum_withdrawal(balance, age, year=2025,
                                        jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance)

    def test_quebec_no_maximum_in_2026(self):
        """Quebec LIF no maximum for ages 55+ in 2026."""
        balance = 300000
        age = 65
        max_w = lif_maximum_withdrawal(balance, age, year=2026,
                                        jurisdiction='quebec')
        self.assertAlmostEqual(max_w, balance)


class TestFallbackAgeHandling(unittest.TestCase):
    """Test fallback behavior for ages outside OSFI factor tables."""

    def test_age_54_federal_uses_annuity_formula(self):
        """Age 54 uses annuity formula for maximum withdrawal (not minimum)."""
        balance = 100000
        max_w = lif_maximum_withdrawal(balance, 54, year=2026)
        # Age 54 at 3.49% CANSIM rate: factor = 1/ä_{36} at i=3.49%
        # Should be close to but higher than minimum (1/(90-54) = 2.78%)
        self.assertGreater(max_w, 0)
        self.assertLess(max_w, balance)  # Maximum is less than full balance

    def test_age_30_federal_uses_annuity_formula(self):
        """Age 30 uses annuity formula for maximum withdrawal."""
        balance = 100000
        max_w = lif_maximum_withdrawal(balance, 30, year=2026)
        # Age 30 factor should be in the data table now
        self.assertGreater(max_w, 0)
        self.assertLess(max_w, balance)

    def test_age_89_returns_full_balance(self):
        """Age 89 has OSFI factor of 100%."""
        balance = 100000
        max_w = lif_maximum_withdrawal(balance, 89, year=2026)
        self.assertAlmostEqual(max_w, balance)


class TestZeroBalanceEdgeCase(unittest.TestCase):
    """Zero balance should produce zero withdrawals."""

    def test_zero_balance_minimum(self):
        self.assertAlmostEqual(lif_minimum_withdrawal(0, 71), 0)

    def test_zero_balance_maximum(self):
        self.assertAlmostEqual(lif_maximum_withdrawal(0, 71, year=2026), 0)

    def test_zero_balance_quebec(self):
        self.assertAlmostEqual(
            lif_maximum_withdrawal(0, 65, year=2026, jurisdiction='quebec'), 0)


class TestWithdrawalRangeIncludesJurisdiction(unittest.TestCase):
    """compute_lif_withdrawal_range includes jurisdiction in return dict."""

    def test_range_includes_jurisdiction_federal(self):
        result = compute_lif_withdrawal_range(300000, 71, year=2026)
        self.assertEqual(result['jurisdiction'], 'federal')

    def test_range_includes_jurisdiction_quebec(self):
        result = compute_lif_withdrawal_range(300000, 71, year=2026,
                                                jurisdiction='quebec')
        self.assertEqual(result['jurisdiction'], 'quebec')


class TestOSFIFactorAccuracy(unittest.TestCase):
    """Verify OSFI factors match officially published values.

    Spot-checks against OSFI tables at:
    https://www.osfi-bsif.gc.ca/en/supervision/regulated-entities/pension-plans/lif-maximum-withdrawal
    """

    def test_osfi_2025_age_71(self):
        """2025: age 71 factor should be 6.9597%."""
        self.assertAlmostEqual(
            LIF_MAX_WITHDRAWAL_FACTORS[2025][71], 0.069597, places=6)

    def test_osfi_2025_age_65(self):
        """2025: age 65 factor should be 5.9101%."""
        self.assertAlmostEqual(
            LIF_MAX_WITHDRAWAL_FACTORS[2025][65], 0.059101, places=6)

    def test_osfi_2025_age_55(self):
        """2025: age 55 factor should be 5.0987%."""
        self.assertAlmostEqual(
            LIF_MAX_WITHDRAWAL_FACTORS[2025][55], 0.050987, places=6)

    def test_osfi_2026_age_71(self):
        """2026: age 71 factor should be 7.0804%."""
        self.assertAlmostEqual(
            LIF_MAX_WITHDRAWAL_FACTORS[2026][71], 0.070804, places=6)

    def test_osfi_2026_age_65(self):
        """2026: age 65 factor should be 6.0272%."""
        self.assertAlmostEqual(
            LIF_MAX_WITHDRAWAL_FACTORS[2026][65], 0.060272, places=6)

    def test_osfi_2026_age_55(self):
        """2026: age 55 factor should be 5.2096%."""
        self.assertAlmostEqual(
            LIF_MAX_WITHDRAWAL_FACTORS[2026][55], 0.052096, places=6)

    def test_osfi_2025_2026_factors_differ(self):
        """2025 and 2026 must have different factors (different CANSIM rates)."""
        for age in LIF_MAX_WITHDRAWAL_FACTORS[2025]:
            if age in LIF_MAX_WITHDRAWAL_FACTORS[2026] and age < 89:
                self.assertNotEqual(
                    LIF_MAX_WITHDRAWAL_FACTORS[2025][age],
                    LIF_MAX_WITHDRAWAL_FACTORS[2026][age],
                    msg=f"Age {age}: 2025 and 2026 factors must differ")


class TestBirthYearDefault(unittest.TestCase):
    """DP#13: birth_year defaults to 0, not a person-specific value."""

    def test_locked_in_account_birth_year_default(self):
        account = LockedInAccount()
        self.assertEqual(account.birth_year, 0)

    def test_lif_fund_birth_year_default(self):
        lif = LIFFund()
        self.assertEqual(lif.owner_birth_year, 0)

    def test_from_dict_birth_year_default(self):
        data = {'balance': 50000}
        account = LockedInAccount.from_dict(data)
        self.assertEqual(account.birth_year, 0)

    def test_lif_from_dict_birth_year_default(self):
        data = {'balance': 50000}
        lif = LIFFund.from_dict(data)
        self.assertEqual(lif.owner_birth_year, 0)


if __name__ == '__main__':
    unittest.main()
