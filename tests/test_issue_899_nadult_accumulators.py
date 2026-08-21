#!/usr/bin/env python3
"""Issue #899 (part a): the mechanical N-adult COMPUTE uncap for ACCUMULATING
adults.

The per-adult storage (#700), the per-adult tax loop (#701) and the N-return
estate (#705) already iterate members; the compute that FILLS them was still
two-slot (primary/spouse). Part (a) uncaps it for adults who are pure
ACCUMULATORS across the whole horizon -- an extra adult is admitted to the
compute only when they never decumulate (never retired, never drawing
CPP/OAS/RRIF, never reaching retirement age within the horizon). A retired /
benefit-drawing extra adult still needs the spending-target + mortality model
tracked in #901, so it is LOUDLY REFUSED (message citing #901).

The two-adult primary-couple path stays byte-identical (golden invariant
9709753.139463063, guarded by test_golden_trajectory_581) -- these tests cover
only >2-adult households, which that path never sees.

All test data is synthetic (role labels, round numbers -- DP#15).
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from member_config import adult_members
from simulation_state import step_extra_adult_accounts


def _load_example():
    with open(ic.EXAMPLE_PATH) as f:
        return json.load(f)


def _two_generation_subset(doc):
    """p1/p2 + their two direct children -- the couple-only sub-family (mirrors
    tests/test_input_contract.py)."""
    from test_input_contract import _two_generation_subset as _sub
    return _sub(doc)


def _add_accumulator_adult(doc, *, benefits=None, retirement_ages=(80,)):
    """Add `ac`, an adult child of p1 who WORKS and CONTRIBUTES: employment
    income, an own RRSP + TFSA with opening balances and room, and (by default)
    a retirement age BEYOND the 2075 horizon so they never decumulate -- a pure
    accumulator. Pass `benefits` (cpp/oas) to make them a RETIRED extra adult
    the compute must refuse to #901."""
    doc = copy.deepcopy(doc)
    p1 = next(p for p in doc["people"] if p["id"] == "p1")
    p1["relationships"].append({"type": "parent_of", "person": "ac"})
    person = {
        "id": "ac", "label": "adult_child", "legal_name": None,
        "birth_date": "1996-05-01", "death_date": None,
        "residency": {"province": "quebec", "since": "1996-05-01"},
        "relationships": [], "study_periods": [],
        "incomes": [{"id": "ac_employment", "kind": "employment",
                     "amount": 60000, "from": "2018-01-01", "to": None}],
        "room": {
            "rrsp": {"contribution_room": 20000, "as_of": "2026-01-01"},
            "tfsa": {"contribution_room": 10000, "as_of": "2026-01-01"},
            "fhsa": None, "resp": None,
        },
    }
    if benefits is not None:
        person["benefits"] = benefits
    doc["people"].append(person)
    doc["accounts"].append({
        "id": "ac_rrsp", "owner": "ac", "kind": "rrsp",
        "balance": {"amount": 15000, "as_of": "2026-06-30"},
        "acb": None, "holdings": [], "beneficiary": None, "successor_holder": None,
    })
    doc["accounts"].append({
        "id": "ac_tfsa", "owner": "ac", "kind": "tfsa",
        "balance": {"amount": 8000, "as_of": "2026-06-30"},
        "acb": None, "holdings": [], "beneficiary": None, "successor_holder": None,
    })
    if retirement_ages:
        doc["decisions"]["retirement_age"].append(
            {"person": "ac", "candidate_ages": list(retirement_ages)})
    return doc


class AccumulatorAdultAdmittedTest(unittest.TestCase):
    """A working, contributing 3rd adult who never retires in-horizon is
    admitted to the (now N-adult) compute, taxed individually, and accumulates
    their OWN RRSP/TFSA end-to-end."""

    def setUp(self):
        base = _two_generation_subset(_load_example())
        self.doc = _add_accumulator_adult(base)
        ic.validate_contract(self.doc)

    def test_third_adult_is_mapped_as_an_adult_member(self):
        legacy = ic.to_internal_config(self.doc)
        roles = {m["role"]: m["id"] for m in legacy["family"]["members"]}
        # primary + spouse + the accumulator, keyed by its stable id as its role
        self.assertEqual(roles, {"primary": "p1", "spouse": "p2", "ac": "ac"})
        cfg = SimulationConfig.from_dict(legacy)
        self.assertEqual([m["id"] for m in cfg.adults()], ["p1", "p2", "ac"])

    def test_third_adult_accumulates_after_tax_across_the_horizon(self):
        cfg = SimulationConfig.from_dict(ic.to_internal_config(self.doc))
        results = FamilySimulation(cfg).run()
        self.assertGreater(len(results), 0)
        terminal = results[-1].extra_adult_accounts
        self.assertEqual([e["id"] for e in terminal], ["ac"])
        ac = terminal[0]
        # Accumulated + grown well beyond the $15k RRSP / $8k TFSA openings.
        self.assertGreater(ac["rrsp_balance"], 15000)
        self.assertGreater(ac["tfsa_balance"], 8000)
        # The compute is AFTER-TAX: the total contributed cannot exceed the
        # after-tax savings, which is strictly less than a full-gross save.
        # A hard upper bound proves tax was withheld (not the gross $60k*rate).
        first = results[0].extra_adult_accounts[0]
        gross_savings_yr0 = 60000 * cfg.savings_rate
        contributed_yr0 = (first["rrsp_balance"] + first["tfsa_balance"]
                           - 15000 - 8000)
        # after-tax savings < gross savings (positive federal+QC tax on $60k)
        self.assertLess(contributed_yr0, gross_savings_yr0)
        self.assertGreater(contributed_yr0, 0)

    def test_third_adult_is_taxed_in_their_own_bracket(self):
        from simulation import _income_tax_by_adult
        from tax_data import default_tax_provider
        cfg = SimulationConfig.from_dict(ic.to_internal_config(self.doc))
        brackets = default_tax_provider().get_combined_brackets(
            cfg.start_year, cfg.province)
        tax = _income_tax_by_adult(
            cfg,
            {"primary": 118000, "spouse": 90000, "ac": 60000},
            {"primary": (0.0, 0.0), "spouse": (0.0, 0.0), "ac": (0.0, 0.0)},
            brackets)
        # ac is taxed on their OWN $60k (no joint filing), at a lower marginal
        # rate than the $118k primary -- a real, separate return.
        self.assertGreater(tax["ac"]["tax_before"], 0)
        self.assertLess(tax["ac"]["rate"], tax["primary"]["rate"])

    def test_family_networth_values_the_extra_adult(self):
        from objective import _extra_adults_after_tax_networth
        cfg_dict = ic.to_internal_config(self.doc)
        cfg = SimulationConfig.from_dict(cfg_dict)
        results = FamilySimulation(cfg).run()
        self.assertGreater(_extra_adults_after_tax_networth(results, cfg_dict), 0)


class RetiredExtraAdultRefusedTest(unittest.TestCase):
    """A retired / benefit-drawing extra adult is LOUDLY REFUSED -- the compute
    uncap models accumulators only; their decumulation needs #901."""

    def test_extra_adult_drawing_benefits_is_refused_citing_901(self):
        base = _two_generation_subset(_load_example())
        doc = _add_accumulator_adult(base, benefits={
            "cpp": {"start_date": "2020-02-01", "monthly_amount": 1000,
                    "as_of": "2019-01-01"},
            "oas": {"start_date": "2020-02-01", "defer_months": 0},
        })
        ic.validate_contract(doc)
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("ac", msg)
        self.assertIn("ADULT", msg)
        self.assertIn("#901", msg)

    def test_extra_adult_retiring_within_horizon_is_refused_citing_901(self):
        base = _two_generation_subset(_load_example())
        # Retirement age 60 -> 1996+60 = 2056, well inside the 2075 horizon:
        # they DECUMULATE mid-horizon, which #899-part-a does not model.
        doc = _add_accumulator_adult(base, retirement_ages=(60,))
        ic.validate_contract(doc)
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("#901", str(ctx.exception))


class StepExtraAdultAccountsTest(unittest.TestCase):
    """Unit invariants on the pure accumulation step."""

    def test_savings_fill_rrsp_then_tfsa_and_grow(self):
        rrsp = {"ac": {"own": 1000.0, "own_room": 500.0}}
        tfsa = {"ac": {"balance": 200.0, "room": 400.0}}
        # $700 after-tax savings: $500 fills RRSP room, remaining $200 into TFSA.
        out = step_extra_adult_accounts(
            rrsp, tfsa,
            [{"id": "ac", "earned_income": 60000.0, "savings": 700.0}],
            investment_return=0.10, rrsp_annual_limit=30000.0,
            tfsa_annual_limit=7000.0)
        (e,) = out
        self.assertAlmostEqual(e["rrsp_balance"], (1000.0 + 500.0) * 1.10)
        self.assertAlmostEqual(e["tfsa_balance"], (200.0 + 200.0) * 1.10)
        # RRSP room: consumed 500, re-accrues 18% of earned income (capped).
        self.assertAlmostEqual(e["rrsp_room"], 0.0 + min(0.18 * 60000.0, 30000.0))
        # TFSA room: consumed 200, gains the annual limit.
        self.assertAlmostEqual(e["tfsa_room"], (400.0 - 200.0) + 7000.0)

    def test_no_extra_adults_is_a_no_op(self):
        """TestAbsenceIsNoOp: an empty spec list yields an empty result -- the
        two-adult household writes nothing back (byte-identical)."""
        self.assertEqual(
            step_extra_adult_accounts({}, {}, [], 0.07, 30000.0, 7000.0), [])


class IsPureAccumulatorBranchTest(unittest.TestCase):
    """Direct unit tests on ``_is_pure_accumulator`` -- hand-built minimal dicts
    (not full-contract round-trips) so the null-birth_date / decumulation-account
    branches are reachable without tripping schema validation."""

    def test_owning_a_decumulation_account_is_not_an_accumulator(self):
        # `ac` has no benefits but OWNS a rrif -- the accumulation-only store has
        # no slot for a decumulation account, so it is refused.
        doc = {"accounts": [{"owner": "ac", "kind": "rrif"}],
               "decisions": {"retirement_age": []}}
        person = {"birth_date": "1996-05-01"}
        self.assertFalse(ic._is_pure_accumulator(doc, "ac", person, 2075))

    def test_unknown_birth_year_cannot_be_proven_accumulator(self):
        # No benefits, no decumulation account, but no birth_date -- DP#32:
        # absence fails loudly rather than being coerced.
        doc = {"accounts": [], "decisions": {"retirement_age": []}}
        person = {"birth_date": None}
        self.assertFalse(ic._is_pure_accumulator(doc, "ac", person, 2075))

    def test_clean_accumulator_retiring_beyond_horizon_is_admitted(self):
        # Positive control / boundary: born 1996, default retirement age 65 ->
        # reaches retirement in 2061, strictly after a 2050 horizon end.
        doc = {"accounts": [], "decisions": {"retirement_age": []}}
        person = {"birth_date": "1996-05-01"}
        self.assertTrue(ic._is_pure_accumulator(doc, "ac", person, 2050))


class HorizonEndYearBranchTest(unittest.TestCase):
    """Direct unit tests on ``_horizon_end_year`` -- both None branches plus the
    happy path."""

    def test_horizon_dated_against_a_non_primary_is_undatable(self):
        doc = {"people": [{"id": "p1", "birth_date": "1980-01-01"}],
               "decisions": {"horizon": {"person": "other", "until_age": 95}}}
        self.assertIsNone(ic._horizon_end_year(doc, "p1"))

    def test_primary_without_birth_date_is_undatable(self):
        doc = {"people": [{"id": "p1", "birth_date": None}],
               "decisions": {"horizon": {"person": "p1", "until_age": 95}}}
        self.assertIsNone(ic._horizon_end_year(doc, "p1"))

    def test_horizon_end_year_is_primary_birth_year_plus_until_age(self):
        doc = {"people": [{"id": "p1", "birth_date": "1980-06-15"}],
               "decisions": {"horizon": {"person": "p1", "until_age": 95}}}
        self.assertEqual(ic._horizon_end_year(doc, "p1"), 2075)


class AdultMembersUncapTest(unittest.TestCase):
    """adult_members keeps primary-then-spouse order and appends extras."""

    def test_two_adults_unchanged(self):
        members = [{"role": "spouse", "id": "s"}, {"role": "child", "id": "c"},
                   {"role": "primary", "id": "p"}]
        self.assertEqual([m["id"] for m in adult_members(members)], ["p", "s"])

    def test_extra_adult_follows_the_couple_and_excludes_children(self):
        members = [{"role": "primary", "id": "p"}, {"role": "spouse", "id": "s"},
                   {"role": "child", "id": "c"}, {"role": "ac", "id": "ac"}]
        self.assertEqual([m["id"] for m in adult_members(members)],
                         ["p", "s", "ac"])


if __name__ == "__main__":
    unittest.main()
