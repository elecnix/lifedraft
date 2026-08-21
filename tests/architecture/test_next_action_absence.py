#!/usr/bin/env python3
"""Architecture test for issue #670: an unknown input may change an
action's VALUE — it must NEVER DELETE the action.

#670 was found by running ``next_action.py`` against a real household:

    if room.get("resp") is None:
        continue                  # <-- the CESG action is SILENTLY DELETED

``room.resp: null`` does not mean "no room". It means "we do not know the
room" — the COMMON case, because the per-beneficiary CESG split is on no
account statement; it lives only in the CRA/ESDC per-beneficiary record
the household has to go and request. So the tool's behaviour was: the user
does not know their grant room → the tool says nothing → the user never
learns that a dated, expiring, ~$750/yr grant exists, and never learns to
go and look up the room. **A missing input silently suppressed the very
action that would have told them to go find that input** — DP#32 violated
inside the module built to surface absences.

Fixing that one ``continue`` is not enough; the bug class has to be made
unrepresentable. This test iterates ``next_action.OBLIGATION_GENERATORS``
and, for each, feeds it a contract whose value- and date-determining
inputs are ALL nulled, then asserts the generator STILL emits its action —
with ``value=None`` and a note that names the fact to go and get.

The coverage assertion is the load-bearing half: a generator added to
``OBLIGATION_GENERATORS`` without an absence fixture here FAILS THE BUILD.
That is what turns "we fixed one ``continue``" into "this cannot come
back" (DESIGN_PRINCIPLES.md's own enforcement-status framing: a principle
is binding to the degree a machine checks it).

DP#15: fabricated round numbers and role-based names only.
"""

import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import next_action as na


AS_OF = date(2026, 7, 1)

# A person whose every value-determining and date-determining field is
# either null or absent -- the state a brand-new user's document is in
# before they have gone and looked anything up (#659's "new-user case is
# the real design target").
_UNKNOWN_PERSON = {
    "id": "unknown_person",
    "label": "household_member",
    "birth_date": None,          # nulled: the date every age gate needs
    "death_date": None,
    "residency": {"province": "quebec", "since": "2000-01-01"},
    "relationships": [],
    "incomes": [],
    "room": {"rrsp": None, "tfsa": None, "fhsa": None, "resp": None},
}


def _contract(accounts=None, liabilities=None, life_insurance=None,
               people=None):
    return {
        "schema_version": "2026-07",
        "as_of": AS_OF.isoformat(),
        "currency": "CAD",
        "people": people if people is not None else [dict(_UNKNOWN_PERSON)],
        "accounts": accounts or [],
        "liabilities": liabilities or [],
        "estate": {
            "default_spousal_rollover": True,
            "rollover_overrides": [],
            "life_insurance": life_insurance or [],
        },
        # No horizon: `decisions.horizon.person` would itself need a
        # birth_date, and an unknown-everything document has none. The
        # obligations must still surface without one.
        "decisions": {"mortgage": {"refinance_options": [],
                                    "renewal_options": []}},
    }


def _account(account_id, kind, **extra):
    acc = {
        "id": account_id,
        "owner": "unknown_person",
        "kind": kind,
        "balance": {"amount": 10000, "as_of": "2026-01-01"},
        "acb": None,
        "holdings": [],
        "beneficiary": None,
        "successor_holder": None,
    }
    acc.update(extra)
    return acc


# For every generator: a contract that TRIGGERS it, but in which every fact
# that would let us value or date the obligation is null/missing.
#
# Keyed by generator __name__ -- the keys are checked against
# OBLIGATION_GENERATORS below, so a new generator cannot slip in unnoticed.
ABSENCE_FIXTURES = {
    # An RRSP whose annuitant has no birth_date: the age-71 RRIF deadline
    # exists, but cannot be dated.
    "rrif_conversion_actions": _contract(
        accounts=[_account("acc_rrsp", "rrsp")],
    ),
    # A LIRA whose owner has no birth_date.
    "lira_conversion_actions": _contract(
        accounts=[_account("acc_lira", "lira")],
    ),
    # #670's headline: an RESP whose named beneficiary has NO stated
    # room.resp (unknown, not zero) and no birth_date.
    "cesg_eligibility_actions": _contract(
        accounts=[_account(
            "acc_resp", "resp",
            resp={"subscribers": [], "beneficiaries": ["unknown_person"],
                  "contributions_total": 0, "cesg_received": 0,
                  "qesi_received": 0, "clb_received": 0},
        )],
    ),
    # A TERM policy with no term_end_date: the renewal exists, undatable.
    "term_life_renewal_actions": _contract(
        life_insurance=[{
            "id": "policy_term", "owner": "unknown_person",
            "insured": "unknown_person", "beneficiary": "unknown_person",
            "kind": "term", "face_amount": 100000, "premium_annual": 500,
            "as_of": "2026-01-01", "term_end_date": None,
        }],
    ),
    # A mortgage with no renewal_date.
    "mortgage_renewal_actions": _contract(
        liabilities=[{
            "id": "loan_mortgage", "owner": "unknown_person",
            "kind": "mortgage",
            "balance": {"amount": 200000, "as_of": "2026-01-01"},
            "rate": 0.05, "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 1500},
            "collateral": None,
        }],
    ),
    # An FHSA with no opened_date and an owner with no birth_date: the
    # 15-year participation window exists, but cannot be dated.
    "fhsa_window_actions": _contract(
        accounts=[_account("acc_fhsa", "fhsa", fhsa={"opened_date": None})],
    ),
}


class UnknownInputNeverDeletesAnActionTest(unittest.TestCase):
    """#670's rule, enforced for EVERY generator, not just the one that
    was reported."""

    def test_every_generator_has_an_absence_fixture(self):
        """The load-bearing half. A new obligation generator added to
        OBLIGATION_GENERATORS without an absence fixture fails the build --
        it cannot silently reintroduce the "missing input deletes the
        action" bug class."""
        registered = {g.__name__ for g in na.OBLIGATION_GENERATORS}
        covered = set(ABSENCE_FIXTURES)
        missing = registered - covered
        self.assertEqual(
            missing, set(),
            "next_action.OBLIGATION_GENERATORS contains generator(s) with no "
            "absence fixture in ABSENCE_FIXTURES: "
            f"{sorted(missing)}. Every generator must be proven (#670) to "
            "EMIT its action when its value/date-determining inputs are "
            "null -- an unknown input may change an action's VALUE, it must "
            "never DELETE the action. Add a fixture that triggers the "
            "generator with those inputs nulled."
        )
        stale = covered - registered
        self.assertEqual(stale, set(),
                          f"ABSENCE_FIXTURES names generators that no longer "
                          f"exist: {sorted(stale)}")

    def test_generator_still_emits_its_action_when_everything_is_unknown(self):
        for generator in na.OBLIGATION_GENERATORS:
            with self.subTest(generator=generator.__name__):
                contract = ABSENCE_FIXTURES[generator.__name__]
                actions = generator(contract, AS_OF, None)
                self.assertTrue(
                    actions,
                    f"{generator.__name__} DELETED its action when its "
                    "value/date-determining inputs were unknown (#670). A "
                    "missing fact is the strongest reason to TELL the user "
                    "the obligation exists -- it is the only way they learn "
                    "the fact is worth going to get."
                )

    def test_the_unknown_value_is_none_and_carries_a_note(self):
        """Never a fabricated number: value=None, plus a note naming what
        to go and get (Action.__post_init__ already refuses the alternative)."""
        for generator in na.OBLIGATION_GENERATORS:
            with self.subTest(generator=generator.__name__):
                contract = ABSENCE_FIXTURES[generator.__name__]
                for action in generator(contract, AS_OF, None):
                    self.assertIsNone(
                        action.value,
                        f"{generator.__name__} invented a dollar value from "
                        "inputs that are unknown."
                    )
                    self.assertTrue(action.value_note)

    def test_an_undatable_deadline_is_flagged_not_treated_as_absent(self):
        """A deadline nobody can compute is NOT 'no deadline' -- it must be
        flagged, so it can neither be silently dropped nor silently sorted
        to the bottom as 'not urgent'."""
        for generator in na.OBLIGATION_GENERATORS:
            with self.subTest(generator=generator.__name__):
                contract = ABSENCE_FIXTURES[generator.__name__]
                for action in generator(contract, AS_OF, None):
                    self.assertIsNone(action.deadline)
                    self.assertTrue(
                        action.deadline_unknown,
                        f"{generator.__name__} emitted an undatable "
                        "obligation as if it simply had no deadline."
                    )

    def test_undatable_actions_sort_above_dated_ones(self):
        """An obligation nobody can date could already be imminent; burying
        it under everything dated is #670's suppression, one step removed."""
        undatable = na.Action(
            what="undatable", deadline=None, value=None,
            value_note="birth_date missing", irreversible=False,
            why="x", deadline_unknown=True,
        )
        soon = na.Action(what="soon", deadline=date(2026, 8, 1), value=1.0,
                          value_note=None, irreversible=False, why="x")
        ordered = na.sort_actions([soon, undatable])
        self.assertEqual([a.what for a in ordered], ["undatable", "soon"])

    def test_deadline_unknown_requires_a_note_naming_the_missing_date(self):
        with self.assertRaises(ValueError):
            na.Action(what="x", deadline=None, value=1.0, value_note=None,
                       irreversible=False, why="x", deadline_unknown=True)

    def test_deadline_unknown_cannot_coexist_with_a_known_deadline(self):
        with self.assertRaises(ValueError):
            na.Action(what="x", deadline=date(2030, 1, 1), value=None,
                       value_note="note", irreversible=False, why="x",
                       deadline_unknown=True)


class KnownDeadlineOutOfWindowStillDropsTest(unittest.TestCase):
    """The one legitimate reason to drop an obligation: its deadline is
    KNOWN and out of window. #670 must not be over-corrected into "never
    filter anything"."""

    def test_a_known_past_deadline_is_still_dropped(self):
        contract = _contract(
            liabilities=[{
                "id": "loan_mortgage", "owner": "unknown_person",
                "kind": "mortgage",
                "balance": {"amount": 200000, "as_of": "2026-01-01"},
                "rate": 0.05, "rate_type": "fixed",
                "amortization": {"years": 20, "payment_monthly": 1500},
                "renewal_date": "2020-01-01",   # known, and long past
                "collateral": None,
            }],
        )
        self.assertEqual(na.mortgage_renewal_actions(contract, AS_OF, None), [])


if __name__ == "__main__":
    unittest.main()
