#!/usr/bin/env python3
"""Issue #138: life-insurance PREMIUMS are a real dated cash flow, and the
term cliff is real.

Before this fix the engine's whole insurance model was one terminal number:
``estate.life_insurance[]`` collapsed to a ``life_insurance_death_benefit``
added tax-free at death. A policy costing $1,200/year was modelled as FREE,
and a 10-year term's expiry cliff moved nothing. This test guards the fix:

  - every declared ``premium_annual`` becomes dated NEGATIVE cash-flow legs
    (one per calendar year the policy charges its declared premium), folded
    by the adapter into the engine's EXISTING dated cash-flow channel -- the
    same channel #139's transaction costs ride -- so every objective sees
    the cost of coverage (DP#8);
  - a TERM policy stops charging at its ``term_end_date`` cliff (the lapse):
    the premium stops AND the death benefit leaves the estate;
  - a declared ``renewal_end_date`` keeps the COVERAGE (death benefit) alive
    past the cliff while the priced premium still stops there -- renewal
    rates are the insurer's to set, never the engine's to invent;
  - DP#32 both ways: an incoherent renewal is REFUSED loudly, and a
    household declaring no policies maps and runs BYTE-IDENTICALLY to before
    (golden terminal total_assets untouched).

DP#4/DP#15: every figure below is fabricated and round ($500,000 face,
$1,200/yr premium); every name is role-based. No real insurer, premium, or
insured person appears anywhere.
"""
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
import contract_schema
from contract_estate import map_insurance_premiums
from contract_schema import validate_contract
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from test_dp_income_scenario_reaches_engine import _two_generation_subset
from test_golden_trajectory_581 import (
    golden_household_config, _run as _run_golden,
)
import contract_errors

TERMINAL_TOTAL_ASSETS = 9709753.139463063


def _policy(*, pol_id="term_p1", kind="term", face=500_000, premium=1_200,
            term_end="2036-06-30", renewal_end=None, insured="p1", owner="p1"):
    entry = {
        "id": pol_id, "owner": owner, "insured": insured,
        "beneficiary": "p2", "kind": kind, "face_amount": face,
        "premium_annual": premium, "as_of": "2026-01-01",
        "term_end_date": term_end,
    }
    if renewal_end is not None:
        entry["renewal_end_date"] = renewal_end
    return entry


def _minimal_doc(policies):
    """The smallest document ``map_insurance_premiums`` can date against:
    horizon person p1 born 1980-01-01 projected to age 95 -> last simulated
    year 2075."""
    return {
        "people": [{"id": "p1", "birth_date": "1980-01-01"}],
        "decisions": {"horizon": {"person": "p1", "until_age": 95}},
        "estate": {"life_insurance": policies},
    }


def _load_doc():
    with open(contract_schema.EXAMPLE_PATH) as fh:
        return _two_generation_subset(json.load(fh))


def _run(doc, years=None):
    logging.disable(logging.WARNING)
    try:
        cfg = ic.to_internal_config(doc)
        sim_cfg = SimulationConfig.from_dict(cfg)
        if years is not None:
            sim_cfg = SimulationConfig(**{**sim_cfg.__dict__,
                                          "projection_years": years})
        return FamilySimulation(sim_cfg).run()
    finally:
        logging.disable(logging.NOTSET)


# ============================================================================
# 1. The pure mapper: one leg per charged-premium year, cliff-aware
# ============================================================================

class TestMapInsurancePremiums(unittest.TestCase):

    def test_term_policy_charges_through_a_mid_year_cliff(self):
        """A Jun-30 2036 expiry means the policy covers part of 2036: the
        $1,200 premium fires 2026..2036 INCLUSIVE, then stops."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-06-30")]),
            primary_id="p1", start_year=2026)
        self.assertEqual([l["year"] for l in legs],
                         list(range(2026, 2037)))
        self.assertEqual({l["amount"] for l in legs}, {-1200.0})

    def test_jan_first_cliff_charges_no_january_premium(self):
        """A Jan-1 2036 expiry: the policy is in force on ZERO days of 2036,
        so the last leg is 2035 (DP#1: dates, not years)."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual([l["year"] for l in legs], list(range(2026, 2036)))

    def test_permanent_policy_charges_through_the_horizon(self):
        """term_end_date=null (permanent): the premium runs to the LAST
        simulated year (1980 + 95 = 2075), not to some invented cap."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(kind="permanent", term_end=None)]),
            primary_id="p1", start_year=2026)
        self.assertEqual(len(legs), 2075 - 2026 + 1)
        self.assertEqual(legs[-1]["year"], 2075)

    def test_already_lapsed_policy_charges_nothing(self):
        legs = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2020-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(legs, [])

    def test_renewal_does_not_invent_a_post_cliff_premium(self):
        """A declared renewal keeps the coverage alive past the cliff, but
        the RENEWAL premium is the insurer's to quote: the priced legs stop
        at term_end_date exactly as if the policy had lapsed."""
        lapsed = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-06-30")]),
            primary_id="p1", start_year=2026)
        renewed = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-06-30",
                                  renewal_end="2046-06-30")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(renewed, lapsed)

    def test_zero_premium_is_a_real_zero_not_a_drop(self):
        """DP#32: a declared $0 premium produces legs carrying 0 -- absence
        and zero stay distinguishable shapes, neither coerced."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(premium=0, term_end="2028-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(len(legs), 2)
        self.assertTrue(all(l["amount"] == 0.0 for l in legs))

    def test_legs_are_post_tax_costs_named_by_policy(self):
        """Premiums are NOT deductible -- each leg is after-tax cash out,
        signed negative, identified by its policy id."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(pol_id="term_a", term_end="2027-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(legs[0]["tax_treatment"], "post-tax")
        self.assertEqual(legs[0]["kind"], "cost")
        self.assertEqual(legs[0]["id"], "term_a")

    def test_no_policies_produce_no_legs(self):
        self.assertEqual(map_insurance_premiums(_minimal_doc([]),
                                                primary_id="p1",
                                                start_year=2026), [])

    def test_renewal_without_a_term_is_refused_loudly(self):
        """A permanent policy does not renew: renewal_end_date on a policy
        with term_end_date=null is contradictory input, refused (DP#32)."""
        with self.assertRaises(contract_errors.ContractAdaptationError):
            map_insurance_premiums(
                _minimal_doc([_policy(kind="permanent", term_end=None,
                                      renewal_end="2046-06-30")]),
                primary_id="p1", start_year=2026)

    def test_renewal_ending_before_the_term_is_refused_loudly(self):
        """Coverage cannot end twice: a renewal_end_date on/before the very
        term_end_date it claims to extend is refused, never truncated."""
        with self.assertRaises(contract_errors.ContractAdaptationError):
            map_insurance_premiums(
                _minimal_doc([_policy(term_end="2036-06-30",
                                      renewal_end="2036-06-30")]),
                primary_id="p1", start_year=2026)


# ============================================================================
# 2. The death-benefit side: renewal moves the estate cliff
# ============================================================================

class TestRenewalMovesDeathBenefit(unittest.TestCase):

    def _mapped_benefit(self, policy):
        doc = _load_doc()
        doc["estate"]["life_insurance"] = [policy]
        return ic.to_internal_config(doc)["estate"]["life_insurance_death_benefit"]

    def test_unrenewed_term_lapsing_before_the_horizon_pays_nothing(self):
        # Horizon: p1 (born 1980-03-14) reaches age 95 in 2075; the term ends
        # 2036 -> lapsed at valuation -> excluded from the terminal estate.
        self.assertEqual(self._mapped_benefit(_policy()), 0.0)

    def test_declared_renewal_keeps_the_face_amount_in_the_estate(self):
        benefit = self._mapped_benefit(
            _policy(term_end="2036-06-30", renewal_end="2085-06-30"))
        self.assertEqual(benefit, 500_000)

    def test_renewal_still_short_of_the_horizon_lapses(self):
        benefit = self._mapped_benefit(
            _policy(term_end="2036-06-30", renewal_end="2060-06-30"))
        self.assertEqual(benefit, 0.0)


# ============================================================================
# 3. Adapter composition: legs fold into cfg.cash_flows (one channel)
# ============================================================================

class TestAdapterFoldsPremiumLegs(unittest.TestCase):

    def test_premium_legs_join_the_dated_cash_flow_channel(self):
        doc = _load_doc()
        doc["estate"]["life_insurance"] = [
            _policy(pol_id="term_only", term_end="2036-06-30")]
        cfg = ic.to_internal_config(doc)
        legs = [cf for cf in cfg["cash_flows"] if cf.get("id") == "term_only"]
        self.assertEqual([(l["year"], l["amount"]) for l in legs],
                         [(y, -1200.0) for y in range(2026, 2037)])

    def test_no_policies_map_cash_flows_exactly_as_before(self):
        """DP#32 at the adapter: emptying life_insurance restores the exact
        pre-feature cash_flows list -- nothing added, nothing reshaped."""
        doc = _load_doc()
        baseline = ic.to_internal_config(doc)["cash_flows"]
        doc["estate"]["life_insurance"] = []
        stripped = ic.to_internal_config(doc)["cash_flows"]
        self.assertEqual(stripped, [cf for cf in baseline
                                    if cf.get("label")
                                    != "life insurance premium"])

    def test_schema_validates_a_renewed_policy_and_refuses_unknown_keys(self):
        import copy
        doc = copy.deepcopy(_load_doc())
        doc["estate"]["life_insurance"] = [
            _policy(term_end="2036-06-30", renewal_end="2046-06-30")]
        validate_contract(doc)
        bad = copy.deepcopy(doc)
        bad["estate"]["life_insurance"][0]["not_a_field"] = True
        with self.assertRaises(Exception):
            validate_contract(bad)


# ============================================================================
# 4. The engine fold: premiums leave the household every year they fire
# ============================================================================

class TestEnginePricesPremiums(unittest.TestCase):

    def test_premium_drops_the_fire_year_savings_channel_exactly(self):
        doc = _load_doc()
        doc["estate"]["life_insurance"] = []
        baseline = _run(doc, years=2)
        doc["estate"]["life_insurance"] = [_policy()]
        with_policy = _run(doc, years=2)
        self.assertAlmostEqual(
            baseline[0].annual_savings - with_policy[0].annual_savings,
            1200.0, places=2,
            msg="a $1,200/yr premium must drop the first year's savings "
                "channel by exactly that amount -- otherwise the cost of "
                "coverage is still invisible to every objective")

    def test_terminal_total_assets_decline_with_coverage_cost(self):
        """The premium stream survives to the horizon: the uninsured run's
        terminal total_assets must EXCEED the insured run's (both runs carry
        the same estate, so this isolates the living cost)."""
        doc = _load_doc()
        doc["estate"]["life_insurance"] = []
        baseline = _run(doc)
        doc["estate"]["life_insurance"] = [_policy()]
        insured = _run(doc)
        self.assertGreater(baseline[-1].total_assets,
                           insured[-1].total_assets)

    def test_golden_household_is_byte_identical_with_no_policies(self):
        """DP#32 (the crux): the golden household declares no life insurance,
        so the terminal invariant must be bit-for-bit unchanged."""
        results = _run_golden(golden_household_config())
        terminal = results[-1].total_assets
        self.assertEqual(
            terminal, TERMINAL_TOTAL_ASSETS,
            f"golden terminal total_assets MOVED: {terminal!r} != "
            f"{TERMINAL_TOTAL_ASSETS!r}")


if __name__ == "__main__":
    unittest.main()
