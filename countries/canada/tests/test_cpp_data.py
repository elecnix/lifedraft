"""Unit tests for countries/canada/cpp_data.py — the one CPP/QPP ceiling lookup.

Issue #412. The module this replaces spread the ceilings over five tables and
four "no row" rules, so these tests are as much about *which* row answers a
year as about the arithmetic. The rules they pin:

* a year with a row answers with that row;
* a year without one answers with the nearest year that has one (ties to the
  later year) — never an invented growth projection, never zero;
* a year before the first row answers with the first row;
* a quantity with no row at all, and an unknown plan, raise.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from countries.canada import cpp_data
from countries.canada.cpp_data import (
    CPP2_START_YEAR,
    cpp_parameters,
    max_benefit_65_for_year,
    max_cpp2_benefit_for_year,
    qpp_max_benefit_65_years,
    register_qpp_max_benefit_65,
    yampe_for_year,
    ympe_for_year,
)


class TestYMPE(unittest.TestCase):
    """YMPE for any year in the contributory period, 1966 onward."""

    def test_historical_row_answers_for_its_own_year(self):
        self.assertEqual(ympe_for_year(1975), 7400)
        self.assertEqual(ympe_for_year(2019), 57400)
        self.assertEqual(ympe_for_year(2022), 64900)

    def test_federal_row_answers_for_its_own_year(self):
        self.assertEqual(ympe_for_year(2023), 66600)
        self.assertEqual(ympe_for_year(2024), 68500)
        self.assertEqual(ympe_for_year(2025), 71300)
        self.assertEqual(ympe_for_year(2026), 74600)

    def test_year_after_the_last_row_carries_the_last_row_forward(self):
        """No published maximum → the last published one, not a projection.

        The rule this replaces projected YMPE 2%/yr off the 2022 row, which
        put 2027 *below* the published 2026 maximum.
        """
        self.assertEqual(ympe_for_year(2027), 74600)
        self.assertEqual(ympe_for_year(2060), 74600)

    def test_year_before_the_first_row_carries_the_first_row_forward(self):
        self.assertEqual(ympe_for_year(1965), 5000)
        self.assertEqual(ympe_for_year(1900), 5000)

    def test_ympe_is_never_zero_and_never_decreasing(self):
        previous = 0.0
        for year in range(1966, 2076):
            current = ympe_for_year(year)
            self.assertGreater(current, 0)
            self.assertGreaterEqual(current, previous)
            previous = current


class TestYAMPE(unittest.TestCase):
    def test_no_second_ceiling_before_cpp2_began(self):
        for year in range(1966, CPP2_START_YEAR):
            self.assertEqual(yampe_for_year(year), ympe_for_year(year))

    def test_second_ceiling_from_2024(self):
        self.assertEqual(yampe_for_year(2024), 73300)
        self.assertEqual(yampe_for_year(2026), 81900)

    def test_second_ceiling_carries_forward_past_the_last_row(self):
        self.assertEqual(yampe_for_year(2030), 81900)

    def test_second_ceiling_exceeds_the_first(self):
        self.assertGreater(yampe_for_year(2026), ympe_for_year(2026))


class TestMaxBenefit65(unittest.TestCase):
    def test_cpp_plan_uses_the_federal_maximum(self):
        self.assertEqual(max_benefit_65_for_year(2026, "cpp"), 18092)
        self.assertEqual(max_benefit_65_for_year(2024, "cpp"), 14448)

    def test_qpp_plan_uses_the_quebec_owned_maximum(self):
        self.assertEqual(max_benefit_65_for_year(2023, "qpp"), 15170)
        self.assertEqual(max_benefit_65_for_year(2026, "qpp"), 17334)

    def test_qpp_year_before_the_qpp_table_falls_through_to_cpp(self):
        """The QPP maxima start in 2023; inventing one for 1990 is fabrication."""
        self.assertEqual(max_benefit_65_for_year(2020, "qpp"),
                         max_benefit_65_for_year(2020, "cpp"))

    def test_qpp_year_after_the_table_carries_the_last_qpp_row(self):
        self.assertEqual(max_benefit_65_for_year(2035, "qpp"), 17334)


class TestMaxCPP2Benefit(unittest.TestCase):
    def test_zero_before_cpp2_began(self):
        self.assertEqual(max_cpp2_benefit_for_year(2023), 0.0)

    def test_row_from_2024(self):
        self.assertEqual(max_cpp2_benefit_for_year(2026), 800)

    def test_carries_forward_past_the_last_row(self):
        self.assertEqual(max_cpp2_benefit_for_year(2040), 800)


class TestLoudFailures(unittest.TestCase):
    """DP#32: absence fails loudly. None of these answer a number."""

    def test_unknown_plan_raises(self):
        with self.assertRaises(ValueError) as ctx:
            cpp_parameters(2026, plan="rrq")
        self.assertIn("rrq", str(ctx.exception))

    def test_unregistered_qpp_maxima_raise(self):
        saved = dict(cpp_data._QPP_MAX_BENEFIT_65_BY_YEAR)
        cpp_data._QPP_MAX_BENEFIT_65_BY_YEAR.clear()
        try:
            with self.assertRaises(ValueError) as ctx:
                max_benefit_65_for_year(2026, "qpp")
            self.assertIn("QPP", str(ctx.exception))
        finally:
            cpp_data._QPP_MAX_BENEFIT_65_BY_YEAR.update(saved)

    def test_a_ceiling_with_no_row_in_any_year_raises(self):
        """Empty the federal table and the 2023+ ceilings have no row left."""
        saved = cpp_data.CPP_OAS_BY_YEAR
        cpp_data.CPP_OAS_BY_YEAR = {}
        try:
            with self.assertRaises(ValueError) as ctx:
                yampe_for_year(2026)
            self.assertIn("YAMPE", str(ctx.exception))
            # YMPE still answers: its historical rows live in this module.
            self.assertEqual(ympe_for_year(2026), 64900)
        finally:
            cpp_data.CPP_OAS_BY_YEAR = saved

    def test_registering_a_zero_qpp_maximum_raises(self):
        class Record:
            year = 2031
            qpp_max_benefit_65 = 0.0

        with self.assertRaises(ValueError) as ctx:
            register_qpp_max_benefit_65([Record()])
        self.assertIn("2031", str(ctx.exception))
        self.assertNotIn(2031, qpp_max_benefit_65_years())


class TestRegistration(unittest.TestCase):
    def test_quebec_records_are_the_registered_source(self):
        """The registered years and values are Quebec's, not a federal copy."""
        from countries.canada.provinces.quebec.tax_data import QuebecTaxData
        registered = dict(zip(qpp_max_benefit_65_years(),
                              [cpp_data._QPP_MAX_BENEFIT_65_BY_YEAR[y]
                               for y in qpp_max_benefit_65_years()]))
        for record in QuebecTaxData.all_years():
            self.assertEqual(registered[record.year], record.qpp_max_benefit_65)


class TestCPPParameters(unittest.TestCase):
    def test_shape_and_purity(self):
        first = cpp_parameters(2026, "qpp")
        second = cpp_parameters(2026, "qpp")
        self.assertEqual(first, second)
        self.assertEqual((first.year, first.plan), (2026, "qpp"))
        self.assertGreater(first.ympe, 0)
        self.assertGreater(first.max_benefit_65, 0)

    def test_plan_selects_only_the_age_65_maximum(self):
        """CPP and QPP share the contributory ceilings (same framework)."""
        cpp = cpp_parameters(2026, "cpp")
        qpp = cpp_parameters(2026, "qpp")
        self.assertEqual(cpp.ympe, qpp.ympe)
        self.assertEqual(cpp.yampe, qpp.yampe)
        self.assertEqual(cpp.max_cpp2_benefit, qpp.max_cpp2_benefit)
        self.assertNotEqual(cpp.max_benefit_65, qpp.max_benefit_65)

    def test_defaults_to_the_cpp_plan(self):
        self.assertEqual(cpp_parameters(2026), cpp_parameters(2026, "cpp"))


if __name__ == "__main__":
    unittest.main()