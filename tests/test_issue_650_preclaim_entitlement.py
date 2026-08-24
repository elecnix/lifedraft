#!/usr/bin/env python3
"""Issue #650: the contract can express a PRE-CLAIM CPP/OAS entitlement.

Before this change the contract could only describe a benefit ALREADY IN PAY
(`benefits.cpp/oas` with a committed start_date). A pre-retirement household --
nobody drawing CPP/OAS yet, but holding a Service Canada statement projecting a
monthly amount at 65 -- had nowhere to put that number, so the engine had to
default/guess it (the DP#32 defect class, in a field the user can answer).

`entitlements.{cpp,oas}` is the new leaf: the dated estimate + a MODELED claim
age, mapped onto the SAME internal keys member_retirement_income already reads
(cpp_monthly_estimated / cpp_start_age / oas_start_age / oas_defer_months), so
the retirement-income model begins the benefit at the modeled claim age.

All test data is synthetic (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from countries.canada.retirement_transition import member_retirement_income
from test_input_contract import _load_example, _two_generation_subset
import contract_errors
import contract_schema


def _preclaim_doc():
    """Two-generation household (p1/p2 both pre-retirement, born 1980/1982,
    nobody drawing a benefit) -- exactly issue #650's migrating household."""
    return _two_generation_subset(_load_example())


def _primary(doc):
    return next(p for p in doc["people"] if p["id"] == "p1")


class PreClaimCppEntitlementReachesEngine(unittest.TestCase):
    """A declared pre-claim CPP entitlement maps onto the read keys and pays
    out from the modeled claim age."""

    def setUp(self):
        self.doc = _preclaim_doc()
        _primary(self.doc)["entitlements"] = {
            "cpp": {"estimated_monthly_at_65": 1300,
                    "as_of": "2026-01-01", "claim_age": 65}
        }

    def test_document_validates(self):
        contract_schema.validate_contract(self.doc)  # raises on any violation

    def test_estimate_and_claim_age_land_on_read_keys(self):
        legacy = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(legacy)
        primary = next(m for m in cfg.family_members if m["role"] == "primary")
        self.assertEqual(primary["cpp_monthly_estimated"], 1300)
        self.assertEqual(primary["cpp_start_age"], 65)

    def test_benefit_flows_from_the_modeled_claim_age(self):
        legacy = ic.to_internal_config(self.doc)
        primary = next(m for m in legacy["family"]["members"]
                       if m["role"] == "primary")
        # p1 born 1980; retirement_age = first candidate (60), so retired by
        # 2040. CPP is gated on the modeled claim age (65 => 2045).
        before = member_retirement_income(primary, 2043, oas_annual_max=8500,
                                           oas_clawback_threshold=90000)
        at_claim = member_retirement_income(primary, 2045, oas_annual_max=8500,
                                            oas_clawback_threshold=90000)
        self.assertEqual(before.cpp, 0.0)          # retired but pre-claim-age
        self.assertEqual(at_claim.cpp, 1300 * 12)  # estimate-at-65 pays in full


class PreClaimOasEntitlementReachesEngine(unittest.TestCase):
    """A declared pre-claim OAS entitlement sets the modeled claim age and the
    DERIVED deferral bonus (DP#32: claim age stated once)."""

    def test_deferred_oas_claim_age_and_derived_defer_months(self):
        doc = _preclaim_doc()
        _primary(doc)["entitlements"] = {
            "oas": {"as_of": "2026-01-01", "claim_age": 68}
        }
        legacy = ic.to_internal_config(doc)
        primary = next(m for m in legacy["family"]["members"]
                       if m["role"] == "primary")
        self.assertEqual(primary["oas_start_age"], 68)
        self.assertEqual(primary["oas_defer_months"], (68 - 65) * 12)


class InPayAndPreClaimAreMutuallyExclusive(unittest.TestCase):
    """A person is either drawing a benefit or not -- declaring both spellings
    for the same program is refused loudly, never silently merged."""

    def test_both_cpp_spellings_refused(self):
        doc = _preclaim_doc()
        p1 = _primary(doc)
        p1["benefits"] = {"cpp": {"start_date": "2026-08-01",
                                  "monthly_amount": 900, "as_of": "2026-01-01"}}
        p1["entitlements"] = {"cpp": {"estimated_monthly_at_65": 1300,
                                      "as_of": "2026-01-01", "claim_age": 65}}
        with self.assertRaises(contract_errors.ContractValidationError):
            ic.to_internal_config(doc)


class TestAbsenceIsNoOp(unittest.TestCase):
    """The new optional leaf must be a pure no-op when undeclared: an absent
    `entitlements` writes NONE of its keys, leaving the mapping byte-identical
    to the pre-#650 output (DP#32: absence is not a coerced default)."""

    def test_absent_entitlement_sets_no_cpp_or_oas_keys(self):
        doc = _preclaim_doc()
        self.assertIsNone(_primary(doc).get("entitlements"))
        legacy = ic.to_internal_config(doc)
        primary = next(m for m in legacy["family"]["members"]
                       if m["role"] == "primary")
        for key in ("cpp_monthly_estimated", "cpp_start_age",
                    "oas_start_age", "oas_defer_months"):
            self.assertNotIn(key, primary)

    def test_mapping_identical_with_and_without_empty_entitlements(self):
        base = ic.to_internal_config(_preclaim_doc())
        with_key = _preclaim_doc()
        # An explicitly-empty entitlements object is still a no-op.
        _primary(with_key)["entitlements"] = {}
        self.assertEqual(ic.to_internal_config(with_key), base)


if __name__ == "__main__":
    unittest.main()
