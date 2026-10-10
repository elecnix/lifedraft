"""Issue #349: the standard HBP repayment starts a year late.

CRA: the normal repayment period begins "the second year after the year you made
your first withdrawal" -- its own example is a 2020 withdrawal repaying from
2022, and ITA s.146.01(4) gives the same. The temporary relief (a 2022-2025
first withdrawal) defers that to the FIFTH year, which the engine gets right.

`hbp_rules` had `HBP_REPAYMENT_START_DELAY = 2` and
`repayment_start_year() = withdrawal_year + delay + 1`, i.e. a 2026 withdrawal
started repaying in 2029 -- the THIRD year after. The relief constant (4, +1 ->
fifth year) was correct, so the two constants used different conventions: the
relief delay is meant to be exactly three years more than the normal one, and
with the normal delay wrongly at 2 that relationship did not hold.

So every NON-relief withdrawal -- which is every withdrawal from 2026 on -- had
its first instalment a year late: the household is modelled as living in year 2
of the HBP free of charge.

This test pins the schedule against the statutory examples rather than against
the code's own arithmetic, so it fails if the delay is ever moved again.
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.hbp_rules import (
    HBP_RELIEF_START_DELAY,
    HBP_REPAYMENT_START_DELAY,
    HBPAccount,
    repayment_start_delay_for_year,
)


class TheStandardScheduleStartsTheSecondYear(unittest.TestCase):
    """CRA: the second year after the withdrawal year."""

    def test_cras_own_example_start_year(self):
        """CRA's page: a 2020 withdrawal repays from 2022."""
        acct = HBPAccount(withdrawal=60000.0, withdrawal_year=2020)
        self.assertEqual(acct.repayment_start_year(), 2022,
                         "ITA s.146.01(4) / CRA: repayment begins the second "
                         "year after the year of the first withdrawal")

    def test_a_2026_withdrawal_starts_in_2028(self):
        """The issue's case: 2026 is outside the relief years, so the NORMAL
        rule applies and it is off by one today."""
        acct = HBPAccount(withdrawal=60000.0, withdrawal_year=2026)
        self.assertEqual(acct.repayment_start_year(), 2028)

    def test_the_schedule_starts_in_the_second_year_too(self):
        """The schedule must agree with the start year it publishes."""
        acct = HBPAccount(withdrawal=60000.0, withdrawal_year=2026)
        schedule = acct.generate_repayment_schedule()
        self.assertEqual(schedule[0]["year"], 2028,
                         "the first instalment row must land in the second "
                         "year after withdrawal")


class TheReliefCaseIsUntouched(unittest.TestCase):
    """The temporary relief already worked; fixing the normal delay must not
    move it. Budget 2024: a 2022 first withdrawal defers repayment to the fifth
    year (2027)."""

    def test_relief_years_still_start_in_the_fifth_year(self):
        for year, expected in ((2022, 2027), (2023, 2028), (2024, 2029), (2025, 2030)):
            with self.subTest(withdrawal_year=year):
                acct = HBPAccount(withdrawal=60000.0, withdrawal_year=year)
                self.assertEqual(acct.repayment_start_year(), expected)

    def test_the_relief_delay_is_exactly_three_years_more_than_the_normal(self):
        """The two constants must differ by THREE, not two -- the whole basis of
        the relief is that it defers the normal start by three years."""
        normal = repayment_start_delay_for_year(2026)
        relief = repayment_start_delay_for_year(2024)
        self.assertEqual(relief - normal, 3)


class TheConstantsMatchTheRule(unittest.TestCase):
    """The normal delay is 1: withdrawal_year + 1 + 1 = the second year after."""

    def test_the_normal_delay_is_one(self):
        self.assertEqual(HBP_REPAYMENT_START_DELAY, 1)

    def test_the_relief_constant_is_untouched(self):
        self.assertEqual(HBP_RELIEF_START_DELAY, 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()