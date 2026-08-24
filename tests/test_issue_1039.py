#!/usr/bin/env python3
"""Issue #1039: honour liabilities[kind=heloc].balance.amount as a true
opening drawn position (#1036 follow-up).

#1036 made a declared opening drawn balance a loud refusal -- the honest
interim. This issue replaces the refusal with the real capability: a
household already partway through a borrow-to-invest strategy starts the
simulation from its TRUE position --

  - ``heloc_balance`` starts at the declared ``balance.amount`` (not 0);
  - ``margin_available`` is the undrawn room (``limit - drawn``);
  - the opening interest carries a deductible proportion equal to the
    DECLARED ``deductibility.investment_portion`` (the original borrowing's
    purpose is a historical fact carried in by the snapshot, never re-derived
    from a simulation decision);
  - the opening position cross-checks against the registered charge (#664):
    mortgage + drawn <= charge.

Absence stays loud (DP#32): an opening balance WITHOUT a declared
deductibility block still refuses -- its trace would be un-derivable, and
defaulting it to fully-deductible or fully-personal would both fabricate a
tax position. A drawn balance above its own limit refuses. A deductibility
ratio declared on an UNDRAWN facility (nothing to apply it to) still refuses,
exactly as #1036 left it.

The golden household declares no opening drawn balance, so every new code
path is gated on ``heloc_opening_balance > 0`` and its trajectory is
byte-identical.

DP#15: no personal data. Fixtures reuse the shipped example contract trimmed
to the two-generation couple, with fabricated round-number modifications.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import input_contract as ic
import contract_errors
import contract_schema
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import (
    SimState,
    borrowing_purpose_tracings,
    compute_heloc_deductible_proportion,
    initial_state_for_run,
)


def _example_doc():
    """The shipped example contract, trimmed to the two-generation couple
    (the same helper tests/test_issue_1036.py uses). All figures are the
    shipped example's fabricated round numbers (DP#15)."""
    from test_input_contract import _load_example, _two_generation_subset
    return _two_generation_subset(_load_example())


def _heloc(doc):
    return next(liab for liab in doc["liabilities"] if liab["kind"] == "heloc")


def _opening_position_doc(drawn=65_000, portion=0.6, limit=150_000):
    """The example household with the HELOC already partway drawn: $65k
    outstanding, 60% of it traced to investment use (a mid-strategy Smith-
    Manoeuvre-style position carried in from before the snapshot)."""
    doc = _example_doc()
    h = _heloc(doc)
    h["balance"]["amount"] = drawn
    h["limit"] = limit
    h["deductibility"] = {"investment_portion": portion,
                          "personal_portion": 1.0 - portion}
    return doc


def _load(doc):
    contract_schema.validate_contract(doc)
    return ic.to_internal_config(doc)


class TestOpeningPositionIsHonoured(unittest.TestCase):
    """A declared balance.amount = $X > 0 with deductibility.investment_
    portion = p starts the run at heloc_balance = $X, margin_available =
    limit - $X, and a deductible proportion p on the opening interest."""

    def test_load_maps_the_opening_position(self):
        cfg = _load(_opening_position_doc())
        prop = cfg["property"]
        self.assertEqual(prop["heloc_opening_balance"], 65_000)
        self.assertEqual(prop["heloc_opening_investment_portion"], 0.6)
        # Undrawn room = limit - drawn: less standby room than the full limit.
        self.assertEqual(prop["margin_available"], 150_000 - 65_000)

    def test_simulation_config_carries_the_position(self):
        sc = SimulationConfig.from_dict(_load(_opening_position_doc()))
        self.assertEqual(sc.heloc_opening_balance, 65_000)
        self.assertEqual(sc.heloc_opening_investment_portion, 0.6)
        self.assertEqual(sc.margin_available, 85_000)

    def test_initial_state_starts_at_the_declared_balance(self):
        sc = SimulationConfig.from_dict(_load(_opening_position_doc()))
        state = SimState.initial(sc)
        self.assertEqual(state.heloc_balance, 65_000,
                         "the opening drawn balance is a true starting "
                         "position, not zero (#577 governs draws the ENGINE "
                         "makes, not draws the household already made)")

    def test_opening_trace_is_derived_from_the_declared_portion(self):
        sc = SimulationConfig.from_dict(_load(_opening_position_doc()))
        canada = SimState.initial(sc).jurisdiction_state["canada"]
        trace = canada["margin_tracing"]
        self.assertEqual(trace["total_advances"], 65_000)
        self.assertAlmostEqual(trace["investment_advances"], 39_000)
        self.assertAlmostEqual(trace["personal_draws"], 26_000)
        # ...and that trace yields exactly the declared deductible proportion.
        self.assertAlmostEqual(
            compute_heloc_deductible_proportion(trace), 0.6)

    def test_engine_prices_the_opening_interest_at_the_declared_proportion(self):
        """FamilySimulation.run(): the opening balance accrues interest from
        year 0 and exactly p of it is deducted under s.20(1)(c)."""
        sc = SimulationConfig(
            projection_years=3,
            investment_return=0.06,
            salary_growth=0.0,
            savings_rate=0.0,
            house_value=500_000,
            mortgage_balance=100_000,
            mortgage_rate=0.05,
            ltv_max=0.80,
            amortization_years=20,
            margin_available=135_000,
            heloc_opening_balance=65_000,
            heloc_opening_investment_portion=0.6,
            heloc_readvance=False,
            capitalize_interest=False,
            heloc_rate=0.05,
            # A non-reg pot the cash servicing can actually be PAID from --
            # interest that can be neither paid nor capitalized is 'unfunded'
            # and correctly deducted at 0 (#1036 D4/N2).
            portfolio_data={'accounts': {'non_reg': {'balance': 50_000,
                                                     'cost_basis': 50_000}}},
            family_members=[
                {"role": "primary", "gross_income": 120_000,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "birth_year": 1985},
                {"role": "spouse", "gross_income": 60_000,
                 "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
                 "birth_year": 1987},
            ],
            children=[],
        )
        sim = FamilySimulation(sc, adapter=CanadaAdapter(sc),
                               use_readvanceable=False, deduct_later=False)
        y1 = sim.run()[0]
        paid_or_payable = (y1.heloc_interest_capitalized
                           + y1.heloc_interest_serviced)
        self.assertGreater(paid_or_payable, 0.0,
                           "an opening drawn balance must accrue interest "
                           "from year 0")
        self.assertAlmostEqual(
            paid_or_payable, 65_000 * 0.05, places=4,
            msg="the opening interest is priced on the "
                "DECLARED balance at the declared rate")
        self.assertAlmostEqual(
            y1.margin_deductible_interest,
            paid_or_payable * 0.6, places=4,
            msg="exactly the declared investment_portion of "
                "the opening interest is deductible")

    def test_year_zero_lump_sum_adds_to_the_opening_position(self):
        """initial_state_for_run books a year-0 draw ON TOP of the opening
        position (and the cap applies to the room that genuinely remains --
        margin_available is already net of the opening draw)."""
        sc = SimulationConfig.from_dict(_load(_opening_position_doc()))
        state = initial_state_for_run(sc, lump_sum=50_000)
        self.assertEqual(state.heloc_balance, 65_000 + 50_000)
        # A draw larger than the remaining room is capped at the room.
        state = initial_state_for_run(sc, lump_sum=500_000)
        self.assertEqual(state.heloc_balance, 65_000 + 85_000)

    def test_borrowing_purpose_composes_with_the_opening_trace(self):
        """A year-0 lump sum must ADD its trace to the opening position's,
        never clobber the historical trace (dropping a declared fact is the
        DP#32 founding defect)."""
        opening = {"total_advances": 65_000.0, "investment_advances": 39_000.0,
                   "rrsp_advances": 0.0, "tfsa_advances": 0.0,
                   "personal_draws": 26_000.0}
        advance_tracing, margin_tracing = borrowing_purpose_tracings(
            lump_sum=100_000, lump_non_reg=100_000, margin_available=85_000,
            mortgage_balance=100_000, opening_margin_tracing=opening)
        # The new draw fills the remaining room ($85k); the $15k overflow is
        # the mortgage advance.
        self.assertAlmostEqual(margin_tracing["total_advances"], 150_000)
        self.assertAlmostEqual(margin_tracing["investment_advances"], 124_000)
        self.assertAlmostEqual(margin_tracing["personal_draws"], 26_000)
        self.assertAlmostEqual(
            compute_heloc_deductible_proportion(margin_tracing),
            124_000 / 150_000)
        # The advance leg is unaffected by the opening margin trace.
        self.assertAlmostEqual(advance_tracing["total_advances"], 100_000)
        self.assertAlmostEqual(advance_tracing["investment_advances"], 15_000)


class TestAbsenceStaysLoud(unittest.TestCase):
    """DP#32: the refusal paths that must survive #1039."""

    def test_opening_balance_without_deductibility_refuses(self):
        """An opening drawn balance WITHOUT a declared deductibility block
        must refuse, not default its trace."""
        doc = _opening_position_doc()
        del _heloc(doc)["deductibility"]
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _load(doc)
        self.assertIn("OPENING DRAWN balance", str(cm.exception))
        self.assertIn("deductibility", str(cm.exception))

    def test_drawn_above_own_limit_refuses(self):
        doc = _opening_position_doc(drawn=160_000, limit=150_000)
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _load(doc)
        self.assertIn("above its own limit", str(cm.exception))

    def test_deductibility_ratio_on_an_undrawn_facility_still_refuses(self):
        """#1036's refusal survives: a declared ratio with nothing drawn has
        no opening interest to apply to, and future draws are traced from
        their borrowing's purpose -- silently dropping it is the DP#32
        defect, so it refuses."""
        doc = _example_doc()
        h = _heloc(doc)
        assert h["balance"]["amount"] == 0
        h["deductibility"] = {"investment_portion": 0.6, "personal_portion": 0.4}
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _load(doc)
        self.assertIn("deductibility", str(cm.exception))

    def test_undrawn_with_zero_portion_is_still_accepted(self):
        """balance = 0 with a personal-use deductibility declaration remains
        the documented accepted state (#577/#1036); margin_available then
        equals the full limit."""
        doc = _example_doc()  # ships balance 0 / investment_portion 0.0
        cfg = _load(doc)
        self.assertEqual(cfg["property"]["margin_available"], 150_000)
        self.assertNotIn("heloc_opening_balance", cfg["property"])


class TestChargeCrossCheck(unittest.TestCase):
    """Issue #664: the OPENING POSITION cross-checks against the registered
    charge -- mortgage + actually-drawn <= the registered charge."""

    def test_opening_position_within_charge_is_accepted(self):
        # House $650k -> charge = 80% = $520k. Mortgage $340k + drawn $100k
        # = $440k: inside, so the position is honoured.
        doc = _opening_position_doc(drawn=100_000)
        cfg = _load(doc)
        self.assertEqual(cfg["property"]["margin_available"], 50_000)

    def test_opening_position_beyond_charge_refuses(self):
        # Mortgage $340k + drawn $190k = $530k > the $520k charge. (The
        # facility LIMIT is raised to $180k so the pre-existing limits check
        # -- mortgage + limit <= charge -- still passes at exactly $520k:
        # this isolates the OPENING POSITION breach, not a limit breach.)
        doc = _opening_position_doc(drawn=190_000, limit=180_000)
        with self.assertRaises(contract_errors.ContractAdaptationError) as cm:
            _load(doc)
        msg = str(cm.exception)
        self.assertIn("opening drawn balance", msg)
        self.assertIn("registered", msg)


class TestGoldenInvariantUnchanged(unittest.TestCase):
    """The golden household declares no opening drawn balance: every new code
    path is gated on heloc_opening_balance > 0, so its trajectory cannot move
    (by construction, not just by measurement)."""

    def test_golden_terminal_total_assets_is_byte_exact(self):
        from test_golden_trajectory_581 import _run, golden_household_config
        self.assertEqual(
            repr(_run(golden_household_config())[-1].total_assets),
            "9709753.139463063")

    def test_golden_config_defaults_opening_balance_to_zero(self):
        from test_golden_trajectory_581 import golden_household_config
        sc = SimulationConfig.from_dict(golden_household_config())
        self.assertEqual(sc.heloc_opening_balance, 0.0,
                         "the golden fixture's internal dict carries no "
                         "heloc_opening_balance key: absence is the "
                         "documented undrawn state (#577)")
        self.assertEqual(sc.heloc_opening_investment_portion, 0.0)


if __name__ == "__main__":
    unittest.main()
