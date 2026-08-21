#!/usr/bin/env python3
"""Issue #909 stage 1: the Money value type and its arithmetic.

Money exists to make illegal currency arithmetic UNREPRESENTABLE. These tests
lock both halves of that contract: the legal operations produce exact cent
results, and every forbidden one (cross-currency combine, Money×Money, mixing a
bare number in) raises rather than silently doing the wrong thing.

Round numbers per DP#13/DP#15; exact cents throughout (the whole point).
"""

import unittest

from money import Money, DEFAULT_CURRENCY


class TestConstruction(unittest.TestCase):
    def test_from_dollars_rounds_half_up(self):
        self.assertEqual(Money.from_dollars(1.005).cents, 101)   # tie -> away from zero
        self.assertEqual(Money.from_dollars(1.004).cents, 100)
        self.assertEqual(Money.from_dollars(2.675).cents, 268)   # not the 2.67 float trap
        self.assertEqual(Money.from_dollars(-1.005).cents, -101)  # ties away from zero both ways
        self.assertEqual(Money.from_dollars(100).cents, 10000)   # an int number of dollars

    def test_from_cents_and_zero(self):
        self.assertEqual(Money.from_cents(2500).cents, 2500)
        self.assertEqual(Money.zero().cents, 0)
        self.assertEqual(Money.zero("GBP").currency, "GBP")

    def test_default_currency(self):
        self.assertEqual(Money.from_cents(1).currency, DEFAULT_CURRENCY)
        self.assertEqual(DEFAULT_CURRENCY, "CAD")

    def test_from_dollars_rejects_non_number(self):
        for bad in ("1.00", None, True):
            with self.assertRaises(TypeError):
                Money.from_dollars(bad)

    def test_cents_must_be_exact_int(self):
        with self.assertRaises(TypeError):
            Money(cents=100.5)          # a fractional cent is a caller bug
        with self.assertRaises(TypeError):
            Money(cents=True)           # bool is not an amount
        with self.assertRaises(ValueError):
            Money(cents=100, currency="")


class TestViews(unittest.TestCase):
    def test_dollars_is_lossy_view(self):
        self.assertEqual(Money.from_cents(12345).dollars, 123.45)
        self.assertEqual(Money.from_cents(-100).dollars, -1.0)


class TestAddSub(unittest.TestCase):
    def test_add_and_sub_same_currency(self):
        self.assertEqual(Money.from_cents(300) + Money.from_cents(200), Money.from_cents(500))
        self.assertEqual(Money.from_cents(300) - Money.from_cents(200), Money.from_cents(100))

    def test_cross_currency_add_sub_raises(self):
        with self.assertRaises(TypeError):
            Money.from_cents(1, "CAD") + Money.from_cents(1, "GBP")
        with self.assertRaises(TypeError):
            Money.from_cents(1, "CAD") - Money.from_cents(1, "GBP")

    def test_add_sub_bare_number_raises(self):
        with self.assertRaises(TypeError):
            Money.from_cents(100) + 100
        with self.assertRaises(TypeError):
            Money.from_cents(100) - 1.0


class TestMul(unittest.TestCase):
    def test_scalar_multiply_rounds_half_up(self):
        self.assertEqual((Money.from_cents(1000) * 0.1).cents, 100)
        self.assertEqual((Money.from_cents(1001) * 0.5).cents, 501)   # 500.5 -> 501
        self.assertEqual((3 * Money.from_cents(250)).cents, 750)      # rmul

    def test_money_times_money_raises(self):
        with self.assertRaises(TypeError):
            Money.from_cents(100) * Money.from_cents(100)

    def test_multiply_by_bool_or_nonnumber_raises(self):
        with self.assertRaises(TypeError):
            Money.from_cents(100) * True
        with self.assertRaises(TypeError):
            Money.from_cents(100) * "2"


class TestDiv(unittest.TestCase):
    def test_money_over_money_is_dimensionless_ratio(self):
        ratio = Money.from_cents(50000) / Money.from_cents(100000)
        self.assertIsInstance(ratio, float)
        self.assertEqual(ratio, 0.5)

    def test_money_over_scalar_is_money(self):
        self.assertEqual((Money.from_cents(1000) / 4), Money.from_cents(250))
        self.assertEqual((Money.from_cents(1000) / 3).cents, 333)  # rounded

    def test_cross_currency_ratio_raises(self):
        with self.assertRaises(TypeError):
            Money.from_cents(1, "CAD") / Money.from_cents(1, "GBP")

    def test_divide_by_zero_raises(self):
        with self.assertRaises(ZeroDivisionError):
            Money.from_cents(100) / Money.zero()
        with self.assertRaises(ZeroDivisionError):
            Money.from_cents(100) / 0

    def test_divide_by_bool_raises(self):
        with self.assertRaises(TypeError):
            Money.from_cents(100) / True


class TestUnaryAndBool(unittest.TestCase):
    def test_neg_and_abs(self):
        self.assertEqual(-Money.from_cents(100), Money.from_cents(-100))
        self.assertEqual(abs(Money.from_cents(-100)), Money.from_cents(100))

    def test_truthiness(self):
        self.assertFalse(Money.zero())
        self.assertTrue(Money.from_cents(1))
        self.assertTrue(Money.from_cents(-1))


class TestComparison(unittest.TestCase):
    def test_ordering_same_currency(self):
        self.assertLess(Money.from_cents(100), Money.from_cents(200))
        self.assertLessEqual(Money.from_cents(100), Money.from_cents(100))
        self.assertGreater(Money.from_cents(200), Money.from_cents(100))
        self.assertGreaterEqual(Money.from_cents(100), Money.from_cents(100))

    def test_cross_currency_compare_raises(self):
        for op in (lambda a, b: a < b, lambda a, b: a <= b,
                   lambda a, b: a > b, lambda a, b: a >= b):
            with self.assertRaises(TypeError):
                op(Money.from_cents(1, "CAD"), Money.from_cents(1, "GBP"))

    def test_compare_with_bare_number_raises(self):
        with self.assertRaises(TypeError):
            Money.from_cents(100) < 100


class TestEqualityAndHash(unittest.TestCase):
    def test_equality_uses_cents_and_currency(self):
        self.assertEqual(Money.from_cents(100), Money.from_cents(100))
        self.assertNotEqual(Money.from_cents(100, "CAD"), Money.from_cents(100, "GBP"))

    def test_not_equal_to_bare_number(self):
        self.assertNotEqual(Money.from_cents(100), 1.0)
        self.assertNotEqual(Money.from_cents(100), 100)

    def test_hashable(self):
        s = {Money.from_cents(100), Money.from_cents(100), Money.from_cents(100, "GBP")}
        self.assertEqual(len(s), 2)

    def test_frozen_is_immutable(self):
        m = Money.from_cents(100)
        with self.assertRaises(Exception):
            m.cents = 200  # frozen dataclass


class TestFormatting(unittest.TestCase):
    def test_str_formats_with_separator_and_cents(self):
        self.assertEqual(str(Money.from_cents(123456789)), "$1,234,567.89 CAD")
        self.assertEqual(str(Money.from_cents(-100)), "-$1.00 CAD")
        self.assertEqual(str(Money.from_cents(5, "GBP")), "$0.05 GBP")

    def test_repr_roundtrips_the_fields(self):
        self.assertEqual(repr(Money.from_cents(100, "CAD")),
                         "Money(cents=100, currency='CAD')")


if __name__ == '__main__':
    unittest.main()
