#!/usr/bin/env python3
"""Issue #760: household_budget.annual_living_costs is a single perpetual
scalar -- it cannot express a finite-term expense that ENDS (a private-school
tuition a child ages out of, childcare, a term cost that stops on a date).
The scalar smears such a cost across the whole horizon, carrying it through
retirement and into the estate -- wrong, and wrong in the direction that makes
the household look poorer than it is.

The fix adds ``household_budget.expense_segments[]`` -- dated, finite-term
living-cost segments layered ON TOP OF the perpetual scalar. Each is an ANNUAL
recurring amount active over the half-open window ``[from, to)``; it contributes
its amount prorated by the days of that window falling in each calendar year
(the outflow-side analog of #674's dated income windows), and a non-null ``to``
means the expense STOPS after that date (reverts to $0, never carried to the
horizon). ``non_discretionary`` splits each segment onto #761's compressibility
axis: a discretionary segment compresses to zero under an income shock, a
non-discretionary one stays rigid.

## What this tests (DP#4/DP#15: fabricated round numbers, role-based names)

1. **The day-count blend (pure).** A perpetual segment (to=null) charges its
   full annual amount; a finite segment ends the year its window closes; a
   mid-year window prorates; leap years are exact.
2. **The finite-term fix, end-to-end.** A segment declared over a finite
   window is charged while active and is ZERO after its ``to`` -- it ENDS, it
   is not carried to the horizon (contrast the perpetual scalar).
3. **The solvency identity wiring.** The segment's charged amount is added to
   the identity's spending outflow (on top of ``living_costs``), reported on
   ``YearResult.expense_segment_outflow``; a non-discretionary segment also
   sizes the emergency reserve.
4. **Compressibility (the #761 axis on a dated segment).** Under a dated
   income shock a DISCRETIONARY segment compresses to zero (folded into
   ``solvency_discretionary_compressed``); a NON-discretionary one stays
   charged in full.
5. **The no-op property (DP#32).** A contract that omits ``expense_segments``
   reproduces today's numbers exactly (the golden invariant does not move).
6. **Absence must not silently default (DP#32).** A segment declared without a
   measured ``annual_living_costs`` fails loudly; a zero/negative window
   (``to <= from``) fails loudly -- neither is silently treated as $0.
7. **Round-trip (DP#24).** The segments survive ``to_dict``/``from_dict``.
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

# Sibling-import the #679 fixture helpers (the repo's no-`tests.`-prefix
# convention -- see test_issue_761_discretionary_living_costs.py).
from test_issue_679_solvency import (
    RUIN_AFTER_TAX_EI,
    RUIN_AFTER_TAX_WORKING,
    RUIN_LIVING_COSTS,
    RUIN_MORTGAGE_PAYMENT,
    RUIN_RRSP_CONTRIBUTION,
    _mort_data,
    _reserve_config,
)
from test_dp_income_scenario_reaches_engine import _two_generation_subset

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers
import input_contract as ic
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_rules import _expense_segment_contribution_in_year
from simulation_state import SimState, simulate_year_pure


# ─── A one-year pure-step helper that varies ONLY the expense segments ─────
# Isolates the solvency identity's spending-outflow term from everything else
# (same mortgage, same income, same reserve) so the test reads the segment
# outflow directly off YearResult, not through a multi-year fold.

def _one_year(*, expense_segments, calendar_year=2026,
              income_shock_active=False, living_costs=RUIN_LIVING_COSTS,
              after_tax_income=RUIN_AFTER_TAX_WORKING,
              discretionary_fraction=None):
    cfg = _reserve_config(
        emergency_reserve_rate=0.0,
        expense_segments=expense_segments,
        discretionary_fraction=discretionary_fraction,
    )
    state = SimState(
        mortgage_balance=cfg.mortgage_balance,
        non_reg_balance=5_000, non_reg_acb=5_000,
        jurisdiction_state={'canada': {'tfsa_primary_balance': 3_000,
                                       'rrsp_balance': 2_000, 'rrsp_room': 0}},
    )
    allocations = {
        '_primary_income': 95_000, '_annual_savings': RUIN_RRSP_CONTRIBUTION,
        'primary_rrsp': RUIN_RRSP_CONTRIBUTION,
    }
    mort = _mort_data(state.mortgage_balance, payment=RUIN_MORTGAGE_PAYMENT)
    result, _ = simulate_year_pure(
        state=state, year=0, calendar_year=calendar_year,
        allocations=allocations, config=cfg, investment_return=0.05,
        primary_marginal_rate=0.30, mortgage_data=mort,
        living_costs=living_costs, after_tax_income=after_tax_income,
        income_shock_active=income_shock_active,
    )
    return result


def _seg(*, amount, frm, to, non_discretionary=True, description="dated cost"):
    return {"description": description, "amount": amount,
            "from": frm, "to": to, "non_discretionary": non_discretionary}


# ============================================================================
# 1. The day-count blend (pure function, DP#3).
# ============================================================================

class TestDayCountBlend(unittest.TestCase):

    def test_perpetual_segment_charges_full_annual_amount(self):
        """to=null (perpetual), window covers the whole year -> full amount."""
        seg = _seg(amount=12_000, frm="2020-01-01", to=None)
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026), 12_000)

    def test_segment_is_zero_before_it_starts(self):
        seg = _seg(amount=12_000, frm="2030-01-01", to=None)
        self.assertEqual(_expense_segment_contribution_in_year(seg, 2026), 0.0)

    def test_finite_segment_is_zero_the_year_after_it_ends(self):
        """A window closing 2028-07-01 contributes 0 in 2029 -- it ENDS, it is
        NOT carried to the horizon the way the perpetual scalar is."""
        seg = _seg(amount=12_000, frm="2020-01-01", to="2028-07-01")
        self.assertEqual(_expense_segment_contribution_in_year(seg, 2029), 0.0)

    def test_mid_year_start_prorates_by_days(self):
        """A segment starting 2026-07-01 is active for the second half of a
        365-day year -> 184/365 of the annual amount (Jul 1 - Dec 31)."""
        seg = _seg(amount=36_500, frm="2026-07-01", to=None)
        expected = 36_500 * (date(2027, 1, 1) - date(2026, 7, 1)).days / 365
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026), expected)

    def test_leap_year_day_count_is_exact(self):
        """2028 is a leap year (366 days); a half-window prorates over 366, not
        a flat 365 -- the blend never approximates."""
        seg = _seg(amount=36_600, frm="2028-07-01", to=None)
        expected = 36_600 * (date(2029, 1, 1) - date(2028, 7, 1)).days / 366
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2028), expected)

    def test_to_on_jan_1_makes_prior_year_full_and_that_year_zero(self):
        """`to` is EXCLUSIVE: to=2027-01-01 charges all of 2026 and nothing in
        2027 (no off-by-one double-charge or gap)."""
        seg = _seg(amount=10_000, frm="2020-01-01", to="2027-01-01")
        self.assertAlmostEqual(
            _expense_segment_contribution_in_year(seg, 2026), 10_000)
        self.assertEqual(
            _expense_segment_contribution_in_year(seg, 2027), 0.0)


# ============================================================================
# 2. The solvency identity wiring (issue #679/#758 path).
# ============================================================================

class TestSolvencyWiring(unittest.TestCase):

    def test_absence_is_a_no_op(self):
        """No expense_segments -> the segment outflow is 0 and the identity's
        spending term is byte-for-byte the base living_costs (today's
        behaviour). This is the DP#32 no-op property."""
        baseline = _one_year(expense_segments=[])
        self.assertEqual(baseline.expense_segment_outflow, 0.0)
        self.assertAlmostEqual(baseline.solvency_spending_outflow,
                               RUIN_LIVING_COSTS)

    def test_non_discretionary_segment_adds_to_spending_outflow(self):
        """An active non-discretionary segment is charged ON TOP of the base
        scalar, raising the identity's required spending by exactly its annual
        amount."""
        base = _one_year(expense_segments=[])
        withseg = _one_year(expense_segments=[
            _seg(amount=14_000, frm="2020-01-01", to=None)])
        self.assertAlmostEqual(withseg.expense_segment_outflow, 14_000)
        self.assertAlmostEqual(
            withseg.solvency_spending_outflow - base.solvency_spending_outflow,
            14_000)

    def test_ended_segment_charges_nothing(self):
        """A segment whose `to` is in an EARLIER year contributes $0 -- the
        finite-term fix, read off the identity (not just the pure helper)."""
        withseg = _one_year(calendar_year=2030, expense_segments=[
            _seg(amount=14_000, frm="2020-01-01", to="2028-07-01")])
        self.assertEqual(withseg.expense_segment_outflow, 0.0)

    def test_non_discretionary_segment_sizes_the_reserve(self):
        """A committed dated expense is an ESSENTIAL outflow the emergency
        reserve must bridge -- the reserve target grows by the segment's
        annual amount (times the target-months factor)."""
        base = _one_year(expense_segments=[])
        withseg = _one_year(expense_segments=[
            _seg(amount=14_000, frm="2020-01-01", to=None)])
        self.assertGreater(withseg.emergency_reserve_target,
                           base.emergency_reserve_target)


# ============================================================================
# 3. Compressibility (the #761 axis, on a dated segment).
# ============================================================================

class TestSegmentCompressibility(unittest.TestCase):

    def test_discretionary_segment_compresses_to_zero_under_shock(self):
        """A DISCRETIONARY dated segment (non_discretionary=false) compresses
        to zero under a dated income shock and the compressed dollars are
        reported on solvency_discretionary_compressed (transparent, DP#32)."""
        r = _one_year(
            income_shock_active=True,
            after_tax_income=RUIN_AFTER_TAX_EI,
            expense_segments=[_seg(amount=9_000, frm="2020-01-01", to=None,
                                   non_discretionary=False)])
        self.assertEqual(r.expense_segment_outflow, 0.0)
        self.assertAlmostEqual(r.solvency_discretionary_compressed, 9_000)

    def test_discretionary_segment_charged_in_a_normal_year(self):
        """No shock -> the same discretionary segment is charged in full (it
        only compresses UNDER a shock)."""
        r = _one_year(
            income_shock_active=False,
            expense_segments=[_seg(amount=9_000, frm="2020-01-01", to=None,
                                   non_discretionary=False)])
        self.assertAlmostEqual(r.expense_segment_outflow, 9_000)
        self.assertEqual(r.solvency_discretionary_compressed, 0.0)

    def test_non_discretionary_segment_stays_rigid_under_shock(self):
        """A NON-discretionary dated segment (tuition, childcare) does NOT
        compress under a shock -- it stays charged in full."""
        r = _one_year(
            income_shock_active=True,
            after_tax_income=RUIN_AFTER_TAX_EI,
            expense_segments=[_seg(amount=14_000, frm="2020-01-01", to=None,
                                   non_discretionary=True)])
        self.assertAlmostEqual(r.expense_segment_outflow, 14_000)
        self.assertEqual(r.solvency_discretionary_compressed, 0.0)


# ============================================================================
# 4. Contract boundary (DP#32: a partial/degenerate declaration fails loudly).
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

    def test_valid_segments_map_into_the_internal_shape(self):
        doc = _doc_with_segments([
            _seg(amount=14_000, frm="2026-09-01", to="2032-07-01",
                 description="tuition, child_a")])
        cfg = self._map(doc)
        segs = cfg["household_budget"]["expense_segments"]
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]["amount"], 14_000)
        self.assertEqual(segs[0]["to"], "2032-07-01")
        self.assertTrue(segs[0]["non_discretionary"])

    def test_segment_without_living_costs_is_refused(self):
        """A dated segment is an ADDITIONAL cost on the measured base scalar --
        declaring one without annual_living_costs must fail loudly, never
        default the base to zero (DP#32)."""
        doc = _example_doc()
        doc["household_budget"] = {
            "annual_living_costs": None,
            "expense_segments": [_seg(amount=14_000, frm="2026-09-01", to=None)],
        }
        with self.assertRaises(ic.ContractAdaptationError) as cm:
            self._map(doc)
        self.assertIn("annual_living_costs", str(cm.exception))

    def test_end_on_or_before_start_is_refused(self):
        """A `to` on or before `from` is an empty/negative window -- refused,
        never silently treated as a $0 segment (DP#32)."""
        doc = _doc_with_segments([
            _seg(amount=14_000, frm="2030-01-01", to="2028-01-01")])
        with self.assertRaises(ic.ContractAdaptationError):
            self._map(doc)


# ============================================================================
# 5. Round-trip (DP#24).
# ============================================================================

class TestRoundTrip(unittest.TestCase):

    def test_segments_survive_to_dict_from_dict(self):
        # The segments live under household_budget, which round-trips only when
        # a measured living_costs engages it (DP#16) -- so the scalar is set.
        cfg = _reserve_config(living_costs=78_000, expense_segments=[
            _seg(amount=14_000, frm="2026-09-01", to="2032-07-01")])
        reloaded = SimulationConfig.from_dict(cfg.to_dict())
        self.assertEqual(reloaded.expense_segments, cfg.expense_segments)

    def test_no_segments_round_trips_to_absent(self):
        cfg = _reserve_config(living_costs=78_000, expense_segments=[])
        self.assertNotIn(
            "expense_segments",
            cfg.to_dict().get("household_budget", {}),
            "an empty segment list must round-trip to 'absent', not a "
            "fabricated block (DP#24/DP#32)")


# ============================================================================
# 6. The golden invariant does not move (pure-addition, undeclared -> no-op).
# ============================================================================

class TestGoldenInvariantUnmoved(unittest.TestCase):

    def test_golden_terminal_assets_unchanged(self):
        from test_golden_trajectory_581 import _run, golden_household_config
        logging.disable(logging.WARNING)
        try:
            terminal = _run(golden_household_config())[-1].total_assets
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(terminal, 9709753.139463063)


if __name__ == "__main__":
    unittest.main()
