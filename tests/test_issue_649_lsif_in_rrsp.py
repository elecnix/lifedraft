#!/usr/bin/env python3
"""Issue #649: an LSIF held INSIDE an RRSP wrapper (the Quebec 'REER FTQ').

The contract modelled accounts as flat: `lsif` and `rrsp` were disjoint
account kinds, so it could not express the overwhelmingly common Quebec
holding -- Fonds de solidarite FTQ shares held inside an RRSP. That nesting
matters because the wrapper (RRSP) supplies the deduction/deferral while the
nested LSIF portion earns its own 30% credit (15% federal + 15% Quebec).

The fix exposes the EXISTING LSIF-credit path (countries/canada/lsif_credit.py)
through a nested contract leaf: a kind=rrsp/spousal_rrsp account may now carry
an `lsif` sub-object with a `holding_amount` naming the LSIF portion. The RRSP
balance still flows into the RRSP pot (wrapper unchanged); the nested leaf adds
only the credit, computed on the declared holding_amount.

All fixtures are synthetic (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import input_contract as ic
from input_contract import ContractAdaptationError
from test_input_contract import _load_example, _two_generation_subset


def _nested_lsif_block(holding_amount):
    """A valid `lsif` sub-object for an LSIF nested inside an RRSP wrapper."""
    return {
        "purchase_date": "2026-03-01",
        "purchase_province": "quebec",
        "federally_registered": False,
        "prior_redemption": False,
        "is_hbp_replacement": False,
        "quebec_carryforward": 0,
        "acquisition_date": "2026-03-01",
        "redeemed_date": None,
        "reference_year_taxable_income": None,
        "holding_amount": holding_amount,
    }


def _doc_with_rrsp_lsif(holding_amount=5000):
    """Two-generation example with the standalone lsif account removed and a
    nested LSIF holding declared inside p1's RRSP (exactly one LSIF holding)."""
    doc = _two_generation_subset(_load_example())
    doc["accounts"] = [a for a in doc["accounts"] if a["kind"] != "lsif"]
    p1_rrsp = next(a for a in doc["accounts"] if a["id"] == "p1_rrsp")
    p1_rrsp["lsif"] = _nested_lsif_block(holding_amount)
    return doc


def _doc_without_any_lsif():
    doc = _two_generation_subset(_load_example())
    doc["accounts"] = [a for a in doc["accounts"] if a["kind"] != "lsif"]
    return doc


class TestLSIFInsideRRSP(unittest.TestCase):
    """The nested LSIF holding reaches the engine's LSIF-credit path."""

    def test_nested_block_validates_on_an_rrsp(self):
        ic.validate_contract(_doc_with_rrsp_lsif())  # raises on any violation

    def test_credit_is_computed_on_the_holding_amount_not_the_balance(self):
        # The RRSP is $210,000 but only $5,000 of it is LSIF: the credit must
        # be sized to the declared LSIF portion, never the whole wrapper.
        legacy = ic.to_internal_config(_doc_with_rrsp_lsif(holding_amount=5000))
        self.assertIn("lsif", legacy)
        self.assertEqual(legacy["lsif"]["purchase_amount"], 5000)

    def test_wrapper_rrsp_balance_still_flows_through(self):
        # The nested leaf adds the credit; it must NOT change the RRSP balance
        # mapping (the wrapper's deduction/deferral is the balance's job).
        legacy = ic.to_internal_config(_doc_with_rrsp_lsif())
        primary = next(m for m in legacy["family"]["members"]
                       if m["role"] == "primary")
        self.assertEqual(primary["rrsp_balance"], 210000)

    def test_nested_lsif_requires_a_holding_amount(self):
        # DP#32: the LSIF portion of the wrapper cannot be guessed from the
        # whole RRSP balance -- an undeclared holding_amount fails loudly.
        doc = _doc_with_rrsp_lsif()
        p1_rrsp = next(a for a in doc["accounts"] if a["id"] == "p1_rrsp")
        p1_rrsp["lsif"]["holding_amount"] = None
        with self.assertRaises(ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_two_lsif_holdings_are_refused(self):
        # The engine represents exactly one LSIF purchase: a standalone lsif
        # account AND a nested-in-RRSP holding together is more than one.
        doc = _doc_with_rrsp_lsif()
        standalone = next(a for a in _two_generation_subset(_load_example())["accounts"]
                          if a["kind"] == "lsif")
        doc["accounts"].append(standalone)
        with self.assertRaises(ContractAdaptationError):
            ic.to_internal_config(doc)


class TestAbsenceIsNoOp(unittest.TestCase):
    """The new optional leaf is a no-op when it is not declared."""

    def test_rrsp_without_nested_lsif_produces_no_lsif_config(self):
        legacy = ic.to_internal_config(_doc_without_any_lsif())
        self.assertNotIn("lsif", legacy)

    def test_an_rrsp_without_the_leaf_still_validates(self):
        ic.validate_contract(_doc_without_any_lsif())

    def test_lsif_block_still_forbidden_on_a_non_wrapper_kind(self):
        # The relaxation is scoped to rrsp/spousal_rrsp: a tfsa carrying an
        # lsif block must still be rejected by the schema.
        doc = _two_generation_subset(_load_example())
        tfsa = next(a for a in doc["accounts"] if a["kind"] == "tfsa")
        tfsa["lsif"] = _nested_lsif_block(5000)
        with self.assertRaises(Exception):
            ic.validate_contract(doc)

    def test_golden_invariant_unchanged(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from test_golden_trajectory_581 import golden_household_config, _run
        self.assertEqual(
            _run(golden_household_config())[-1].total_assets,
            9709753.139463063,
        )


if __name__ == "__main__":
    unittest.main()
