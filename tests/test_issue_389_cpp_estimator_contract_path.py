#!/usr/bin/env python3
"""Issue #389: wire earnings_history / CPP estimator onto the contract path.

`compute_benefit_estimate` existed and was tested, but the JSON contract →
optimize path never mapped `earnings_history` (field absent from the Canada
input schema) and never called the estimator. Omitting `entitlements.cpp`
therefore silently yielded cpp_income=0 for the whole horizon even when a
usable earnings history was available.

This file locks the end-to-end path: schema accepts the leaf, `_map_member`
passes it through, the estimator sets `cpp_monthly_estimated` (age-65 +
CPP2) when no Statement is present, a Statement still wins, and a loud
warning fires when CPP would stay zero despite employment income.

All test data is synthetic (DP#15). Always-on incomes + salary_growth
future years is #390 (see test_issue_390_cpp_estimate_from_incomes.py).
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


def _doc_with_incomes():
    """No Statement and no declared history: the adapter estimates from the
    dated incomes and publishes the series it used under
    ``cpp_estimated_earnings``, so a test can re-price it without rebuilding it."""
    doc = _two_generation_subset(_load_example())
    p1 = next(p for p in doc["people"] if p["id"] == "p1")
    p1.pop("entitlements", None)
    benefits = p1.get("benefits") or {}
    benefits.pop("cpp", None)
    if benefits:
        p1["benefits"] = benefits
    else:
        p1.pop("benefits", None)
    p1.pop("earnings_history", None)
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

    def test_stored_monthly_includes_the_cpp2_tier(self):
        """#388: the stored figure is base CPP **plus** CPP2, at age 65.

        Dropping the CPP2 tier is silent — the household still gets a
        plausible benefit, just short by the enhancement. So assert the
        stored monthly exceeds the base tier and equals their sum.
        """
        from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate
        doc = _doc_with_incomes()
        # p1 is a Quebec resident in the example fixture, so the adapter prices
        # the estimate off the QPP max-benefit tables. Assert the residency
        # rather than assume it: a fixture change must fail here, not silently
        # re-price the test against the wrong table.
        self.assertEqual(
            next(p for p in doc["people"] if p["id"] == "p1")["residency"]["province"],
            "quebec",
        )
        primary, _ = _primary_member(doc)
        # The series the adapter actually estimated from — production output,
        # not a series this test rebuilds.
        entries = [
            EarningsEntry(year=e["year"], employment_income=e["employment_income"])
            for e in primary["cpp_estimated_earnings"]
        ]
        estimate = compute_benefit_estimate(entries, start_age=65, plan="qpp")
        self.assertGreater(estimate.cpp2_age_65_monthly, 0)
        self.assertGreater(
            primary["cpp_monthly_estimated"], estimate.age_65_monthly)
        self.assertAlmostEqual(
            primary["cpp_monthly_estimated"],
            estimate.age_65_monthly + estimate.cpp2_age_65_monthly,
            places=6,
        )

    def test_stored_monthly_is_the_age_65_amount_not_the_claim_age_amount(self):
        """#388: claim-age adjustment happens once, in ``cpp_from_estimate``.

        Storing the already-penalized claim-age figure would apply the early
        reduction a second time.
        """
        from countries.canada.cpp_estimator import EarningsEntry, compute_benefit_estimate
        from countries.canada.retirement_transition import cpp_from_estimate
        primary, _ = _primary_member(_doc_with_incomes())
        entries = [
            EarningsEntry(year=e["year"], employment_income=e["employment_income"])
            for e in primary["cpp_estimated_earnings"]
        ]
        estimate = compute_benefit_estimate(entries, start_age=65, plan="qpp")
        self.assertAlmostEqual(
            primary["cpp_monthly_estimated"],
            estimate.age_65_monthly + estimate.cpp2_age_65_monthly,
            places=6,
        )
        # Exactly one 0.6%/month early reduction (60 months → ×0.64). The
        # double-adjusted figure — what the defect looked like — is the
        # already-penalized age-60 total reduced a second time.
        self.assertAlmostEqual(
            cpp_from_estimate(primary["cpp_monthly_estimated"],
                              start_age=60, claim_age=60),
            (estimate.age_65_monthly + estimate.cpp2_age_65_monthly) * 12 * 0.64,
            places=2,
        )
        double_adjusted = (
            estimate.age_60_monthly + estimate.cpp2_age_60_monthly
        ) * 12 * 0.64
        self.assertGreater(
            abs(double_adjusted - (estimate.age_65_monthly
                                   + estimate.cpp2_age_65_monthly) * 12 * 0.64),
            1000,
        )


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
        self.assertEqual(primary["cpp_benefit_source"], "statement")
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
        # Issue #390: strip employment incomes too — otherwise always-on
        # estimation would set cpp_monthly_estimated and silence the warn.
        for _inc in p1.get("incomes", []):
            _inc["kind"] = "other"
        # Age 55 as of 2026-07-12 — near retirement, no Statement/history/incomes.
        p1["birth_date"] = "1971-03-14"

        with self.assertLogs("contract_people", level="WARNING") as cm:
            ic.to_internal_config(doc)
        self.assertTrue(
            any("person 'p1'" in msg and "cpp_income=0" in msg
                for msg in cm.output),
            msg=f"expected #389 warning, got: {cm.output}",
        )

    def test_younger_earner_without_cpp_does_not_warn(self):
        """Age floor: under 50 with no CPP source stays silent (DP#32).

        Keep pensionable incomes neutered so always-on (#390) cannot set an
        estimate — silence must come from the age gate, not from a
        non-zero estimate. Pair with the age>=50 warning test above.
        """
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("entitlements", None)
        p1.pop("earnings_history", None)
        for _inc in p1.get("incomes", []):
            _inc["kind"] = "other"
        # Explicit mid-40s (example p1 is ~46 as of 2026-07-12; pin it).
        p1["birth_date"] = "1980-03-14"
        try:
            with self.assertLogs("contract_people", level="WARNING") as cm:
                ic.to_internal_config(doc)
            msgs = cm.output
        except AssertionError:
            msgs = []
        self.assertFalse(
            any("cpp_income=0" in msg for msg in msgs),
            msg=f"under-50 with no CPP source must stay silent: {msgs}",
        )

    def test_mid_career_with_incomes_estimate_does_not_warn(self):
        """#390 always-on: mid-career with employment incomes gets an
        estimate, so the silent-zero warning must not fire."""
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        p1.pop("entitlements", None)
        p1.pop("earnings_history", None)
        p1["birth_date"] = "1980-03-14"
        try:
            with self.assertLogs("contract_people", level="WARNING") as cm:
                ic.to_internal_config(doc)
            msgs = cm.output
        except AssertionError:
            msgs = []
        self.assertFalse(
            any("person 'p1'" in msg and "cpp_income=0" in msg for msg in msgs),
            msg=f"estimated earner must not warn: {msgs}",
        )
        primary = next(
            m for m in ic.to_internal_config(doc)["family"]["members"]
            if m["role"] == "primary")
        self.assertGreater(primary["cpp_monthly_estimated"], 0)

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
    """Absent earnings_history AND absent pensionable incomes must not invent
    CPP keys for households that intentionally omit CPP (beyond the warning).

    Issue #390: employment incomes alone trigger always-on estimation — strip
    them here so this case remains the intentional-omit no-op.
    """

    def test_absent_history_sets_no_cpp_keys(self):
        doc = _two_generation_subset(_load_example())
        p1 = next(p for p in doc["people"] if p["id"] == "p1")
        self.assertIsNone(p1.get("earnings_history"))
        self.assertIsNone(p1.get("entitlements"))
        for _inc in p1.get("incomes", []):
            _inc["kind"] = "other"
        # Suppress expected #389/#390 warning for this absence check.
        logging.getLogger("contract_people").setLevel(logging.ERROR)
        try:
            legacy = ic.to_internal_config(doc)
        finally:
            logging.getLogger("contract_people").setLevel(logging.NOTSET)
        primary = next(m for m in legacy["family"]["members"]
                       if m["role"] == "primary")
        for key in ("cpp_monthly_estimated", "cpp_start_age",
                    "earnings_history", "cpp_benefit_source"):
            self.assertNotIn(key, primary)



class MissingBirthYearFailsLoudly(unittest.TestCase):
    """DP#1/DP#32 / #389 follow-up: never invent a fabricated birth year."""

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
        # assertRaises proves _map_member did not return a member — no
        # successful result to inspect (a locals()-based check is vacuous).
        with self.assertRaises(ContractAdaptationError) as ctx:
            _map_member(doc, "p1", "primary", {})
        msg = str(ctx.exception)
        self.assertIn("earnings_history", msg)
        self.assertTrue(
            "birth_year" in msg or "birth_date" in msg,
            msg=f"expected birth_year/birth_date in: {msg}",
        )
        # The refusal must not hand back a plausible-looking person: a
        # substituted birth year is the DP#32 defect this guards.
        self.assertNotRegex(msg, r"\bbirth_year\s*=\s*\d{4}\b")



if __name__ == "__main__":
    unittest.main()
