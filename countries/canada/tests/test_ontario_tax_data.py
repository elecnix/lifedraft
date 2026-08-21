#!/usr/bin/env python3
"""Unit tests for the Ontario tax-data module (issue #324).

DP#11: this file tests ONE module — ``countries.canada.provinces.ontario``
(OntarioTaxData) — in isolation: its brackets, no-abatement rule, DTC rates,
and year-versioned parameters. The Ontario *credit* functions (surtax, health
premium, sales tax credit / Trillium, LIFT) live in a separate module
(``ontario_credits``) and are tested in ``test_ontario_credits.py``.
``TestOntarioRegistration`` is the one composition assertion: it verifies
``tax_data.TaxDataProvider`` resolves Ontario data (DP#16 auto-inclusion /
DP#11 integration).

Per DP#17: tests exercise every rule path, not just every module.

Run with: python3 -m pytest countries/canada/tests/test_ontario_tax_data.py -v
"""

import os
import sys

sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    ),
)

import unittest

from tax_data import TaxDataProvider
from countries.canada.provinces.ontario import OntarioTaxData


def _provider() -> TaxDataProvider:
    return TaxDataProvider()


# =============================================================================
# Brackets, abatement, DTC, and year-versioning (the previously-untested core)
# =============================================================================

class TestOntarioBrackets(unittest.TestCase):
    """Ontario provincial brackets (5 rates) — DP#17."""

    def setUp(self):
        self.d2026 = OntarioTaxData.year_2026()
        self.d2025 = OntarioTaxData.year_2025()

    def test_five_brackets(self):
        self.assertEqual(len(self.d2026.provincial_brackets), 5)
        self.assertEqual(len(self.d2025.provincial_brackets), 5)

    def test_bracket_rates_in_order(self):
        rates = [b.rate for b in self.d2026.provincial_brackets]
        self.assertEqual(rates, [0.0505, 0.0915, 0.1116, 0.1216, 0.1316])

    def test_brackets_are_contiguous(self):
        for data in (self.d2026, self.d2025):
            br = data.provincial_brackets
            for i in range(len(br) - 1):
                self.assertEqual(br[i].max_income, br[i + 1].min_income)
            self.assertEqual(br[-1].max_income, 0)  # top bracket unlimited

    def test_first_bracket_threshold_changes_by_year(self):
        # 2026 indexed up from 2025 (DP#20 year-versioning)
        self.assertEqual(self.d2026.provincial_brackets[0].max_income, 51446)
        self.assertEqual(self.d2025.provincial_brackets[0].max_income, 51346)
        self.assertGreater(
            self.d2026.provincial_brackets[0].max_income,
            self.d2025.provincial_brackets[0].max_income,
        )

    def test_top_two_thresholds_are_unindexed(self):
        # Ontario's $150k / $220k surtax-tier thresholds are not indexed.
        for data in (self.d2026, self.d2025):
            self.assertEqual(data.provincial_brackets[2].max_income, 150000)
            self.assertEqual(data.provincial_brackets[3].max_income, 220000)


class TestOntarioAbatement(unittest.TestCase):
    """Ontario has no federal abatement (only Quebec does) — DP#17."""

    def test_abatement_is_zero(self):
        self.assertEqual(OntarioTaxData.ABATEMENT, 0.0)
        self.assertEqual(OntarioTaxData.year_2026().provincial_abatement, 0.0)
        self.assertEqual(OntarioTaxData.year_2025().provincial_abatement, 0.0)

    def test_no_qpp(self):
        # Ontario uses CPP, not QPP.
        self.assertEqual(OntarioTaxData.year_2026().qpp_rate, 0.0)


class TestOntarioDTC(unittest.TestCase):
    """Provincial dividend tax credit rates — DP#17."""

    def test_eligible_dtc_rate(self):
        self.assertAlmostEqual(
            OntarioTaxData.year_2026().provincial_eligible_dtc_rate, 0.1008)

    def test_non_eligible_dtc_rate(self):
        self.assertAlmostEqual(
            OntarioTaxData.year_2026().provincial_non_eligible_dtc_rate, 0.0455)


class TestOntarioRegistration(unittest.TestCase):
    """Ontario data is discoverable through TaxDataProvider — DP#16."""

    def test_provider_resolves_ontario(self):
        p = _provider()
        d = p.get_year_data(2026, 'canada', 'ontario')
        self.assertEqual(d.province, 'ontario')
        self.assertEqual(len(d.provincial_brackets), 5)

    def test_postal_alias_resolves(self):
        p = _provider()
        d = p.get_year_data(2026, 'canada', 'on')
        self.assertEqual(len(d.provincial_brackets), 5)

    def test_combined_brackets_have_no_abatement_reduction(self):
        # With abatement=0, the federal portion of combined ON brackets is the
        # full federal rate (× 1.000), unlike Quebec (× 0.835).
        p = _provider()
        brackets = p.get_brackets(2026, province='ontario', combined=True)
        self.assertTrue(brackets)
        # Lowest combined rate = federal lowest + Ontario 5.05%, with NO
        # abatement reduction applied to the federal portion (Ontario, not QC).
        p2 = _provider()
        fed = p2.get_year_data(2026, 'canada', 'federal').federal_brackets
        expected = round(fed[0].rate, 4) + 0.0505
        self.assertAlmostEqual(brackets[0].rate, expected, places=4)


if __name__ == '__main__':
    unittest.main()
