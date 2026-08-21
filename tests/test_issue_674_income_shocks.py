#!/usr/bin/env python3
"""Tests for issue #674: an income scenario can only be PERMANENT, and EI is
wrongly treated as RRSP-earning income.

Follow-up to #665 (decisions.income[] reaches the optimizer for real, but
could only express a flat, whole-horizon amount with no kind). Two gaps:

Gap 1 -- duration: ``income_override`` now carries ``from``/``to`` (``to``
null = permanent). ``simulation.py``'s ``_income_components_for_year``
computes, for any calendar year, the day-weighted blend of whatever
``income_segments`` are declared on a family member and the base (pre-
scenario) salary-grown income for any part of the year no segment covers --
so a shock is present inside its window and absent (reverted to the base
income) outside it (DP#17, both sides).

Gap 2 -- ITA s.146(1) "earned income": ``income_override`` now REQUIRES a
``kind`` (no default -- DP#32). ``EARNED_INCOME_KINDS`` in
countries/canada/earned_income.py (epic #795 bite 3)
(employment, self_employment) is what ``simulation_rules.py``'s
``apply_contribution_room`` reads for RRSP-room accrual; an EI-kind segment
contributes its dollars to taxable income but zero to earned income, so it
adds zero RRSP room for the years it is in effect -- verified against the
Income Tax Act's s.146(1) "earned income" definition (https://laws-lois.
justice.gc.ca/eng/acts/I-3.3/section-146.html): employment income and net
self-employment income count; Employment Insurance benefits explicitly do
not (a Supplementary Unemployment Benefit Plan top-up an EMPLOYER pays on
top of EI is earned income; the EI benefit itself, paid by Service Canada,
is not).

All data is fabricated: round numbers, role-based names (DP#4/DP#15).
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
import scenario_discovery as sd
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation, _income_components_for_year
# Epic #795 bite 3: the ITA s.146(1) earned-income classification moved out of
# the generic fold into the Canada jurisdiction module (DP#10/DP#25).
from countries.canada.earned_income import (
    EARNED_INCOME_KINDS, NON_EARNED_INCOME_KINDS, is_earned_income,
)
from simulation_config import SimulationConfig


# ═══════════════════════════════════════════════════════════════════════════
# 1. _income_components_for_year -- the pure day-weighted blend (DP#11: unit
#    tests verify this module in isolation, with fabricated data).
# ═══════════════════════════════════════════════════════════════════════════

class TestIncomeComponentsForYear(unittest.TestCase):
    def test_no_segments_falls_back_to_base_salary_growth(self):
        """Unchanged pre-#674 behaviour when a member has no income_segments
        at all: base * (1 + salary_growth) ** year_index, fully earned."""
        total, earned = _income_components_for_year(100_000, None, 2026, 0.02, 3)
        expected = 100_000 * (1.02 ** 3)
        self.assertAlmostEqual(total, expected)
        self.assertAlmostEqual(earned, expected, msg="base income is employment -- fully earned")

    def test_segment_covering_the_whole_year_wins_entirely(self):
        segments = [{"kind": "ei", "amount": 30_000, "from": "2026-01-01", "to": "2027-01-01"}]
        total, earned = _income_components_for_year(100_000, segments, 2026, 0.0, 0)
        self.assertAlmostEqual(total, 30_000)
        self.assertEqual(earned, 0.0, "EI is not earned income (ITA s.146(1))")

    def test_dated_window_present_inside_absent_outside(self):
        """DP#17, both sides of the window: a calendar year strictly BEFORE
        `from` and one strictly AFTER `to` both revert fully to the base
        income; only the year inside the window sees the override."""
        segments = [{"kind": "ei", "amount": 20_000, "from": "2027-01-01", "to": "2028-01-01"}]
        before_total, before_earned = _income_components_for_year(90_000, segments, 2026, 0.0, 0)
        inside_total, inside_earned = _income_components_for_year(90_000, segments, 2027, 0.0, 1)
        after_total, after_earned = _income_components_for_year(90_000, segments, 2028, 0.0, 2)
        self.assertEqual(before_total, 90_000)
        self.assertEqual(before_earned, 90_000)
        self.assertEqual(inside_total, 20_000)
        self.assertEqual(inside_earned, 0.0)
        self.assertEqual(after_total, 90_000, "income reverts to the base after `to` -- not held at the override forever")
        self.assertEqual(after_earned, 90_000)

    def test_midyear_start_blends_by_day_count(self):
        """Job loss March 1 (open-ended): the calendar year is NOT rounded
        up to a full year of EI -- Jan 1-Mar 1 stays base income."""
        segments = [{"kind": "ei", "amount": 36_500, "from": "2026-03-01", "to": None}]
        total, earned = _income_components_for_year(73_000, segments, 2026, 0.0, 0)
        # 2026 is not a leap year: Jan 1 -> Mar 1 is 59 days of base income,
        # Mar 1 -> Jan 1 2027 is 306 days of EI.
        expected_base_part = 73_000 * 59 / 365
        expected_ei_part = 36_500 * 306 / 365
        self.assertAlmostEqual(total, expected_base_part + expected_ei_part)
        self.assertAlmostEqual(earned, expected_base_part, msg="only the base-income days are earned")

    def test_midyear_end_blends_by_day_count(self):
        """Re-employment November 1 (segment ends mid-year): the remainder
        of that calendar year is base income, not still EI."""
        segments = [{"kind": "ei", "amount": 36_500, "from": "2025-01-01", "to": "2026-11-01"}]
        total, earned = _income_components_for_year(73_000, segments, 2026, 0.0, 0)
        expected_ei_part = 36_500 * 304 / 365   # Jan 1 -> Nov 1, 2026
        expected_base_part = 73_000 * 61 / 365  # Nov 1 -> Jan 1 2027
        self.assertAlmostEqual(total, expected_base_part + expected_ei_part)
        self.assertAlmostEqual(earned, expected_base_part)

    def test_overlapping_segments_raise_instead_of_picking_one(self):
        """AGENTS.md's 'returning the first match silently drops the rest'
        trap, applied to income segments: two declared states for the same
        income_id in the same window must not silently resolve to whichever
        happens to sort first."""
        segments = [
            {"kind": "employment", "amount": 100_000, "from": "2026-01-01", "to": None},
            {"kind": "ei", "amount": 20_000, "from": "2026-06-01", "to": "2027-01-01"},
        ]
        with self.assertRaises(ValueError):
            _income_components_for_year(90_000, segments, 2026, 0.0, 0)

    def test_adjoining_non_overlapping_segments_are_a_valid_schedule(self):
        """Issue #674's 'small schedule of segments': job loss (EI) then
        re-employment at a DIFFERENT income, as two adjoining segments --
        must NOT raise, and must sum correctly."""
        segments = [
            {"kind": "ei", "amount": 20_000, "from": "2026-01-01", "to": "2026-07-01"},
            {"kind": "employment", "amount": 80_000, "from": "2026-07-01", "to": None},
        ]
        total, earned = _income_components_for_year(999_999, segments, 2026, 0.0, 0)
        ei_part = 20_000 * 181 / 365   # Jan 1 -> Jul 1, 2026 (not a leap year)
        employment_part = 80_000 * 184 / 365
        self.assertAlmostEqual(total, ei_part + employment_part)
        self.assertAlmostEqual(earned, employment_part, msg="only the re-employment slice is earned")

    def test_every_kind_is_classified_earned_or_not(self):
        """DP#17: both sides for every declared kind, not just employment/ei."""
        for kind in ("employment", "self_employment"):
            with self.subTest(kind=kind):
                self.assertIn(kind, EARNED_INCOME_KINDS)
        for kind in ("ei", "rental", "investment", "other"):
            with self.subTest(kind=kind):
                self.assertNotIn(kind, EARNED_INCOME_KINDS)


# ═══════════════════════════════════════════════════════════════════════════
# 2. RRSP room accrual reads the MAPPED, kind-filtered earned income -- not
#    the config, and not the taxable total (issue #674's Gap 2, full engine).
# ═══════════════════════════════════════════════════════════════════════════

def _room_household(kind: str) -> SimulationConfig:
    """A fabricated household whose ENTIRE year-0 income is a declared
    income_segment of the given kind -- isolates the room-accrual effect of
    `kind` from every other engine mechanism (savings_rate=0: nothing is
    contributed, so room accrual is not muddied by room being consumed the
    same year)."""
    return SimulationConfig(
        projection_years=1,
        house_value=0, mortgage_balance=0, margin_available=0,
        start_year=2026,
        savings_rate=0.0,
        family_members=[
            {"role": "primary", "birth_year": 1985, "gross_income": 50_000,
             "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
             "income_segments": [
                 {"kind": kind, "amount": 50_000, "from": "2026-01-01", "to": None},
             ]},
        ],
    )


class TestRRSPRoomExcludesEIEarnedIncome(unittest.TestCase):
    def _run_and_get_room(self, kind: str) -> float:
        cfg = _room_household(kind)
        sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
        sim.run()
        # Issue #674: assert on the MAPPED room in SimState.jurisdiction_state
        # (what apply_contribution_room actually wrote), not on the
        # allocations dict / config that fed it.
        # #700: RRSP room lives in the per-adult store now -- the primary
        # adult's own_room (slot 0).
        from simulation_state import adult_rrsp_slot
        return adult_rrsp_slot(sim._state.jurisdiction_state["canada"], 0)[1]

    def test_ei_income_adds_zero_rrsp_room(self):
        room = self._run_and_get_room("ei")
        self.assertEqual(
            room, 0.0,
            "an EI-kind income must add ZERO RRSP room (ITA s.146(1) "
            "excludes EI benefits from 'earned income')."
        )

    def test_employment_income_of_the_same_amount_adds_normal_room(self):
        room = self._run_and_get_room("employment")
        self.assertAlmostEqual(
            room, 0.18 * 50_000,
            msg="an employment-kind income of the same dollar amount must "
                "add the normal 18% room -- proving the zero above is about "
                "`kind`, not the dollar amount.",
        )

    def test_the_two_kinds_produce_materially_different_room(self):
        ei_room = self._run_and_get_room("ei")
        employment_room = self._run_and_get_room("employment")
        self.assertGreater(
            employment_room, ei_room + 8_000,
            "an EI-shaped override must not overstate RRSP room the way "
            "mapping it onto gross_income used to (#674's Gap 2)."
        )


# ═══════════════════════════════════════════════════════════════════════════
# 3. No declared kind fails loudly -- no silent default to employment
#    (DP#32), at BOTH the schema boundary and the mapping layer.
# ═══════════════════════════════════════════════════════════════════════════

class TestNoKindFailsLoudly(unittest.TestCase):
    def _example_doc(self):
        with open(ic.EXAMPLE_PATH) as f:
            return json.load(f)

    def test_schema_rejects_an_override_with_no_kind(self):
        """schema/example.json's own 'p1_promotion' scenario, with `kind`
        deleted -- the schema must refuse to load it, not silently accept
        an income_override with no declared kind."""
        doc = self._example_doc()
        override = doc["decisions"]["income"][1]["overrides"][0]
        self.assertIn("kind", override, "fixture assumption broken -- update this test")
        del override["kind"]
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_schema_rejects_an_override_with_no_dates(self):
        """Same for `from`/`to` -- issue #674's duration is not optional
        decoration; every income_override declares its window explicitly
        (a `to` of null IS the explicit 'permanent' spelling, not an
        omitted key)."""
        doc = self._example_doc()
        override = doc["decisions"]["income"][1]["overrides"][0]
        del override["from"]
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_mapping_refuses_an_override_for_an_unknown_income_id(self):
        """input_contract.py's decisions.income mapping (issue #674 sweep):
        an override whose income_id belongs to neither the primary nor the
        spouse used to vanish silently (matched neither branch of the old
        if/elif). It now refuses loudly instead of pretending the household
        never declared that scenario."""
        doc = self._example_doc()
        doc["decisions"]["income"][1]["overrides"][0]["income_id"] = "no_such_income"
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)


# ═══════════════════════════════════════════════════════════════════════════
# 3b. ...and at the THIRD boundary: an internal config assembled in code.
#
#     `input_contract.load_and_map` is the one loading boundary for a config
#     read off disk, and the schema makes `kind` required there. But an
#     internal cfg dict can also be built directly (a test fixture, a
#     script), and `scenario_discovery._convert_income_scenarios` is the one
#     place those `scenarios.income[].members[]` are read. It must refuse a
#     member with no declared kind -- BY NAME, naming the scenario, the role
#     and the field -- and must never fall back to 'employment'. A silent
#     fallback there would re-create issue #674's bug exactly: EI money
#     accruing RRSP room ITA s.146(1) does not grant it.
# ═══════════════════════════════════════════════════════════════════════════

class TestInternalConfigRefusesUndeclaredKind(unittest.TestCase):
    def _scenario(self, member: dict) -> list:
        return [{"id": "job_loss", "label": "Job loss",
                 "members": [member]}]

    def _full_member(self) -> dict:
        return {"role": "primary", "gross_income": 20_000,
                "kind": "ei", "from": "2027-01-01", "to": "2027-07-01"}

    def test_a_complete_member_converts(self):
        """Control: the whole-shape member is accepted, and its kind is
        carried through to the segment the engine will read."""
        converted = sd._convert_income_scenarios(
            self._scenario(self._full_member()))
        self.assertEqual(converted[0]["primary_segments"][0]["kind"], "ei")

    def test_a_member_with_no_kind_is_refused(self):
        """The regression this test exists for: NOT defaulted to
        'employment', NOT a bare KeyError -- a named refusal."""
        member = self._full_member()
        del member["kind"]
        with self.assertRaises(sd.IncomeScenarioError) as ctx:
            sd._convert_income_scenarios(self._scenario(member))
        msg = str(ctx.exception)
        self.assertIn("kind", msg)
        self.assertIn("job_loss", msg)   # names the scenario
        self.assertIn("primary", msg)    # names the role

    def test_no_code_path_defaults_an_undeclared_kind_to_employment(self):
        """The negative claim, asserted directly: there is NO input to
        `_convert_income_scenarios` that produces an 'employment' segment
        without 'employment' having been declared. If someone reintroduces
        `member.get('kind', 'employment')`, this fails."""
        member = self._full_member()
        del member["kind"]
        try:
            converted = sd._convert_income_scenarios(self._scenario(member))
        except sd.IncomeScenarioError:
            return  # refused, as it must be
        self.fail(
            "an income override with no declared kind was silently accepted "
            f"as {converted[0]['primary_segments'][0]['kind']!r} -- ITA "
            "s.146(1): EI is not earned income, and a guessed kind decides "
            "whether RRSP room accrues (DP#32)"
        )

    def test_a_member_with_no_window_is_refused(self):
        """`from`/`to` are required for the same reason (issue #674 Gap 1):
        an undated override is a permanent one by accident."""
        for field in ("from", "to"):
            with self.subTest(field=field):
                member = self._full_member()
                del member[field]
                with self.assertRaises(sd.IncomeScenarioError):
                    sd._convert_income_scenarios(self._scenario(member))

    def test_a_null_to_is_the_explicit_permanent_spelling(self):
        """`to: null` is required-but-nullable -- it MUST be accepted, since
        it is how a permanent shock is spelled. Only the missing KEY is an
        error (DP#32: absence != a legitimately-supplied value)."""
        member = self._full_member()
        member["to"] = None
        converted = sd._convert_income_scenarios(self._scenario(member))
        self.assertIsNone(converted[0]["primary_segments"][0]["to"])

    def test_an_unclassified_kind_raises_rather_than_quietly_earning_no_room(self):
        """The mirror image of #674's bug. `if kind in EARNED_INCOME_KINDS`
        alone answers "not earned" for a kind nobody classified -- silently
        UNDERSTATING RRSP room. An unclassified kind must raise."""
        with self.assertRaises(ValueError) as ctx:
            is_earned_income("salary")  # plausible typo for "employment"
        self.assertIn("not classified", str(ctx.exception))

    def test_every_schema_income_kind_is_classified_by_the_engine(self):
        """The drift guard, and the reason the partition is worth having:
        if someone adds a kind to $defs/income_kind's enum without deciding
        its ITA s.146(1) treatment, this fails HERE -- at the point of the
        omission -- instead of the engine silently accruing no room for it."""
        with open(ic.UNIVERSAL_SCHEMA_PATH) as f:
            enum = json.load(f)["$defs"]["income_kind"]["enum"]
        classified = EARNED_INCOME_KINDS | NON_EARNED_INCOME_KINDS
        self.assertEqual(
            set(enum), classified,
            "every $defs/income_kind value must be classified as earned or "
            "not-earned in simulation.py (ITA s.146(1)), and the engine must "
            "not classify kinds the schema cannot express")

    def test_the_auto_discovered_baseline_declares_its_empty_segments(self):
        """The other producer of an income anchor overrides nobody, and says
        so with `[]` -- so every anchor carries both keys and
        `optimize._apply_income_scenario` can read them by subscript rather
        than treating an absent key as 'no override'."""
        anchor = sd._discover_income_scenarios(
            {"family": {"members": [
                {"role": "primary", "gross_income": 150_000}]}})[0]
        self.assertEqual(anchor["primary_segments"], [])
        self.assertEqual(anchor["spouse_segments"], [])


# ═══════════════════════════════════════════════════════════════════════════
# 4. The whole point: a TEMPORARY shock (recovers) must produce a DIFFERENT
#    solvency/ruin outcome (#679) than the SAME shock made PERMANENT. If it
#    doesn't, duration isn't actually being modelled -- it's decoration.
# ═══════════════════════════════════════════════════════════════════════════

def _job_loss_household(ei_to) -> SimulationConfig:
    """A fabricated, leveraged household: comfortable on its base income,
    underwater on EI-level income alone (mortgage + living costs exceed
    take-home EI pay, and there is no cushion -- no reserve, no non-reg/TFSA
    balance, no margin -- to bridge the gap). ``ei_to=None`` is the
    permanent shock (today's pre-#674-fix shape); a real date is the
    temporary shock with recovery."""
    return SimulationConfig(
        projection_years=6,
        house_value=800_000,
        mortgage_balance=300_000,
        mortgage_rate=0.05,
        amortization_years=25,
        margin_available=0,
        savings_rate=0.0,
        living_costs=54_000,
        start_year=2026,
        family_members=[
            {"role": "primary", "birth_year": 1985, "gross_income": 150_000,
             "rrsp_room_accumulated": 0, "tfsa_room_accumulated": 0,
             "income_segments": [
                 {"kind": "ei", "amount": 20_000,
                  "from": "2027-01-01", "to": ei_to},
             ]},
        ],
    )


def _run(cfg: SimulationConfig):
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
    return sim.run()


class TestTemporaryShockRecoversPermanentDoesNot(unittest.TestCase):
    def test_both_shocks_ruin_the_gap_year_identically(self):
        """Same shock, same year -- the two trajectories must NOT differ
        yet (proves the difference asserted below comes from the recovery,
        not from some other divergence between the two configs)."""
        temporary = _run(_job_loss_household(ei_to="2028-01-01"))
        permanent = _run(_job_loss_household(ei_to=None))
        self.assertTrue(temporary[1].ruined, "gap year (2027, index 1) must be ruined")
        self.assertTrue(permanent[1].ruined, "gap year (2027, index 1) must be ruined")

    def test_temporary_shock_recovers_permanent_does_not(self):
        """Issue #674's core enforcement: index 1 = calendar 2027 (the EI
        year) for both. Index 2+ = calendar 2028 onward -- the temporary
        scenario's income has reverted to the base $150k; the permanent
        scenario's has not. Ruin must differ in exactly the years after the
        window closes."""
        temporary = _run(_job_loss_household(ei_to="2028-01-01"))
        permanent = _run(_job_loss_household(ei_to=None))

        self.assertFalse(
            temporary[2].ruined,
            "the temporary scenario has re-employment income from 2028 -- "
            "must NOT still be ruined once the household is back to full "
            "income."
        )
        self.assertTrue(
            permanent[2].ruined,
            "the permanent scenario's income never recovers -- ruin must "
            "persist, or the flat-forever bug is still there."
        )
        self.assertNotEqual(
            temporary[2].ruined, permanent[2].ruined,
            "a duration-bounded shock and the SAME shock made permanent "
            "must produce a DIFFERENT ruin outcome after the window closes "
            "-- if they don't, duration isn't being modelled (issue #674)."
        )

    def test_temporary_shock_income_is_reported_recovered(self):
        """Same fact, at the income level rather than the ruin level: the
        temporary scenario's reported primary_income in 2028 must be back
        near the base $150k (salary-grown), not still at the $20k EI level."""
        temporary = _run(_job_loss_household(ei_to="2028-01-01"))
        self.assertLess(temporary[1].primary_income, 25_000, "gap year is EI-level")
        self.assertGreater(
            temporary[2].primary_income, 100_000,
            "post-recovery year must be back near the base income, not "
            "held at the EI level forever."
        )


if __name__ == "__main__":
    unittest.main()
