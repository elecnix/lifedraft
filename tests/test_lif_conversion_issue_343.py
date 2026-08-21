#!/usr/bin/env python3
"""Issue #343: LIRA→LIF conversion must fire on the live FamilySimulation path.

Root cause regression: the conversion gate in simulate_year_pure compared the
0-based projection index `year` against a calendar conversion year
(birth_year + 71), so on FamilySimulation.run() the gate `year >= 2050` was
effectively `0..N >= 2050` and never fired. The fix threads the absolute
calendar year (calendar_year=start_year+year) into the gate.

These tests exercise the END-TO-END FamilySimulation.run() path — the layer the
bug actually lived in — not just the pure helpers (which already passed because
their unit tests pass a calendar year directly as `year`).

Run: uv run pytest tests/test_lif_conversion_issue_343.py -v
"""

import unittest

from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.locked_in_account import LIFFund


def _make_config(projection_years):
    """Quebec household, primary born 1979 (turns 71 in 2050), CRI/LIRA present."""
    return SimulationConfig(
        projection_years=projection_years,
        investment_return=0.07,
        house_value=600000,
        mortgage_balance=300000,
        mortgage_rate=0.05,
        margin_available=100000,
        start_year=2026,
        family_members=[
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1979,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 70000,
             'retirement_age': 65},
            {'role': 'spouse', 'gross_income': 90000, 'birth_year': 1981,
             'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 60000,
             'retirement_age': 65},
        ],
        children=[],
        lira_data={'balance': 100000, 'birth_year': 1979,
                   'jurisdiction': 'quebec', 'reference_rate': 0.06},
    )


class TestLongHorizonConversionFires(unittest.TestCase):
    """≥26-yr horizon: conversion fires at age 71 on the live run path."""

    @classmethod
    def setUpClass(cls):
        # 28-yr horizon: 2026..2053, crosses the age-71 boundary (2050).
        cls.results = FamilySimulation(_make_config(28)).run()
        # YearResult.year is 1-based; calendar = start_year + year - 1.
        cls.by_cal = {2026 + r.year - 1: r for r in cls.results}

    def test_lira_zero_and_lif_positive_at_age_71(self):
        """At 2050 (age 71): lira_balance → 0 and lif_balance > 0 (relational)."""
        conv = self.by_cal[2050]
        prior = self.by_cal[2049]
        # Relational: LIRA was growing the year before, then collapses to 0.
        self.assertGreater(prior.lira_balance, 0)
        self.assertEqual(conv.lira_balance, 0)
        self.assertGreater(conv.lif_balance, 0)
        # The converted LIF balance equals the pre-conversion LIRA balance.
        self.assertAlmostEqual(conv.lif_balance, prior.lira_balance, delta=1.0)

    def test_lira_stays_zero_after_conversion(self):
        """Once converted, LIRA does not reappear."""
        for cal in range(2050, 2054):
            self.assertEqual(self.by_cal[cal].lira_balance, 0,
                             f"lira_balance should stay 0 in {cal}")

    def test_lif_withdrawal_positive_after_conversion(self):
        """Years after conversion have mandatory LIF withdrawals > 0."""
        for cal in range(2051, 2054):
            self.assertGreater(self.by_cal[cal].lif_withdrawal, 0,
                               f"lif_withdrawal should be > 0 in {cal}")

    def test_lif_withdrawal_within_qc_min_max(self):
        """Each post-conversion withdrawal respects the QC LIF min/max bounds."""
        for cal in range(2051, 2054):
            prior = self.by_cal[cal - 1]
            r = self.by_cal[cal]
            # The withdrawal is computed against the start-of-year LIF balance.
            fund = LIFFund(balance=prior.lif_balance, owner_birth_year=1979,
                           reference_rate=0.06, jurisdiction='quebec')
            min_w = fund.minimum_withdrawal(cal)
            max_w = fund.maximum_withdrawal(cal)
            self.assertGreaterEqual(r.lif_withdrawal, min_w - 1.0,
                                    f"{cal}: below QC minimum")
            self.assertLessEqual(r.lif_withdrawal, max_w + 1.0,
                                 f"{cal}: above QC maximum")

    def test_lif_withdrawal_flows_into_retirement_income(self):
        """LIF withdrawal is part of taxable retirement income (#302 wiring)."""
        r = self.by_cal[2051]
        self.assertGreater(r.lif_withdrawal, 0)
        self.assertGreaterEqual(r.retirement_income, r.lif_withdrawal)


class TestShortHorizonUnchanged(unittest.TestCase):
    """Short horizon that never reaches age 71: no LIF, LIRA keeps growing."""

    def test_no_conversion_before_age_71(self):
        results = FamilySimulation(_make_config(10)).run()  # 2026..2035
        for r in results:
            self.assertEqual(r.lif_balance, 0.0,
                             "LIF must not exist before age 71")
            self.assertEqual(r.lif_withdrawal, 0.0,
                             "No LIF withdrawal before age 71")
        # LIRA still present and growing (never converted).
        self.assertGreater(results[-1].lira_balance, 0)


if __name__ == '__main__':
    unittest.main()
