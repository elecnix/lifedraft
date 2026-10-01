#!/usr/bin/env python3
"""Issue #389: wire earnings_history / CPP estimator onto the contract path.

`compute_benefit_estimate` and `MemberRetirementData.from_dict(...,
earnings_history=...)` existed and were tested, but the JSON contract →
optimize path never mapped `earnings_history` (field absent from the Canada
input schema) and never called the estimator. Omitting `entitlements.cpp`
therefore silently yielded cpp_income=0 for the whole horizon even when a
usable earnings history was available.

This file locks the end-to-end path: schema accepts the leaf, `_map_member`
passes it through, the estimator sets `cpp_monthly_estimated` (age-65 +
CPP2) when no Statement is present, a Statement still wins, and a loud
warning fires when CPP would stay zero despite employment income.

All test data is synthetic (DP#15). Full always-on incomes + salary_growth
future years is #390 and is deliberately out of scope here.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from countries.canada.retirement_transition import member_retirement_income
from countries.canada.retirement import MemberRetirementData
from contract_errors import ContractAdaptationError
from test_input_contract import _load_example, _two_generation_subset
import contract_schema


def _doc_with_history():
    """Two-generation household; primary gets a full synthetic career history
    and NO Statement entitlement/claim."""
    doc = _two_generation_subset(_load_example())
    p1 = next(p for p in doc["people"] if p["id"] == "p1")
    p1.pop("entitlements", None)
    benefits = p1.get("benefits") or {}
    benefits.pop("cpp", None)
    if benefits:
        p1["benefits"] = benefits
    else:
        p1.pop("benefits", None)
    # 35 years at YMPE-ish income — enough for a clearly non-zero estimate.
    p1["earnings_history"] = [
        {"year": 1990 + i, "employment_income": 100_000} for i in range(35)
    ]
    return doc


def _primary_member(doc):
    legacy = ic.to_internal_config(doc)
    return next(m for m in legacy["family"]["members"] if m["role"] == "primary"), legacy


class EarningsHistorySchemaAccepts(unittest.TestCase):
    def test_document_with_earnings_history_validates(self):
        contract_schema.validate_contract(_doc_with_history())


class EarningsHistoryReachesEngine(unittest.TestCase):
    """Omitting entitlements.cpp + supplying earnings_history yields a
    non-zero cpp_monthly_estimated on the mapped member."""

    def test_estimate_lands_on_read_keys(self):
        primary, _ = _primary_member(_doc_with_history())
        self.assertIn("earnings_history", primary)
        self.assertEqual(len(primary["earnings_history"]), 35)
        self.assertGreater(primary["cpp_monthly_estimated"], 0)
        self.assertEqual(primary.get("cpp_start_age"), 65)

    def test_benefit_flows_at_claim_age(self):
        primary, _ = _primary_member(_doc_with_history())
        # p1 born 1980; claim age default 65 => 2045.
        before = member_retirement_income(
            primary, 2043, oas_annual_max=8500, oas_clawback_threshold=90000)
        at_claim = member_retirement_income(
            primary, 2045, oas_annual_max=8500, oas_clawback_threshold=90000)
        self.assertEqual(before.cpp, 0.0)
        self.assertGreater(at_claim.cpp, 0.0)
        self.assertEqual(at_claim.cpp, primary["cpp_monthly_estimated"] * 12)

    def test_simulation_config_round_trips_estimate(self):
        _, legacy = _primary_member(_doc_with_history())
        cfg = SimulationConfig.from_dict(legacy)
        primary = next(m for m in cfg.family_members if m["role"] == "primary")
        self.assertGreater(primary["cpp_monthly_estimated"], 0)


class StatementPrecedence(unittest.TestCase):
    """A Service Canada Statement (entitlements.cpp or benefits.cpp) always
    wins — the estimator must not blend or override it."""

    def test_entitlements_cpp_wins_over_earnings_history(self):
        doc = _doc_with_history()
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1["entitlements"] = {
            "cpp": {"estimated_monthly_at_65": 1300,
                    "as_of": "2026-01-01", "claim_age": 67},
        }
        primary, _ = _primary_member(doc)
        self.assertEqual(primary["cpp_monthly_estimated"], 1300)
        self.assertEqual(primary["cpp_start_age"], 67)
        # History is still carried for provenance / later tools.
        self.assertIn("earnings_history", primary)

    def test_in_pay_benefits_cpp_wins_over_earnings_history(self):
        doc = _doc_with_history()
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        # Shift birth so an in-pay start_date around as_of maps to a claim age.
        p1["birth_date"] = "1960-03-14"
        p1["benefits"] = {
            "cpp": {"start_date": "2025-08-01",
                    "monthly_amount": 900, "as_of": "2026-01-01"},
        }
        primary, _ = _primary_member(doc)
        self.assertEqual(primary["cpp_monthly_estimated"], 900)
        self.assertIn("cpp_start_age", primary)


class SilentZeroWarned(unittest.TestCase):
    """When CPP would stay zero despite employment income, warn loudly."""

    def test_warning_when_near_retirement_but_no_cpp_source(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("entitlements", None)
        p1.pop("earnings_history", None)
        # Age 55 as of 2026-07-12 — near retirement, no Statement/history.
        p1["birth_date"] = "1971-03-14"

        with self.assertLogs("contract_people", level="WARNING") as cm:
            ic.to_internal_config(doc)
        self.assertTrue(
            any("person 'p1'" in msg and "cpp_income=0" in msg
                for msg in cm.output),
            msg=f"expected #389 warning, got: {cm.output}",
        )

    def test_younger_earner_without_cpp_does_not_warn(self):
        """Golden-example ages (~mid-40s) intentionally omit CPP; silence."""
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("entitlements", None)
        p1.pop("earnings_history", None)
        try:
            with self.assertLogs("contract_people", level="WARNING") as cm:
                ic.to_internal_config(doc)
            msgs = cm.output
        except AssertionError:
            msgs = []
        self.assertFalse(
            any("cpp_income=0" in msg for msg in msgs),
            msg=f"mid-career earners must stay silent: {msgs}",
        )

    def test_no_warning_for_person_with_estimate(self):
        doc = _doc_with_history()
        # Spouse (p2) still has employment and no history — may warn for p2.
        # The person WITH earnings_history (p1) must not be warned about.
        try:
            with self.assertLogs("contract_people", level="WARNING") as cm:
                ic.to_internal_config(doc)
            msgs = cm.output
        except AssertionError:
            msgs = []
        self.assertFalse(
            any("person 'p1'" in msg and "cpp_income=0" in msg for msg in msgs),
            msg=f"p1 has an estimate; must not warn: {msgs}",
        )


class AbsentHistoryIsNoOp(unittest.TestCase):
    """Absent earnings_history must not invent keys for households that
    intentionally omit CPP (beyond the warning)."""

    def test_absent_history_sets_no_cpp_keys(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        self.assertIsNone(p1.get("earnings_history"))
        self.assertIsNone(p1.get("entitlements"))
        # Suppress expected #389 warning for this absence check.
        logging.getLogger("contract_people").setLevel(logging.ERROR)
        try:
            legacy = ic.to_internal_config(doc)
        finally:
            logging.getLogger("contract_people").setLevel(logging.NOTSET)
        primary = next(m for m in legacy["family"]["members"]
                       if m["role"] == "primary")
        for key in ("cpp_monthly_estimated", "cpp_start_age",
                    "earnings_history"):
            self.assertNotIn(key, primary)



class MissingBirthYearFailsLoudly(unittest.TestCase):
    """DP#1/DP#32 / #389 follow-up: never invent a fabricated birth year."""

    def test_from_dict_without_birth_year_raises(self):
        with self.assertRaises(ValueError) as ctx:
            MemberRetirementData.from_dict({
                "role": "primary",
                "cpp_monthly_estimated": 0,
                "earnings_history": [
                    {"year": 2000, "employment_income": 80_000},
                ],
            })
        msg = str(ctx.exception)
        self.assertIn("birth_year is required", msg)
        # Error must describe the requirement, not substitute a person year.
        self.assertNotRegex(msg, r"\b19[0-9]{2}\b")

    def test_from_dict_without_birth_year_does_not_invent_a_person(self):
        """A partial dict must raise — never construct with a fabricated year."""
        with self.assertRaises(ValueError):
            MemberRetirementData.from_dict({"role": "primary"})

    def test_contract_earnings_history_without_birth_date_refuses(self):
        """_map_member with earnings_history but no birth_date must refuse
        loudly (ContractAdaptationError). map_members also refuses adults
        without DOB; this exercises the estimator path directly."""
        from contract_people import _map_member
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1["birth_date"] = None
        p1["earnings_history"] = [
            {"year": 1990 + i, "employment_income": 100_000} for i in range(35)
        ]
        p1.pop("entitlements", None)
        p1.pop("benefits", None)
        with self.assertRaises(ContractAdaptationError) as ctx:
            member = _map_member(doc, "p1", "primary", {})
        msg = str(ctx.exception)
        self.assertIn("earnings_history", msg)
        self.assertTrue(
            "birth_year" in msg or "birth_date" in msg,
            msg=f"expected birth_year/birth_date in: {msg}",
        )
        # Must not have silently produced a dated member.
        self.assertNotIn("cpp_monthly_estimated", locals().get("member", {}) or {})



if __name__ == "__main__":
    unittest.main()
