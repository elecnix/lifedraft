#!/usr/bin/env python3
"""Tests for issue #767: employment-contract terms (non-competition,
probation, notice) constrain when a job-loss recovery income segment can
begin.

The contract could not previously express that a terminated employee is
bound by a non-competition covenant, so a recovery ``income_override``
could be dated BEFORE the contract/law allows re-employment -- overstating
runway (the household appears to be back on full salary during a window
when re-employment in the person's specialty is legally barred).

This file covers:
  1. A recovery segment dated INSIDE a declared 12-month non-compete is
     CLAMPED forward to the earliest allowed date (termination + months),
     and the shock segment is EXTENDED to cover the window so the engine
     does not silently revert to base salary during the covenant.
  2. The clamp reaches the engine: the non-compete year's primary income
     reflects EI-level income (not base salary), i.e. runway shrinks.
  3. A partial non-compete declaration (scope but no months) FAILS LOUDLY
     at schema validation (DP#32), never silently defaulted.
  4. A contract with no ``employment`` block still loads (backward compat).
  5. ``notice_days`` is modelled as a paid employment-income segment at
     termination, extending runway.

All data is fabricated: round numbers, role-based names (DP#4, DP#15).
"""
import json
import os
import sys
import copy
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from input_contract import (
    _apply_non_compete_to_overrides,
    _apply_notice_segments,
    _add_months,
)
from datetime import date


def _owner_ids(owner):
    """An owner string 'p1' or a joint dict {'joint': [...]} -> set of ids."""
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _two_generation_subset(doc: dict) -> dict:
    """Trim example.json's 4-generation household to the couple + children
    the legacy engine can run (mirrors the established pattern in
    tests/architecture/test_dp_income_scenario_reaches_engine.py)."""
    doc = copy.deepcopy(doc)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"]
                             if r["person"] in keep]
    doc["accounts"] = [a for a in doc["accounts"]
                      if _owner_ids(a["owner"]) <= keep]
    doc["liabilities"] = [l for l in doc["liabilities"]
                         if _owner_ids(l["owner"]) <= keep]
    doc["properties"] = [p for p in doc["properties"]
                        if _owner_ids(p["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep]
    return doc


def _example_doc():
    """schema/example.json, whose p1_employment income now declares a
    12-month non-compete (months=12, notice_days=0) -- the fixture this
    file's assertions hinge on."""
    with open(ic.EXAMPLE_PATH) as f:
        return json.load(f)


def _runnable_doc():
    """The example trimmed to two generations so to_internal_config accepts
    it (the legacy engine is two-adults-plus-children, #598)."""
    return _two_generation_subset(_example_doc())


def _people_by_id(doc):
    return ic._people_by_id(doc)


def _person_income_ids(doc):
    return {
        inc["id"]: pid
        for pid, p in _people_by_id(doc).items()
        for inc in p.get("incomes", [])
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. The non-compete clamps a recovery segment dated inside the window.
# ═══════════════════════════════════════════════════════════════════════════

class TestNonCompeteClampsRecovery(unittest.TestCase):
    """A recovery income_override dated before termination + non_compete
    .months is pushed forward to that earliest-allowed date."""

    def _overrides_with_recovery_inside_window(self):
        """A job-loss shock (EI for 6 months from 2027-01-01) followed by a
        re-employment recovery dated 2027-07-01 -- INSIDE a 12-month
        non-compete that bars re-employment before 2028-01-01."""
        return [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2027-07-01"},
            {"income_id": "p1_employment", "kind": "employment",
             "amount": 110_000, "from": "2027-07-01", "to": None},
        ]

    def test_recovery_from_is_clamped_to_termination_plus_months(self):
        doc = _example_doc()
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        out = _apply_non_compete_to_overrides(
            self._overrides_with_recovery_inside_window(),
            people, pids, "sc1")
        recovery = next(o for o in out if o["kind"] == "employment"
                        and o["from"] != "2015-01-01")
        self.assertEqual(
            recovery["from"], "2028-01-01",
            "a recovery dated 2027-07-01 inside a 12-month non-compete "
            "(termination 2027-01-01) must be clamped to 2028-01-01.")

    def test_shock_to_is_extended_to_cover_the_non_compete_window(self):
        """Without extending the shock's `to`, the gap between the EI segment
        end (2027-07-01) and the clamped recovery (2028-01-01) would revert
        to BASE salary -- silently modelling re-employment the contract
        forbids. The shock must be extended to 2028-01-01."""
        doc = _example_doc()
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        out = _apply_non_compete_to_overrides(
            self._overrides_with_recovery_inside_window(),
            people, pids, "sc1")
        shock = next(o for o in out if o["kind"] == "ei")
        self.assertEqual(
            shock["to"], "2028-01-01",
            "the EI shock must be extended to 2028-01-01 so the non-compete "
            "window is covered at EI-level income, not base salary.")

    def test_a_recovery_dated_outside_the_window_is_untouched(self):
        """A recovery dated AFTER the non-compete expiry is legitimate and
        must NOT be clamped (the covenant has expired)."""
        doc = _example_doc()
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        overrides = [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2028-06-01"},
            {"income_id": "p1_employment", "kind": "employment",
             "amount": 110_000, "from": "2028-06-01", "to": None},
        ]
        out = _apply_non_compete_to_overrides(overrides, people, pids, "sc1")
        recovery = next(o for o in out if o["kind"] == "employment"
                        and o["from"] != "2015-01-01")
        self.assertEqual(recovery["from"], "2028-06-01",
                         "a recovery after the non-compete expiry is untouched.")

    def test_continued_ei_during_the_window_is_not_clamped(self):
        """A second EI segment (not re-employment) during the non-compete
        window is legitimate -- the covenant bars re-employment, not the
        statutory EI benefit. Its `from` must NOT be clamped."""
        doc = _example_doc()
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        overrides = [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2027-07-01"},
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 20_000, "from": "2027-07-01", "to": "2028-01-01"},
            {"income_id": "p1_employment", "kind": "employment",
             "amount": 110_000, "from": "2028-01-01", "to": None},
        ]
        out = _apply_non_compete_to_overrides(overrides, people, pids, "sc1")
        second_ei = [o for o in out if o["kind"] == "ei"][1]
        self.assertEqual(second_ei["from"], "2027-07-01",
                         "continued EI during the non-compete is not clamped.")

    def test_shock_with_no_reemployment_segment_is_untouched(self):
        """A job-loss schedule with a shock and continued EI but NO
        re-employment recovery segment has nothing for the non-compete to
        clamp (the covenant bars re-employment, and there is no
        re-employment segment to bar). Both EI segments pass through
        unchanged and no warning fires.

        This exercises the `recovery is None` branch of the first pass -- a
        multi-segment schedule that is NOT a shock-then-re-employment shape
        must not be misread as one."""
        doc = _example_doc()
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        overrides = [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2027-07-01"},
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 20_000, "from": "2027-07-01", "to": None},
        ]
        import logging
        with self.assertNoLogs("input_contract", level="WARNING"):
            out = _apply_non_compete_to_overrides(overrides, people, pids, "sc1")
        # Both EI segments are unchanged (no recovery to clamp, no shock `to`
        # to extend -- the second EI is open-ended).
        self.assertEqual(out[0]["to"], "2027-07-01")
        self.assertIsNone(out[1]["to"])
        self.assertEqual(out[1]["from"], "2027-07-01")


# ═══════════════════════════════════════════════════════════════════════════
# 2. The clamp reaches the engine: runway shrinks.
# ═══════════════════════════════════════════════════════════════════════════

class TestNonCompeteReachesEngine(unittest.TestCase):
    """The clamped schedule flows through to_internal_config into the
    legacy scenarios.income shape the engine reads -- the recovery's `from`
    in the mapped config is the clamped date, proving the clamp is not a
    dead write."""

    def test_clamped_recovery_from_reaches_the_mapped_config(self):
        doc = _runnable_doc()
        # Replace the 'p1_job_loss_ei' scenario with a shock + an
        # inside-the-window recovery, so the non-compete fires.
        sc = next(s for s in doc["decisions"]["income"]
                  if s["id"] == "p1_job_loss_ei")
        sc["overrides"] = [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2027-07-01"},
            {"income_id": "p1_employment", "kind": "employment",
             "amount": 110_000, "from": "2027-07-01", "to": None},
        ]
        legacy = ic.to_internal_config(doc)
        mapped = legacy["scenarios"]["income"]
        job_loss = next(s for s in mapped if s["id"] == "p1_job_loss_ei")
        # The mapped members carry role/kind/amount/from/to; the recovery
        # member (kind=employment, the LATER one) must carry the clamped from.
        recovery_member = max(
            (m for m in job_loss["members"] if m["kind"] == "employment"),
            key=lambda m: m["from"])
        self.assertEqual(
            recovery_member["from"], "2028-01-01",
            "the clamped recovery `from` must reach the mapped legacy config "
            "the engine reads -- otherwise the clamp is a dead write (#665).")


# ═══════════════════════════════════════════════════════════════════════════
# 3. A partial non-compete declaration fails loudly (DP#32).
# ═══════════════════════════════════════════════════════════════════════════

class TestPartialNonCompeteFailsLoudly(unittest.TestCase):
    """A non_compete with a scope but no months is a PARTIAL declaration --
    the schema must reject it, never default the missing piece to 0 (which
    would silently widen what re-employment is barred)."""

    def test_scope_without_months_is_rejected(self):
        doc = _example_doc()
        inc = next(i for i in doc["people"] if i["id"] == "p1")["incomes"][0]
        inc["employment"]["non_compete"] = {
            "scope": "payments technology", "geography": "Canada",
        }  # months intentionally omitted
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_months_without_scope_is_rejected(self):
        doc = _example_doc()
        inc = next(i for i in doc["people"] if i["id"] == "p1")["incomes"][0]
        inc["employment"]["non_compete"] = {
            "months": 12, "geography": "Canada",
        }  # scope intentionally omitted
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)


# ═══════════════════════════════════════════════════════════════════════════
# 4. A contract with no employment block still loads (backward compatible).
# ═══════════════════════════════════════════════════════════════════════════

class TestNoEmploymentBlockIsBackwardCompatible(unittest.TestCase):
    """Absence of an `employment` block is modelled, not guessed: the
    contract still loads and a recovery segment is untouched (no
    non-compete to enforce)."""

    def test_income_without_employment_block_loads_and_is_not_clamped(self):
        doc = _example_doc()
        # Remove the employment block from p1_employment.
        inc = next(i for i in doc["people"] if i["id"] == "p1")["incomes"][0]
        inc["employment"] = None
        ic.validate_contract(doc)  # must not raise
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        overrides = [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2027-07-01"},
            {"income_id": "p1_employment", "kind": "employment",
             "amount": 110_000, "from": "2027-07-01", "to": None},
        ]
        out = _apply_non_compete_to_overrides(overrides, people, pids, "sc1")
        recovery = next(o for o in out if o["kind"] == "employment"
                        and o["from"] != "2015-01-01")
        self.assertEqual(recovery["from"], "2027-07-01",
                         "no employment block -> no non-compete -> no clamp.")


# ═══════════════════════════════════════════════════════════════════════════
# 5. notice_days is modelled as a paid employment-income segment.
# ═══════════════════════════════════════════════════════════════════════════

class TestNoticeDaysExtendsRunway(unittest.TestCase):
    """Contractual pay-in-lieu-of-notice is modelled as a kind=employment
    segment at the base salary for notice_days after termination, and the
    shock's `from` shifts forward so the two do not overlap."""

    def test_notice_segment_inserted_and_shock_shifted(self):
        doc = _example_doc()
        # Give p1_employment a 90-day notice entitlement.
        inc = next(i for i in doc["people"] if i["id"] == "p1")["incomes"][0]
        inc["employment"]["notice_days"] = 90
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        overrides = [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2027-07-01"},
        ]
        out = _apply_notice_segments(overrides, people, pids, "sc1")
        notice = [o for o in out if o["kind"] == "employment"
                  and o["from"] == "2027-01-01"]
        self.assertEqual(len(notice), 1, "a notice segment must be inserted")
        self.assertEqual(notice[0]["to"], "2027-04-01",
                         "90 days from 2027-01-01 is 2027-04-01")
        ei = [o for o in out if o["kind"] == "ei"]
        self.assertEqual(ei[0]["from"], "2027-04-01",
                         "the EI shock must shift to after the notice window")

    def test_zero_notice_days_inserts_nothing(self):
        doc = _example_doc()  # p1_employment has notice_days=0
        people = _people_by_id(doc)
        pids = _person_income_ids(doc)
        overrides = [
            {"income_id": "p1_employment", "kind": "ei",
             "amount": 25_000, "from": "2027-01-01", "to": "2027-07-01"},
        ]
        out = _apply_notice_segments(overrides, people, pids, "sc1")
        self.assertEqual(len(out), 1,
                         "notice_days=0 inserts no segment (explicit, not absent)")
        self.assertEqual(out[0]["from"], "2027-01-01",
                         "the shock is not shifted when there is no notice")


# ═══════════════════════════════════════════════════════════════════════════
# 6. _add_months calendar arithmetic (the non-compete expiry helper).
# ═══════════════════════════════════════════════════════════════════════════

class TestAddMonths(unittest.TestCase):
    def test_year_rollover(self):
        self.assertEqual(_add_months(date(2027, 1, 1), 12), date(2028, 1, 1))
        self.assertEqual(_add_months(date(2027, 11, 1), 3), date(2028, 2, 1))
        self.assertEqual(_add_months(date(2027, 6, 15), 18), date(2028, 12, 15))

    def test_zero_months_is_identity(self):
        self.assertEqual(_add_months(date(2027, 6, 15), 0), date(2027, 6, 15))


if __name__ == "__main__":
    unittest.main()