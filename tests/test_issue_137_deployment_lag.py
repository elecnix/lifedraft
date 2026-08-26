#!/usr/bin/env python3
"""Issue #137: a DECLARABLE deployment lag on the refinance cash-out advance.

The engine's default assumes borrowed money is deployed the instant it is
borrowed -- a year-0 refinance cash-out lump is invested in that same
simulated year, so a household that takes months to actually move the money
gets a projection byte-identical to one that moved it same-day. The delay's
cost (the spread between the debt's interest rate and the idle money's parking
rate) is silently modelled as $0.

A household that DECLARES ``deployment_lag_months`` opts into pricing that cost.
This module tests both the pure arithmetic seam (``deployment_lag_cost``,
DP#11 -- a unit test of a pure function calling that function directly is
correct) and the end-to-end engine behaviour (driving ``FamilySimulation.run``
and asserting the engine's observable output, DP#11/DP#18 -- never hand-building
engine internals).

Fabricated round numbers, role-based names (DP#4/DP#15).
"""
import unittest


import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deployment_lag import deployment_lag_cost


# ============================================================================
# Unit tests: the pure arithmetic seam (DP#11 -- call the function directly).
# ============================================================================

class DeploymentLagCostPureTest(unittest.TestCase):
    """``deployment_lag_cost`` is a pure function (DP#3): same inputs -> same
    output, no hidden state. These tests verify its contract directly."""

    def test_no_lag_is_hard_zero(self):
        """months == 0 (no lag declared) is a hard zero -- the carry IS zero,
        never a default that masks a missing input (DP#32). Byte-for-byte the
        pre-feature behaviour."""
        self.assertEqual(deployment_lag_cost(100_000, 0, 0.05, 0.0), 0.0)

    def test_no_lump_is_hard_zero(self):
        """lump == 0 (no cash-out to lag, e.g. a no-refinance scenario) is a
        hard zero -- there is nothing to carry a cost on."""
        self.assertEqual(deployment_lag_cost(0, 6, 0.05, 0.0), 0.0)

    def test_zero_spread_is_zero(self):
        """When the investment return equals the parking rate the net carry is
        exactly zero -- the idle money earns what investing would have."""
        self.assertEqual(deployment_lag_cost(100_000, 12, 0.05, 0.05), 0.0)

    def test_positive_carry_basic(self):
        """lump * (investment_return - parking) * (months / 12). A $100k lump,
        5% investment return, 0% parking, 12 months -> $5,000 of foregone
        return net of parking earnings."""
        self.assertAlmostEqual(
            deployment_lag_cost(100_000, 12, 0.05, 0.0), 5_000.0, places=6)

    def test_positive_carry_partial_year(self):
        """A 3-month lag on the same lump -> a quarter of the annual carry."""
        self.assertAlmostEqual(
            deployment_lag_cost(100_000, 3, 0.05, 0.0), 1_250.0, places=6)

    def test_parking_rate_above_investment_return_is_negative_carry(self):
        """parking_rate > investment_return is a real, representable scenario
        (idle money earning more than the portfolio's return) and returns a
        NEGATIVE carry -- a gain, not floored at zero. A $100k lump, 2%
        investment return, 5% parking, 12 months -> -$3,000 (the household
        GAINS $3,000 during the lag by keeping the money in cash)."""
        self.assertAlmostEqual(
            deployment_lag_cost(100_000, 12, 0.02, 0.05), -3_000.0, places=6)

    def test_negative_carry_not_floored_at_zero(self):
        """The negative-carry case is honoured as-is, never silently floored
        at zero (DP#32: a plausible number from a real declared scenario is
        not a fabricated zero)."""
        self.assertLess(
            deployment_lag_cost(50_000, 6, 0.03, 0.06), 0.0)

    def test_large_lag_scales_linearly(self):
        """The carry is linear in months -- a 24-month lag is twice a
        12-month lag (the 'large' side of the lag threshold)."""
        twelve = deployment_lag_cost(200_000, 12, 0.06, 0.01)
        twenty_four = deployment_lag_cost(200_000, 24, 0.06, 0.01)
        self.assertAlmostEqual(twenty_four, 2 * twelve, places=6)

    def test_negative_months_raises(self):
        """months < 0 is a bad input, not a 'lag in the other direction' --
        a plausible sign-flipped carry would be a confident wrong number
        (DP#32)."""
        with self.assertRaises(ValueError):
            deployment_lag_cost(100_000, -1, 0.05, 0.0)

    def test_negative_lump_raises(self):
        """lump < 0 is a bad input, not a carry to price."""
        with self.assertRaises(ValueError):
            deployment_lag_cost(-1, 6, 0.05, 0.0)

    def test_rate_spread_at_or_below_negative_100_percent_raises(self):
        """A net rate (investment_return - parking) at or below -100% is an absurd
        scenario (parking earning 100%+ more than the investment return) whose
        'cost' would be a large fabricated gain; refuse rather than silently
        invent money (DP#32)."""
        with self.assertRaises(ValueError):
            # investment return 0.0, parking 1.0 -> spread -1.0 (exactly -100%)
            deployment_lag_cost(100_000, 12, 0.0, 1.0)
        with self.assertRaises(ValueError):
            # investment return 0.0, parking 1.5 -> spread -1.5 (below -100%)
            deployment_lag_cost(100_000, 12, 0.0, 1.5)

    def test_rate_spread_just_above_negative_100_percent_ok(self):
        """A spread just above -100% (e.g. -99%) is extreme but not the absurd
        -100% boundary -- it returns a negative carry rather than raising."""
        carry = deployment_lag_cost(100_000, 12, 0.005, 1.0)  # spread -0.995
        self.assertLess(carry, 0.0)

    def test_is_pure(self):
        """DP#3: same inputs always yield the same output -- no hidden state."""
        args = (123_456, 7, 0.045, 0.015)
        self.assertEqual(
            deployment_lag_cost(*args), deployment_lag_cost(*args))


# ============================================================================
# Integration tests: drive FamilySimulation.run() and assert the engine's
# observable output (DP#11/DP#18 -- never hand-build engine internals).
# ============================================================================

from scenario_overlay import ScenarioOverlay, apply_overlay
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation

# A fabricated household (DP#4/DP#15) with a mortgage and undrawn HELOC room,
# mirroring test_golden_trajectory_581's cash-out fixture. Round numbers,
# role-based names. RRSP room is 0 for both members so the year-0 draw is not
# confounded by an RRSP-refund HELOC paydown (see the cash-out fixture's own
# DP#17 note).
LAG_START_YEAR = 2026
LAG_PRIMARY_BIRTH = 1980
LAG_SPOUSE_BIRTH = 1982
LAG_GROSS_RETURN = 0.06
LAG_OPENING_MORTGAGE = 200_000
LAG_OPENING_MARGIN = 150_000
LAG_HOUSE_VALUE = 600_000
LAG_MORTGAGE_RATE = 0.05
LAG_CASH_OUT = 100_000
LAG_AMORTIZATION = 25


def _lag_base_config() -> dict:
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': LAG_PRIMARY_BIRTH, 'gross_income': 150_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
            {'role': 'spouse', 'birth_year': LAG_SPOUSE_BIRTH, 'gross_income': 60_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 20_000},
        ]},
        'accounts': {'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': LAG_START_YEAR, 'projection_years': 10,
            'investment_return': LAG_GROSS_RETURN, 'salary_growth': 0.0,
            'frozen_brackets': True,
        },
        'property': {
            'house_value': LAG_HOUSE_VALUE, 'mortgage_balance': LAG_OPENING_MORTGAGE,
            'mortgage_rate': LAG_MORTGAGE_RATE, 'amortization_years': 20,
            'margin_available': LAG_OPENING_MARGIN, 'ltv_max': 0.80,
            'heloc_readvance': False,
        },
        'savings': {'rate': 0.10},
        'tax': {'province': 'qc'},
    }


def _run_cashout(deployment_lag_months=0, parking_rate=0.0, time_step='yearly'):
    """Drive the engine end-to-end: apply_overlay books the cash-out refinance,
    then FamilySimulation.run() folds the projection. The deployment lag is set
    on the property dict (the from_dict path the adapter writes), exercising the
    full config -> engine seam (DP#18: the leaf reaches the engine, not just the
    merged config). Returns the list of YearResult. ``time_step`` selects the
    yearly or monthly fold (finding #2: the monthly per-year loop must surface
    the year-0 carry too)."""
    base_cfg = _lag_base_config()
    overlay = ScenarioOverlay(label='cashout_lag_test', cash_out=LAG_CASH_OUT,
                              mortgage_rate=base_cfg['property']['mortgage_rate'],
                              refinance_amortization_years=LAG_AMORTIZATION)
    overlaid_cfg = apply_overlay(base_cfg, overlay)
    # Issue #137: set the deployment lag on the property dict, the same key
    # input_contract.py / contract_decisions.py map the declared leaf onto.
    overlaid_cfg['property']['deployment_lag_months'] = deployment_lag_months
    overlaid_cfg['property']['deployment_lag_parking_rate'] = parking_rate
    overlaid_cfg['assumptions']['time_step'] = time_step
    sim_cfg = SimulationConfig.from_dict(overlaid_cfg)
    lump_sum = sim_cfg.margin_available + sim_cfg.cash_out
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                          use_readvanceable=False, deduct_later=False,
                          lump_sum=lump_sum)
    return sim.run()


class DeploymentLagIntegrationTest(unittest.TestCase):
    """Drive FamilySimulation.run() and assert the engine's observable output
    (DP#11/DP#18): the lag's carry cost flows into the year-0 result and the
    terminal trajectory."""

    def test_no_declared_lag_surfaces_zero_cost(self):
        """A run with no declared lag (months 0) surfaces deployment_lag_cost
        = 0.0 on the year-0 result -- the no-op path, byte-identical to the
        pre-feature behaviour (DP#32)."""
        results = _run_cashout(deployment_lag_months=0)
        self.assertEqual(results[0].deployment_lag_cost, 0.0)

    def test_declared_lag_surfaces_positive_cost_on_year_0(self):
        """A declared lag surfaces the computed carry on the year-0 result.
        carry = cash_out * (investment_return - parking) * (months / 12) =
        100_000 * (0.06 - 0.0) * (6 / 12) = 3_000. The investment_return is
        the portfolio's year-0 return (LAG_GROSS_RETURN), NOT the mortgage
        rate -- the borrowing rate double-counts the debt cost the mortgage
        already pays (finding #1)."""
        results = _run_cashout(deployment_lag_months=6, parking_rate=0.0)
        self.assertAlmostEqual(
            results[0].deployment_lag_cost, 3_000.0, places=6)

    def test_declared_lag_cost_only_on_year_0(self):
        """The carry is a YEAR-0 cost -- it is 0.0 on every year after year 0
        (the lag is a one-time deployment-timing cost, not a recurring charge)."""
        results = _run_cashout(deployment_lag_months=6, parking_rate=0.0)
        self.assertGreater(results[0].deployment_lag_cost, 0.0)
        for r in results[1:]:
            self.assertEqual(r.deployment_lag_cost, 0.0)

    def test_declared_lag_reduces_terminal_assets(self):
        """The carry reduces the deployable principal at year 0, so less
        compounds -- the lag's cost flows into every objective via terminal
        total_assets. A run WITH a lag ends with LESS than the same run
        WITHOUT one (the cost is real, not silently $0)."""
        no_lag = _run_cashout(deployment_lag_months=0)
        with_lag = _run_cashout(deployment_lag_months=6, parking_rate=0.0)
        self.assertGreater(no_lag[-1].total_assets, with_lag[-1].total_assets)

    def test_lag_zero_is_byte_identical_to_no_lag(self):
        """A declared lag of 0 months is byte-identical to no declared lag --
        both surface deployment_lag_cost = 0.0 and the same terminal assets
        (DP#32: 0 is a value, not a fallback; the no-lag path is unchanged)."""
        no_lag = _run_cashout(deployment_lag_months=0)
        zero_lag = _run_cashout(deployment_lag_months=0, parking_rate=0.0)
        self.assertEqual(no_lag[-1].total_assets, zero_lag[-1].total_assets)
        self.assertEqual(no_lag[0].deployment_lag_cost, zero_lag[0].deployment_lag_cost)

    def test_larger_lag_costs_more(self):
        """Both sides of the lag threshold (DP#17): a 12-month lag costs more
        than a 6-month lag, so terminal assets are lower."""
        six = _run_cashout(deployment_lag_months=6, parking_rate=0.0)
        twelve = _run_cashout(deployment_lag_months=12, parking_rate=0.0)
        self.assertGreater(twelve[0].deployment_lag_cost, six[0].deployment_lag_cost)
        self.assertGreater(six[-1].total_assets, twelve[-1].total_assets)

    def test_negative_carry_caps_borrowed_investment_at_lump(self):
        """Finding #6 (negative-carry solvency inflation): when the parking rate
        EXCEEDS the investment return the carry is negative (a gain). The
        deployable principal is CAPPED at the borrowed lump -- the idle window
        cannot inflate invested principal above what was actually borrowed
        (that would invest money the household never borrowed). The raw
        negative carry is still surfaced on the year-0 result for
        observability, but terminal assets do NOT exceed the no-lag baseline
        (the parking-earnings excess is not routed into invested principal).
        Both sides of the parking/return threshold (DP#17): the surfaced cost
        IS negative (a real gain), but it is not realized as borrowed money."""
        no_lag = _run_cashout(deployment_lag_months=0)
        # investment return 6%, parking 8% -> negative carry (gain)
        gain_lag = _run_cashout(deployment_lag_months=12, parking_rate=0.08)
        self.assertLess(gain_lag[0].deployment_lag_cost, 0.0)
        # Capped: the year-0 invested principal does not exceed the no-lag run
        # (borrowed_investment <= lump_sum, no solvency inflation).
        self.assertLessEqual(
            gain_lag[0].contributions_total,
            no_lag[0].contributions_total + 1e-6)
        # Terminal assets do not exceed no-lag -- the gain is not invented as
        # borrowed money (the deployed principal is capped at the lump).
        self.assertLessEqual(
            gain_lag[-1].total_assets, no_lag[-1].total_assets + 1e-6)

    def test_parking_rate_below_investment_return_costs_more_than_zero_parking(self):
        """Both sides of the parking/return threshold: a positive parking
        rate (idle money earning something) reduces the carry vs 0% parking,
        so terminal assets are higher than the 0%-parking lag run."""
        zero_parking = _run_cashout(deployment_lag_months=6, parking_rate=0.0)
        two_pct_parking = _run_cashout(deployment_lag_months=6, parking_rate=0.02)
        # carry with 2% parking = 100k * (0.06 - 0.02) * 0.5 = 2000 < 3000
        self.assertLess(
            two_pct_parking[0].deployment_lag_cost,
            zero_parking[0].deployment_lag_cost)
        self.assertGreater(
            two_pct_parking[-1].total_assets,
            zero_parking[-1].total_assets)

    def test_debt_side_unchanged_by_lag(self):
        """The full borrowed lump stays on the debt side (the year-0 purpose
        tracing is untouched): the year-0 mortgage balance (which carries the
        full cash_out advance) is the same with and without a declared lag.
        The carry reduces only the DEPLOYED principal, not the debt booked."""
        no_lag = _run_cashout(deployment_lag_months=0)
        with_lag = _run_cashout(deployment_lag_months=6, parking_rate=0.0)
        self.assertAlmostEqual(
            no_lag[0].mortgage_balance, with_lag[0].mortgage_balance, places=2)

    def test_monthly_path_surfaces_year_0_carry(self):
        """Finding #2: the monthly fold's per-year loop must surface the year-0
        deployment-lag carry on results[0], mirroring the yearly path. The
        monthly path's pre-projection step computes a year-0 result0 that is
        NOT appended (it only deploys the lump), so without threading the carry
        into the per-year loop's simulate_year_pure call the first appended
        result would carry deployment_lag_cost=0.0 even when a carry was
        applied -- the cost would be paid but invisible. carry =
        100_000 * (0.06 - 0.0) * (6/12) = 3_000 (investment_return, not the
        mortgage rate)."""
        results = _run_cashout(
            deployment_lag_months=6, parking_rate=0.0, time_step='monthly')
        self.assertAlmostEqual(
            results[0].deployment_lag_cost, 3_000.0, places=6)
        for r in results[1:]:
            self.assertEqual(r.deployment_lag_cost, 0.0)

    def test_monthly_and_yearly_surface_the_same_carry(self):
        """Both folds surface the same year-0 carry (DP#9: one spelling of the
        year-0 fact)."""
        yearly = _run_cashout(
            deployment_lag_months=6, parking_rate=0.0, time_step='yearly')
        monthly = _run_cashout(
            deployment_lag_months=6, parking_rate=0.0, time_step='monthly')
        self.assertAlmostEqual(
            yearly[0].deployment_lag_cost, monthly[0].deployment_lag_cost, places=6)


# ============================================================================
# Contract-mapping test: the adapter (contract_decisions.map_mortgage_decisions)
# reads deployment_lag_months / parking_rate off the refinance_option and maps
# them onto the internal property keys SimulationConfig.from_dict consumes --
# the schema-coverage-relevant hop (the leaf is consumed, not merely parsed).
# ============================================================================

import copy
from test_input_contract import _load_example, _two_generation_subset  # noqa: E402
import input_contract as ic  # noqa: E402
from contract_schema import validate_contract  # noqa: E402


def _example_doc_with_lag(months: int, parking_rate: float) -> dict:
    """Load the shipped example, trim to the two-generation sub-family the
    adapter can map, and declare a deployment lag + parking_rate on a
    refinance option with a cash_out (the refi_50k option -- the one whose
    advance is actually lagged). Fabricated values, DP#15."""
    doc = _two_generation_subset(_load_example())
    refi = copy.deepcopy(doc["decisions"]["mortgage"]["refinance_options"])
    for opt in refi:
        if opt.get("cash_out", 0) > 0:
            opt["deployment_lag_months"] = months
            opt["parking_rate"] = parking_rate
            break
    doc["decisions"]["mortgage"]["refinance_options"] = refi
    return doc


class DeploymentLagContractMappingTest(unittest.TestCase):
    """The contract leaf -> internal config -> SimulationConfig seam (DP#18:
    the leaf reaches the engine, not just the merged config)."""

    def test_contract_with_lag_is_schema_valid_and_maps_to_internal_keys(self):
        """A contract declaring deployment_lag_months + parking_rate on a
        refinance option is schema-valid and maps to the internal property
        keys SimulationConfig.from_dict reads."""
        doc = _example_doc_with_lag(months=4, parking_rate=0.015)
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy["property"]["deployment_lag_months"], 4)
        self.assertEqual(legacy["property"]["deployment_lag_parking_rate"], 0.015)

    def test_contract_lag_loads_onto_simulation_config(self):
        """The mapped internal keys load onto the SimulationConfig fields the
        year-0 deployment reads."""
        doc = _example_doc_with_lag(months=4, parking_rate=0.015)
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual(sim_cfg.deployment_lag_months, 4)
        self.assertAlmostEqual(sim_cfg.deployment_lag_parking_rate, 0.015)

    def test_contract_with_no_lag_maps_no_keys(self):
        """A contract whose refinance options declare no deployment lag maps
        NEITHER internal key -- the no-lag path carries no keys, byte-identical
        to the pre-feature shape (DP#24/DP#32: absence is absence)."""
        doc = _two_generation_subset(_load_example())
        # Strip any lag the shipped example carries (finding #3 adds one to
        # refi_50k) so this is the true no-lag contract -- exactly the way the
        # #792 advance-split test strips advance_split.
        for opt in doc["decisions"]["mortgage"]["refinance_options"]:
            opt.pop("deployment_lag_months", None)
            opt.pop("parking_rate", None)
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertNotIn("deployment_lag_months", legacy["property"])
        self.assertNotIn("deployment_lag_parking_rate", legacy["property"])

    def test_round_trip_preserves_a_declared_lag(self):
        """DP#24: a declared lag survives a load -> modify -> save cycle.
        to_dict re-emits the lag (only when declared > 0); from_dict reads it
        back. A no-lag config round-trips to absence."""
        doc = _example_doc_with_lag(months=4, parking_rate=0.015)
        legacy = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(legacy)
        round_tripped = sim_cfg.to_dict()
        self.assertEqual(round_tripped["property"]["deployment_lag_months"], 4)
        self.assertAlmostEqual(
            round_tripped["property"]["deployment_lag_parking_rate"], 0.015)
        no_lag_doc = _two_generation_subset(_load_example())
        for opt in no_lag_doc["decisions"]["mortgage"]["refinance_options"]:
            opt.pop("deployment_lag_months", None)
            opt.pop("parking_rate", None)
        no_lag_cfg = SimulationConfig.from_dict(
            ic.to_internal_config(no_lag_doc))
        no_lag_rt = no_lag_cfg.to_dict()
        self.assertNotIn("deployment_lag_months", no_lag_rt["property"])
        self.assertNotIn("deployment_lag_parking_rate", no_lag_rt["property"])


# ============================================================================
# Finding #4: multi-option bleed -- the lag is a single scalar from the FIRST
# option that declares one, applied across a continuous-cash_out sweep. Two
# options with different lags pin the documented first-option-wins behaviour
# and the model_fidelity disclosure that fires when the bleed is present.
# ============================================================================

import model_fidelity  # noqa: E402


def _two_lag_options_doc(first_months: int, second_months: int) -> dict:
    """A contract with TWO cash-out refinance options each declaring a
    deployment lag, so the single-scalar carry cannot represent both. The
    FIRST declaring option's lag is the one carried (first-option-wins, the
    same shape refinance_amortization_years uses). Fabricated, DP#15."""
    doc = _two_generation_subset(_load_example())
    refi = copy.deepcopy(doc["decisions"]["mortgage"]["refinance_options"])
    # Ensure at least two CASH-OUT options (the lag applies to cash-out advances
    # only; the no_refi baseline with cash_out 0 carries no lag cost).
    cash_out_opts = [o for o in refi if o.get("cash_out", 0) > 0]
    if not cash_out_opts:
        refi.append({"id": "refi_extra", "label": "extra cash-out",
                     "cash_out": 50_000, "ltv": 0.55,
                     "amortization_years": 25})
    if len(cash_out_opts) < 2:
        refi.append({"id": "refi_extra2", "label": "second cash-out",
                     "cash_out": 100_000, "ltv": 0.65,
                     "amortization_years": 25})
    cash_out_opts = [o for o in refi if o.get("cash_out", 0) > 0]
    cash_out_opts[0]["deployment_lag_months"] = first_months
    cash_out_opts[0]["parking_rate"] = 0.0
    cash_out_opts[1]["deployment_lag_months"] = second_months
    cash_out_opts[1]["parking_rate"] = 0.0
    doc["decisions"]["mortgage"]["refinance_options"] = refi
    return doc


class DeploymentLagMultiOptionBleedTest(unittest.TestCase):
    """Finding #4: the lag is a single scalar from the first declaring option,
    applied across the refinance sweep (a continuous cash_out). Two options
    with different lags pin the documented first-option-wins behaviour."""

    def test_first_declaring_option_wins(self):
        """The carried lag is the FIRST option (in declaration order) that
        declares a deployment_lag_months -- a later option's different lag is
        not distinguishable through the single scalar and is dropped (the
        same real, separate follow-up work refinance_amortization_years
        already notes)."""
        doc = _two_lag_options_doc(first_months=3, second_months=9)
        validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        self.assertEqual(legacy["property"]["deployment_lag_months"], 3)
        self.assertIn("deployment_lag_declared_cash_out", legacy["property"])

    def test_multi_option_bleed_caveat_is_active(self):
        """model_fidelity discloses the bleed (Direction.UNKNOWN, issue #137):
        with two refinance options the single lag scalar is applied across a
        sweep that explores a cash_out the declaring option never stated it
        for, so the carry is an approximation. The caveat fires for this run."""
        doc = _two_lag_options_doc(first_months=3, second_months=9)
        legacy = ic.to_internal_config(doc)
        active = {a.id for a in model_fidelity.active_approximations(legacy)}
        self.assertIn("deployment_lag_multi_option_bleed", active)

    def test_single_option_no_bleed_caveat_when_booked_matches_declared(self):
        """When there is only ONE cash-out refinance option and the booked
        cash_out IS the declaring option's cash_out, the bleed does not bite
        -- the lag is priced on exactly the lump the option stated it for."""
        doc = _example_doc_with_lag(months=4, parking_rate=0.0)
        legacy = ic.to_internal_config(doc)
        declared = legacy["property"]["deployment_lag_declared_cash_out"]
        # Book exactly the declaring option's cash_out so the two match, and
        # there is only one cash-out option (the example's no_refi has cash_out
        # 0, which carries no lag cost and is not counted as a cash-out option).
        legacy["property"]["cash_out"] = declared
        legacy["property"]["deployment_lag_declared_cash_out"] = declared
        refi = legacy.get("scenarios", {}).get("refinance", [])
        cash_out_opts = [o for o in refi if o.get("cash_out", 0) > 0]
        self.assertLess(len(cash_out_opts), 2)
        active = {a.id for a in model_fidelity.active_approximations(legacy)}
        self.assertNotIn("deployment_lag_multi_option_bleed", active)

    def test_bleed_caveat_fires_when_booked_differs_from_declared(self):
        """The declaring option's cash_out differs from the booked cash_out
        (the base config books cash_out 0 while the option declares a lag on a
        $50k advance) -- the lag is priced on a lump the option never stated
        it for, so the bleed caveat fires even with a single cash-out option."""
        doc = _example_doc_with_lag(months=4, parking_rate=0.0)
        legacy = ic.to_internal_config(doc)
        # Base config: cash_out 0 (no refinance booked) != declaring 50_000.
        self.assertNotEqual(
            legacy["property"].get("cash_out", 0),
            legacy["property"]["deployment_lag_declared_cash_out"])
        active = {a.id for a in model_fidelity.active_approximations(legacy)}
        self.assertIn("deployment_lag_multi_option_bleed", active)

    def test_linear_carry_caveat_fires_when_lag_declared(self):
        """Finding #5: the linear (non-compounded) carry window is disclosed
        (Direction.UNDERSTATES) whenever a lag is declared -- the true
        compounded carry over the window is slightly larger than the linear
        estimate, so the reported carry understates the cost."""
        doc = _example_doc_with_lag(months=6, parking_rate=0.0)
        legacy = ic.to_internal_config(doc)
        active = {a.id for a in model_fidelity.active_approximations(legacy)}
        self.assertIn("deployment_lag_carry_is_linear", active)

    def test_no_caveats_when_no_lag_declared(self):
        """A household that declares no lag carries neither caveat -- the
        no-lag path is byte-identical and has no approximation to disclose."""
        doc = _two_generation_subset(_load_example())
        for opt in doc["decisions"]["mortgage"]["refinance_options"]:
            opt.pop("deployment_lag_months", None)
            opt.pop("parking_rate", None)
        legacy = ic.to_internal_config(doc)
        active = {a.id for a in model_fidelity.active_approximations(legacy)}
        self.assertNotIn("deployment_lag_multi_option_bleed", active)
        self.assertNotIn("deployment_lag_carry_is_linear", active)


class DeploymentLagPredicateTest(unittest.TestCase):
    """Unit tests of the model_fidelity predicate functions (DP#11: a unit test
    of a pure predicate calling that predicate directly is correct). Covers
    the defensive branches an end-to-end run through the contract path never
    reaches (a non-dict cfg, a lag declared on an in-memory config with no
    scenarios block, and a lag with no carried declared_cash_out)."""

    def test_non_dict_cfg_carries_no_lag_caveat(self):
        ctx = model_fidelity.FidelityContext(cfg=None)
        self.assertFalse(model_fidelity._deployment_lag_declared(ctx))
        self.assertFalse(model_fidelity._deployment_lag_multi_option_bleed(ctx))

    def test_non_dict_property_carries_no_lag_caveat(self):
        ctx = model_fidelity.FidelityContext(cfg={'property': 'not-a-dict'})
        self.assertFalse(model_fidelity._deployment_lag_declared(ctx))

    def test_no_refinance_list_fires_when_booked_differs(self):
        """An in-memory config with a declared lag + declared_cash_out but no
        scenarios block: the booked cash_out (0) differs from the declaring
        option's (50k), so the bleed bites (covers the empty-refinance-list
        branch)."""
        ctx = model_fidelity.FidelityContext(cfg={
            'property': {'deployment_lag_months': 6,
                         'deployment_lag_declared_cash_out': 50_000}})
        self.assertTrue(model_fidelity._deployment_lag_multi_option_bleed(ctx))

    def test_declared_cash_out_absent_discloses_fail_open(self):
        """An in-memory config that set the lag directly on property without
        the contract path carries no deployment_lag_declared_cash_out -- DP#32
        fail open: disclose rather than silently exonerate."""
        ctx = model_fidelity.FidelityContext(cfg={
            'property': {'deployment_lag_months': 6}})
        self.assertTrue(model_fidelity._deployment_lag_multi_option_bleed(ctx))


if __name__ == "__main__":
    unittest.main()