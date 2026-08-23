#!/usr/bin/env python3
"""Issue #1075 (optimizer half): the 3-tranche readvanceable structure is
SWEPT and GENERATED, not just expressed.

The data-model half (commit 62f91b6) made the adapter consume N kind=mortgage
tranches sharing one charge -- balances sum, the rate blends balance-weighted,
each tranche's ``deductible`` flag surfaces as
``deductible_mortgage_balance`` + EXACT ``deductible_mortgage_interest``, and
``cash_back`` credits at year 0. This file pins the OPTIMIZER half:

  (1) ``decisions.mortgage.structure_options[].tranches`` -- a structure may
      declare the 3-tranche split (house mortgage, deductible investment
      mortgage, readvanceable line) as an additive opt-in over the #687 share
      form, which stays byte-identical;
  (2) the s.20(1)(c) pricing consumes the EXACT deductible interest (sum of
      each flagged tranche's balance x ITS OWN rate), never the blended-rate
      product, when a structure carries tranches at different rates;
  (3) the optimizer SWEEPS BOTH axes -- the HOUSE amount from its sweep floor
      (``min_house_floor``, defaulting to 60% of the charge) UP to the charge,
      and the surplus split between the investment tranche and the line at
      10% steps -- and RETURNS the optimal amounts, printed by the #687
      report. The house tranche's ``min_amount`` is the CASH-BACK THRESHOLD,
      not a floor: the sweep may put the house mortgage BELOW it, and a
      declared cash-back conditional on the house amount (``cash_back.
      min_house_amount``) is then FORGONE -- the incentive-boundary trade-off
      this file pins;
  (4) an invalid tranche spec is refused loudly (DP#32) -- at schema level
      and at contract-load level;
  (5) a contract without tranche declarations keeps the exact #687
      A/B/C/D mapping and ranking output.

DP#4/DP#15: every figure below is fabricated and round; every name is
role-based. No real household's data appears here.
"""
import copy
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
import optimize
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import (
    ChargeLimitExceededError, SimulationConfig, YearResult,
    apply_sourcing_overlay, apply_structure_overlay,
)
from test_dp_income_scenario_reaches_engine import _two_generation_subset
from trajectory_invariants import assert_invariant
import contract_errors
import contract_schema

# ── The fabricated household (DP#15) ────────────────────────────────────────
# House 900,000 -> 80% charge = 720,000. The house mortgage is swept from its
# floor -- 60% of the charge, 432,000, when no min_house_floor is declared --
# UP to the 720,000 charge; the $600,000 ``min_amount`` is the CASH-BACK
# THRESHOLD (a fabricated $1,200 cash-back program prices a $600k+ house
# tranche), NOT a floor: the sweep may go below it and forgo the incentive.
HOUSE_VALUE = 900_000
CHARGE = 720_000
HOUSE_MIN = 600_000          # the CASH-BACK THRESHOLD, not the sweep floor
HOUSE_FLOOR = int(CHARGE * 0.6)  # 432,000 -- the default sweep floor
CASH_BACK_AMOUNT = 1200      # fabricated round number (DP#15)
HOUSE_RATE = 0.045   # amortizing house mortgage
INVEST_RATE = 0.065  # deductible investment tranche -- DEARER than the house
LINE_RATE = 0.05     # revolving line
SURPLUS = CHARGE - HOUSE_MIN  # 120,000

ALL_IN_ONE = {
    "id": "all_in_one",
    "label": "All-in-one 3 tranches",
    "tranches": [
        {"kind": "house", "min_amount": HOUSE_MIN, "rate": HOUSE_RATE},
        {"kind": "investment", "deductible": True, "rate": INVEST_RATE},
        {"kind": "line"},
    ],
    "revolving_rate": LINE_RATE,
    "revolving_rate_type": "variable",
    "readvanceable": True,
}


def _doc(structure_options=None, house_mortgage=HOUSE_MIN,
         heloc_room=CHARGE - HOUSE_MIN, cash_back=None):
    """The shipped example trimmed to the couple+children, re-based to the
    #1075 household: house 900,000 (80% charge = 720,000), house mortgage
    ``house_mortgage`` (default 600,000 -- the cash-back threshold), HELOC
    room ``heloc_room`` (default 120,000 so the charge is exactly 720,000),
    ONE refinance basis (no cash-out) and ONE income scenario.

    ``cash_back``, when given, is attached to the mortgage liability (a
    CONDITIONAL origination cash-back declaring ``min_house_amount`` -- the
    sweep credits it only for a house tranche at/above the threshold). The
    default is None: most fixtures here are about the split mechanics, not
    the incentive, and a contract without a declared cash-back must stay
    byte-identical to pre-#1075.

    The share-form tests pass a SMALLER drawn mortgage + room (450,000 +
    110,000 = 560,000 charge): the #687 share machinery carries the drawn
    position through unchanged and carves the line from the charge, so a
    drawn mortgage of 600,000 beside a 30% line (216,000) would exceed the
    charge once drawn (600k + 216k > 720k -- the #851 drawn/room separation
    keeps the undrawn case safe but the drawn case is capped only by
    margin_available). #687's own fixture uses the same headroom technique;
    the tranched form never needs it (its amounts partition the charge by
    construction)."""
    with open(contract_schema.EXAMPLE_PATH) as fh:
        doc = _two_generation_subset(json.load(fh))
    for prop in doc["properties"]:
        if prop["kind"] == "principal":
            prop["value"]["amount"] = HOUSE_VALUE
    for liab in doc["liabilities"]:
        if liab["kind"] == "mortgage":
            liab["balance"]["amount"] = house_mortgage
            liab["rate"] = HOUSE_RATE
            liab["amortization"]["payment_monthly"] = 3300
            if cash_back is not None:
                liab["cash_back"] = cash_back
        elif liab["kind"] == "heloc":
            liab["limit"] = heloc_room
            liab["balance"]["amount"] = 0
            liab["rate"] = LINE_RATE
    doc["decisions"]["income"] = [{"id": "stay", "label": "Stay at current jobs",
                                   "overrides": []}]
    doc["decisions"]["mortgage"]["refinance_options"] = [{
        "id": "no_refi", "label": "No refinance", "cash_out": 0,
        "ltv": 0.0, "amortization_years": 18}]
    doc["decisions"]["mortgage"]["structure_options"] = (
        structure_options if structure_options is not None else [ALL_IN_ONE])
    return doc


def _point(investment, house=HOUSE_MIN, line=None):
    """A concrete sweep point (the shape optimize's sweep enumerates)."""
    if line is None:
        line = CHARGE - house - investment
    return {"house": house, "investment": investment, "line": line}


# ============================================================================
# (1) A 3-tranche structure option loads and applies
# ============================================================================

class TestTranchedStructureLoadsAndApplies(unittest.TestCase):
    def test_the_structure_option_validates_and_maps(self):
        doc = _doc()
        contract_schema.validate_contract(doc)  # must not raise (schema: oneOf tranches)
        cfg = ic.to_internal_config(doc)
        mapped = cfg["property"]["structure_options"][0]
        self.assertEqual(mapped["id"], "all_in_one")
        self.assertNotIn("revolving_share", mapped)
        kinds = [t["kind"] for t in mapped["tranches"]]
        self.assertEqual(kinds, ["house", "investment", "line"])
        house_t = mapped["tranches"][0]
        self.assertEqual(house_t["min_amount"], HOUSE_MIN)
        self.assertTrue(mapped["tranches"][1]["deductible"])
        self.assertTrue(mapped["readvanceable"])

    def test_a_sweep_point_applies_the_split(self):
        """The concrete split lands on the property config exactly: drawn
        mortgage = house + investment, undrawn room = the line, and the
        deductible tranche's EXACT interest (balance x ITS OWN 6.5% rate) --
        never 70,000 x the blended rate, which drags the house tranche's
        4.5% in and understates the s.20(1)(c) deduction."""
        base = {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}
        structure = copy.deepcopy(ALL_IN_ONE)
        structure["tranche_amounts"] = _point(investment=70_000)
        out = apply_structure_overlay(base, structure)
        self.assertAlmostEqual(out["mortgage_balance"], HOUSE_MIN + 70_000)
        # Undrawn room = the split's line (the surplus left after the house
        # floor and the investment tranche), not the investment amount.
        self.assertAlmostEqual(out["margin_available"], CHARGE - HOUSE_MIN - 70_000)
        self.assertEqual(out["deductible_mortgage_balance"], 70_000)
        self.assertEqual(out["deductible_mortgage_interest"], 70_000 * INVEST_RATE)
        blended = (HOUSE_MIN * HOUSE_RATE + 70_000 * INVEST_RATE) / (HOUSE_MIN + 70_000)
        self.assertAlmostEqual(out["mortgage_rate"], blended)
        self.assertNotAlmostEqual(out["deductible_mortgage_interest"],
                                  70_000 * out["mortgage_rate"])
        self.assertEqual(out["heloc_rate"], LINE_RATE)
        self.assertTrue(out["heloc_readvance"])

    def test_an_undeductible_split_carries_no_deductible_keys(self):
        """DP#32: a sweep point whose investment tranche is not flagged
        deductible (or is 0) adds no deductible_mortgage_balance/interest
        keys -- never a fabricated zero."""
        base = {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}
        structure = {
            "id": "plain", "label": "no deductible tranche",
            "tranches": [{"kind": "house", "min_amount": HOUSE_MIN},
                         {"kind": "investment"},
                         {"kind": "line"}],
            "revolving_rate": LINE_RATE, "revolving_rate_type": "variable",
            "tranche_amounts": _point(investment=70_000),
        }
        out = apply_structure_overlay(base, structure)
        self.assertNotIn("deductible_mortgage_balance", out)
        self.assertNotIn("deductible_mortgage_interest", out)


# ============================================================================
# (2) The EXACT deductible interest drives the s.20(1)(c) leg, per-tranche
# ============================================================================

def _tranched_run(investment, line, use_readvanceable=False, lump_sum=0.0,
                  non_reg_balance=400_000):
    """Run the engine on the #1075 household structured as house 600,000 +
    investment tranche + line. The property block is produced by the REAL
    overlay (apply_structure_overlay) so this test exercises the exact path
    the sweep will feed the engine (DP#11 -- not a hand-built state)."""
    base = {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
            "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}
    structure = copy.deepcopy(ALL_IN_ONE)
    structure["tranche_amounts"] = _point(investment=investment, line=line)
    prop = apply_structure_overlay(base, structure)
    cfg = {
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
             'retirement_age': 65,
             # No registered room: every borrowed dollar is an income-producing
             # non-registered use (the s.20(1)(c) purpose test).
             'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
        ], 'children': []},
        'accounts': {'rrsp_annual_max': 0},
        'assumptions': {'start_year': 2026, 'horizon_age': 60,
                        'investment_return': 0.06, 'salary_growth': 0.0,
                        'inflation': 0.0, 'frozen_brackets': True},
        'portfolio': {'accounts': {'non_reg': {
            'balance': non_reg_balance, 'cost_basis': non_reg_balance,
            'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
            'yield': {'eligible_dividends': 0.02, 'interest': 0.0}}}},
        'property': prop,
        'household_budget': {'annual_living_costs': 60_000},
    }
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=use_readvanceable,
                           deduct_later=False, lump_sum=lump_sum)
    return sim.run()


def _declared_liabilities_run(investment=80_000, lump_sum=100_000):
    """Run the engine on a household whose DEDUCTIBLE investment tranche is
    DECLARED IN LIABILITIES (a kind=mortgage liability with deductible: true,
    sharing the principal's collateral), NOT carved out by a structure
    overlay -- the adapter's ``_aggregate_mortgage_facility`` sums the
    tranches and surfaces ``deductible_mortgage_balance`` /
    ``deductible_mortgage_interest`` (the flagged tranche's balance x ITS OWN
    rate) on the config, which is exactly the seam the s.20(1)(c) leg's
    leg-2 exact-interest path consumes. ``lump_sum`` is a NEW year-0 cash-out
    advance: with no declared deductible keys the traced path would price the
    blended product against it, and this fixture pins that the keys win.

    Returns ``(cfg, results)`` -- the internal config (so the assertions can
    read the mapped deductible keys and the blended mortgage_rate) and the
    engine's trajectory.
    """
    doc = _doc()
    template = copy.deepcopy(next(
        l for l in doc["liabilities"] if l["kind"] == "mortgage"))
    invest = copy.deepcopy(template)
    invest["id"] = "mortgage_invest"
    invest["balance"] = {"amount": investment,
                          "as_of": template["balance"]["as_of"]}
    invest["rate"] = INVEST_RATE
    invest["deductible"] = True
    doc["liabilities"] = (
        [l for l in doc["liabilities"] if l["kind"] != "mortgage"]
        + [template, invest])
    # Keep the registered charge at 720,000: house 600,000 + investment
    # tranche + 40,000 of undrawn room (a larger total would breach the
    # 80% LTV ceiling at load).
    for liab in doc["liabilities"]:
        if liab["kind"] == "heloc":
            liab["limit"] = CHARGE - HOUSE_MIN - investment
    contract_schema.validate_contract(doc)
    cfg = ic.to_internal_config(doc)
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False,
                           lump_sum=lump_sum)
    return cfg, sim.run()


class TestExactDeductibleInterestDrivesTheSmLeg(unittest.TestCase):
    def test_the_leg_prices_the_tranches_own_rate_not_the_blend(self):
        """house 600,000 @ 4.5% + investment 80,000 @ 6.5%: the advance leg's
        year-0 deductible interest must be exactly 80,000 x 6.5% = 5,200 --
        the s.20(1)(c) deduction the taxpayer can claim -- NOT 80,000 times
        the balance-weighted blended rate (~4.74%), which blends the house
        tranche's cheaper rate in and understates the deduction."""
        results = _tranched_run(investment=80_000, line=40_000)
        exact = 80_000 * INVEST_RATE
        blended = (HOUSE_MIN * HOUSE_RATE + 80_000 * INVEST_RATE) / (HOUSE_MIN + 80_000)
        self.assertEqual(results[0].advance_deductible_interest, exact)
        self.assertNotAlmostEqual(results[0].advance_deductible_interest,
                                  80_000 * blended)
        self.assertNotAlmostEqual(results[0].advance_deductible_interest,
                                  results[0].advance_deductible_balance
                                  * blended)

    def test_the_exact_interest_amortizes_with_the_mortgage(self):
        """The exact path preserves #850's erosion: the deductible balance
        (and hence the deduction) falls as the mortgage amortizes -- the
        tranche amortizes pro rata on the single schedule."""
        results = _tranched_run(investment=80_000, line=40_000)
        balances = [r.advance_deductible_balance for r in results]
        self.assertGreater(balances[0], 0)
        for earlier, later in zip(balances, balances[1:]):
            if later > 0:
                # Strict erosion for every year the tranche still has a
                # balance; once the mortgage is fully paid the balance stays
                # at exactly 0 (the payoff year can land on either side of
                # the horizon boundary, so the paid-off tail must not be read
                # as a violation of the erosion).
                self.assertLess(later, earlier,
                                f"the deductible balance must erode: {earlier:,.0f} "
                                f"-> {later:,.0f} (#849)")

    def test_a_declared_deductible_liability_prices_the_own_rate_with_a_cash_out(self):
        """apply_sm_interest leg 2 activates for ANY config carrying the
        deductible keys -- not only the structure-overlay path but the
        DECLARED-LIABILITIES path (kind=mortgage + deductible: true, summed
        by the adapter) -- and it wins even when a NEW year-0 cash-out
        advance is present: the year-0 deductible interest is exactly
        80,000 x 6.5% = 5,200 (the tranche's OWN rate, what s.20(1)(c)
        lets the taxpayer claim), never 80,000 times the blended
        balance-weighted rate and never the whole mortgage's blended
        interest the traced path would price against the new advance --
        and afterwards the balance/interest fall pro rata with the
        amortizing principal (the #849 erosion, preserved)."""
        cfg, results = _declared_liabilities_run(investment=80_000,
                                                 lump_sum=100_000)
        # The adapter surfaced the tranche's facts from the DECLARED
        # liability: balance 600,000 + 80,000, exact own-rate interest.
        self.assertEqual(cfg["property"]["deductible_mortgage_balance"], 80_000)
        self.assertEqual(cfg["property"]["deductible_mortgage_interest"],
                         80_000 * INVEST_RATE)
        blended = cfg["property"]["mortgage_rate"]
        self.assertNotAlmostEqual(blended, INVEST_RATE)
        # A NEW cash-out advance is present (the traced leg would price it).
        self.assertGreater(results[0].mortgage_interest, 0)
        # Own rate, not the blend.
        self.assertEqual(results[0].advance_deductible_interest,
                         80_000 * INVEST_RATE)
        self.assertNotAlmostEqual(results[0].advance_deductible_interest,
                                  results[0].advance_deductible_balance
                                  * blended)
        self.assertNotAlmostEqual(results[0].advance_deductible_interest,
                                  results[0].mortgage_interest)
        self.assertEqual(results[0].advance_deductible_balance, 80_000)
        # Amortization-scaled semantics: the deductible balance/interest fall
        # pro rata with the principal (year N's opening = year N-1's end).
        scale = results[0].mortgage_balance / cfg["property"]["mortgage_balance"]
        self.assertAlmostEqual(results[1].advance_deductible_balance,
                               80_000 * scale)
        self.assertAlmostEqual(results[1].advance_deductible_interest,
                               80_000 * INVEST_RATE * scale)
        balances = [r.advance_deductible_balance for r in results]
        for earlier, later in zip(balances, balances[1:]):
            self.assertLessEqual(later, earlier,
                                 "the declared tranche's deductible balance "
                                 "must never grow (#849)")
        # Year-0 -> year-1 strict erosion is pinned exactly by the scaled
        # assertions above (scale < 1); the balance then runs down to 0 with
        # the amortizing principal instead of eroding below it.
        self.assertLess(balances[1], balances[0])

    def test_the_deduction_reaches_the_ranking(self):
        """DP#32/#850: a deduction the ranking cannot see is a trade-off the
        engine did not compute -- the traced savings must flow (the household
        has non-registered income, so the shared QC cap does not strand it)."""
        results = _tranched_run(investment=80_000, line=40_000)
        saved = sum(r.traced_borrowing_tax_savings for r in results)
        self.assertGreater(saved, 0)
        self.assertGreater(sum(r.advance_deductible_interest for r in results), 0)

    def test_a_zero_investment_tranche_prices_no_exact_deduction(self):
        """The flip side (DP#32): a split with no deductible tranche takes the
        pre-#1075 traced path -- with no lump sum and no SM line, nothing is
        deducted, exactly as before this feature."""
        results = _tranched_run(investment=0.0, line=120_000)
        self.assertTrue(all(r.advance_deductible_interest == 0.0 for r in results))
        self.assertTrue(all(r.traced_borrowing_tax_savings == 0.0 for r in results))


# ============================================================================
# (3) The optimizer SWEEPS the 3-tranche split and returns the amounts
# ============================================================================

class TestOptimizerSweepGeneratesTrancheAmounts(unittest.TestCase):
    """The sweep is a full structure exploration -- computed ONCE in
    setUpClass and reused across the assertions, same as #687's own
    exploration test."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = ic.to_internal_config(_doc())
        cls.cells = optimize.structure_refinance_cells(cls.cfg)
        cls.results = optimize.run_mortgage_structure_exploration(cls.cfg)

    def test_the_sweep_grid_partitions_house_and_split(self):
        """The template expands into the FULL 2-D grid: 8 house amounts
        (from the 60%-of-charge default floor 432,000 UP to the 720,000
        charge, 50k steps -- the 25k grid plus the 600k cash-back anchor
        would exceed the cell cap, so the sweep coarsens, keeping the
        charge and the threshold ON the grid) x 11 split fractions (0..100%
        in 10% steps). Every cell sums to the charge by construction (a
        partition, DP#18); the house never exceeds the charge; and the
        surplus (charge - house) is what the 10% ladder splits -- so a 10%
        step (e.g. investment 12,000 of the 120,000 surplus at house 600k)
        is reachable."""
        self.assertEqual(len(self.cells), 88)
        houses = sorted(
            round(c['structure']['tranche_amounts']['house']) for c in self.cells)
        self.assertEqual(
            list(dict.fromkeys(houses)),
            [432_000, 482_000, 532_000, 582_000, 600_000,
             632_000, 682_000, 720_000])
        for c in self.cells:
            a = c['structure']['tranche_amounts']
            self.assertIsNotNone(c['cfg'], f"cell refused: {c['refusal']}")
            self.assertGreaterEqual(a['house'], HOUSE_FLOOR)
            self.assertLessEqual(a['house'], CHARGE)
            self.assertAlmostEqual(a['house'] + a['investment'] + a['line'], CHARGE)
        # At the 600k threshold point the surplus is 120,000, split at 10%
        # steps: 0, 12k, 24k, ..., 120k.
        at_threshold = [c['structure']['tranche_amounts'] for c in self.cells
                        if abs(c['structure']['tranche_amounts']['house'] - HOUSE_MIN) < 1]
        self.assertEqual(len(at_threshold), 11)
        investments = sorted(round(a['investment']) for a in at_threshold)
        self.assertEqual(investments, [0, 12_000, 24_000, 36_000, 48_000, 60_000,
                                       72_000, 84_000, 96_000, 108_000, 120_000])

    def test_the_winning_split_is_returned_and_sums_to_the_charge(self):
        """The deliverable: the optimizer GENERATES the amounts -- the winning
        row (per basis x income scenario) carries its own tranche_amounts,
        with house within the swept range (>= the 432k default floor -- it
        MAY be below the $600k cash-back threshold, that is the point), the
        three tranches summing to the $720k charge, and a non-negative
        line."""
        self.assertTrue(self.results)
        winners = optimize.winners_by_structure_scenario(self.results)
        tranche_winners = [w for w in winners if w.get('tranche_amounts')]
        self.assertTrue(tranche_winners)
        for w in tranche_winners:
            a = w['tranche_amounts']
            self.assertGreaterEqual(a['house'], HOUSE_FLOOR)
            self.assertLessEqual(a['house'], CHARGE)
            self.assertGreaterEqual(a['investment'], 0)
            self.assertGreaterEqual(a['line'], 0)
            self.assertAlmostEqual(a['house'] + a['investment'] + a['line'], CHARGE)

    def test_every_result_row_is_tagged_with_its_sweep_point(self):
        """DP#9: the reported amounts are read off the rows that produced
        them -- every result row carries the exact split it was scored at,
        so the printed optimal split cannot disagree with the numbers."""
        tagged = [r for r in self.results if r.get('structure_tranche_amounts')]
        self.assertEqual(len(tagged), len(self.results))
        for r in tagged:
            a = r['structure_tranche_amounts']
            self.assertAlmostEqual(
                a['house'] + a['investment'] + a['line'], CHARGE, places=0)

    def test_charge_invariants_hold_every_year(self):
        """The #681 trajectory invariants hold for every sweep point the
        exploration ran -- the sweep cannot be used to launder debt past the
        registered charge or the revolving-only ceiling."""
        house_value = self.cfg['property']['house_value']
        for r in self.results:
            year_results = [YearResult(**yr) for yr in r['year_by_year']]
            assert_invariant('total_secured_debt_within_charge_limit',
                             year_results, {'house_value': house_value})
            assert_invariant('heloc_within_revolving_limit',
                             year_results, {'house_value': house_value})

    def test_the_report_prints_the_optimal_split(self):
        """Task C's console deliverable: the #687 structure report names the
        optimal per-tranche amounts -- including the WINNING HOUSE amount
        (which the sweep now varies, so a fixed $600k expectation would be
        wrong) -- and the strategy that produced them."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(self.results, cells=self.cells)
        out = buf.getvalue()
        self.assertIn('OPTIMAL 3-TRANCHE SPLIT', out)
        self.assertRegex(out, r'house \$[0-9,]+,000')
        self.assertIn('investment', out)
        self.assertIn('line', out)


# ============================================================================
# (3b) The cash-back is CONDITIONAL on the swept house amount (optimizer
# half): a declared ``cash_back.min_house_amount`` is credited only for a
# house tranche at/above the threshold; below it the sweep FORGOES the
# incentive -- the exact trade-off issue #1075 asks the optimizer to price.
# ============================================================================

CONDITIONAL_CASH_BACK = {"amount": CASH_BACK_AMOUNT, "clawback_rate": 0.5,
                         "term_years": 5, "min_house_amount": HOUSE_MIN}


class TestCashBackConditionalOnHouseAmount(unittest.TestCase):
    """The sweep explores house amounts BELOW the cash-back threshold (the
    $600k min_amount is the threshold, not a floor) and the origination
    inflow is withheld there; at/above the threshold it is credited. The
    verdict travels ON the sweep point and the winning row (DP#9), and the
    engine actually sees the difference at year 0."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = ic.to_internal_config(_doc(cash_back=CONDITIONAL_CASH_BACK))
        cls.cells = optimize.structure_refinance_cells(cls.cfg)
        cls.results = optimize.run_mortgage_structure_exploration(cls.cfg)

    def test_below_the_threshold_the_cash_back_is_forgone(self):
        """The sweep EXPLORES below the threshold (the whole point: house
        582,000 < 600,000 is on the grid), and every such point is scored
        WITHOUT the origination inflow -- the cell config's cash_flows carry
        no conditional cash-back, and the point's own verdict says FORGONE."""
        below = [c for c in self.cells
                 if c['structure']['tranche_amounts']['house'] < HOUSE_MIN]
        self.assertTrue(below)
        self.assertTrue(all(c['structure']['cash_back_credited'] is False
                            for c in below))
        for c in below:
            self.assertIsNotNone(c['cfg'], f"cell refused: {c['refusal']}")
            self.assertFalse(any(cf.get('min_house_amount') is not None
                                 for cf in c['cfg']['cash_flows']),
                             "a forgone point must not receive the inflow")

    def test_at_or_above_the_threshold_the_cash_back_is_credited(self):
        """At the 600k boundary and above, the inflow IS credited -- the cell
        config keeps the conditional origination cash-flow (the adapter
        created it from the declared cash_back), and the point's verdict
        says CREDITED."""
        at_or_above = [c for c in self.cells
                       if c['structure']['tranche_amounts']['house'] >= HOUSE_MIN]
        self.assertTrue(at_or_above)
        self.assertTrue(all(c['structure']['cash_back_credited'] is True
                            for c in at_or_above))
        for c in at_or_above:
            self.assertIsNotNone(c['cfg'], f"cell refused: {c['refusal']}")
            self.assertTrue(any(cf.get('min_house_amount') == HOUSE_MIN
                                and cf['amount'] == CASH_BACK_AMOUNT
                                for cf in c['cfg']['cash_flows']),
                            "an above-threshold point must keep the inflow")

    def test_the_credit_reaches_the_engine_only_above_the_threshold(self):
        """Not config-only prose: run the engine on one forgone and one
        credited sweep point -- the credited cell's year-0 annual_savings is
        exactly $1,200 higher (the inflow is a one-time year-0 credit), and
        later years match."""
        def _year_zero_savings(cell):
            from simulation import FamilySimulation
            from simulation_config import SimulationConfig
            sim_cfg = SimulationConfig.from_dict(cell['cfg'])
            sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg))
            return sim.run()[0].annual_savings

        forgone = next(c for c in self.cells if not c['structure']['cash_back_credited'])
        credited = next(c for c in self.cells if c['structure']['cash_back_credited'])
        # The splits differ (different house amounts), but the inflow must
        # account for exactly the $1,200 gap between the two year-0 savings
        # lines. Rather than assert the absolute figures, compare each cell
        # against the SAME split with the inflow stripped/added by hand.
        self.assertAlmostEqual(_year_zero_savings(credited)
                               - _year_zero_savings(forgone), 1200.0, places=0)

    def test_the_verdict_rides_the_winning_row_into_the_report(self):
        """The report states the verdict beside the winning split (DP#9: the
        printed verdict is the condition the printed net benefit was scored
        under)."""
        winners = optimize.winners_by_structure_scenario(self.results)
        tranche_winners = [w for w in winners if w.get('tranche_amounts')]
        self.assertTrue(tranche_winners)
        for w in tranche_winners:
            self.assertIsNotNone(w.get('cash_back_credited'))
            self.assertEqual(w['cash_back_amount'], CASH_BACK_AMOUNT)
            self.assertEqual(w['cash_back_threshold'], HOUSE_MIN)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(self.results, cells=self.cells)
        out = buf.getvalue()
        self.assertIn('OPTIMAL 3-TRANCHE SPLIT', out)
        self.assertIn('cash-back $1,200', out)
        self.assertRegex(out, r'CREDITED|FORGONE')

    def test_the_report_states_the_credited_verdict_too(self):
        """The CREDITED branch of the report's cash-back note is printed when
        the winning split sits at/above the threshold. (The fabricated
        household's REAL sweep wins below it -- house 532,000, FORGONE -- so
        this print branch is pinned with a minimal winning row, the same
        technique the #687 print tests use; the cell-level credit on the
        above-threshold points is pinned by the engine test above.)"""
        row = {
            'structure_id': 'all_in_one',
            'structure_label': 'All-in-one 3 tranches',
            'income_scenario_id': 'stay',
            'income_scenario_label': 'Stay at current jobs',
            'strategy': 'readvance_priority', 'deduct_later': False,
            'net_benefit': 7_500_000,
            'solvency': {'engaged': True, 'ruined': False},
            'structure_revolving_share': None,
            'structure_readvanceable': True,
            'structure_tranche_amounts': {'house': 600_000, 'investment': 60_000,
                                          'line': 60_000},
            'structure_cash_back_amount': CASH_BACK_AMOUNT,
            'structure_cash_back_threshold': HOUSE_MIN,
            'structure_cash_back_credited': True,
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report([row])
        out = buf.getvalue()
        self.assertIn('OPTIMAL 3-TRANCHE SPLIT', out)
        self.assertIn('cash-back $1,200 CREDITED', out)
        self.assertIn('house $600,000 >= the $600,000 threshold', out)

    def test_an_unconditional_cash_back_is_never_withheld(self):
        """DP#13/DP#32: a cash_back that declares NO min_house_amount keeps
        the pre-#1075 behaviour -- credited at origination at EVERY sweep
        point: the origination flow carries no condition marker, so the cell
        gate never strips it, and no sweep point ever reports the credit as
        forgone (there is nothing to forgo -- the flag stays None, and the
        flow stays in every cell's config)."""
        cfg = ic.to_internal_config(_doc(
            cash_back={"amount": CASH_BACK_AMOUNT, "clawback_rate": 0.5,
                       "term_years": 5}))
        cells = optimize.structure_refinance_cells(cfg)
        self.assertTrue(cells)
        for c in cells:
            self.assertIsNotNone(c['cfg'])
            self.assertFalse(c['structure']['cash_back_credited'] is False)
            self.assertTrue(any(
                cf['amount'] == CASH_BACK_AMOUNT
                and cf.get('min_house_amount') is None
                for cf in c['cfg']['cash_flows']),
                "an unconditional credit must survive every sweep point")

    def test_the_house_grid_anchors_on_the_threshold(self):
        """The boundary point -- house EXACTLY at the $600k threshold -- is
        on the grid (the sweep must evaluate the exact point where the
        incentive flips, not merely straddle it), and it is scored as
        credited."""
        boundary = [c for c in self.cells
                    if abs(c['structure']['tranche_amounts']['house']
                           - HOUSE_MIN) < 1]
        self.assertTrue(boundary)
        self.assertTrue(all(c['structure']['cash_back_credited'] for c in boundary))

    def test_the_house_grid_anchors_on_the_strictest_threshold(self):
        """When the declared cash-back condition is STRICTER than the
        structure's own min_amount (the credit's threshold wins -- the
        strictest condition decides where the incentive flips), the grid
        anchors there: house 650k is on the grid, everything below is
        forgone, and the 600k min_amount point does not count as credited."""
        doc = _doc(cash_back=dict(CONDITIONAL_CASH_BACK, min_house_amount=650_000))
        cfg = ic.to_internal_config(doc)
        cells = optimize.structure_refinance_cells(cfg)
        houses = sorted({round(c['structure']['tranche_amounts']['house'])
                         for c in cells})
        self.assertEqual(houses[0], HOUSE_FLOOR)
        self.assertIn(650_000, houses, "the strictest threshold must anchor")
        self.assertNotIn(600_000, houses)
        for c in cells:
            if c['structure']['tranche_amounts']['house'] < 650_000:
                self.assertFalse(c['structure']['cash_back_credited'])
                self.assertFalse(any(cf.get('min_house_amount') is not None
                                     for cf in c['cfg']['cash_flows']))
            else:
                self.assertTrue(c['structure']['cash_back_credited'])

    def test_a_structure_with_no_house_tranche_pins_house_at_zero(self):
        """A degenerate tranches form with no house tranche keeps the
        pre-#1075 behaviour: house pinned at 0, the whole charge split
        between the investment tranche and the line (the sweep's house
        dimension exists for the house tranche; a structure that never
        declared one does not sprout a swept house amount, DP#13). Built
        on a 500,000 charge -- the full-charge line at house 0 must stay
        under the 65% revolving ceiling to load."""
        cfg = ic.to_internal_config(_doc(
            structure_options=[{
                "id": "no_house", "label": "No house tranche",
                "tranches": [{"kind": "investment", "deductible": True,
                               "rate": INVEST_RATE},
                              {"kind": "line"}],
                "revolving_rate": LINE_RATE, "revolving_rate_type": "variable",
                "readvanceable": True,
            }],
            house_mortgage=400_000, heloc_room=80_000))
        cells = optimize.structure_refinance_cells(cfg)
        self.assertTrue(cells)
        self.assertTrue(all(c['cfg'] is not None for c in cells))
        for c in cells:
            a = c['structure']['tranche_amounts']
            self.assertEqual(a['house'], 0)
            self.assertAlmostEqual(a['investment'] + a['line'], 480_000)

    def test_charge_conservation_holds_everywhere(self):
        """Every cell -- credited or forgone -- still partitions the charge
        exactly, and the house never exceeds it: the conditional credit
        changes the CASH-FLOW, never the debt geometry (DP#18)."""
        for c in self.cells:
            a = c['structure']['tranche_amounts']
            self.assertLessEqual(a['house'], CHARGE)
            self.assertAlmostEqual(a['house'] + a['investment'] + a['line'], CHARGE)


# ============================================================================
# (4) Invalid tranche specs are refused loudly (DP#32)
# ============================================================================

class TestInvalidTrancheSpecsRefusedLoudly(unittest.TestCase):
    def _load(self, tranches, **structure_overrides):
        structure = {"id": "bad", "label": "bad", "tranches": tranches}
        structure.update(structure_overrides)
        doc = _doc([structure])
        return ic.to_internal_config(doc)

    def test_overlapping_kinds_are_refused(self):
        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            self._load([{"kind": "house"}, {"kind": "house", "min_amount": 100},
                        {"kind": "line"}],
                       revolving_rate=LINE_RATE, revolving_rate_type="variable")
        self.assertIn("TWO tranches of kind", str(ctx.exception))

    def test_min_amount_is_rejected_on_a_non_house_tranche(self):
        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            self._load([{"kind": "house"}, {"kind": "investment", "min_amount": 10_000}],
                       revolving_rate=LINE_RATE, revolving_rate_type="variable")
        self.assertIn("only the 'house' tranche", str(ctx.exception))

    def test_min_house_floor_is_rejected_on_a_non_house_tranche(self):
        """The sweep floor is a house-tranche-only declaration -- putting it
        on the line (whose amount is the residual of the charge, never a
        swept lower bound) is refused loudly (DP#32)."""
        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            self._load([{"kind": "house"}, {"kind": "line", "min_house_floor": 10_000}],
                       revolving_rate=LINE_RATE, revolving_rate_type="variable")
        self.assertIn("min_house_floor", str(ctx.exception))
        self.assertIn("only the 'house' tranche", str(ctx.exception))

    def test_deductible_is_rejected_on_a_non_investment_tranche(self):
        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            self._load([{"kind": "house", "deductible": True}, {"kind": "line"}],
                       revolving_rate=LINE_RATE, revolving_rate_type="variable")
        self.assertIn("only the 'investment' tranche", str(ctx.exception))

    def test_a_house_floor_above_the_charge_is_refused_at_load(self):
        """min_house_floor 750,000 > the 720,000 registered charge -- no house
        amount exists between the floor and the charge, so no 3-tranche split
        exists to sweep; refused the moment the contract loads. (The old
        floor-on-``min_amount`` semantics are gone: a min_amount above the
        charge is now merely a cash-back threshold the sweep never reaches --
        the incentive is never credited, which is a fact, not an error.)"""
        with self.assertRaises(ChargeLimitExceededError):
            self._load([{"kind": "house", "min_house_floor": 750_000}, {"kind": "line"}],
                       revolving_rate=LINE_RATE, revolving_rate_type="variable")

    def test_an_unpriced_line_is_refused(self):
        """A readvanceable structure with no line rate anywhere (#654): the
        line can draw later via readvance, so it must be priced today."""
        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            self._load([{"kind": "house", "min_amount": HOUSE_MIN},
                        {"kind": "investment"}, {"kind": "line"}],
                       readvanceable=True)
        self.assertIn("must be priced", str(ctx.exception))

    def test_amounts_that_do_not_sum_to_the_charge_are_refused(self):
        """A direct overlay call with a split that exceeds (or undercuts) the
        charge is refused -- a borrowed dollar that exists twice, or not at
        all (DP#18)."""
        base = {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}
        structure = copy.deepcopy(ALL_IN_ONE)
        structure["tranche_amounts"] = {"house": HOUSE_MIN, "investment": 100_000,
                                        "line": 100_000}  # 800k > 720k charge
        with self.assertRaises(ChargeLimitExceededError) as ctx:
            apply_structure_overlay(base, structure)
        self.assertIn("do not sum to the", str(ctx.exception))

    def test_a_house_tranche_below_the_sweep_floor_is_refused(self):
        """The house floor is now the SWEEP floor (declared min_house_floor,
        defaulting to 60% of the charge = 432,000) -- a hand-built split
        whose house is below it refuses loudly, exactly as the sweep would
        never enumerate it (DP#32)."""
        base = {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}
        structure = copy.deepcopy(ALL_IN_ONE)
        structure["tranche_amounts"] = {"house": 400_000, "investment": 100_000,
                                        "line": 220_000}
        with self.assertRaises(ChargeLimitExceededError) as ctx:
            apply_structure_overlay(base, structure)
        self.assertIn("sweep floor", str(ctx.exception))

    def test_a_house_tranche_below_the_cash_back_threshold_is_accepted(self):
        """Issue #1075 (optimizer half): ``min_amount`` is the CASH-BACK
        THRESHOLD, not a floor -- a split whose house (520,000) is below it
        but above the 432,000 sweep floor is a legitimate candidate (the
        household forgoes the incentive and puts the freed surplus to work).
        This is the exact split the old code refused."""
        base = {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}
        structure = copy.deepcopy(ALL_IN_ONE)
        structure["tranche_amounts"] = {"house": 520_000, "investment": 100_000,
                                        "line": 100_000}
        out = apply_structure_overlay(base, structure)
        self.assertAlmostEqual(out["mortgage_balance"], 620_000)

    def test_declaring_both_revolving_share_and_tranches_is_refused(self):
        """The schema's oneOf keeps the two forms mutually exclusive -- a
        structure that says both how it splits (share) and what it splits
        into (tranches) is ambiguous and refused at validation."""
        doc = _doc([{"id": "both", "label": "both",
                     "revolving_share": 0.3, "tranches": [{"kind": "house"}]}])
        with self.assertRaises(contract_errors.ContractValidationError):
            contract_schema.validate_contract(doc)

    def test_an_unknown_tranche_kind_is_refused(self):
        doc = _doc([{"id": "k", "label": "k",
                     "tranches": [{"kind": "cottage"}]}])
        with self.assertRaises(contract_errors.ContractValidationError):
            contract_schema.validate_contract(doc)


# ============================================================================
# (3b) Sweep composition details: cash-out bases, cell-time refusals
# ============================================================================

class TestSweepCompositionDetails(unittest.TestCase):
    def test_a_cash_out_basis_is_swept_and_reported_with_sourcing(self):
        """The sweep is composed per refinance basis (issue #845): with a
        declared cash-out option, the tranche points partition the basis's
        REGISTERED charge (720,000 -- the 50k cash-out is the separate
        year-0 drawn lump the sourcing machinery prices, line-first per
        #849, not part of the split), every cell carries the basis's
        cash_out, and the printed optimal split states the advance-vs-line
        sourcing."""
        doc = _doc()
        doc["decisions"]["mortgage"]["refinance_options"] = [{
            "id": "refi_50k", "label": "Refinance 50k", "cash_out": 50_000,
            "ltv": 0.68, "amortization_years": 25}]
        cfg = ic.to_internal_config(doc)
        cells = optimize.structure_refinance_cells(cfg)
        self.assertGreaterEqual(len(cells), 50)
        self.assertTrue(all(c['cfg'] is not None for c in cells))
        for c in cells:
            self.assertEqual(c['cfg']['property']['cash_out'], 50_000)
            a = c['structure']['tranche_amounts']
            self.assertAlmostEqual(
                a['house'] + a['investment'] + a['line'], CHARGE)
        results = optimize.run_mortgage_structure_exploration(cfg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(results, cells=cells)
        out = buf.getvalue()
        self.assertIn("OPTIMAL 3-TRANCHE SPLIT", out)
        self.assertIn("cash-out sourcing", out)
        self.assertIn("as a mortgage advance", out)
        self.assertIn("drawn from the line", out)

    def test_a_house_floor_above_the_basis_charge_is_refused_at_cell_time(self):
        """A structure whose house SWEEP FLOOR exceeds the basis's charge
        produces NO sweep point -- the cell is refused with a named reason
        (DP#32), never silently dropped from the ranking. Built as an
        internal config so the load-time check is bypassed (the cell
        composition must refuse on its own). (The old ``min_amount``-as-floor
        case is gone: a min_amount above the charge merely never credits the
        cash-back, which is a fact, not a refusal.)"""
        cfg = {
            'property': {
                'house_value': HOUSE_VALUE, 'mortgage_balance': HOUSE_MIN,
                'margin_available': CHARGE - HOUSE_MIN, 'mortgage_rate': HOUSE_RATE,
                'structure_options': [{
                    'id': 'too_tall', 'label': 'Too tall',
                    'tranches': [{'kind': 'house', 'min_house_floor': 750_000},
                                 {'kind': 'line'}],
                    'revolving_rate': LINE_RATE, 'revolving_rate_type': 'variable',
                }],
            },
            'scenarios': {},
        }
        cells = optimize.structure_refinance_cells(cfg)
        self.assertEqual(len(cells), 1)
        self.assertIsNone(cells[0]['cfg'])
        self.assertIn("no 3-tranche split exists to sweep", cells[0]['refusal'])

    def test_an_invalid_tranche_spec_is_refused_at_cell_time(self):
        """A tranches-declared structure whose spec is INVALID (here: an
        unknown kind) produces the named refusal cell, never a crash:
        ``_apply_tranched_structure`` -> ``_validate_tranche_spec`` raises a
        plain ``ValueError`` (which the typed ``ChargeLimitExceededError``
        subclasses -- the typed errors must be caught FIRST), and the
        cell-composition path turns it into the refusal cell. The spec is
        validated in ``_tranche_sweep_points`` BEFORE any point is
        enumerated, so the template form is refused ONCE. Built as an
        internal config so the load-time ``ContractAdaptationError`` is
        bypassed (the cell composition must refuse on its own)."""
        cfg = {
            'property': {
                'house_value': HOUSE_VALUE, 'mortgage_balance': HOUSE_MIN,
                'margin_available': CHARGE - HOUSE_MIN, 'mortgage_rate': HOUSE_RATE,
                'structure_options': [{
                    'id': 'bad', 'label': 'bad',
                    'tranches': [{'kind': 'cottage'}],
                }],
            },
            'scenarios': {},
        }
        cells = optimize.structure_refinance_cells(cfg)
        self.assertEqual(len(cells), 1)
        self.assertIsNone(cells[0]['cfg'])
        self.assertIn("ValueError", cells[0]['refusal'])
        self.assertIn("unknown kind", cells[0]['refusal'])

    def test_an_invalid_sweep_point_is_refused_at_cell_time(self):
        """The same refusal for a pre-built sweep point (``tranche_amounts``
        present, so ``_tranche_sweep_points`` is skipped): the cell branch
        itself must catch ``_validate_tranche_spec``'s ValueError and refuse
        -- the recursive per-point composition is the exact path the crash
        reproduced (issue #1075)."""
        cfg = {
            'property': {
                'house_value': HOUSE_VALUE, 'mortgage_balance': HOUSE_MIN,
                'margin_available': CHARGE - HOUSE_MIN, 'mortgage_rate': HOUSE_RATE,
                'structure_options': [{
                    'id': 'bad', 'label': 'bad',
                    'tranches': [{'kind': 'cottage'}],
                    'tranche_amounts': {'house': HOUSE_MIN,
                                        'investment': 70_000, 'line': 50_000},
                }],
            },
            'scenarios': {},
        }
        cells = optimize.structure_refinance_cells(cfg)
        self.assertEqual(len(cells), 1)
        self.assertIsNone(cells[0]['cfg'])
        self.assertIn("ValueError", cells[0]['refusal'])
        self.assertIn("unknown kind", cells[0]['refusal'])

    def test_a_house_range_too_wide_for_the_cell_cap_is_refused(self):
        """The sweep caps its cell count (house grid x split grid) so the
        optimizer stays fast; a household whose swept range cannot fit even
        at the coarsest step is refused LOUDLY with the remedy named
        (declare a min_house_floor nearer the charge) -- never silently
        dropped from the ranking, never a surprise 300-cell sweep (DP#32)."""
        cfg = {
            'property': {
                'house_value': 5_000_000, 'mortgage_balance': 3_200_000,
                'margin_available': 800_000, 'mortgage_rate': HOUSE_RATE,
                'structure_options': [{
                    'id': 'huge', 'label': 'Huge house',
                    'tranches': [{'kind': 'house'},
                                 {'kind': 'investment', 'deductible': True},
                                 {'kind': 'line'}],
                    'revolving_rate': LINE_RATE, 'revolving_rate_type': 'variable',
                    'readvanceable': True,
                }],
            },
            'scenarios': {},
        }
        cells = optimize.structure_refinance_cells(cfg)
        self.assertEqual(len(cells), 1)
        self.assertIsNone(cells[0]['cfg'])
        self.assertIn("sweep cap", cells[0]['refusal'])
        self.assertIn("min_house_floor", cells[0]['refusal'])

    def test_a_cash_out_basis_that_breaches_the_charge_is_refused(self):
        """A declared refinance option whose cash-out pushes the charge past
        the OSFI B-20 cap refuses the tranched structure at that basis -- the
        refusal names the basis, and the cell is not scored (but is reported)."""
        doc = _doc()
        doc["decisions"]["mortgage"]["refinance_options"] = [{
            "id": "too_big", "label": "Too big", "cash_out": 200_000,
            "ltv": 0.88, "amortization_years": 25}]
        cfg = ic.to_internal_config(doc)
        cells = optimize.structure_refinance_cells(cfg)
        self.assertEqual(len(cells), 1)
        self.assertIsNone(cells[0]['cfg'])
        self.assertIn("ChargeLimitExceededError", cells[0]['refusal'])

    def test_a_low_house_floor_that_breaches_the_revolving_cap_is_refused(self):
        """The 65% revolving-only ceiling is enforced per sweep point (DP#32,
        never clamped): a declared min_house_floor of 0 lets the sweep reach
        house amounts whose residual LINE exceeds 65% of the house value
        (585,000 for the 900,000 house) -- those cells are refused loudly
        with the cap named, while the rest of the sweep is scored. (With the
        DEFAULT 60%-of-charge floor the cap can never bind -- the line is at
        most 40% of the charge <= 32% of the house value.)"""
        cfg = {
            'property': {
                'house_value': HOUSE_VALUE, 'mortgage_balance': HOUSE_MIN,
                'margin_available': CHARGE - HOUSE_MIN, 'mortgage_rate': HOUSE_RATE,
                'structure_options': [{
                    'id': 'low_floor', 'label': 'Low floor',
                    'tranches': [{'kind': 'house', 'min_house_floor': 0},
                                 {'kind': 'investment', 'deductible': True,
                                  'rate': INVEST_RATE},
                                 {'kind': 'line'}],
                    'revolving_rate': LINE_RATE, 'revolving_rate_type': 'variable',
                    'readvanceable': True,
                }],
            },
            'scenarios': {},
        }
        cells = optimize.structure_refinance_cells(cfg)
        refused = [c for c in cells if c['refusal'] is not None]
        scored = [c for c in cells if c['cfg'] is not None]
        self.assertTrue(refused, "some low-house cells must breach the cap")
        self.assertTrue(scored)
        for c in refused:
            self.assertIn("ChargeLimitExceededError", c['refusal'])
            self.assertIn("65%", c['refusal'])
        for c in scored:
            a = c['structure']['tranche_amounts']
            self.assertLessEqual(a['line'], 0.65 * HOUSE_VALUE + 0.011)
            self.assertAlmostEqual(
                a['house'] + a['investment'] + a['line'], CHARGE)

    def test_a_declared_min_house_floor_overrides_the_default(self):
        """DP#13: a declared min_house_floor is the sweep floor -- 550,000
        here, NOT the 432,000 default -- so no cell's house goes below it,
        and (unlike the default) the 25k grid fits inside the cell cap
        without coarsening (550k..720k is a 7-point range)."""
        cfg = ic.to_internal_config(_doc(structure_options=[{
            "id": "tall_floor", "label": "Tall floor",
            "tranches": [{"kind": "house", "min_house_floor": 550_000,
                           "min_amount": HOUSE_MIN},
                          {"kind": "investment", "deductible": True,
                           "rate": INVEST_RATE},
                          {"kind": "line"}],
            "revolving_rate": LINE_RATE, "revolving_rate_type": "variable",
            "readvanceable": True,
        }]))
        cells = optimize.structure_refinance_cells(cfg)
        self.assertTrue(all(c['cfg'] is not None for c in cells))
        houses = sorted({round(c['structure']['tranche_amounts']['house'])
                         for c in cells})
        self.assertEqual(houses[0], 550_000)
        self.assertTrue(all(h >= 550_000 for h in houses))
        self.assertEqual(houses, [550_000, 575_000, 600_000, 625_000,
                                  650_000, 675_000, 700_000, 720_000])


# ============================================================================
# (3c) Direct-overlay refusals for the tranche machinery (DP#32)
# ============================================================================

class TestTrancheOverlayRefusals(unittest.TestCase):
    def _base(self):
        return {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}

    def test_an_empty_tranches_array_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            apply_structure_overlay(self._base(),
                                    {"id": "e", "label": "e", "tranches": []})
        self.assertIn("empty", str(ctx.exception))

    def test_an_unknown_tranche_kind_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            apply_structure_overlay(self._base(),
                                    {"id": "u", "label": "u",
                                     "tranches": [{"kind": "cottage"}]})
        self.assertIn("unknown kind", str(ctx.exception))

    def test_a_rate_type_without_its_rate_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            apply_structure_overlay(self._base(),
                                    {"id": "r", "label": "r",
                                     "tranches": [{"kind": "house",
                                                    "rate_type": "fixed"}]})
        self.assertIn("rate_type but no rate", str(ctx.exception))

    def test_a_line_rate_without_its_rate_type_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            apply_structure_overlay(
                self._base(),
                {"id": "l", "label": "l",
                 "tranches": [{"kind": "house", "min_amount": HOUSE_MIN},
                              {"kind": "line", "rate": LINE_RATE}],
                 "readvanceable": True})
        self.assertIn("rate but no rate_type", str(ctx.exception))

    def test_a_line_tranche_carrying_its_own_rate_prices_the_facility(self):
        """The line's rate may live on the line tranche itself rather than the
        structure-level revolving_rate -- the facility is priced from the
        tranche's own pair (#654)."""
        out = apply_structure_overlay(
            self._base(),
            {"id": "l2", "label": "l2",
             "tranches": [{"kind": "house", "min_amount": HOUSE_MIN},
                          {"kind": "line", "rate": LINE_RATE,
                           "rate_type": "variable"}],
             "readvanceable": True})
        self.assertEqual(out["heloc_rate"], LINE_RATE)
        self.assertEqual(out["heloc_rate_type"], "variable")

    def test_a_zero_charge_zero_amount_split_does_not_divide_by_zero(self):
        """A degenerate all-zero split at a zero charge keeps the baseline
        rate (no balance, no blend) -- the guard exists so the overlay never
        divides by zero, and the facility fields are absent, not zeroed."""
        out = apply_structure_overlay(
            {"house_value": 0, "mortgage_balance": 0, "mortgage_rate": HOUSE_RATE},
            {"id": "z", "label": "z",
             "tranches": [{"kind": "house"}],
             "tranche_amounts": {"house": 0, "investment": 0, "line": 0}})
        self.assertEqual(out["mortgage_rate"], HOUSE_RATE)
        self.assertNotIn("margin_available", out)
        self.assertNotIn("deductible_mortgage_interest", out)


# ============================================================================
# (3d) The SOURCING overlay routes tranches-declared structures to the tranche
# machinery (issue #1075)
# ============================================================================

class TestTrancheSourcingOverlay(unittest.TestCase):
    """Issue #1075: ``apply_sourcing_overlay`` (the #845/#849 composition
    that applies ONE candidate structure to a property which has ALREADY had
    a cash-out refinance booked) must route a ``tranches``-declared structure
    through the tranche machinery, NOT the ``revolving_share`` re-split: the
    sweep point's AMOUNTS already ARE the post-refinance drawn/room split
    (the investment tranche is the advance; the line holds the rest as room),
    so the sourcing line-draw re-split does not apply."""

    def _base(self):
        return {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}

    def test_a_tranches_declared_structure_reaches_the_tranche_machinery(self):
        """The routing line: a tranches-declared structure is applied by
        ``_apply_tranched_structure`` exactly as the share form's overlay
        applies it -- drawn mortgage = house + investment, undrawn room =
        the line, the deductible tranche's EXACT interest (balance x its
        OWN rate) on the s.20(1)(c) keys, and the facility priced from the
        line rate. The sourcing re-split must NOT re-partition the amounts
        (the year-0 lump-sum machinery prices how much of the surplus is
        drawn from the line instead)."""
        base = self._base()
        structure = copy.deepcopy(ALL_IN_ONE)
        structure["tranche_amounts"] = _point(investment=70_000)
        out = apply_sourcing_overlay(base, structure)
        # Same split the structure overlay books for the identical input.
        self.assertEqual(out, apply_structure_overlay(base, structure))
        self.assertAlmostEqual(out["mortgage_balance"], HOUSE_MIN + 70_000)
        # Undrawn room = the split's line (the surplus left after the house
        # floor and the investment tranche), not the investment amount.
        self.assertAlmostEqual(out["margin_available"], CHARGE - HOUSE_MIN - 70_000)
        self.assertEqual(out["deductible_mortgage_balance"], 70_000)
        self.assertEqual(out["deductible_mortgage_interest"], 70_000 * INVEST_RATE)
        self.assertEqual(out["heloc_rate"], LINE_RATE)
        self.assertTrue(out["heloc_readvance"])

    def test_a_tranches_declared_structure_is_not_the_identity_return(self):
        """The share form's no-``revolving_share`` case returns the property
        untouched (DP#13); a tranches-declared structure must NOT fall
        through to that no-op -- it applies its split even with no
        ``revolving_share`` key present."""
        structure = copy.deepcopy(ALL_IN_ONE)
        structure["tranche_amounts"] = _point(investment=70_000)
        structure.pop("revolving_share", None)  # ALL_IN_ONE carries none anyway
        out = apply_sourcing_overlay(self._base(), structure)
        self.assertAlmostEqual(out["mortgage_balance"], HOUSE_MIN + 70_000)
        self.assertIn("deductible_mortgage_interest", out)


# ============================================================================
# (5) The existing A/B/C/D share form is byte-identical (DP#13: additive opt-in)
# ============================================================================

ALL_MORTGAGE = {"id": "all_mortgage", "label": "Whole charge as an amortizing mortgage",
                "revolving_share": 0.0}
READVANCEABLE = {"id": "readvanceable", "label": "Same amount, readvanceable",
                 "revolving_share": 0.0, "readvanceable": True,
                 "revolving_rate": LINE_RATE, "revolving_rate_type": "variable"}
SPLIT_WITH_LINE = {"id": "split_with_line", "label": "Smaller mortgage + revolving line",
                   "revolving_share": 0.30, "revolving_rate": LINE_RATE,
                   "revolving_rate_type": "variable"}


class TestShareFormUnchanged(unittest.TestCase):
    def test_the_share_form_maps_without_tranche_keys(self):
        doc = _doc([ALL_MORTGAGE, READVANCEABLE, SPLIT_WITH_LINE],
                   house_mortgage=450_000, heloc_room=110_000)
        cfg = ic.to_internal_config(doc)
        for opt in cfg["property"]["structure_options"]:
            self.assertNotIn("tranches", opt)
            self.assertIn("revolving_share", opt)

    def test_the_ranking_output_contains_no_tranche_report(self):
        """A contract that never declares tranches sees the exact #687 report
        -- the new 3-tranche block is gated on tranche rows existing, and
        none exist here."""
        cfg = ic.to_internal_config(_doc(
            [ALL_MORTGAGE, READVANCEABLE, SPLIT_WITH_LINE],
            house_mortgage=450_000, heloc_room=110_000))
        results = optimize.run_mortgage_structure_exploration(cfg)
        self.assertTrue(results)
        self.assertTrue(all(r.get('structure_tranche_amounts') is None
                            for r in results))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(results)
        out = buf.getvalue()
        self.assertIn("MORTGAGE STRUCTURE RANKING", out)
        self.assertNotIn("3-TRANCHE", out)
        self.assertNotIn("OPTIMAL", out)


if __name__ == "__main__":
    unittest.main()
