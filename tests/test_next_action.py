#!/usr/bin/env python3
"""Tests for next_action.py (issue #662, Track 3 of epic #659).

Covers:
  - each dated obligation fires on both sides of its threshold (DP#17):
    RRIF at 71, CESG at 17 (per child), LIRA at 71, FHSA's 15-year window,
    term life at term_end_date, mortgage at renewal_date, and the horizon
    boundary (inside vs outside the projection window).
  - actions sort by deadline, with an irreversible/no-date action flagged
    and sorted first, and a reversible/no-date action sorted last.
  - an action whose value depends on an unresolved unknown reports
    value=None with a note, never a fabricated number.
  - the winning-decision renderer (source 1) looks up the contract's own
    decisions.* candidate label and supports the VOI-interlock shape.

All test data uses fabricated round numbers and role-based names (DP#15);
none of it is drawn from any real household.
"""

import copy
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import next_action as na


# ── Fixture builders ────────────────────────────────────────────────────

def _person(person_id, label, birth_date, *, province="quebec",
            resp_room=None, death_date=None):
    return {
        "id": person_id,
        "label": label,
        "birth_date": birth_date,
        "death_date": death_date,
        "residency": {"province": province, "since": birth_date},
        "relationships": [],
        "incomes": [],
        "room": {
            "rrsp": None, "tfsa": None, "fhsa": None,
            "resp": ({"contribution_room": 4000, "as_of": "2026-01-01"}
                     if resp_room else None),
        },
    }


def _account(account_id, owner, kind, *, fhsa=None):
    acc = {
        "id": account_id,
        "owner": owner,
        "kind": kind,
        "balance": {"amount": 10000, "as_of": "2026-01-01"},
        "acb": None,
        "holdings": [],
        "beneficiary": None,
        "successor_holder": None,
    }
    if fhsa is not None:
        acc["fhsa"] = fhsa
    return acc


def base_contract(as_of="2026-07-01"):
    """A minimal, empty-of-obligations contract. Tests add exactly the
    piece under test (DP#11: unit tests verify one module/rule path)."""
    return {
        "schema_version": "2026-07",
        "as_of": as_of,
        "currency": "CAD",
        "people": [_person("p1", "primary", "1980-01-01")],
        "accounts": [],
        "liabilities": [],
        "estate": {"default_spousal_rollover": True, "rollover_overrides": [],
                    "life_insurance": []},
        "decisions": {
            "horizon": {"person": "p1", "until_age": 95},
            "contribution_strategy": [
                {"id": "balanced", "label": "Balanced allocation",
                 "allocation": {}, "use_smith": False, "deduct_later": False,
                 "deduct_later_bracket_target": None},
            ],
            "mortgage": {"refinance_options": [], "renewal_options": []},
            "resp_action": [],
            "estate_elections": [
                {"id": "rollover", "label": "Elect spousal rollover",
                 "spousal_rollover": True},
            ],
        },
    }


AS_OF = date(2026, 7, 1)


# ── Action record invariants ────────────────────────────────────────────

class ActionRecordTest(unittest.TestCase):

    def test_value_none_requires_a_note(self):
        with self.assertRaises(ValueError):
            na.Action(what="x", deadline=None, value=None, value_note=None,
                       irreversible=False, why="because")

    def test_value_none_with_note_is_fine(self):
        action = na.Action(what="x", deadline=None, value=None,
                            value_note="depends on an unresolved unknown",
                            irreversible=False, why="because")
        self.assertIsNone(action.value)
        self.assertTrue(action.value_note)

    def test_concrete_value_needs_no_note(self):
        action = na.Action(what="x", deadline=None, value=750.0,
                            value_note=None, irreversible=False, why="because")
        self.assertEqual(action.value, 750.0)


# ── RRIF conversion — both sides of the age-71 threshold ───────────────

class RrifConversionTest(unittest.TestCase):

    def _contract_with_annuitant(self, birth_year):
        contract = base_contract()
        contract["people"] = [_person("p1", "primary", f"{birth_year}-06-15")]
        contract["accounts"] = [_account("p1_rrsp", "p1", "rrsp")]
        return contract

    def test_fires_the_year_annuitant_turns_71(self):
        contract = self._contract_with_annuitant(AS_OF.year - 71)
        actions = na.rrif_conversion_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(AS_OF.year, 12, 31))
        self.assertFalse(actions[0].irreversible)

    def test_fires_for_a_future_71st_year_with_that_years_deadline(self):
        # Turning 71 next year is still a real, dated obligation -- it
        # simply sorts further down the list (DP#28: eligibility is a
        # single point-in-time gate, always computed, never suppressed
        # just because it isn't imminent).
        contract = self._contract_with_annuitant(AS_OF.year - 70)
        actions = na.rrif_conversion_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(AS_OF.year + 1, 12, 31))

    def test_does_not_fire_after_deadline_has_passed(self):
        contract = self._contract_with_annuitant(AS_OF.year - 72)
        actions = na.rrif_conversion_actions(contract, AS_OF)
        self.assertEqual(actions, [])

    def test_exact_day_boundary(self):
        # Deadline is exactly as_of: still open (inclusive).
        contract = self._contract_with_annuitant(2000)
        as_of = date(2071, 12, 31)
        self.assertEqual(len(na.rrif_conversion_actions(contract, as_of)), 1)
        # One day later: closed.
        as_of_plus_1 = date(2072, 1, 1)
        self.assertEqual(na.rrif_conversion_actions(contract, as_of_plus_1), [])

    def test_spousal_rrsp_gates_on_the_annuitant_owner(self):
        contract = self._contract_with_annuitant(AS_OF.year - 71)
        contract["accounts"] = [
            _account("spousal_rrsp_p1", "p1", "spousal_rrsp"),
        ]
        actions = na.rrif_conversion_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)

    def test_one_action_per_person_not_per_account(self):
        contract = self._contract_with_annuitant(AS_OF.year - 71)
        contract["accounts"] = [
            _account("p1_rrsp", "p1", "rrsp"),
            _account("spousal_rrsp_p1", "p1", "spousal_rrsp"),
        ]
        actions = na.rrif_conversion_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)

    def test_rrif_kind_account_does_not_refire(self):
        # Already converted -- kind is 'rrif', not 'rrsp'/'spousal_rrsp'.
        contract = self._contract_with_annuitant(AS_OF.year - 71)
        contract["accounts"] = [_account("p1_rrif", "p1", "rrif")]
        self.assertEqual(na.rrif_conversion_actions(contract, AS_OF), [])


# ── CESG eligibility — both sides of the age-17 threshold, per child ───

class CesgEligibilityTest(unittest.TestCase):

    def _contract_with_child(self, birth_year, province="quebec"):
        contract = base_contract()
        contract["people"] = [
            _person("p1", "primary", "1980-01-01"),
            _person("ch", "child_a", f"{birth_year}-06-15",
                    province=province, resp_room=True),
        ]
        return contract

    def test_fires_the_final_eligible_year(self):
        contract = self._contract_with_child(AS_OF.year - 17)
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(AS_OF.year, 12, 31))
        self.assertTrue(actions[0].irreversible)
        self.assertIsNotNone(actions[0].value)

    def test_fires_for_a_child_not_yet_17_with_that_years_deadline(self):
        # A child who turns 17 next year still has a real, dated final-
        # year deadline -- it fires now, dated for next year, so it can be
        # sorted correctly rather than appearing from nowhere.
        contract = self._contract_with_child(AS_OF.year - 16)
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(AS_OF.year + 1, 12, 31))

    def test_does_not_fire_the_year_after_turning_17(self):
        contract = self._contract_with_child(AS_OF.year - 18)
        self.assertEqual(na.cesg_eligibility_actions(contract, AS_OF), [])

    def test_fires_for_a_child_turning_17_next_year_only_in_that_year(self):
        contract = self._contract_with_child(AS_OF.year - 16)
        next_year_as_of = date(AS_OF.year + 1, 3, 1)
        actions = na.cesg_eligibility_actions(contract, next_year_as_of)
        self.assertEqual(len(actions), 1)

    def test_unknown_resp_room_still_emits_the_action(self):
        """#670: `room.resp: null` means "we do not know this child's
        remaining grant room" -- the COMMON case, since the per-beneficiary
        CESG split is on no account statement. It must NOT delete the
        action: the deadline is no less real because the amount is
        uncertain, and the report is the only place the user learns the
        record is worth requesting."""
        contract = self._contract_with_child(AS_OF.year - 17)
        contract["people"][1]["room"]["resp"] = None
        # The child is still reachable as a child of the household.
        contract["people"][0]["relationships"] = [
            {"type": "parent_of", "person": "ch", "from": None, "to": None},
        ]
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        action = actions[0]
        # The deadline survives intact...
        self.assertEqual(action.deadline, date(AS_OF.year, 12, 31))
        self.assertTrue(action.irreversible)
        # ...and the unknown changes the VALUE, not the action's existence.
        self.assertIsNone(action.value)
        self.assertIn("UNKNOWN", action.value_note)
        self.assertIn("CRA/ESDC", action.value_note)   # where to go get it

    def test_unknown_resp_room_still_emits_for_a_named_resp_beneficiary(self):
        """The other trigger path: a child named on an RESP account, whose
        room is null."""
        contract = self._contract_with_child(AS_OF.year - 17)
        contract["people"][1]["room"]["resp"] = None
        contract["accounts"] = [{
            "id": "family_resp", "owner": "p1", "kind": "resp",
            "balance": {"amount": 20000, "as_of": "2026-01-01"},
            "acb": None, "holdings": [], "beneficiary": None,
            "successor_holder": None,
            "resp": {"subscribers": ["p1"], "beneficiaries": ["ch"],
                      "contributions_total": 10000, "cesg_received": 2000,
                      "qesi_received": 1000, "clb_received": 0},
        }]
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertIsNone(actions[0].value)
        self.assertTrue(actions[0].value_note)

    def test_a_known_room_still_reports_a_concrete_value(self):
        """The fix must not over-correct: when the room IS stated, the
        grant is still costed, not hedged into None."""
        contract = self._contract_with_child(AS_OF.year - 17)
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        self.assertAlmostEqual(actions[0].value, 750.0)
        self.assertIsNone(actions[0].value_note)

    def test_value_matches_basic_cesg_plus_qesi_in_quebec(self):
        contract = self._contract_with_child(AS_OF.year - 17, province="quebec")
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        # $2,500 basic contribution x (20% CESG + 10% QESI) = $750.
        self.assertAlmostEqual(actions[0].value, 750.0)

    def test_qesi_not_added_outside_quebec(self):
        contract = self._contract_with_child(AS_OF.year - 17, province="ontario")
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        # Basic CESG only: $2,500 x 20% = $500.
        self.assertAlmostEqual(actions[0].value, 500.0)

    def test_per_child_not_per_family(self):
        # #659's own headline example: eligibility is per child, with each
        # child's OWN final year -- not a single family-level total/date.
        contract = self._contract_with_child(AS_OF.year - 17)
        contract["people"].append(
            _person("ch2", "child_b", f"{AS_OF.year - 10}-06-15", resp_room=True)
        )
        actions = na.cesg_eligibility_actions(contract, AS_OF)
        self.assertEqual(len(actions), 2)
        deadlines = {a.deadline for a in actions}
        self.assertEqual(len(deadlines), 2)  # two distinct, per-child deadlines
        self.assertIn(date(AS_OF.year, 12, 31), deadlines)       # child_a
        self.assertIn(date(AS_OF.year + 7, 12, 31), deadlines)   # child_b


# ── LIRA conversion — both sides of the age-71 threshold ───────────────

class LiraConversionTest(unittest.TestCase):

    def _contract_with_lira_owner(self, birth_year):
        contract = base_contract()
        contract["people"] = [_person("p1", "primary", f"{birth_year}-06-15")]
        contract["accounts"] = [_account("p1_lira", "p1", "lira")]
        return contract

    def test_fires_the_year_owner_turns_71(self):
        contract = self._contract_with_lira_owner(AS_OF.year - 71)
        actions = na.lira_conversion_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(AS_OF.year, 12, 31))

    def test_fires_for_a_future_71st_year_with_that_years_deadline(self):
        contract = self._contract_with_lira_owner(AS_OF.year - 70)
        actions = na.lira_conversion_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(AS_OF.year + 1, 12, 31))

    def test_lif_kind_does_not_refire(self):
        contract = self._contract_with_lira_owner(AS_OF.year - 71)
        contract["accounts"] = [_account("p1_lif", "p1", "lif")]
        self.assertEqual(na.lira_conversion_actions(contract, AS_OF), [])


# ── Term life renewal — inside vs outside the horizon ───────────────────

class TermLifeRenewalTest(unittest.TestCase):

    def _contract_with_policy(self, term_end_date, until_age=95):
        contract = base_contract()
        contract["decisions"]["horizon"]["until_age"] = until_age
        contract["estate"]["life_insurance"] = [{
            "id": "term_p1", "owner": "p1", "insured": "p1",
            "beneficiary": "p1", "kind": "term", "face_amount": 500000,
            "premium_annual": 600, "as_of": "2026-01-01",
            "term_end_date": term_end_date,
        }]
        return contract

    def test_fires_when_inside_the_horizon(self):
        contract = self._contract_with_policy("2030-05-01", until_age=95)
        actions = na.term_life_renewal_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(2030, 5, 1))
        self.assertFalse(actions[0].irreversible)
        self.assertIsNone(actions[0].value)
        self.assertTrue(actions[0].value_note)

    def test_does_not_fire_when_outside_the_horizon(self):
        # p1 born 1980-01-01; until_age=1 -> horizon ends 1981-12-31,
        # long before the policy's term_end_date or as_of. Horizon
        # filtering is threaded through derive_dated_obligations (the
        # individual *_actions functions are horizon-agnostic unless
        # given one explicitly, so they stay independently testable).
        contract = self._contract_with_policy("2030-05-01", until_age=1)
        self.assertEqual(na.derive_dated_obligations(contract, AS_OF,
                                                        na._horizon_end_date(contract)),
                          [])

    def test_horizon_end_date_excludes_the_function_directly_when_passed(self):
        contract = self._contract_with_policy("2030-05-01", until_age=1)
        horizon_end = na._horizon_end_date(contract)
        self.assertEqual(horizon_end, date(1981, 12, 31))
        self.assertEqual(
            na.term_life_renewal_actions(contract, AS_OF, horizon_end), [])

    def test_does_not_fire_for_a_permanent_policy(self):
        contract = self._contract_with_policy("2030-05-01")
        contract["estate"]["life_insurance"][0]["kind"] = "permanent"
        self.assertEqual(na.term_life_renewal_actions(contract, AS_OF), [])

    def test_does_not_fire_once_term_has_already_ended(self):
        contract = self._contract_with_policy("2020-05-01")
        self.assertEqual(na.term_life_renewal_actions(contract, AS_OF), [])


# ── Mortgage renewal ────────────────────────────────────────────────────

class MortgageRenewalTest(unittest.TestCase):

    def _contract_with_mortgage(self, renewal_date):
        contract = base_contract()
        contract["liabilities"] = [{
            "id": "mortgage_main", "owner": "p1", "kind": "mortgage",
            "balance": {"amount": 300000, "as_of": "2026-01-01"},
            "rate": 0.045, "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 2000},
            "renewal_date": renewal_date, "term_start_date": "2023-01-01",
            "collateral": None,
        }]
        return contract

    def test_fires_when_renewal_is_in_the_future(self):
        contract = self._contract_with_mortgage("2028-01-01")
        actions = na.mortgage_renewal_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(2028, 1, 1))

    def test_does_not_fire_when_renewal_already_passed(self):
        contract = self._contract_with_mortgage("2020-01-01")
        self.assertEqual(na.mortgage_renewal_actions(contract, AS_OF), [])


# ── FHSA's 15-year participation window ─────────────────────────────────

class FhsaWindowTest(unittest.TestCase):

    def _contract_with_fhsa(self, opened_date, birth_date="1990-01-01"):
        contract = base_contract()
        contract["people"] = [_person("p1", "primary", birth_date)]
        contract["accounts"] = [_account(
            "p1_fhsa", "p1", "fhsa",
            fhsa={"opened_date": opened_date, "first_time_buyer_since": opened_date},
        )]
        return contract

    def test_fires_within_the_15_year_window(self):
        contract = self._contract_with_fhsa("2023-05-01")
        actions = na.fhsa_window_actions(contract, AS_OF)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(2038, 12, 31))
        self.assertTrue(actions[0].irreversible)

    def test_does_not_fire_after_the_window_has_closed(self):
        contract = self._contract_with_fhsa("2000-01-01")
        self.assertEqual(na.fhsa_window_actions(contract, AS_OF), [])

    def test_age_71_can_close_the_window_before_15_years_elapse(self):
        # Opened at age 60 -> the 15th anniversary (age 75) is later than
        # age 71, so age 71 is the binding constraint.
        contract = self._contract_with_fhsa("2023-05-01", birth_date="1965-01-01")
        actions = na.fhsa_window_actions(contract, AS_OF)
        self.assertEqual(actions[0].deadline, date(2036, 12, 31))


# ── Decision windows: disbursement segregation ──────────────────────────

class DisbursementSegregationTest(unittest.TestCase):

    def test_fires_when_a_cash_out_refinance_option_exists(self):
        contract = base_contract()
        contract["decisions"]["mortgage"]["refinance_options"] = [
            {"id": "no_refi", "label": "No refinance", "cash_out": 0, "ltv": 0.0},
            {"id": "refi_50k", "label": "Refinance, cash out $50k",
             "cash_out": 50000, "ltv": 0.55},
        ]
        actions = na.disbursement_segregation_actions(contract)
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0].irreversible)
        self.assertIsNone(actions[0].deadline)
        self.assertIsNone(actions[0].value)
        self.assertTrue(actions[0].value_note)

    def test_does_not_fire_when_no_cash_out_option_exists(self):
        contract = base_contract()
        contract["decisions"]["mortgage"]["refinance_options"] = [
            {"id": "no_refi", "label": "No refinance", "cash_out": 0, "ltv": 0.0},
        ]
        self.assertEqual(na.disbursement_segregation_actions(contract), [])

    def test_cites_the_rules_that_create_the_obligation(self):
        # The archetype action of #662: no calendar slack, irreversible,
        # and it must name the rule -- s.18(11) (registered portion is
        # never deductible), s.20(1)(c) (only an income-producing
        # non-registered use qualifies), and traceability.
        contract = base_contract()
        contract["decisions"]["mortgage"]["refinance_options"] = [
            {"id": "refi", "label": "Refinance", "cash_out": 50000, "ltv": 0.5},
        ]
        why = na.disbursement_segregation_actions(contract)[0].why
        self.assertIn("18(11)", why)
        self.assertIn("20(1)(c)", why)
        self.assertIn("TRACEABLE", why)

    def test_is_triggered_by_a_pending_disbursement_not_a_calendar_date(self):
        # The Action model must be able to say "irreversible, and the
        # window closes at an event, not on a date" -- deadline=None, yet
        # it still sorts ahead of every dated action.
        contract = base_contract()
        contract["decisions"]["mortgage"]["refinance_options"] = [
            {"id": "refi", "label": "Refinance", "cash_out": 50000, "ltv": 0.5},
        ]
        dated = na.Action(what="something dated", deadline=date(2026, 8, 1),
                           value=1.0, value_note=None, irreversible=False,
                           why="x")
        actions = na.derive_actions(contract, winning_decisions=[dated])
        self.assertIsNone(actions[0].deadline)
        self.assertTrue(actions[0].irreversible)
        self.assertIn("Segregate", actions[0].what)


# ── Sorting: deadline order + irreversibility flag ──────────────────────

class EmergencyReserveShortfallTest(unittest.TestCase):
    """Issue #688/#679: "you are N months short of your declared reserve" --
    a dated, costed gap the engine deliberately does NOT raise on
    (SimState.from_config clamps to the host account and calls the shortfall
    "a real, reportable fact, not an error"). This is where it gets reported.

    Fabricated round numbers only (DP#4/DP#15).
    """

    def _contract(self, *, target_months=12, balance=20_000,
                  living_costs=60_000, payment_monthly=3_000, held_in="p1_tfsa"):
        contract = base_contract()
        contract["accounts"] = [_account("p1_tfsa", "p1", "tfsa")]
        contract["accounts"][0]["balance"]["amount"] = balance
        contract["liabilities"] = [{
            "id": "m1", "owner": "p1", "kind": "mortgage",
            "balance": {"amount": 400_000, "as_of": "2026-01-01"},
            "rate": 0.05, "rate_type": "fixed", "collateral": None,
            "amortization": {"years": 25, "payment_monthly": payment_monthly},
        }]
        contract["household_budget"] = {"annual_living_costs": living_costs}
        contract["assumptions"] = {"emergency_reserve": {
            "target_months": target_months, "held_in": held_in,
            "instrument": "cash", "rate": 0.03, "replenish_priority": 1,
        }}
        return contract

    def test_reports_the_months_the_household_is_short(self):
        # Essential outflows = 60,000 living + 36,000 debt service = 96,000/yr
        # = 8,000/mo. A 12-month target is 96,000; the account holds 20,000,
        # which covers 2.5 months -> 9.5 months short, a 76,000 gap.
        actions = na.emergency_reserve_shortfall_actions(self._contract())
        self.assertEqual(len(actions), 1)
        self.assertIn("9.5 months short", actions[0].what)
        self.assertIn("$76,000", actions[0].what)

    def test_a_fully_funded_reserve_produces_no_action(self):
        """DP#17's other side -- it must not nag a household that did the
        thing it was told to do."""
        self.assertEqual(
            na.emergency_reserve_shortfall_actions(self._contract(balance=500_000)),
            [])

    def test_no_declared_reserve_is_not_a_shortfall(self):
        """A household that never asked for a reserve is not 'short' of one.
        Inventing a target here so we can declare it short would be the engine
        holding an opinion (DP#2/DP#13); its $0 reserve is reported by the
        #679 solvency output instead."""
        contract = self._contract()
        contract["assumptions"] = {}
        self.assertEqual(na.emergency_reserve_shortfall_actions(contract), [])

    def test_a_target_with_no_budget_asks_for_the_budget_rather_than_guessing(self):
        """DP#32: a reserve is denominated in MONTHS of essential outflows.
        With no living-cost budget a month cannot be priced -- so name the
        document that settles it, never invent a figure."""
        contract = self._contract()
        del contract["household_budget"]
        actions = na.emergency_reserve_shortfall_actions(contract)
        self.assertEqual(len(actions), 1)
        self.assertIn("Measure your annual living costs", actions[0].what)
        self.assertIsNone(actions[0].value)
        self.assertIn("annual_living_costs", actions[0].value_note)

    def test_a_reserve_held_outside_every_declared_account_is_not_judged(self):
        """held_in=null means the cash sits in a chequing account the contract
        does not model. We cannot see its balance -- so we must not imply it is
        short (that would be inventing a fact, the inverse of DP#32's sin)."""
        contract = self._contract(held_in=None)
        self.assertEqual(na.emergency_reserve_shortfall_actions(contract), [])

    def test_the_value_is_not_faked_as_a_single_dollar_figure(self):
        """The worth of a reserve is the forced-sale cost it avoids, which
        depends on the shock -- it is what the emergency_reserve_months sweep
        prices. Action.value=None + a value_note is the honest encoding."""
        action = na.emergency_reserve_shortfall_actions(self._contract())[0]
        self.assertIsNone(action.value)
        self.assertIn("sweep", action.value_note)


class SortingTest(unittest.TestCase):

    def test_sorts_by_deadline_ascending(self):
        near = na.Action(what="near", deadline=date(2027, 1, 1), value=1.0,
                          value_note=None, irreversible=False, why="x")
        far = na.Action(what="far", deadline=date(2040, 1, 1), value=1.0,
                         value_note=None, irreversible=False, why="x")
        ordered = na.sort_actions([far, near])
        self.assertEqual([a.what for a in ordered], ["near", "far"])

    def test_irreversible_no_deadline_sorts_before_every_dated_action(self):
        urgent = na.Action(what="segregate", deadline=None, value=None,
                            value_note="depends", irreversible=True, why="x")
        near = na.Action(what="near", deadline=date(2027, 1, 1), value=1.0,
                          value_note=None, irreversible=False, why="x")
        ordered = na.sort_actions([near, urgent])
        self.assertEqual([a.what for a in ordered], ["segregate", "near"])

    def test_reversible_no_deadline_sorts_after_every_dated_action(self):
        someday = na.Action(what="someday", deadline=None, value=None,
                             value_note="not urgent", irreversible=False, why="x")
        far = na.Action(what="far", deadline=date(2040, 1, 1), value=1.0,
                         value_note=None, irreversible=False, why="x")
        ordered = na.sort_actions([someday, far])
        self.assertEqual([a.what for a in ordered], ["far", "someday"])

    def test_a_larger_value_reversible_action_is_still_outranked(self):
        # DP: irreversibility outranks a larger but reversible value.
        big_reversible = na.Action(what="big", deadline=date(2030, 1, 1),
                                    value=1_000_000.0, value_note=None,
                                    irreversible=False, why="x")
        small_irreversible = na.Action(what="small", deadline=None, value=None,
                                        value_note="depends", irreversible=True,
                                        why="x")
        ordered = na.sort_actions([big_reversible, small_irreversible])
        self.assertEqual(ordered[0].what, "small")


# ── Source 1: rendering the optimizer's winning decisions.* option ─────

class WinningDecisionRenderingTest(unittest.TestCase):

    def test_renders_the_contract_label_verbatim(self):
        contract = base_contract()
        action = na.action_from_winning_decision(
            contract, "contribution_strategy", "balanced", value=42.0,
        )
        self.assertEqual(action.what, "Balanced allocation")
        self.assertEqual(action.value, 42.0)

    def test_unknown_candidate_id_raises(self):
        contract = base_contract()
        with self.assertRaises(KeyError):
            na.action_from_winning_decision(
                contract, "contribution_strategy", "does_not_exist",
            )

    def test_mortgage_category_defaults_deadline_to_renewal_date(self):
        contract = base_contract()
        contract["liabilities"] = [{
            "id": "mortgage_main", "owner": "p1", "kind": "mortgage",
            "balance": {"amount": 300000, "as_of": "2026-01-01"},
            "rate": 0.045, "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 2000},
            "renewal_date": "2029-03-01", "term_start_date": "2023-01-01",
            "collateral": None,
        }]
        contract["decisions"]["mortgage"]["renewal_options"] = [
            {"id": "5yr_fixed", "label": "5-year fixed", "rate": 0.045,
             "type": "fixed", "term_years": 5},
        ]
        action = na.action_from_winning_decision(
            contract, "mortgage_renewal", "5yr_fixed", value=None,
            value_note="depends on the rollover election",
        )
        self.assertEqual(action.deadline, date(2029, 3, 1))

    def test_supports_the_voi_interlock_shape(self):
        # An action whose value depends on a high-VOI unknown must say so
        # rather than quoting a confident number derived from a guess.
        contract = base_contract()
        action = na.action_from_winning_decision(
            contract, "estate_elections", "rollover",
            value=None,
            value_note="Worth $180,000 if the rollover is elected, "
                        "$40,000 if not -- resolve that first.",
        )
        self.assertIsNone(action.value)
        self.assertIn("if the rollover is elected", action.value_note)


# ── End-to-end assembly ─────────────────────────────────────────────────

class DeriveActionsTest(unittest.TestCase):

    def test_defaults_as_of_to_the_document(self):
        contract = base_contract(as_of="2026-07-01")
        contract["people"] = [_person("p1", "primary",
                                       f"{2026 - 71}-06-15")]
        contract["accounts"] = [_account("p1_rrsp", "p1", "rrsp")]
        actions = na.derive_actions(contract)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].deadline, date(2026, 12, 31))

    def test_combines_all_sources_and_sorts(self):
        contract = base_contract(as_of="2026-07-01")
        contract["people"] = [
            _person("p1", "primary", f"{2026 - 71}-06-15"),
        ]
        contract["accounts"] = [_account("p1_rrsp", "p1", "rrsp")]
        contract["decisions"]["mortgage"]["refinance_options"] = [
            {"id": "refi", "label": "Refinance", "cash_out": 50000, "ltv": 0.5},
        ]
        winner = [na.action_from_winning_decision(
            contract, "contribution_strategy", "balanced", value=10_000.0,
        )]
        actions = na.derive_actions(contract, winning_decisions=winner)
        whats = [a.what for a in actions]
        # Irreversible/no-deadline segregation action first...
        self.assertIn("Segregate", actions[0].what)
        # ...and the whole list is present.
        self.assertEqual(len(actions), 3)
        self.assertIn("Balanced allocation", whats)


if __name__ == "__main__":
    unittest.main()
