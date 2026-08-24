#!/usr/bin/env python3
"""Tests for issue #826: Fonds FTQ product flag encodes the program's
well-known rules (locked-until-65 + 7.3% 10-yr-CAR expected return) in the
module, not restated in the input JSON (DP#7/#10/#12).

This corrects #823's design: #823 added the GENERIC expected_return /
locked_until primitives (kept); #826 makes an account flagged
``product='fonds_ftq'`` pick up the FTQ module's rules with NO restatement,
while an explicit account.expected_return / account.locked_until still wins
(DP#13).

Tests:
  - The FTQ module's sourced constants (7.3% CAR, unlock age 65).
  - resolve_product: fonds_ftq -> ProductRules; None -> None; unknown raises.
  - An account with product='fonds_ftq' and NO explicit fields gets 7.3% +
    locked-until-65 from the module, feeding the SAME #823 downstream maps.
  - An explicit expected_return / locked_until on the same account OVERRIDES
    the product default (DP#13).
  - Golden unchanged (no product declared).

All test data uses fabricated round numbers per DP#13/DP#15.
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from countries.canada import fonds_ftq
from simulation_config import SimulationConfig
import contract_errors
import contract_schema


def _load_example_doc():
    """The shipped contract example, trimmed to the two-generation subset the
    adapter maps (same helper tests/test_input_contract.py uses)."""
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = json.load(f)
    doc = copy.deepcopy(doc)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"]
                              if r["person"] in keep]

    def _owner_id(acc):
        o = acc.get("owner")
        if isinstance(o, dict):
            return o.get("person")
        return o

    doc["accounts"] = [a for a in doc["accounts"] if _owner_id(a) in keep]
    return doc


def _p1_rrsp(doc):
    return next(a for a in doc["accounts"]
                if a["kind"] == "rrsp" and a.get("owner") == "p1")


# ── The FTQ module's sourced rules ──────────────────────────────────────────

class TestFondsFtqModule(unittest.TestCase):
    def test_sourced_10yr_car(self):
        """The module's expected return is FTQ's published 10-year CAR (7.3%),
        a sourced/centrally-maintained figure (DP#12), not a bare magic
        number."""
        self.assertAlmostEqual(fonds_ftq.FTQ_10Y_CAR, 0.073)

    def test_unlock_age_65(self):
        """RRSP-held FTQ shares are locked until age 65 (the standard unlock)."""
        self.assertEqual(fonds_ftq.FTQ_UNLOCK_AGE, 65)

    def test_ftq_product_rules_carries_both_rules(self):
        r = fonds_ftq.ftq_product_rules()
        self.assertAlmostEqual(r.expected_return, 0.073)
        self.assertEqual(r.locked_until, {"age": 65})

    def test_resolve_fonds_ftq(self):
        r = fonds_ftq.resolve_product("fonds_ftq")
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r.expected_return, 0.073)
        self.assertEqual(r.locked_until, {"age": 65})

    def test_resolve_none_is_generic(self):
        """No product flag -> None (a generic account with no product rules;
        today's behaviour, golden)."""
        self.assertIsNone(fonds_ftq.resolve_product(None))
        self.assertIsNone(fonds_ftq.resolve_product(""))

    def test_resolve_unknown_product_raises(self):
        """DP#32: an unknown product id is a typo / not-yet-implemented
        product, not a silent fallback to generic."""
        with self.assertRaises(ValueError):
            fonds_ftq.resolve_product("not_a_product")

    def test_lsif_credit_reexported(self):
        """The LSIF 30% credit is owned by lsif_credit.py and re-exported
        through the FTQ module (DP#10: one program entry point; no
        duplication)."""
        from countries.canada import lsif_credit
        self.assertIs(fonds_ftq.compute_lsif_credit, lsif_credit.compute_lsif_credit)
        self.assertIs(fonds_ftq.LSIFPurchase, lsif_credit.LSIFPurchase)
        self.assertEqual(fonds_ftq.LSIF_PURCHASE_MAX, 5000)


# ── Contract resolution: product flag -> #823 downstream maps ───────────────

class TestProductResolution(unittest.TestCase):
    def test_ftq_product_supplies_return_and_lock_with_no_explicit_fields(self):
        """An account flagged product='fonds_ftq' with NO explicit
        expected_return / locked_until gets 7.3% + locked-until-65 from the
        module, feeding the SAME #823 downstream maps (issue #826)."""
        doc = _load_example_doc()
        rrsp = _p1_rrsp(doc)
        rrsp["product"] = "fonds_ftq"
        # No explicit expected_return / locked_until on the account.
        rrsp.pop("expected_return", None)
        rrsp.pop("locked_until", None)
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        # The rrsp override pot is populated at the module's 7.3%.
        self.assertIn("rrsp", cfg.account_return_overrides)
        ov = cfg.account_return_overrides["rrsp"]
        self.assertAlmostEqual(ov["override_balance"], rrsp["balance"]["amount"])
        self.assertAlmostEqual(ov["weighted_rate_sum"],
                               rrsp["balance"]["amount"] * 0.073)
        # The locked map carries unlock_age 65 + the owner's birth year.
        self.assertIn("rrsp", cfg.account_locked)
        entry = cfg.account_locked["rrsp"][0]
        self.assertEqual(entry["unlock_age"], 65)
        self.assertEqual(entry["balance"], rrsp["balance"]["amount"])
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        self.assertEqual(entry["owner_birth_year"], int(p1["birth_date"][:4]))

    def test_explicit_expected_return_overrides_product_default(self):
        """DP#13: an explicit account.expected_return wins over the product
        module's 7.3% default -- a household that disagrees with the module's
        rate declares its own and it wins."""
        doc = _load_example_doc()
        rrsp = _p1_rrsp(doc)
        rrsp["product"] = "fonds_ftq"
        rrsp["expected_return"] = 0.05  # explicit, beats the 7.3% default
        rrsp.pop("locked_until", None)
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        ov = cfg.account_return_overrides["rrsp"]
        self.assertAlmostEqual(ov["weighted_rate_sum"],
                               rrsp["balance"]["amount"] * 0.05)

    def test_explicit_locked_until_overrides_product_default(self):
        """DP#13: an explicit account.locked_until wins over the product
        module's locked-until-65 default."""
        doc = _load_example_doc()
        rrsp = _p1_rrsp(doc)
        rrsp["product"] = "fonds_ftq"
        rrsp.pop("expected_return", None)
        rrsp["locked_until"] = {"age": 55}  # explicit, beats the 65 default
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        entry = cfg.account_locked["rrsp"][0]
        self.assertEqual(entry["unlock_age"], 55)

    def test_explicit_null_does_not_override_product(self):
        """An explicit null expected_return means 'I have no opinion' -- the
        product default STILL applies (null is absence, not a zero override;
        DP#32). The product flag is what triggers the module's rules; an
        explicit null is not a declared value that beats a fallback."""
        doc = _load_example_doc()
        rrsp = _p1_rrsp(doc)
        rrsp["product"] = "fonds_ftq"
        rrsp["expected_return"] = None  # explicit null = no opinion
        rrsp["locked_until"] = None
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        # The product default (7.3%) still applies.
        ov = cfg.account_return_overrides["rrsp"]
        self.assertAlmostEqual(ov["weighted_rate_sum"],
                               rrsp["balance"]["amount"] * 0.073)
        self.assertEqual(cfg.account_locked["rrsp"][0]["unlock_age"], 65)

    def test_no_product_no_explicit_is_absent(self):
        """The unmodified example declares no product and no explicit override
        -- the config's override maps are empty (golden: today's global-rate,
        fully-liquid behaviour, DP#32)."""
        doc = _load_example_doc()
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        self.assertEqual(cfg.account_return_overrides, {})
        self.assertEqual(cfg.account_locked, {})

    def test_ftq_product_only_supplies_for_that_account(self):
        """A product flag on one RRSP account does not bleed into the other
        spouse's RRSP account (the override is balance-weighted within the
        rrsp pot, but only the flagged account's balance carries the 7.3%
        rate)."""
        doc = _load_example_doc()
        p1_rrsp = _p1_rrsp(doc)
        p1_rrsp["product"] = "fonds_ftq"
        p1_rrsp.pop("expected_return", None)
        p1_rrsp.pop("locked_until", None)
        contract_schema.validate_contract(doc)
        cfg = SimulationConfig.from_dict(ic.to_internal_config(doc))
        ov = cfg.account_return_overrides["rrsp"]
        # Only p1's flagged balance is in the override; the weighted rate sum
        # is p1's balance * 7.3%, NOT the whole rrsp pot * 7.3%.
        self.assertAlmostEqual(ov["override_balance"], p1_rrsp["balance"]["amount"])
        self.assertAlmostEqual(ov["weighted_rate_sum"],
                               p1_rrsp["balance"]["amount"] * 0.073)


# ── Schema validation ───────────────────────────────────────────────────────

class TestProductSchema(unittest.TestCase):
    def test_valid_product_accepted(self):
        doc = _load_example_doc()
        _p1_rrsp(doc)["product"] = "fonds_ftq"
        contract_schema.validate_contract(doc)  # no exception

    def test_unknown_product_rejected(self):
        doc = _load_example_doc()
        _p1_rrsp(doc)["product"] = "not_a_product"
        with self.assertRaises(contract_errors.ContractValidationError):
            contract_schema.validate_contract(doc)

    def test_null_product_accepted(self):
        doc = _load_example_doc()
        _p1_rrsp(doc)["product"] = None
        contract_schema.validate_contract(doc)  # no exception


if __name__ == "__main__":
    unittest.main()
