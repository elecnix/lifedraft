#!/usr/bin/env python3
"""Unit tests for NonRegAccount capital-gains realization (issue #550).

DP#19: cost basis is recorded on contribution; tax is computed only on
disposition. A non-registered holding may grow for many years with NO tax
recognized year-over-year (deferral); tax is recognized only when the gain
is realized via capital_gains_tax(mtr).

Invariant under test:
    realized tax == (balance - cost_basis) * inclusion_rate * mtr

Run with:
    python3 -m pytest countries/canada/tests/test_non_reg_capital_gains_realization.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.account_models import NonRegAccount
from countries.canada.income_type import capital_gains_inclusion_rate


class TestCapitalGainsRealization(unittest.TestCase):
    """capital_gains_tax() is the realization event — gains defer until called."""

    def test_growth_defers_tax_until_realization(self):
        """Several years of growth recognize zero tax; tax appears only on realize."""
        acct = NonRegAccount()
        acct.contribute(100_000)  # cost basis fixed at 100k
        mtr = 0.45

        # Grow for several periods. The grow() event must never recognize tax,
        # and cost basis must stay fixed (deferral invariant).
        for _ in range(5):
            acct.grow(0.07)
            self.assertEqual(acct.cost_basis, 100_000)

        gain = acct.balance - acct.cost_basis
        self.assertGreater(gain, 0)  # unrealized gain has accrued

        # The realization event: tax is exactly inclusion * mtr on the full gain.
        tax = acct.capital_gains_tax(mtr)
        expected = gain * acct.capital_gains_inclusion * mtr
        self.assertAlmostEqual(tax, expected)

    def test_no_gain_no_tax(self):
        """A holding at or below cost basis realizes zero tax."""
        acct = NonRegAccount()
        acct.contribute(100_000)
        acct.grow(-0.20)  # loss
        self.assertEqual(acct.capital_gains_tax(0.45), 0.0)

    def test_tiered_inclusion_on_realization(self):
        """Post-2024 tiered inclusion applied via the realization method (DP#27).

        A large realized gain crosses the $250K boundary: first $250K at 50%,
        the excess at 66.67%. The blended rate is supplied to the account, and
        capital_gains_tax() must apply it to the whole gain.
        """
        acct = NonRegAccount()
        acct.contribute(200_000)
        # Drive a $300K gain (balance 500K, cost basis 200K).
        acct.grow(1.5)
        gain = acct.balance - acct.cost_basis
        self.assertAlmostEqual(gain, 300_000)

        blended_rate = capital_gains_inclusion_rate(gain_amount=gain, year=2026)
        acct.capital_gains_inclusion = blended_rate
        mtr = 0.45

        tax = acct.capital_gains_tax(mtr)
        expected_inclusion = (250_000 * 0.50 + 50_000 * (2 / 3))
        expected = expected_inclusion * mtr
        self.assertAlmostEqual(tax, expected)


if __name__ == '__main__':
    unittest.main()
