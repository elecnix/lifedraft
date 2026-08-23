#!/usr/bin/env python3
"""Issue #882: intra-year expense SEASONALITY (follow-up to #760).

#760 gave ``household_budget.expense_segments[]`` a finite-term window
``[from, to)`` and a day-count blend that answers "what fraction of the
segment's ANNUAL amount is charged in calendar year Y". But within an active
year that blend spreads the amount evenly over every day -- it cannot express
a bill that recurs only M months a year (heating that runs Nov-Mar), or a
one-shot charge dated to a specific month (a property-tax bill paid every
July). The current solvency/reserve resolution is annual, so a bill whose
whole year's cost lands in one month is sized as if it dribbled out at
1/12 a month -- understating the reserve the household must hold to bridge the
peak.

This issue adds an OPTIONAL ``active_months`` list (calendar month numbers
1-12) to an expense_segment. The segment's ``amount`` (still the ANNUAL
recurring figure) is spent in equal shares across ONLY the listed months, and
solvency/reserve sizing resolves the within-year pattern at MONTHLY
granularity: the charged outflow counts only the active months touched by the
``[from, to)`` window, and the emergency reserve is sized on the PEAK month,
not the annual average.

## What this tests

1. **The seasonal within-year blend (pure).** ``amount`` is split over the
   active months; a full-year window charges the full annual amount (ties out
   with a plain #760 segment); active months before ``from`` or on/after ``to``
   drop out; a month partially inside the window prorates by its days.
2. **Solvency wiring, end-to-end.** A seasonal non-discretionary segment is
   charged its active-month amount on the identity's spending outflow, and the
   emergency reserve is sized on the PEAK month -- a concentrated seasonal bill
   demands a strictly LARGER reserve than the same annual dollars spread evenly.
3. **Absence is a no-op (DP#32).** A #760 segment that omits ``active_months``
   reproduces the day-count blend BYTE-for-byte (same outflow, same reserve
   target); the golden invariant does not move.
4. **Contract boundary (DP#32).** A valid ``active_months`` maps through; an
   empty list or a duplicated month is refused loudly (never silently coerced).
5. **Round-trip (DP#24).** ``active_months`` survives to_dict/from_dict.
"""

import json
import logging
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
import input_contract as ic
from simulation_config import SimulationConfig
from rules_solvency import _expense_segment_contribution_in_year
from test_dp_income_scenario_reaches_engine import _two_generation_subset

# Reuse the #760 one-year pure-step harness and its RUIN_* fixtures verbatim --
# the seasonality axis is layered on the SAME expense_segment plumbing.
from test_issue_760_finite_term_living_costs import _one_year, _seg
from test_issue_679_solvency import RUIN_LIVING_COSTS


def _sseg(*, amount, frm="2020-01-01", to=None, active_months,
          non_discretionary=True, description="seasonal cost"):
    return {"description": description, "amount": amount, "from": frm, "to": to,
            "non_discretionary": non_discretionary, "active_months": active_months}


# ============================================================================
# 1. The seasonal within-year blend (pure function).
# ============================================================================

class TestSeasonalBlend(unittest.TestCase):

    def test_one_shot_month_charges_full_amount_in_its_year(self):
        """active_months=[7], perpetual window -> the whole annual amount lands
        in the year (July is inside the window), exactly like a property-tax
        bill paid once each July."""
        seg = _sseg(amount=8_000, active_months=[7])
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026), 8_000)

    def test_winter_months_full_year_ties_out_to_annual_amount(self):
        """A five-month winter bill over a full-year window still charges the
        full ANNUAL amount -- seasonality redistributes WITHIN the year, it does
        not change the annual total (ties out with a plain #760 segment)."""
        seg = _sseg(amount=6_000, active_months=[1, 2, 3, 11, 12])
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026), 6_000)
        plain = _seg(amount=6_000, frm="2020-01-01", to=None)
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026),
            _expense_segment_contribution_in_year(plain, 2026))

    def test_active_month_before_start_drops_out(self):
        """active_months=[7] with a window that starts in August -> July is
        before `from`, so nothing is charged this year (monthly resolution: the
        active month must fall inside the window)."""
        seg = _sseg(amount=8_000, frm="2026-08-01", active_months=[7])
        self.assertEqual(_expense_segment_contribution_in_year(seg, 2026), 0.0)
        # The next year the July bill lands in full.
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2027), 8_000)

    def test_active_month_on_or_after_end_drops_out(self):
        """A window ending 2026-03-01 excludes a July active month -> $0 that
        year (the expense ENDS before its active month arrives)."""
        seg = _sseg(amount=8_000, frm="2020-01-01", to="2026-03-01",
                    active_months=[7])
        self.assertEqual(_expense_segment_contribution_in_year(seg, 2026), 0.0)

    def test_partial_active_month_prorates_by_days(self):
        """A window starting mid-July charges only the days of July inside the
        window (monthly resolution still day-exact for the boundary month)."""
        seg = _sseg(amount=8_000, frm="2026-07-15", active_months=[7])
        july_days = (date(2026, 8, 1) - date(2026, 7, 15)).days
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026),
            8_000 * july_days / 31)

    def test_two_active_months_split_the_amount_evenly(self):
        """amount is split over the active months: [6, 12] with only June
        inside a window ending Aug 1 charges exactly half."""
        seg = _sseg(amount=8_000, frm="2020-01-01", to="2026-08-01",
                    active_months=[6, 12])
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026), 4_000)


# ============================================================================
# 2. Solvency + reserve wiring (monthly-granularity reserve sizing).
# ============================================================================

class TestSeasonalSolvencyWiring(unittest.TestCase):

    def test_seasonal_segment_charged_on_spending_outflow(self):
        r = _one_year(expense_segments=[_sseg(amount=8_000, active_months=[7])])
        self.assertAlmostEqual(r.expense_segment_outflow, 8_000)

    def test_reserve_sized_on_peak_month_not_annual_average(self):
        """The crux of #882: a bill whose whole year lands in ONE month needs a
        bigger reserve than the same annual dollars dribbled evenly over 12.
        Same annual amount, same window, same non_discretionary flag -- only the
        seasonality differs, and the concentrated one demands more reserve."""
        even = _one_year(expense_segments=[
            _seg(amount=12_000, frm="2020-01-01", to=None)])
        concentrated = _one_year(expense_segments=[
            _sseg(amount=12_000, active_months=[1])])
        self.assertAlmostEqual(concentrated.expense_segment_outflow,
                               even.expense_segment_outflow)
        self.assertGreater(concentrated.emergency_reserve_target,
                           even.emergency_reserve_target)


# ============================================================================
# 3. Absence is a no-op (DP#32).
# ============================================================================

class TestAbsenceIsNoOp(unittest.TestCase):

    def test_undeclared_active_months_is_byte_identical(self):
        """A #760 segment with no active_months key charges and sizes exactly as
        before -- the seasonality branch is never entered (DP#32 no-op)."""
        plain = _seg(amount=14_000, frm="2020-01-01", to=None)
        # Pure helper: unchanged day-count value.
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(plain, 2026), 14_000)
        # End-to-end: outflow and reserve target identical to the base #760 run.
        withseg = _one_year(expense_segments=[plain])
        base = _one_year(expense_segments=[])
        self.assertAlmostEqual(withseg.expense_segment_outflow, 14_000)
        # The reserve target for a plain (non-seasonal) segment is the evenly
        # spread #760 sizing -- the seasonal peak premium is exactly 0.
        expected_reserve = base.emergency_reserve_target + 6 * (14_000 / 12.0)
        self.assertAlmostEqual(withseg.emergency_reserve_target,
                               expected_reserve)

    def test_golden_terminal_assets_unchanged(self):
        from test_golden_trajectory_581 import _run, golden_household_config
        logging.disable(logging.WARNING)
        try:
            terminal = _run(golden_household_config())[-1].total_assets
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(terminal, 9709753.139463063)


# ============================================================================
# 4. Contract boundary (DP#32).
# ============================================================================

def _example_doc():
    with open(ic.EXAMPLE_PATH) as fh:
        return _two_generation_subset(json.load(fh))


def _doc_with_segments(segments, *, living_costs=78_000):
    doc = _example_doc()
    doc["household_budget"] = {"annual_living_costs": living_costs,
                               "expense_segments": segments}
    return doc


class TestContractBoundary(unittest.TestCase):

    def _map(self, doc):
        logging.disable(logging.WARNING)
        try:
            return ic.to_internal_config(doc)
        finally:
            logging.disable(logging.NOTSET)

    def test_valid_active_months_map_into_the_internal_shape(self):
        doc = _doc_with_segments([
            _sseg(amount=8_000, frm="2026-01-01", to="2032-01-01",
                  active_months=[1, 2, 3, 11, 12], description="heating")])
        cfg = self._map(doc)
        seg = cfg["household_budget"]["expense_segments"][0]
        self.assertEqual(seg["active_months"], [1, 2, 3, 11, 12])

    def test_empty_active_months_is_refused(self):
        """An empty active_months is an empty within-year window -- refused by
        the schema (minItems), never silently 'active every month' or $0
        (DP#32)."""
        doc = _doc_with_segments([
            _sseg(amount=8_000, frm="2026-01-01", to=None, active_months=[])])
        with self.assertRaises(ic.ContractValidationError):
            self._map(doc)

    def test_duplicate_active_month_is_refused(self):
        """A repeated month is ambiguous (does it charge twice?) -- refused by
        the schema (uniqueItems), not silently de-duplicated (DP#32)."""
        doc = _doc_with_segments([
            _sseg(amount=8_000, frm="2026-01-01", to=None,
                  active_months=[7, 7])])
        with self.assertRaises(ic.ContractValidationError):
            self._map(doc)


# ============================================================================
# 5. Round-trip (DP#24).
# ============================================================================

class TestRoundTrip(unittest.TestCase):

    def test_active_months_survive_to_dict_from_dict(self):
        cfg = SimulationConfig(
            projection_years=6, investment_return=0.05,
            mortgage_balance=300_000, mortgage_rate=0.05, margin_available=0,
            family_members=[{'role': 'primary', 'gross_income': 95_000,
                             'birth_year': 1985}],
            children=[], living_costs=78_000,
            expense_segments=[_sseg(amount=6_000, frm="2026-01-01",
                                    to="2032-01-01",
                                    active_months=[1, 2, 12])])
        reloaded = SimulationConfig.from_dict(cfg.to_dict())
        self.assertEqual(reloaded.expense_segments, cfg.expense_segments)


if __name__ == "__main__":
    unittest.main()
