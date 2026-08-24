#!/usr/bin/env python3
"""Issue #784: carry-forward of unused non-refundable tuition credits.

The federal + Quebec tuition credits (#764/#783) are NON-REFUNDABLE. In a
year where a student's credit exceeds their tax payable, the unused portion
CARRIES FORWARD to reduce tax in a future year (CRA and Revenu Québec both
allow indefinite carry-forward). Pre-#784 the fold floored each year's
credit at that year's tax and DISCARDED the unused remainder -- a silent
under-credit for a low-income student.

This tests (DP#4/DP#15: fabricated round numbers):
1. The pure carry-forward helper: credit > tax -> applied = tax, remainder
   carried; credit < tax -> applied = credit, no remainder.
2. End-to-end: a low-income student whose year-1 credit exceeds year-1 tax
   carries the remainder and applies it in year 2 (year-2 after-tax income
   is higher than the pre-#784 discard behaviour).
3. No tuition -> no spurious carry-forward (DP#32: absence inert).
"""

import unittest

import countries.canada  # noqa: F401
from rules_tuition_credit import _apply_tuition_credit_with_carryforward


class TestApplyTuitionCreditsWithCarryforward(unittest.TestCase):
    """The pure carry-forward helper (DP#3)."""

    def test_credit_exceeding_tax_carries_remainder(self):
        # $11,000 credit against $4,000 tax: $4,000 applied, $7,000 carried.
        applied, new_cf = _apply_tuition_credit_with_carryforward(
            11_000, 0.0, 4_000)
        self.assertAlmostEqual(applied, 4_000)
        self.assertAlmostEqual(new_cf, 7_000)

    def test_credit_below_tax_applies_all_no_remainder(self):
        # $1,000 credit against $5,000 tax: $1,000 applied, $0 carried.
        applied, new_cf = _apply_tuition_credit_with_carryforward(
            1_000, 0.0, 5_000)
        self.assertAlmostEqual(applied, 1_000)
        self.assertAlmostEqual(new_cf, 0.0)

    def test_carryforward_from_prior_year_is_consumed_before_new_credit(self):
        # $3,000 carried + $2,000 this year = $5,000 available against $5,000 tax.
        applied, new_cf = _apply_tuition_credit_with_carryforward(
            2_000, 3_000, 5_000)
        self.assertAlmostEqual(applied, 5_000)
        self.assertAlmostEqual(new_cf, 0.0)

    def test_zero_tax_carries_everything_forward(self):
        # No tax this year -> the full available credit carries forward.
        applied, new_cf = _apply_tuition_credit_with_carryforward(
            1_000, 500, 0.0)
        self.assertAlmostEqual(applied, 0.0)
        self.assertAlmostEqual(new_cf, 1_500)

    def test_spouse_independent(self):
        # Per-member (Canada has no joint filing): primary carries, spouse doesn't.
        # The helper is per-member, so the two members are applied independently.
        p_applied, p_cf = _apply_tuition_credit_with_carryforward(
            5_000, 0.0, 2_000)
        s_applied, s_cf = _apply_tuition_credit_with_carryforward(
            1_000, 0.0, 3_000)
        self.assertAlmostEqual(p_applied, 2_000)
        self.assertAlmostEqual(p_cf, 3_000)
        self.assertAlmostEqual(s_applied, 1_000)
        self.assertAlmostEqual(s_cf, 0.0)


class TestCarryforwardEndToEnd(unittest.TestCase):
    """A low-income student whose year-1 credit exceeds year-1 tax carries
    the remainder and applies it in year 2 (the #784 characterization)."""

    def _run(self, tuition_by_year, *, gross_income=15_000, years=3):
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        cfg = SimulationConfig(
            projection_years=years, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.0, living_costs=40_000, start_year=2026,
            province='quebec', investment_return=0.0, salary_growth=0.02,
            family_members=[{'role': 'primary', 'birth_year': 1985,
                             'gross_income': gross_income,
                             'rrsp_room_accumulated': 0,
                             'tfsa_room_accumulated': 0,
                             'tuition_by_year': tuition_by_year}])
        return FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()

    def test_year1_excess_carries_and_applies_in_year2(self):
        # $50,000 tuition at QC (fed 14% + QC 8% = 22%) = $11,000 credit.
        # Year-1 tax on $15,000 is ~$3,853, so ~$7,147 carries forward.
        r = self._run({2026: 50_000})
        # Year 0: the credit exceeds tax -> after_tax = gross (tax eliminated),
        # and the remainder is carried (positive carry-forward on the result).
        self.assertAlmostEqual(r[0].after_tax_income, r[0].employment_income,
                               places=2,
                               msg="year-1 tax is eliminated by the credit")
        self.assertGreater(r[0].primary_tuition_carryforward, 5_000,
                          "the year-1 unused credit must carry forward")
        # Year 1 (no new tuition): the carry-forward eliminates (or nearly
        # eliminates) year-2 tax, so after_tax is close to gross income --
        # WITHOUT the fix, year-2 tax would NOT be eliminated (the excess was
        # discarded). Pre-#784 year-2 after_tax was ~11,369; with carry-forward
        # it is close to the gross income (~15,300).
        self.assertGreater(r[1].after_tax_income, 14_000,
                           msg="the carried-forward credit must reduce year-2 "
                               "tax significantly (pre-#784 it was discarded)")
        # The carry-forward depletes over time (finite credit, finite tax).
        self.assertLess(r[2].primary_tuition_carryforward,
                        r[1].primary_tuition_carryforward,
                        "the carry-forward is consumed over time, not growing")

    def test_no_tuition_no_carryforward(self):
        # DP#32: no tuition declared -> no credit, no carry-forward, no effect.
        r = self._run({})
        for yr in r:
            self.assertEqual(yr.primary_tuition_carryforward, 0.0)


if __name__ == '__main__':
    unittest.main()