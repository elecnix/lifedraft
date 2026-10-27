#!/usr/bin/env python3
"""Tests for issue #653: an employment income whose ``from`` date is after the
document's ``as_of`` was silently mapped to ``gross_income = 0.0``.

``_active_employment_income`` sums only the incomes ACTIVE at ``as_of`` and
skips any income that starts later. That is correct for the flat base scalar
-- a not-yet-started job pays nothing on the snapshot date -- but the
projection begins at ``as_of`` and reaches that future start date within its
first year(s). Flattening the schedule to one scalar therefore read the
earner's salary as $0 for the WHOLE horizon: the marginal rate came out
roughly half, RRSP deductions looked worthless, and the ranked strategy
flipped -- all silently.

The fix models a future-dated employment income as a dated ``income_segments``
entry on the member (the same machinery #674 added for job-loss scenarios), so
simulation.py's ``_income_components_for_year`` turns the income ON in the
calendar year it actually begins -- $0 before, the declared amount after --
instead of never.

All data is fabricated: round numbers, role-based ids (DP#4/DP#15).
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation import _income_components_for_year
import contract_people
import contract_schema

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_example():
    with open(contract_schema.EXAMPLE_PATH) as f:
        return json.load(f)


def _single_earner_doc(income_from: str, amount: float = 200_000.0,
                       as_of: str = "2026-01-01"):
    """The shipped example trimmed to the primary couple, with the primary's
    ONE employment income re-dated. decisions.income[] is cleared so the only
    thing under test is how the BASE income schedule maps."""
    from test_input_contract import _two_generation_subset
    doc = _two_generation_subset(_load_example())
    doc["as_of"] = as_of
    doc["decisions"]["income"] = []
    for p in doc["people"]:
        if p["id"] == "p1":
            inc = p["incomes"][0]
            inc["amount"] = amount
            inc["from"] = income_from
            inc["to"] = None
    contract_schema.validate_contract(doc)
    return doc


def _primary_member(doc):
    cfg = ic.to_internal_config(doc)
    return next(m for m in cfg["family"]["members"] if m["role"] == "primary")


class TestFutureDatedIncomeReachesEngine(unittest.TestCase):
    def test_future_income_is_modelled_as_a_dated_segment(self):
        """A ``from`` strictly after ``as_of`` must not vanish: the member
        carries an income_segments entry describing when it starts."""
        member = _primary_member(_single_earner_doc("2029-01-01"))
        segments = member.get("income_segments")
        self.assertTrue(segments, "future-dated income was silently dropped")
        self.assertEqual(segments[0]["from"], "2029-01-01")
        self.assertEqual(segments[0]["amount"], 200_000.0)
        self.assertEqual(segments[0]["kind"], "employment")

    def test_income_is_zero_before_and_full_after_the_start_year(self):
        """The engine's own income function must read $0 for the years before
        the job starts and the full amount for the start year onward -- the
        exact behaviour that was silently zeroed across the whole horizon."""
        member = _primary_member(_single_earner_doc("2029-01-01"))
        base = member["gross_income"]
        segments = member.get("income_segments")

        # start_year 2026 (as_of), 0% salary growth to isolate the schedule.
        for calendar_year, year_index in [(2026, 0), (2027, 1), (2028, 2)]:
            total, earned = _income_components_for_year(
                base, segments, calendar_year, 0.0, year_index)
            self.assertEqual(total, 0.0,
                             f"income should be $0 in {calendar_year} (job not started)")

        total, earned = _income_components_for_year(
            base, segments, 2029, 0.0, 3)
        self.assertAlmostEqual(total, 200_000.0)
        self.assertAlmostEqual(earned, 200_000.0,
                               msg="employment income accrues RRSP room once it starts")

        total, _ = _income_components_for_year(base, segments, 2030, 0.0, 4)
        self.assertAlmostEqual(total, 200_000.0)


class TestFutureSegmentsHelper(unittest.TestCase):
    """Unit coverage of ``_dated_income_segments``' EMPLOYMENT rule in isolation
    (DP#11). The helper carries one rule per KIND since #377 -- a job only needs
    a window when it starts in the future, a self-employment income needs one
    whenever it has not ended -- so these cases pin the #653 half of it and the
    #377 half is pinned in tests/test_issue_377_business_home_cca.py."""

    def test_employment_becomes_a_segment_only_when_it_starts_in_the_future(self):
        person = {"incomes": [
            {"id": "past", "kind": "employment", "amount": 90_000,
             "from": "2015-01-01", "to": None},          # active -> base scalar
            {"id": "future_emp", "kind": "employment", "amount": 200_000,
             "from": "2029-01-01", "to": None},           # future -> segment
        ]}
        segments = contract_people._dated_income_segments(person, "2026-01-01")
        self.assertEqual([s["from"] for s in segments], ["2029-01-01"])
        self.assertEqual(segments[0]["amount"], 200_000)

    def test_a_self_employment_income_becomes_a_segment_whenever_it_is_live(self):
        """The other rule in the same helper (#377): a business carries its
        window whether it started before or after the snapshot, and one that
        already ENDED carries nothing."""
        person = {"incomes": [
            {"id": "closed", "kind": "self_employment", "amount": 10_000,
             "from": "2015-01-01", "to": "2020-01-01"},   # ended -> nothing
            {"id": "running", "kind": "self_employment", "amount": 50_000,
             "from": "2015-01-01", "to": None},           # live -> segment
            {"id": "future_biz", "kind": "self_employment", "amount": 30_000,
             "from": "2030-01-01", "to": None},           # future -> segment
        ]}
        segments = contract_people._dated_income_segments(person, "2026-01-01")
        self.assertEqual([s["from"] for s in segments],
                         ["2015-01-01", "2030-01-01"])

    def test_a_kind_the_scalar_does_not_carry_yields_no_segment_either(self):
        """Pins the fall-through explicitly rather than by omission.

        The helper carries ``employment`` and ``self_employment``. The other
        kinds the schema admits (``ei``, ``rental``, ``investment``, ``other``)
        are carried by NEITHER this helper nor the base scalar, which folds only
        ``employment`` -- so they contribute nothing to the projection. That is
        a real gap, and it is recorded here so it is visible rather than an
        untested branch: fixing it is a change to how those incomes are priced,
        which is not #377's scope (rental income has its own path through
        ``properties[].rental``; an ``incomes[].rental`` line would be a second
        spelling of it, DP#9)."""
        person = {"incomes": [
            {"id": "ei", "kind": "ei", "amount": 32_000,
             "from": "2026-09-01", "to": "2027-06-01"},
            {"id": "inv", "kind": "investment", "amount": 5_000,
             "from": "2026-01-01", "to": None},
        ]}
        self.assertEqual(contract_people._dated_income_segments(person, "2026-01-01"), [])

    def test_no_incomes_yields_no_segments(self):
        self.assertEqual(contract_people._dated_income_segments({"incomes": []}, "2026-01-01"), [])


class TestActiveIncomeIsUnchanged(unittest.TestCase):
    """DP#32 / golden invariant: a person whose income is ACTIVE at as_of is
    mapped exactly as before -- a flat base ``gross_income`` and NO
    income_segments key. The fix is a strict no-op unless a future start date
    is declared."""

    def test_currently_active_income_has_no_segments(self):
        member = _primary_member(_single_earner_doc("2015-01-01"))
        self.assertEqual(member["gross_income"], 200_000.0)
        self.assertNotIn("income_segments", member)


if __name__ == "__main__":
    unittest.main()
