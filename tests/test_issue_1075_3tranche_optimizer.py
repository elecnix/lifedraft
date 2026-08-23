#!/usr/bin/env python3
"""Issue #1075 (optimizer half): the 3-tranche readvanceable structure is
SWEPT and GENERATED, not just expressed.

The data-model half (commit 62f91b6) made the adapter consume N kind=mortgage
tranches sharing one charge -- balances sum, the rate blends balance-weighted,
each tranche's ``deductible`` flag surfaces as
``deductible_mortgage_balance`` + EXACT ``deductible_mortgage_interest``, and
``cash_back`` credits at year 0. This file pins the OPTIMIZER half:

  (1) ``decisions.mortgage.structure_options[].tranches`` -- a structure may
      declare the 3-tranche split (house >= a declared minimum -- $600k for
      the $1,200 cash-back programs -- deductible investment mortgage,
      readvanceable line) as an additive opt-in over the #687 share form,
      which stays byte-identical;
  (2) the s.20(1)(c) pricing consumes the EXACT deductible interest (sum of
      each flagged tranche's balance x ITS OWN rate), never the blended-rate
      product, when a structure carries tranches at different rates;
  (3) the optimizer SWEEPS the split (house at its floor, investment and the
      line sharing the surplus) and RETURNS the optimal amounts, printed by
      the #687 report;
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
# House 900,000 -> 80% charge = 720,000. The house mortgage is 600,000 (the
# cash-back floor); the surplus 120,000 is what the sweep splits between the
# deductible investment tranche and the readvanceable line.
HOUSE_VALUE = 900_000
CHARGE = 720_000
HOUSE_MIN = 600_000
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
         heloc_room=CHARGE - HOUSE_MIN):
    """The shipped example trimmed to the couple+children, re-based to the
    #1075 household: house 900,000 (80% charge = 720,000), house mortgage
    ``house_mortgage`` (default 600,000 -- the cash-back floor), HELOC room
    ``heloc_room`` (default 120,000 so the charge is exactly 720,000), ONE
    refinance basis (no cash-out) and ONE income scenario (the exploration
    is 5 cells, not 30).

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

    def test_five_sweep_points_partition_the_surplus(self):
        """The template expands into 5 concrete splits of the $720k charge:
        house pinned at its $600k floor, the $120k surplus split between the
        investment tranche and the line at 0/25/50/75/100% -- every point
        sums to the charge by construction (a partition, DP#18)."""
        self.assertEqual(len(self.cells), 5)
        investment_amounts = sorted(
            c['structure']['tranche_amounts']['investment'] for c in self.cells)
        self.assertEqual(investment_amounts, [0.0, 30_000, 60_000, 90_000, 120_000])
        for c in self.cells:
            a = c['structure']['tranche_amounts']
            self.assertIsNotNone(c['cfg'], f"cell refused: {c['refusal']}")
            self.assertEqual(a['house'], HOUSE_MIN)
            self.assertAlmostEqual(a['house'] + a['investment'] + a['line'], CHARGE)

    def test_the_winning_split_is_returned_and_sums_to_the_charge(self):
        """The deliverable: the optimizer GENERATES the amounts -- the winning
        row (per basis x income scenario) carries its own tranche_amounts, with
        house >= the $600k floor and the three tranches summing to the $720k
        charge, and a non-negative line."""
        self.assertTrue(self.results)
        winners = optimize.winners_by_structure_scenario(self.results)
        tranche_winners = [w for w in winners if w.get('tranche_amounts')]
        self.assertTrue(tranche_winners)
        for w in tranche_winners:
            a = w['tranche_amounts']
            self.assertGreaterEqual(a['house'], HOUSE_MIN)
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
        optimal per-tranche amounts (and the strategy that produced them)."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_structure_report(self.results, cells=self.cells)
        out = buf.getvalue()
        self.assertIn('OPTIMAL 3-TRANCHE SPLIT', out)
        self.assertIn('house $600,000', out)
        self.assertIn('investment', out)
        self.assertIn('line', out)


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

    def test_deductible_is_rejected_on_a_non_investment_tranche(self):
        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            self._load([{"kind": "house", "deductible": True}, {"kind": "line"}],
                       revolving_rate=LINE_RATE, revolving_rate_type="variable")
        self.assertIn("only the 'investment' tranche", str(ctx.exception))

    def test_a_house_floor_above_the_charge_is_refused_at_load(self):
        """min_amount 750,000 > the 720,000 registered charge -- no 3-tranche
        split exists to sweep, refused the moment the contract loads."""
        with self.assertRaises(ChargeLimitExceededError):
            self._load([{"kind": "house", "min_amount": 750_000}, {"kind": "line"}],
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

    def test_a_house_tranche_below_its_minimum_is_refused(self):
        base = {"house_value": HOUSE_VALUE, "mortgage_balance": HOUSE_MIN,
                "margin_available": CHARGE - HOUSE_MIN, "mortgage_rate": HOUSE_RATE}
        structure = copy.deepcopy(ALL_IN_ONE)
        structure["tranche_amounts"] = {"house": 400_000, "investment": 100_000,
                                        "line": 220_000}
        with self.assertRaises(ChargeLimitExceededError) as ctx:
            apply_structure_overlay(base, structure)
        self.assertIn("below the declared", str(ctx.exception))

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
        post-refinance charge, every cell carries the basis's cash_out (the
        year-0 invested lump, drawn line-first per #849), and the printed
        optimal split states the advance-vs-line sourcing."""
        doc = _doc()
        doc["decisions"]["mortgage"]["refinance_options"] = [{
            "id": "refi_50k", "label": "Refinance 50k", "cash_out": 50_000,
            "ltv": 0.68, "amortization_years": 25}]
        cfg = ic.to_internal_config(doc)
        cells = optimize.structure_refinance_cells(cfg)
        self.assertEqual(len(cells), 5)
        for c in cells:
            self.assertIsNotNone(c['cfg'])
            self.assertEqual(c['cfg']['property']['cash_out'], 50_000)
            a = c['structure']['tranche_amounts']
            self.assertAlmostEqual(a['house'] + a['investment'] + a['line'], CHARGE)
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
        """A structure whose house floor exceeds the basis's charge produces
        NO sweep point -- the cell is refused with a named reason (DP#32),
        never silently dropped from the ranking. Built as an internal config
        so the load-time check is bypassed (the cell composition must refuse
        on its own)."""
        cfg = {
            'property': {
                'house_value': HOUSE_VALUE, 'mortgage_balance': HOUSE_MIN,
                'margin_available': CHARGE - HOUSE_MIN, 'mortgage_rate': HOUSE_RATE,
                'structure_options': [{
                    'id': 'too_tall', 'label': 'Too tall',
                    'tranches': [{'kind': 'house', 'min_amount': 750_000},
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
