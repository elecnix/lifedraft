#!/usr/bin/env python3
"""Issue #139: one-time transaction costs and credits reach EVERY objective.

The engine prices the *recurring* difference between two strategies to the
dollar, but one-time frictions were a hardcoded zero everywhere: refinance
origination / discharge-quittance fees, legal, title insurance, appraisal,
transfer-out fees, and transfer-in rebates and match bonuses were all
invisible to every objective (net benefit, solvency, estate). This test
guards the fix — a first-class ``transaction_costs[]`` contract block owned
by ``transaction_costs.py`` (DP#3: one spelling), folded by the adapter into
the engine's EXISTING dated cash-flow channel so every objective that folds
the balance sheet sees the one-time delta with no new engine machinery (DP#8).

DP#32 is asserted both ways:

  - a household declaring no ``transaction_costs[]`` maps to a byte-identical
    cash_flows list and runs to the golden terminal total_assets bit-for-bit;
  - a household that DOES declare costs/credits books them at the year they
    fire, and the balance sheet (and thus the terminal objective) moves.

DP#4/DP#15: every figure below is fabricated and round; every name is
role-based. No real household's data appears anywhere.
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
import transaction_costs as tc
from contract_schema import validate_contract
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from test_dp_income_scenario_reaches_engine import _two_generation_subset
from test_golden_trajectory_581 import (
    golden_household_config, _run as _run_golden,
)
import contract_errors  # noqa: F401
import contract_schema

TERMINAL_TOTAL_ASSETS = 9709753.139463063


def _txn(*, txn_id, label, kind, date, amount, installment_years=None):
    entry = {"id": txn_id, "label": label, "kind": kind, "date": date,
             "amount": amount}
    if installment_years is not None:
        entry["installment_years"] = installment_years
    return entry


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
# 1. The pure module: the ONE spelling of a one-time leg
# ============================================================================

class TestMapTransactionCosts(unittest.TestCase):
    """The mapper reduces each declared cost/credit to dated, signed
    cash-flow legs — one per calendar year it fires. Direction stays
    explicit (DP#32), instalments spread evenly, and the sum is exact
    to the cent (never a silent float loss)."""

    def test_cost_fires_as_a_negative_leg_in_the_date_year(self):
        legs = tc.map_transaction_costs(
            {"transaction_costs": [_txn(txn_id="orig", label="lender origination fee",
                                        kind="cost", date="2026-06-30", amount=2000.0)]},
            start_year=2026)
        self.assertEqual(len(legs), 1)
        leg = legs[0]
        self.assertEqual(leg["year"], 2026)
        self.assertEqual(leg["amount"], -2000.0)
        self.assertEqual(leg["tax_treatment"], "non-taxable")
        self.assertEqual(leg["kind"], "cost")

    def test_credit_fires_as_a_positive_leg_in_the_date_year(self):
        legs = tc.map_transaction_costs(
            {"transaction_costs": [_txn(txn_id="rebate", label="transfer-in rebate",
                                            kind="credit", date="2026-06-30", amount=500.0)]},
            start_year=2026)
        self.assertEqual(legs[0]["amount"], 500.0)
        self.assertEqual(legs[0]["kind"], "credit")

    def test_installment_years_spreads_across_equal_annual_legs(self):
        """A $2,400 match paid out over 2 years is NOT one year-0 lump: two
        equal $1,200 legs land in successive calendar years."""
        legs = tc.map_transaction_costs(
            {"transaction_costs": [_txn(txn_id="match", label="2-year match",
                                            kind="credit", date="2026-03-01",
                                            amount=2400.0, installment_years=2)]},
            start_year=2026)
        self.assertEqual(
            [(l["year"], l["amount"]) for l in legs], [(2026, 1200.0), (2027, 1200.0)])

    def test_instalment_legs_sum_to_the_declared_amount_exactly(self):
        """Whole-cent equal legs, with the final leg absorbing the remainder,
        so the declared total is never rounded away."""
        legs = tc.map_transaction_costs(
            {"transaction_costs": [_txn(txn_id="fees", label="legal + title",
                                        kind="cost", date="2026-06-30",
                                        amount=2000.01, installment_years=3)]},
            start_year=2026)
        self.assertAlmostEqual(sum(l["amount"] for l in legs), -2000.01, places=6)
        self.assertEqual([l["year"] for l in legs], [2026, 2027, 2028])

    def test_absence_produces_no_legs(self):
        """DP#32: no transaction_costs -> no legs, so the fold is unchanged."""
        self.assertEqual(tc.map_transaction_costs({}, start_year=2026), [])
        self.assertEqual(
            tc.map_transaction_costs({"transaction_costs": []}, start_year=2026), [])


# ============================================================================
# 2. Adapter composition: legs fold into cfg.cash_flows (one channel)
# ============================================================================

class TestAdapterFoldsIntoCashFlows(unittest.TestCase):
    """The mapper appends the one-time legs to the engine's EXISTING dated
    cash-flow channel — the same channel the #1075 origination cash-back rides
    — so every objective reads them through the engine's single fold (DP#8)."""

    def test_transaction_costs_append_to_cash_flows(self):
        doc = _load_doc()
        # Isolate #139's legs: empty the estate's policies so #138's premium
        # legs (a separate feature's channel contribution) don't shift counts.
        doc["estate"]["life_insurance"] = []
        doc["transaction_costs"] = [
            _txn(txn_id="orig", label="lender origination fee", kind="cost",
                 date="2026-06-30", amount=2000.0),
            _txn(txn_id="title", label="title insurance", kind="cost",
                 date="2026-06-30", amount=1500.0),
            _txn(txn_id="rebate", label="transfer-in rebate", kind="credit",
                 date="2026-06-30", amount=500.0),
        ]
        cfg = ic.to_internal_config(doc)
        orig_len = len(doc.get("cash_flows", []))
        self.assertEqual(len(cfg["cash_flows"]), orig_len + 3)
        by_id = {cf.get("id"): cf["amount"] for cf in cfg["cash_flows"]
                 if cf.get("id")}
        self.assertEqual(by_id["orig"], -2000.0)
        self.assertEqual(by_id["title"], -1500.0)
        self.assertEqual(by_id["rebate"], 500.0)

    def test_no_block_folds_nothing(self):
        """DP#32 at the adapter: a document without transaction_costs[] maps
        cash_flows EXACTLY as it did before the feature."""
        doc = _load_doc()
        # Policy-free household (#138's premium legs are a different block's
        # contribution -- this test scopes the absent-transaction_costs claim).
        doc["estate"]["life_insurance"] = []
        cfg = ic.to_internal_config(doc)
        baseline = [{"year": int(cf["date"][:4]), "amount": cf["amount"],
                     "tax_treatment": "non-taxable" if cf["tax_treatment"] == "tax_free"
                     else "post-tax"}
                    for cf in doc.get("cash_flows", [])]
        self.assertEqual(cfg["cash_flows"], baseline)

    def test_schema_validates_and_rejects_unknown_keys(self):
        """The block validates against the composed universal schema, an
        unknown directive is refused loudly (additionalProperties:false)."""
        import copy
        doc = copy.deepcopy(_load_doc())
        doc["transaction_costs"] = [
            _txn(txn_id="orig", label="lender origination fee", kind="cost",
                 date="2026-06-30", amount=2000.0),
        ]
        validate_contract(doc)
        doc["transaction_costs"][0]["not_a_field"] = True
        with self.assertRaises(Exception):
            validate_contract(doc)


# ============================================================================
# 3. The engine fold: costs reduce savings in the year they fire
# ============================================================================

class TestEngineFoldsTransactionCosts(unittest.TestCase):
    """A declared one-time cost MUST reduce the household's investment in the
    fire year (and thus the terminal objective); an absent block must not
    move the golden invariant one cent."""

    def test_cost_reduces_the_fire_year_savings_channel(self):
        doc = _load_doc()
        baseline = _run(doc, years=2)
        doc["transaction_costs"] = [_txn(txn_id="orig", label="origination fees",
                                         kind="cost", date="2026-06-30",
                                         amount=2000.0)]
        with_cost = _run(doc, years=2)
        self.assertAlmostEqual(
            baseline[0].annual_savings - with_cost[0].annual_savings,
            2000.0, places=2,
            msg="a declared $2,000 origination fee must drop the fire year's "
                "savings channel by exactly that amount — otherwise the "
                "one-time friction is still invisible to the objective")

    def test_terminal_total_assets_declines_by_the_compounded_fee(self):
        """The balance-sheet delta survives to the horizon: the no_fee run's
        terminal total_assets must EXCEED the fee run's, the output any fee
        comparison wants to see."""
        doc = _load_doc()
        baseline = _run(doc)
        doc["transaction_costs"] = [
            _txn(txn_id="fee", label="origination/discharge fees", kind="cost",
                 date="2026-06-30", amount=2000.0),
            _txn(txn_id="title", label="title insurance", kind="cost",
                 date="2026-06-30", amount=1500.0),
            _txn(txn_id="rebate", label="transfer-in rebate", kind="credit",
                 date="2026-06-30", amount=500.0),
        ]
        with_costs = _run(doc)
        self.assertGreater(
            baseline[-1].total_assets, with_costs[-1].total_assets,
            "a net $3,000 one-time cost must lower the terminal objective")

    def test_golden_household_is_byte_identical_with_no_fees(self):
        """DP#32 (the crux): the golden household declares no one-time costs,
        so the engine's terminal total_assets must be byte-identical to the
        committed invariant — proving the new block is inert when absent."""
        results = _run_golden(golden_household_config())
        terminal = results[-1].total_assets
        self.assertEqual(
            terminal, TERMINAL_TOTAL_ASSETS,
            f"golden terminal total_assets MOVED: {terminal!r} != "
            f"{TERMINAL_TOTAL_ASSETS!r}")


if __name__ == "__main__":
    unittest.main()