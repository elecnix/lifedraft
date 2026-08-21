#!/usr/bin/env python3
"""Unit tests for the core ``tax_calculator`` module (DP#11).

One module per file: this file tests ONLY the jurisdiction-agnostic core
``tax_calculator`` — brackets / ``_BracketProxy``, ``marginal_rate``,
``effective_tax_rate``, ``capital_gains_rate``, the ``InvestmentIncomeType``
enum, and the ``tax_on_investment_income`` dispatch function — plus
``tax_data.TaxDataProvider.get_combined_brackets`` (core data).

``tax_on_investment_income`` is a *dispatch* function: for eligible
dividends, capital-gains inclusion, and US withholding it delegates to
``countries.canada.tax_calc`` / ``countries.canada.income_type``. Tests that
exercise it therefore compose core + Canada (DP#11 integration); they live
here because the function under test is core, and the delegation is internal.
The Canada-side functions themselves are unit-tested in isolation in
``countries/canada/tests/test_tax_calc.py``.

The ``InvestmentIncomeType`` / capital-gains / interest / US-dividend / ROC
classes were relocated from ``countries/canada/tests/test_investment_income.py``
(which sat in the country tree but tested core functions); the
``TestEligibleDividendTax`` / WHT / asset-location / federal-tax / RRSP /
spousal classes that used to live here were relocated to
``countries/canada/tests/test_tax_calc.py`` (country module → country tests).
No assertion was weakened or dropped; the bodies are unchanged. The unused
``marginal_rate`` import carried by the investment-income file, and the
unused ``tax_on_income`` import carried here, were dropped (neither was
called).

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/test_tax_calc_full.py -v
"""

from tax_data import default_tax_provider
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from tax_calculator import (
    QUEBEC_TAX_BRACKETS_2026,
    marginal_rate,
    effective_tax_rate, capital_gains_rate,
    InvestmentIncomeType, tax_on_investment_income,
)


class TestBracketProxy(unittest.TestCase):
    """Test _BracketProxy lazy bracket accessor (lines 69-90)."""

    def test_iter(self):
        """Proxy supports iteration."""
        brackets = list(QUEBEC_TAX_BRACKETS_2026)
        self.assertGreater(len(brackets), 0)

    def test_getitem(self):
        """Proxy supports indexing."""
        first = QUEBEC_TAX_BRACKETS_2026[0]
        self.assertIsNotNone(first)

    def test_len(self):
        """Proxy supports len()."""
        self.assertGreater(len(QUEBEC_TAX_BRACKETS_2026), 0)

    def test_bracket_structure(self):
        """Each bracket is a tuple (min, max, unused, unused, rate)."""
        for b in QUEBEC_TAX_BRACKETS_2026:
            self.assertEqual(len(b), 5)


class TestGetCombinedBrackets(unittest.TestCase):
    """Test get_combined_brackets with province parameter."""

    def test_quebec_brackets(self):
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        self.assertGreater(len(brackets), 0)

    def test_ontario_brackets(self):
        """Non-Quebec province: different brackets."""
        brackets = default_tax_provider().get_combined_brackets(2026, "ontario")
        self.assertGreater(len(brackets), 0)

    def test_bracket_fields(self):
        """Each bracket has min, max, rate."""
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        for b in brackets:
            self.assertIn('min', b)
            self.assertIn('rate', b)


class TestEffectiveTaxRate(unittest.TestCase):
    """Test effective_tax_rate() (line 229)."""

    def test_effective_rate(self):
        rate = effective_tax_rate(100000)
        self.assertGreater(rate, 0)
        self.assertLess(rate, 0.50)

    def test_zero_income(self):
        rate = effective_tax_rate(0)
        self.assertAlmostEqual(rate, 0)


class TestCapitalGainsRate(unittest.TestCase):
    """Test capital_gains_rate() (lines 247-250, 265-266)."""

    def test_cg_rate_at_income(self):
        rate = capital_gains_rate(100000)
        # At 50% inclusion, effective CG rate = MTR × 50%
        mtr = marginal_rate(100000)
        self.assertAlmostEqual(rate, mtr * 0.50, places=3)

    def test_cg_rate_with_custom_inclusion(self):
        rate = capital_gains_rate(100000, inclusion_rate=0.667)
        mtr = marginal_rate(100000)
        self.assertAlmostEqual(rate, mtr * 0.667, places=3)


class TestInvestmentIncomeEdgeCases(unittest.TestCase):
    """Test tax_on_investment_income for edge cases (lines 562-570, 608, 643-644)."""

    def test_zero_amount(self):
        result = tax_on_investment_income(0, InvestmentIncomeType.INTEREST, 0.4571)
        self.assertEqual(result['tax'], 0)
        self.assertEqual(result['effective_rate'], 0)

    def test_foreign_dividend_non_us(self):
        """Non-US dividend: 25% WHT default, FTC limited."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.FOREIGN_DIVIDEND_NON_US,
            marginal_rate=0.4571)
        self.assertGreater(result['tax'], 0)
        self.assertEqual(result['wht'], 2500)  # 25% × $10k
        self.assertGreater(result['effective_rate'], 0)

    def test_foreign_dividend_non_us_low_mtr(self):
        """When Canadian tax < WHT → FTC limited to Canadian tax."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.FOREIGN_DIVIDEND_NON_US,
            marginal_rate=0.15)
        # Canadian tax = $1,500, WHT = $2,500 → FTC = $1,500 (limited)
        self.assertAlmostEqual(result['foreign_tax_credit'], 1500, places=0)

    def test_roc_with_acb_zero(self):
        """ROC when ACB is already 0 → all capital gain."""
        result = tax_on_investment_income(
            5000, InvestmentIncomeType.RETURN_OF_CAPITAL,
            marginal_rate=0.4571, acb=0)
        self.assertGreater(result['tax'], 0)
        self.assertAlmostEqual(result['new_acb'], 0)

    def test_unknown_income_type(self):
        """Unknown income type → zero tax with message."""
        # We can't easily create an unknown InvestmentIncomeType enum,
        # but we can test that all defined types work
        for itype in InvestmentIncomeType:
            result = tax_on_investment_income(10000, itype, 0.4571)
            self.assertIsNotNone(result['tax'])


class TestInvestmentIncomeTypeEnum(unittest.TestCase):
    """Test the InvestmentIncomeType enum."""

    def test_all_types_exist(self):
        """All expected income types are defined."""
        expected = [
            'CANADIAN_ELIGIBLE_DIVIDEND', 'CAPITAL_GAIN', 'INTEREST',
            'FOREIGN_DIVIDEND_US', 'FOREIGN_DIVIDEND_NON_US', 'RETURN_OF_CAPITAL',
        ]
        for name in expected:
            self.assertIn(name, [e.name for e in InvestmentIncomeType])

    def test_enum_values(self):
        """Enum values are lowercase strings."""
        self.assertEqual(InvestmentIncomeType.CAPITAL_GAIN.value, "capital_gain")
        self.assertEqual(InvestmentIncomeType.INTEREST.value, "interest")


class TestCapitalGainsTax(unittest.TestCase):
    """Test capital gains via investment income function."""

    MARGINAL_RATE = 0.4571

    def test_50pct_inclusion(self):
        """Capital gains: 50% inclusion rate → effective rate = 50% × MTR."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.CAPITAL_GAIN, self.MARGINAL_RATE)
        expected_tax = 10000 * 0.50 * self.MARGINAL_RATE
        self.assertAlmostEqual(result['tax'], expected_tax, places=0)
        self.assertAlmostEqual(result['effective_rate'], 0.50 * self.MARGINAL_RATE, places=3)

    def test_zero_gain(self):
        """Zero capital gain = zero tax."""
        result = tax_on_investment_income(
            0, InvestmentIncomeType.CAPITAL_GAIN, self.MARGINAL_RATE)
        self.assertEqual(result['tax'], 0)


class TestInterestIncomeTax(unittest.TestCase):
    """Test interest/bond income: fully taxable at marginal rate."""

    MARGINAL_RATE = 0.4571

    def test_interest_fully_taxable(self):
        """Interest income is fully taxable at marginal rate."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.INTEREST, self.MARGINAL_RATE)
        expected = 10000 * self.MARGINAL_RATE
        self.assertAlmostEqual(result['tax'], expected, places=0)
        self.assertAlmostEqual(result['effective_rate'], self.MARGINAL_RATE, places=3)


class TestUSDividendTax(unittest.TestCase):
    """Test US dividend withholding tax and foreign tax credit."""

    MARGINAL_RATE = 0.4571

    def test_non_reg_ftc_recovers_wht(self):
        """In non-reg, FTC recovers the 15% WHT."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.FOREIGN_DIVIDEND_US, self.MARGINAL_RATE)
        # WHT is $1,500 (15% of $10,000)
        self.assertAlmostEqual(result['wht'], 1500, places=0)
        # FTC covers the WHT
        self.assertAlmostEqual(result['foreign_tax_credit'], 1500, places=0)
        # Tax in non-reg ≈ (marginal_rate × $10k) - $1.5k
        self.assertGreater(result['tax'], 0)
        self.assertLess(result['tax'], 10000 * self.MARGINAL_RATE)

    def test_rrsp_zero_wht(self):
        """In RRSP, US WHT is 0% via tax treaty."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.FOREIGN_DIVIDEND_US, self.MARGINAL_RATE)
        self.assertEqual(result['tax_in_rrsp'], 0)

    def test_tfsa_unrecoverable_drag(self):
        """In TFSA, 15% WHT is unrecoverable drag."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.FOREIGN_DIVIDEND_US, self.MARGINAL_RATE)
        self.assertAlmostEqual(result['tax_in_tfsa'], 1500, places=0)


class TestReturnOfCapital(unittest.TestCase):
    """Test ROC: reduces ACB, triggers capital gains when ACB hits 0."""

    MARGINAL_RATE = 0.4571

    def test_roc_reduces_acb(self):
        """ROC reduces ACB, no immediate tax."""
        result = tax_on_investment_income(
            5000, InvestmentIncomeType.RETURN_OF_CAPITAL, self.MARGINAL_RATE,
            acb=10000)
        self.assertEqual(result['tax'], 0)
        self.assertEqual(result['new_acb'], 5000)

    def test_roc_exhausts_acb(self):
        """ROC exceeding ACB triggers capital gains."""
        result = tax_on_investment_income(
            15000, InvestmentIncomeType.RETURN_OF_CAPITAL, self.MARGINAL_RATE,
            acb=10000)
        # Excess: $15k - $10k ACB = $5k capital gain
        expected_cg_tax = 5000 * 0.50 * self.MARGINAL_RATE
        self.assertAlmostEqual(result['tax'], expected_cg_tax, places=0)
        self.assertEqual(result['new_acb'], 0)

    def test_roc_zero_acb(self):
        """ROC with zero ACB: full amount is capital gain."""
        result = tax_on_investment_income(
            5000, InvestmentIncomeType.RETURN_OF_CAPITAL, self.MARGINAL_RATE,
            acb=0)
        expected_tax = 5000 * 0.50 * self.MARGINAL_RATE
        self.assertAlmostEqual(result['tax'], expected_tax, places=0)


class TestInvestmentIncomeTypeOrdering(unittest.TestCase):
    """Compare effective tax rates across income types at same MTR.

    Relocated from ``TestTaxComparisonAcrossTypes`` in
    ``test_investment_income.py``: this single assertion exercises only the
    core ``tax_on_investment_income`` dispatch, so it lives with the core
    tests. The companion assertion comparing the Canada
    ``effective_dividend_rate`` to the capital-gains rate lives in
    ``countries/canada/tests/test_tax_calc.py`` (``TestDividendVsCapitalGainsRate``).
    """

    MARGINAL_RATE = 0.4571
    AMOUNT = 10000

    def test_interest_highest_tax(self):
        """Interest income has the highest effective tax rate."""
        interest_result = tax_on_investment_income(
            self.AMOUNT, InvestmentIncomeType.INTEREST, self.MARGINAL_RATE)
        cg_result = tax_on_investment_income(
            self.AMOUNT, InvestmentIncomeType.CAPITAL_GAIN, self.MARGINAL_RATE)
        div_result = tax_on_investment_income(
            self.AMOUNT, InvestmentIncomeType.CANADIAN_ELIGIBLE_DIVIDEND, self.MARGINAL_RATE)
        self.assertGreater(interest_result['effective_rate'], cg_result['effective_rate'])
        self.assertGreater(interest_result['effective_rate'], div_result['effective_rate'])


if __name__ == '__main__':
    unittest.main()