"""Issue #366: the Quebec home-support credit for seniors (CIMAD), priced.

Every figure pinned below comes from one of the two sources named in the
module's docstring — Revenu Québec's line-458 page (2025 return) and the CFFP's
tax-measure guide — and NOT from the issue, which describes the program without
stating a rate, a cap or a threshold.

The tests are grouped by the question they answer, because the interesting
failures here are all of one shape: a *plausible* number produced from data the
module was never given (an unpriced year, another year's reduction thresholds,
or a situation the caller never declared).
"""
import unittest

from countries.canada.provinces.quebec.home_support import (
    autonomous_expense_cap,
    credit_rate,
    eligible_service_expenses_from_rent,
    home_support_credit,
    renter_credit,
)

SINGLE = "single_autonomous"
COUPLE = "couple_autonomous"


class TestThePublishedParameters(unittest.TestCase):
    """The rate, the caps, the thresholds — each with its source."""

    def test_the_rate_is_the_published_year_rate(self):
        """39% for 2025 (line 458: 'égal à 39 % de vos dépenses admissibles'),
        40% for 2026 (the CFFP's gradual-increase table, 36/37/38/39/40)."""
        self.assertEqual(credit_rate(2025), 0.39)
        self.assertEqual(credit_rate(2026), 0.40)

    def test_the_cap_is_the_published_annual_maximum(self):
        """$19,500 single / $39,000 couple, unchanged since 2017."""
        self.assertEqual(autonomous_expense_cap(2025, SINGLE), 19_500.0)
        self.assertEqual(autonomous_expense_cap(2025, COUPLE), 39_000.0)

    def test_the_maximum_credit_is_the_published_product(self):
        """Line 458 prints both columns: 39% x 19,500 = 7,605 for a single
        autonomous person, and 39% x 39,000 = 15,210 for a couple, in 2025.
        The 2026 preview prints 7,800 and 15,600 at 40%."""
        self.assertAlmostEqual(
            home_support_credit(2025, 19_500.0, 0.0, situation=SINGLE).credit,
            7_605.0, places=6)
        self.assertAlmostEqual(
            home_support_credit(2025, 39_000.0, 0.0, situation=COUPLE).credit,
            15_210.0, places=6)
        self.assertAlmostEqual(
            home_support_credit(2026, 19_500.0, 0.0, situation=SINGLE).credit,
            7_800.0, places=6)
        self.assertAlmostEqual(
            home_support_credit(2026, 39_000.0, 0.0, situation=COUPLE).credit,
            15_600.0, places=6)


class TestTheExpenseCap(unittest.TestCase):

    def test_the_cap_binds_but_does_not_discard_the_excess_silently(self):
        """$25,000 of expenses on a single autonomous claimant is capped at
        $19,500 — and the result SAYS so, so the household can see which limit
        it hit rather than wondering why the credit stopped growing."""
        result = home_support_credit(2025, 25_000.0, 0.0, situation=SINGLE)
        self.assertEqual(result.eligible_expenses, 19_500.0)
        self.assertEqual(result.expense_cap, 19_500.0)
        self.assertAlmostEqual(result.credit, 7_605.0, places=6)

    def test_a_couple_cap_is_not_the_single_cap(self):
        """The whole reason the situation is an argument: 39,000 of expenses
        priced as a COUPLE is 15,210, priced as a SINGLE it is capped at 7,605.
        A caller that forgot to declare the situation cannot get the cheaper
        answer by accident — it must pass one."""
        couple = home_support_credit(2025, 39_000.0, 0.0, situation=COUPLE)
        single = home_support_credit(2025, 39_000.0, 0.0, situation=SINGLE)
        self.assertAlmostEqual(couple.credit, 15_210.0, places=6)
        self.assertAlmostEqual(single.credit, 7_605.0, places=6)

    def test_expenses_below_the_cap_are_priced_as_paid(self):
        result = home_support_credit(2025, 4_000.0, 0.0, situation=SINGLE)
        self.assertAlmostEqual(result.credit, 1_560.0, places=6)


class TestTheIncomeReduction(unittest.TestCase):
    """2025 thresholds: $71,010 and $115,035; 3% then 7% (CFFP)."""

    def test_no_reduction_at_or_below_the_first_threshold(self):
        for income in (0.0, 40_000.0, 71_010.0):
            result = home_support_credit(2025, 19_500.0, income, situation=SINGLE)
            self.assertEqual(result.reduction, 0.0, income)
            self.assertAlmostEqual(result.credit, 7_605.0, places=6)

    def test_the_first_tier_is_three_percent_of_the_income_above_the_threshold(self):
        """$80,000 is $8,990 above $71,010, so the reduction is $269.70."""
        result = home_support_credit(2025, 19_500.0, 80_000.0, situation=SINGLE)
        self.assertAlmostEqual(result.reduction, 8_990.0 * 0.03, places=6)
        self.assertAlmostEqual(result.credit, 7_605.0 - 269.70, places=6)

    def test_the_second_tier_adds_seven_percent_above_the_second_threshold(self):
        """$120,000: 3% of (115,035 - 71,010) = $1,320.75, plus 7% of the
        $4,965 above $115,035 = $347.55."""
        result = home_support_credit(2025, 19_500.0, 120_000.0, situation=SINGLE)
        expected = (115_035.0 - 71_010.0) * 0.03 + (120_000.0 - 115_035.0) * 0.07
        self.assertAlmostEqual(result.reduction, expected, places=6)
        self.assertAlmostEqual(result.credit, 7_605.0 - expected, places=6)

    def test_the_2026_thresholds_are_the_published_ones(self):
        """$72,465 and $117,395, and NO reduction exactly at 72,465."""
        at_threshold = home_support_credit(2026, 19_500.0, 72_465.0, situation=SINGLE)
        self.assertEqual(at_threshold.reduction, 0.0)
        above = home_support_credit(2026, 19_500.0, 80_000.0, situation=SINGLE)
        self.assertAlmostEqual(above.reduction, (80_000.0 - 72_465.0) * 0.03,
                               places=6)

    def test_a_high_income_cannot_make_the_credit_negative(self):
        """The reduction can extinguish the credit, never reverse it: this is a
        payment, so a household is never shown as owing money for it."""
        result = home_support_credit(2025, 19_500.0, 400_000.0, situation=SINGLE)
        self.assertEqual(result.credit, 0.0)
        self.assertGreater(result.reduction, result.credit_before_reduction)

    def test_the_thresholds_belong_to_their_own_year(self):
        """2026's higher thresholds must not be applied to 2025: the same income
        gets a SMALLER reduction in 2026 than in 2025."""
        income = 80_000.0
        r2025 = home_support_credit(2025, 19_500.0, income, situation=SINGLE)
        r2026 = home_support_credit(2026, 19_500.0, income, situation=SINGLE)
        self.assertLess(r2026.reduction, r2025.reduction)


class TestTheRenterShare(unittest.TestCase):
    """5% of the monthly rent, rent floored at $600 and capped at $1,200."""

    def test_the_floor_is_a_floor_not_a_threshold(self):
        """A $540 rent still yields 5% of $600 — line 458: 'Si ce coût est
        inférieur à 600 $, inscrivez tout de même 600 $'."""
        self.assertAlmostEqual(
            eligible_service_expenses_from_rent(540.0), 600.0 * 0.05 * 12.0,
            places=6)

    def test_the_cap_is_a_cap(self):
        """A $2,000 rent yields 5% of $1,200: $60 a month, $720 a year."""
        self.assertAlmostEqual(
            eligible_service_expenses_from_rent(2_000.0), 720.0, places=6)

    def test_a_rent_inside_the_range_is_used_as_paid(self):
        self.assertAlmostEqual(
            eligible_service_expenses_from_rent(900.0), 900.0 * 0.05 * 12.0,
            places=6)

    def test_the_renter_path_prices_the_same_credit(self):
        result = renter_credit(2025, 900.0, 50_000.0, situation=SINGLE)
        self.assertAlmostEqual(result.eligible_expenses, 540.0, places=6)
        self.assertAlmostEqual(result.credit, 540.0 * 0.39, places=6)


class TestAbsenceRefusesRatherThanGuessing(unittest.TestCase):
    """DP#32: an input the module was never given must raise, not resolve."""

    def test_an_unpriced_year_refuses_and_names_the_years_that_are_priced(self):
        """2022-2024 rates are published (36/37/38%) but their indexed
        reduction thresholds are not on either source, so a 2024 credit would
        silently ride 2025's thresholds."""
        for year in (2022, 2023, 2024, 2027):
            with self.assertRaises(ValueError) as ctx:
                credit_rate(year)
            self.assertIn(str(year), str(ctx.exception))
            self.assertIn("2025", str(ctx.exception))

    def test_a_non_autonomous_claimant_refuses_rather_than_using_the_wrong_cap(self):
        """The two sources disagree on the reduction cap for this case (4% of
        expenses in the 2025 table, 3% in the 2026 preview) and Revenu Québec
        uses grid 458, so pricing it here would understate the credit."""
        with self.assertRaises(ValueError) as ctx:
            home_support_credit(2025, 25_500.0, 0.0,
                                situation="non_autonomous")
        self.assertIn("grille de calcul 458", str(ctx.exception))

    def test_an_unknown_situation_refuses(self):
        with self.assertRaises(ValueError) as ctx:
            home_support_credit(2025, 1_000.0, 0.0, situation="single")
        self.assertIn("Unknown home-support situation", str(ctx.exception))

    def test_the_situation_cannot_be_omitted(self):
        """No default, so a caller cannot silently receive the SINGLE cap and
        understate a couple's payment by half."""
        with self.assertRaises(TypeError):
            home_support_credit(2025, 19_500.0, 0.0)

    def test_negative_expenses_refuse(self):
        with self.assertRaises(ValueError):
            home_support_credit(2025, -1.0, 0.0, situation=SINGLE)

    def test_negative_income_refuses(self):
        with self.assertRaises(ValueError):
            home_support_credit(2025, 1_000.0, -1.0, situation=SINGLE)

    def test_negative_rent_refuses(self):
        with self.assertRaises(ValueError):
            eligible_service_expenses_from_rent(-1.0)

    def test_the_cap_helper_refuses_the_non_autonomous_situation_too(self):
        """The refusal is one fact used by every entry point, not a check in
        one of them: `autonomous_expense_cap` is public and is what a future
        caller would reach for first."""
        with self.assertRaises(ValueError) as ctx:
            autonomous_expense_cap(2025, "non_autonomous")
        self.assertIn("grille de calcul 458", str(ctx.exception))

    def test_the_reduction_refuses_a_year_with_no_registered_thresholds(self):
        """A guard against a DATA EDIT rather than against a caller: the rate
        table and the threshold table are separate, so adding a rate without its
        indexed thresholds (exactly the 2022-2024 situation) must refuse inside
        the reduction rather than fall through to another year's numbers."""
        from countries.canada.provinces.quebec.home_support import (
            _autonomous_reduction,
        )
        with self.assertRaises(ValueError) as ctx:
            _autonomous_reduction(2024, 100_000.0)
        self.assertIn("reduction thresholds", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
