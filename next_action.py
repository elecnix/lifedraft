#!/usr/bin/env python3
"""next_action.py — Track 3 of epic #659 (issue #662).

The optimizer emits a ranking of strategy *names*. A person needs an
*action*: what to do, by when, worth how much, and — the single most
useful thing the tool can say and today says nothing — whether the window
closes for good.

This module produces a flat, sorted list of ``Action`` records from three
sources:

1. **The optimizer's winning ``decisions.*`` option** — rendered via
   ``action_from_winning_decision()``. This module does NOT import
   ``optimize.py`` (owned by a parallel track, and not yet wired to the
   ``schema/input_schema.json`` contract this module reads); the caller
   supplies the winning candidate's id and its already-computed dollar
   value, and this module renders it as an ``Action`` against the
   contract's own ``decisions.*`` candidate list (so the wording always
   matches the document, never a hardcoded label).

2. **Dated obligations already implied by the contract that nothing
   currently prints** (DP#28: eligibility/obligation dates are computed
   from the document's own dates, never hardcoded or asked for again):
   RRIF conversion (age 71), CESG/QESI eligibility (age 17, per child),
   term life insurance renewal, mortgage renewal, the FHSA 15-year
   participation window, and LIRA-to-LIF conversion (age 71).

3. **Decision windows that expire** — today, the one concretely
   derivable from the schema: segregating a mortgage cash-out advance at
   disbursement, because ITA s.20(1)(c) interest tracing is lost forever
   the moment borrowed funds are commingled with other cash. (A held
   mortgage rate and a notary valuation appointment are real examples
   from the epic, but the current schema has no field for either date —
   fabricating one would violate DP#2/DP#32, so they are left as a
   documented gap, not silently claimed.)

Every date-driven function here is a PURE function of dates already in
the document (DP#1) — nothing is hardcoded except well-known statutory
ages/rates that are themselves the rule (71, 17, the CESG/QESI
percentages — the same constants ``countries/canada/resp_rules.py`` and
``countries/canada/locked_in_account.py`` already carry, reused here
rather than re-derived, per DP#10).

DP#15: this module and its tests use only fabricated round numbers and
role-based names. No real figure, name, account number, or path from any
real household ever appears here.
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

# Reuse existing government-program modules for the numbers that ARE
# rules, rather than re-deriving them (DP#10 — one module owns each
# program). next_action.py owns none of these programs; it only reads
# their published constants to cost an obligation it discovered from the
# contract's own dates.
from countries.canada.resp_rules import (
    RESPCalculator,
    get_cesg_contribution_max,
)
from countries.canada.fhsa import FHSA_MAX_YEARS_OPEN, FHSA_MAX_AGE

# Statutory ages that ARE the rule (not personal data, DP#2) — duplicated
# locally the same way countries/canada/locked_in_account.py's
# CONVERSION_AGE and countries/canada/retirement.py's rrif_conversion_age
# default already do; no single module owns "age 71" across RRSP/LIRA.
RRIF_CONVERSION_AGE = 71   # ITA s.146(3)/s.146.3
LIRA_CONVERSION_AGE = 71   # Pension benefits standards legislation
CESG_ELIGIBILITY_END_AGE = 17  # Canada Education Savings Act


# ─────────────────────────────────────────────────────────────────────────
# The Action record
# ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Action:
    """A single dated, costed, owned action.

    ``value=None`` means the dollar value depends on something not yet
    resolved (an unelected choice, an unquoted premium, an unrequested
    CRA record) — in that case ``value_note`` MUST explain what resolves
    it, never a fabricated number (DP#32: absence must fail loudly, never
    read as zero — the same principle applied to a dollar VALUE here:
    unknown must read as ``None``, never as a plausible-looking guess).

    ``deadline`` has THREE distinct states, and collapsing any two of them
    is the bug class #670 was filed for:

    - a ``date``           — a hard calendar deadline.
    - ``None``             — genuinely no calendar deadline. Either the
                             window closes at an EVENT rather than a date
                             (``irreversible=True``: "before disbursement"),
                             or the action is simply not time-boxed.
    - ``deadline_unknown`` — a deadline that DOES exist and that we cannot
                             date, because a date the document should carry
                             is missing (a null ``birth_date``, a term
                             policy with no ``term_end_date``). This is NOT
                             "no deadline" and must never be sorted as if it
                             were: an undatable obligation could be
                             imminent. It sorts to the TOP, with a note
                             saying which date to go and get.
    """
    what: str
    deadline: Optional[date]
    value: Optional[float]
    value_note: Optional[str]
    irreversible: bool
    why: str
    deadline_unknown: bool = False

    def __post_init__(self) -> None:
        if self.value is None and not self.value_note:
            raise ValueError(
                f"Action(what={self.what!r}): value=None requires a value_note "
                "explaining what resolves it (never a silent unknown)."
            )
        if self.deadline_unknown and self.deadline is not None:
            raise ValueError(
                f"Action(what={self.what!r}): deadline_unknown=True is "
                "incompatible with a known deadline."
            )
        if self.deadline_unknown and not self.value_note:
            raise ValueError(
                f"Action(what={self.what!r}): deadline_unknown=True requires a "
                "value_note naming the date that is missing and where to get it."
            )


# ─────────────────────────────────────────────────────────────────────────
# Small date helpers — pure functions of dates already in the document
# ─────────────────────────────────────────────────────────────────────────

def _parse_date(value: Optional[str]) -> Optional[date]:
    if value is None:
        return None
    return date.fromisoformat(value)


def _end_of_year_turning(birth_date: date, age: int) -> date:
    """Dec 31 of the calendar year ``birth_date``'s person turns ``age``."""
    return date(birth_date.year + age, 12, 31)


def _people_by_id(contract: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {p["id"]: p for p in contract.get("people", [])}


def _horizon_end_date(contract: Dict[str, Any]) -> Optional[date]:
    """The projection's terminal date: Dec 31 of the year the horizon
    person reaches ``decisions.horizon.until_age``. Used only to decide
    whether an obligation falls inside or outside the planning window —
    obligations outside it are not suppressed from existing, just not
    reported (nothing today prints them at all, so reporting only what's
    in scope is a strict improvement, not a regression)."""
    horizon = contract.get("decisions", {}).get("horizon")
    if not horizon:
        return None
    person = _people_by_id(contract).get(horizon.get("person"))
    if not person or not person.get("birth_date"):
        return None
    return _end_of_year_turning(_parse_date(person["birth_date"]), horizon["until_age"])


def _owner_person_ids(owner: Any) -> List[str]:
    """Every person id behind an ``owner_ref`` (a bare id, or a joint
    split). Returning a LIST rather than skipping anything that isn't a
    plain string means an unexpected ownership shape can never silently
    delete an obligation (#670's rule, applied to ownership)."""
    if isinstance(owner, str):
        return [owner]
    if isinstance(owner, dict):
        return [j["person"] for j in owner.get("joint", [])]
    return []


def _out_of_window(deadline: Optional[date], as_of: date,
                    horizon_end: Optional[date]) -> bool:
    """True only when a deadline is KNOWN and falls outside the reporting
    window — i.e. it has already passed, or it lies beyond the projection's
    terminal date.

    A ``deadline`` of ``None`` is NOT out of window. That distinction is
    the whole of #670: an obligation we cannot DATE is not an obligation
    that does not EXIST, and dropping it is exactly the silent suppression
    this module was built to eliminate. Only a deadline we successfully
    computed can be judged in or out of scope.
    """
    if deadline is None:
        return False
    if deadline < as_of:
        return True
    if horizon_end is not None and deadline > horizon_end:
        return True
    return False


# ── #670's rule, stated once, applied by every generator below ──────────
#
#   AN UNKNOWN INPUT MAY CHANGE AN ACTION'S VALUE.
#   IT MUST NEVER DELETE THE ACTION.
#
# A missing fact is the strongest possible reason to TELL the user about a
# dated obligation -- it is the only way they learn the fact is worth going
# to get. The module that suppressed a $750/yr expiring grant because the
# household had not yet requested the CRA record that sizes it was DP#32
# violated inside the module built to surface absences (#670).
#
# The only thing a generator may legitimately drop is an obligation whose
# deadline is KNOWN and out of window (already past, or beyond the
# horizon) -- see ``_out_of_window``. Everything else is emitted, with
# ``value=None`` + a ``value_note`` naming the fact to go and get, and/or
# ``deadline_unknown=True`` naming the date to go and get.
#
# ``tests/architecture/test_next_action_absence.py`` enforces this for
# EVERY generator in ``OBLIGATION_GENERATORS``, and fails the build if a
# new generator is added without an absence fixture -- so "we fixed one
# `continue`" becomes "this class cannot come back".

MISSING_BIRTH_DATE_NOTE = (
    "This person's birth_date is missing from the document, so the "
    "deadline CANNOT BE DATED — it may already be imminent. Supply "
    "people[].birth_date to date it."
)


def _resp_beneficiary_ids(contract: Dict[str, Any]) -> set:
    """Everyone named as a beneficiary of an RESP account."""
    ids: set = set()
    for account in contract.get("accounts", []):
        if account.get("kind") != "resp":
            continue
        resp = account.get("resp")
        if resp is None:
            continue
        ids.update(resp.get("beneficiaries", []))
    return ids


def _child_person_ids(contract: Dict[str, Any]) -> set:
    """Everyone who is the target of a ``parent_of`` edge — i.e. a child of
    someone in the document. A child with NO RESP account and NO stated
    room is exactly the household that most needs to hear that a grant with
    a hard expiry exists (#670), so being a child is itself a trigger; the
    age-17 deadline then filters the adults back out on its own."""
    ids: set = set()
    for person in contract.get("people", []):
        for rel in person.get("relationships", []):
            if rel.get("type") == "parent_of":
                ids.add(rel["person"])
    return ids


# ─────────────────────────────────────────────────────────────────────────
# Source 1 — the optimizer's winning decisions.* option
# ─────────────────────────────────────────────────────────────────────────

# decisions.* category -> path to the candidate list inside the contract.
_DECISION_CATEGORY_PATHS: Dict[str, Tuple[str, ...]] = {
    "contribution_strategy": ("decisions", "contribution_strategy"),
    "income": ("decisions", "income"),
    "resp_action": ("decisions", "resp_action"),
    "estate_elections": ("decisions", "estate_elections"),
    "mortgage_refinance": ("decisions", "mortgage", "refinance_options"),
    "mortgage_renewal": ("decisions", "mortgage", "renewal_options"),
}


def find_decision_candidate(contract: Dict[str, Any], category: str,
                             candidate_id: str) -> Optional[Dict[str, Any]]:
    """Look up one candidate dict by id inside ``decisions.<category>``
    (or ``decisions.mortgage.<category>`` for the two mortgage lists)."""
    path = _DECISION_CATEGORY_PATHS.get(category)
    if path is None:
        raise KeyError(
            f"unknown decisions.* category {category!r}; "
            f"expected one of {sorted(_DECISION_CATEGORY_PATHS)}"
        )
    node: Any = contract
    for key in path:
        node = node.get(key, []) if isinstance(node, dict) else node
    for candidate in node:
        if candidate.get("id") == candidate_id:
            return candidate
    return None


def _first_liability(contract: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    for liability in contract.get("liabilities", []):
        if liability.get("kind") == kind:
            return liability
    return None


def action_from_winning_decision(
    contract: Dict[str, Any],
    category: str,
    candidate_id: str,
    *,
    value: Optional[float] = None,
    value_note: Optional[str] = None,
    deadline: Optional[date] = None,
    irreversible: bool = False,
    why: str = "Selected by the optimizer as the highest-ranked option.",
) -> Action:
    """Render the optimizer's winning ``decisions.<category>`` candidate as
    an ``Action``, using the contract's own ``label`` text (never a
    hardcoded string). ``value``/``value_note`` follow the same shape as
    every other Action here: pass ``value=None`` with a ``value_note``
    rather than a number derived from a guess when the dollar value
    depends on something not yet resolved (Track 2's VOI ranking, once
    wired, tells you which unknowns those are — this module doesn't need
    to import it to support the shape).

    For the two mortgage categories, ``deadline`` defaults to the first
    ``kind: mortgage`` liability's ``renewal_date`` when not supplied
    explicitly, since that's the natural "must decide by" date for a
    refinance/renewal choice.
    """
    candidate = find_decision_candidate(contract, category, candidate_id)
    if candidate is None:
        raise KeyError(
            f"no decisions.{category} candidate with id={candidate_id!r}"
        )
    if deadline is None and category in ("mortgage_refinance", "mortgage_renewal"):
        mortgage = _first_liability(contract, "mortgage")
        if mortgage is not None and mortgage.get("renewal_date"):
            deadline = _parse_date(mortgage["renewal_date"])
    return Action(
        what=candidate.get("label", candidate_id),
        deadline=deadline,
        value=value,
        value_note=value_note,
        irreversible=irreversible,
        why=why,
    )


# ─────────────────────────────────────────────────────────────────────────
# Source 2 — dated obligations already implied by the contract
# ─────────────────────────────────────────────────────────────────────────

def rrif_conversion_actions(contract: Dict[str, Any], as_of: date,
                             horizon_end: Optional[date] = None) -> List[Action]:
    """RRIF conversion: by Dec 31 of the year the annuitant turns 71, per
    PERSON holding an rrsp/spousal_rrsp account (not per account — the
    deadline is the same person-level obligation regardless of how many
    registered accounts they hold)."""
    people = _people_by_id(contract)
    owners_seen: set = set()
    actions: List[Action] = []
    for account in contract.get("accounts", []):
        if account.get("kind") not in ("rrsp", "spousal_rrsp"):
            continue
        for owner_id in _owner_person_ids(account.get("owner")):
            if owner_id in owners_seen:
                continue
            person = people.get(owner_id, {})
            # #670: a missing birth_date makes the deadline UNDATABLE, not
            # nonexistent. Emit it, flagged, rather than deleting an
            # obligation that could already be imminent.
            birth_date = _parse_date(person.get("birth_date"))
            deadline = (_end_of_year_turning(birth_date, RRIF_CONVERSION_AGE)
                        if birth_date is not None else None)
            if _out_of_window(deadline, as_of, horizon_end):
                continue
            owners_seen.add(owner_id)
            tax_note = (
                "Forces mandatory annual minimum withdrawals starting the "
                "following year; the tax cost depends on the account "
                "balance and marginal rate at conversion, neither known "
                "today."
            )
            actions.append(Action(
                what=f"Convert {person.get('label', owner_id)}'s RRSP(s) to a "
                     "RRIF (or annuitize/cash out).",
                deadline=deadline,
                value=None,
                value_note=(tax_note if birth_date is not None
                            else f"{MISSING_BIRTH_DATE_NOTE} {tax_note}"),
                irreversible=False,
                why=(
                    "ITA s.146(3)/s.146.3: an RRSP must be converted to a RRIF "
                    "(or matured otherwise) by December 31 of the year the "
                    "annuitant turns 71. Mandatory; not a choice to avoid, "
                    "only when."
                ),
                deadline_unknown=birth_date is None,
            ))
    return actions


def lira_conversion_actions(contract: Dict[str, Any], as_of: date,
                             horizon_end: Optional[date] = None) -> List[Action]:
    """LIRA/CRI conversion to a LIF (or annuity): by Dec 31 of the year the
    owner turns 71 — the same age-71 deadline as an RRSP's RRIF
    conversion, but a separate statutory regime (pension standards
    legislation, not the ITA), and per person, like RRIF above."""
    people = _people_by_id(contract)
    owners_seen: set = set()
    actions: List[Action] = []
    for account in contract.get("accounts", []):
        if account.get("kind") != "lira":
            continue
        for owner_id in _owner_person_ids(account.get("owner")):
            if owner_id in owners_seen:
                continue
            person = people.get(owner_id, {})
            birth_date = _parse_date(person.get("birth_date"))  # #670
            deadline = (_end_of_year_turning(birth_date, LIRA_CONVERSION_AGE)
                        if birth_date is not None else None)
            if _out_of_window(deadline, as_of, horizon_end):
                continue
            owners_seen.add(owner_id)
            band_note = (
                "Sets the LIF minimum/maximum withdrawal band going "
                "forward; the tax cost depends on the account balance and "
                "marginal rate at conversion, neither known today."
            )
            actions.append(Action(
                what=f"Convert {person.get('label', owner_id)}'s LIRA/CRI to a "
                     "LIF (or annuitize).",
                deadline=deadline,
                value=None,
                value_note=(band_note if birth_date is not None
                            else f"{MISSING_BIRTH_DATE_NOTE} {band_note}"),
                irreversible=False,
                why=(
                    "Pension benefits standards legislation requires a "
                    "LIRA/CRI to convert to a LIF (or be annuitized) by "
                    "December 31 of the year the owner turns 71."
                ),
                deadline_unknown=birth_date is None,
        ))
    return actions


def cesg_eligibility_actions(contract: Dict[str, Any], as_of: date,
                              horizon_end: Optional[date] = None) -> List[Action]:
    """CESG/QESI eligibility: ends Dec 31 of the year the child turns 17 —
    PER CHILD, not per family (#659's own headline example of an error the
    tool used to make silently). Triggered by any person who carries RESP
    room (``people[].room.resp``), by being named as an RESP account's
    beneficiary, or simply by being a child of the household.

    #670 — THE TRIGGER MUST NOT BE THE UNKNOWN FACT. This generator used
    to key off ``room.resp is not None``, so a child whose RESP room was
    ``null`` produced NO action at all. But ``room.resp: null`` does not
    mean "no room" — it means "we do not know the room", which is the
    COMMON case: the per-beneficiary CESG split appears on no account
    statement and exists only in the CRA/ESDC per-beneficiary record the
    household has to go and request. The tool therefore said nothing
    precisely when the user most needed to hear it, and never told them to
    go and get the record. A missing input must change the VALUE
    (``value=None`` + a note naming the record to request); it must never
    delete a dated, expiring, irreversible obligation.
    """
    contribution_max = get_cesg_contribution_max(as_of.year)
    cesg_value = contribution_max * RESPCalculator.CESG_BASIC_RATE
    qesi_value = min(contribution_max, RESPCalculator.QESI_ANNUAL_CONTRIBUTION_MAX) \
        * RESPCalculator.QESI_BASIC_RATE

    beneficiary_ids = _resp_beneficiary_ids(contract)
    child_ids = _child_person_ids(contract)

    actions: List[Action] = []
    for person in contract.get("people", []):
        person_id = person["id"]
        room = person.get("room", {})
        has_stated_room = room.get("resp") is not None
        if not (has_stated_room or person_id in beneficiary_ids
                or person_id in child_ids):
            continue

        birth_date = _parse_date(person.get("birth_date"))
        deadline = (_end_of_year_turning(birth_date, CESG_ELIGIBILITY_END_AGE)
                    if birth_date is not None else None)
        # An age-17 deadline already past is a genuinely closed window --
        # that, and only that, is a legitimate reason to drop it. (It is
        # also what keeps the adults of the household off this list.)
        if _out_of_window(deadline, as_of, horizon_end):
            continue

        province = person.get("residency", {}).get("province", "")
        in_quebec = province.lower() in ("quebec", "qc")
        max_grant = cesg_value + (qesi_value if in_quebec else 0.0)

        if has_stated_room:
            value: Optional[float] = max_grant
            value_note: Optional[str] = None
        else:
            # #670: unknown room changes the VALUE, never deletes the action.
            value = None
            grant_desc = (
                f"20% CESG + 10% QESI on the first ${contribution_max:,.0f}"
                if in_quebec else
                f"20% CESG on the first ${contribution_max:,.0f}"
            )
            value_note = (
                f"Worth up to ${max_grant:,.0f}/yr ({grant_desc}), but this "
                "child's REMAINING grant room is UNKNOWN — the per-beneficiary "
                "split is on no account statement. Request the CRA/ESDC "
                "per-beneficiary CESG record to size it."
            )
        if birth_date is None:
            value = None
            value_note = f"{MISSING_BIRTH_DATE_NOTE} {value_note or ''}".strip()

        actions.append(Action(
            what=(
                f"Contribute ${contribution_max:,.0f} to the RESP for "
                f"{person.get('label', person_id)} — final year of CESG "
                "eligibility."
            ),
            deadline=deadline,
            value=value,
            value_note=value_note,
            irreversible=True,
            why=(
                "Canada Education Savings Act: CESG (and, in Quebec, QESI) "
                "are only paid on contributions made on or before December "
                "31 of the year the beneficiary turns 17. A missed final "
                "year's grant room cannot be recovered later."
            ),
            deadline_unknown=birth_date is None,
        ))
    return actions


def term_life_renewal_actions(contract: Dict[str, Any], as_of: date,
                               horizon_end: Optional[date] = None) -> List[Action]:
    """Term life insurance renewal: the ``term_end_date`` on any
    ``estate.life_insurance[]`` entry of ``kind: term`` — a renewal
    decision, and term premiums typically step steeply at renewal."""
    actions: List[Action] = []
    for policy in contract.get("estate", {}).get("life_insurance", []):
        if policy.get("kind") != "term":
            continue
        # #670: a TERM policy with no term_end_date is a policy whose term
        # ends on a date nobody has looked up -- not a policy without a
        # term. Emit it, flagged, and say where the date lives.
        deadline = _parse_date(policy.get("term_end_date"))
        if _out_of_window(deadline, as_of, horizon_end):
            continue
        premium_note = (
            "The insurer's renewal premium isn't known until quoted; "
            "term premiums typically step steeply (often several-fold) "
            "at renewal — get a fresh quote before the term ends."
        )
        missing_date_note = (
            "This policy is kind=term but carries NO term_end_date, so the "
            "renewal CANNOT BE DATED — it may already have passed. Read the "
            "policy contract's term/expiry date."
        )
        undated = deadline is None
        actions.append(Action(
            what=f"Decide on the term-life renewal ({policy['id']}).",
            deadline=deadline,
            value=None,
            value_note=(f"{missing_date_note} {premium_note}" if undated
                        else premium_note),
            irreversible=False,
            why=(
                "The policy's fixed-premium term ends on term_end_date; "
                "the insurer re-underwrites or steps the premium at "
                "renewal."
            ),
            deadline_unknown=undated,
        ))
    return actions


def mortgage_renewal_actions(contract: Dict[str, Any], as_of: date,
                              horizon_end: Optional[date] = None) -> List[Action]:
    """Mortgage renewal: ``liabilities[].renewal_date``, one action per
    mortgage liability."""
    actions: List[Action] = []
    for liability in contract.get("liabilities", []):
        if liability.get("kind") != "mortgage":
            continue
        deadline = _parse_date(liability.get("renewal_date"))  # #670
        if _out_of_window(deadline, as_of, horizon_end):
            continue
        rate_note = (
            "The payment/interest impact depends on rates prevailing "
            "at renewal, and on which decisions.mortgage.renewal_options "
            "candidate is taken — neither is known today."
        )
        missing_date_note = (
            "This mortgage carries NO renewal_date, so the renewal CANNOT "
            "BE DATED — it may be imminent. Read it off the mortgage "
            "contract or the lender's statement."
        )
        undated = deadline is None
        actions.append(Action(
            what=f"Shop or renew the mortgage ({liability['id']}).",
            deadline=deadline,
            value=None,
            value_note=(f"{missing_date_note} {rate_note}" if undated
                        else rate_note),
            irreversible=False,
            why="The current term matures on renewal_date; the lender's "
                "current-rate offer window closes at that date.",
            deadline_unknown=undated,
        ))
    return actions


def fhsa_window_actions(contract: Dict[str, Any], as_of: date,
                         horizon_end: Optional[date] = None) -> List[Action]:
    """FHSA's 15-year participation window: must close by Dec 31 of the
    earliest of the 15th anniversary of opening or the year the holder
    turns 71 (ITA s.146.6). Reuses FHSA_MAX_YEARS_OPEN/FHSA_MAX_AGE from
    countries/canada/fhsa.py rather than re-deriving them (DP#10)."""
    people = _people_by_id(contract)
    actions: List[Action] = []
    for account in contract.get("accounts", []):
        if account.get("kind") != "fhsa":
            continue
        owner_ids = _owner_person_ids(account.get("owner"))
        owner_id = owner_ids[0] if owner_ids else None
        person = people.get(owner_id, {}) if owner_id else {}
        # DP#32: explicit absence-testing, never `x.get(k) or DEFAULT`.
        # #670: a missing fhsa sub-object or opened_date leaves the 15-year
        # window UNDATABLE -- it does not abolish the window. Emit, flagged.
        fhsa_data = account.get("fhsa")
        opened_date = (_parse_date(fhsa_data.get("opened_date"))
                       if fhsa_data is not None else None)
        birth_date = _parse_date(person.get("birth_date"))

        candidates: List[date] = []
        if opened_date is not None:
            candidates.append(date(opened_date.year + FHSA_MAX_YEARS_OPEN, 12, 31))
        if birth_date is not None:
            candidates.append(_end_of_year_turning(birth_date, FHSA_MAX_AGE))
        deadline = min(candidates) if candidates else None
        if _out_of_window(deadline, as_of, horizon_end):
            continue

        use_note = (
            "Value depends on whether a qualifying home purchase "
            "happens before the window closes (tax-free withdrawal) "
            "or the balance is instead transferred tax-deferred to an "
            "RRSP — neither is resolved today."
        )
        missing_date_note = (
            "The account's opened_date (and the holder's birth_date) are "
            "missing, so the 15-year participation window CANNOT BE DATED "
            "— it may be close to closing. The opening date is on the FHSA "
            "account statement."
        )
        undated = deadline is None
        owner_label = person.get("label", owner_id) if person else owner_id
        actions.append(Action(
            what=f"Close out {owner_label}'s FHSA ({account['id']}) — use it "
                 "on a qualifying home purchase or transfer it to an RRSP.",
            deadline=deadline,
            value=None,
            value_note=(f"{missing_date_note} {use_note}" if undated
                        else use_note),
            deadline_unknown=undated,
            irreversible=True,
            why=(
                "ITA s.146.6: an FHSA must be closed by December 31 of the "
                "earliest of the 15th anniversary of opening or the year "
                "the holder turns 71. Once the window closes without "
                "action, the account's special tax-free-withdrawal status "
                "is gone for good."
            ),
        ))
    return actions


# Every source-2 generator, in one registry. This is not decoration: it is
# the list ``tests/architecture/test_next_action_absence.py`` iterates over
# to prove that NO generator deletes its action when its value-determining
# (or date-determining) inputs are null (#670). A new generator added here
# without a matching absence fixture FAILS THE BUILD -- which is what turns
# "we fixed one `continue`" into "this bug class cannot come back".
OBLIGATION_GENERATORS = (
    rrif_conversion_actions,
    lira_conversion_actions,
    cesg_eligibility_actions,
    term_life_renewal_actions,
    mortgage_renewal_actions,
    fhsa_window_actions,
)


def derive_dated_obligations(contract: Dict[str, Any], as_of: date,
                              horizon_end: Optional[date] = None) -> List[Action]:
    """All of source 2 (dated obligations), concatenated."""
    actions: List[Action] = []
    for generator in OBLIGATION_GENERATORS:
        actions.extend(generator(contract, as_of, horizon_end))
    return actions


# ─────────────────────────────────────────────────────────────────────────
# Source 3 — decision windows that expire
# ─────────────────────────────────────────────────────────────────────────

def disbursement_segregation_actions(contract: Dict[str, Any]) -> List[Action]:
    """A mortgage cash-out that will be invested must be segregated at the
    moment it is disbursed, or interest tracing under ITA s.20(1)(c) is
    lost for good the instant it is commingled with other cash. Triggered
    by any ``decisions.mortgage.refinance_options`` candidate with a
    nonzero ``cash_out`` — the option doesn't have to have "won" yet; the
    obligation is on the table the moment cash-out financing is even a
    live candidate, so it must be flagged before, not after, the
    optimizer picks a winner.

    No calendar date exists for "disbursement" in the contract (it
    depends on which option is chosen and when the lender funds it), so
    ``deadline=None`` here — the sort order still surfaces this first
    because it is irreversible (see ``_sort_key``)."""
    refinance_options = (
        contract.get("decisions", {}).get("mortgage", {}).get("refinance_options", [])
    )
    if not any(opt.get("cash_out", 0) > 0 for opt in refinance_options):
        return []
    return [Action(
        what="Segregate the investable portion of any mortgage advance at "
             "source, before disbursement.",
        deadline=None,
        value=None,
        value_note=(
            "The interest deduction preserved depends on which "
            "decisions.mortgage.refinance_options candidate is taken and "
            "how much of its cash-out is invested versus used personally "
            "— resolve which option wins before disbursing."
        ),
        irreversible=True,
        why=(
            "ITA s.18(11) bars an interest deduction on any portion of "
            "borrowed money used to contribute to an RRSP/TFSA/RESP/RRIF. "
            "Only a non-registered, income-producing investment qualifies "
            "under ITA s.20(1)(c), and only if the borrowed funds are "
            "TRACEABLE to it (CRA IT-533R). Once the advance is "
            "commingled in a chequing account with renovation money and "
            "registered contributions, the tracing — and with it the "
            "deduction — is lost PERMANENTLY."
        ),
    )]


def emergency_reserve_shortfall_actions(contract: Dict[str, Any]) -> List[Action]:
    """"You are N months short of the reserve you said you wanted" (#688,
    the action #679's ruin report implies).

    A household that declares ``assumptions.emergency_reserve.target_months``
    but whose ``held_in`` account cannot actually cover that many months of
    essential outflows is carrying a gap it has not been told about. The
    engine already computes this and deliberately does NOT raise on it --
    ``SimState.from_config`` clamps the opening reserve to the host account's
    balance with the comment "falling short of the declared target is a real,
    reportable fact, not an error." This is where it gets reported.

    Arithmetic is reused from ``liquidation_waterfall`` (``reserve_target`` /
    ``months_covered``) rather than restated, so there is exactly ONE
    definition of "a month of essential outflows" in the codebase and this
    action can never drift from the waterfall that draws the reserve down.

    No action when no reserve is declared: a household that never asked for a
    reserve is not "short" of one. Its $0 reserve is reported by the solvency
    output (#679), which is the right place for an absence -- inventing a
    target here in order to declare the household short of it would be the
    engine having an opinion (DP#2/DP#13).
    """
    from liquidation_waterfall import months_covered, reserve_target

    # DP#32 throughout: explicit absence tests, never `x or DEFAULT`. Each of
    # these keys is legitimately ABSENT for some households, and an absence is
    # a different fact from a zero -- conflating them is what this guard exists
    # to prevent.
    assumptions = contract.get("assumptions")
    reserve = None if assumptions is None else assumptions.get("emergency_reserve")
    if reserve is None:
        return []
    target_months = reserve.get("target_months")
    # target_months == 0 is a real, declarable answer ("I hold no reserve", #688)
    # -- a household that chose to hold nothing is not "short" of anything.
    if target_months is None or target_months <= 0:
        return []

    budget = contract.get("household_budget")
    living_costs = None if budget is None else budget.get("annual_living_costs")
    if living_costs is None:
        # DP#32: the target is declared in MONTHS of essential outflows, and
        # without the budget there is no way to price a month. Do not guess
        # one -- say which document settles it.
        return [Action(
            what="Measure your annual living costs, so the emergency reserve "
                 "you declared can actually be sized.",
            deadline=None,
            value=None,
            value_note=(
                "assumptions.emergency_reserve.target_months is declared, but "
                "household_budget.annual_living_costs is not -- a reserve is "
                "denominated in MONTHS of essential outflows, so without the "
                "budget the target cannot be priced. Resolve it from 12 months "
                "of bank/credit statements (total spend, minus debt payments "
                "and minus savings)."
            ),
            irreversible=False,
            why=(
                "The reserve is the first thing drawn when income stops "
                "(issue #679's liquidation waterfall). A target expressed in "
                "months that nobody can convert into dollars is not a plan."
            ),
        )]

    debt_service = 0.0
    mortgage = _first_liability(contract, "mortgage")
    if mortgage is not None:
        amortization = mortgage.get("amortization")
        if amortization is not None:
            debt_service = amortization.get("payment_monthly", 0.0) * 12

    target = reserve_target(target_months, annual_living_costs=living_costs,
                            annual_debt_service=debt_service)

    held_in = reserve.get("held_in")
    if held_in is None:
        # The reserve sits outside every declared account (an ordinary chequing
        # balance the contract does not model). We cannot see its balance, so we
        # cannot say whether it is short -- and must not imply that it is.
        return []

    account = next((a for a in contract.get("accounts", [])
                    if a.get("id") == held_in), None)
    if account is None:
        return []
    balance = account.get("balance")
    if balance is None:
        # The named account carries no balance at all. We cannot see what is in
        # it, so we cannot say it is short -- and must not imply that it is.
        return []
    available = balance.get("amount", 0.0)

    # The engine's own carve rule: you cannot hold more cash in an account
    # than that account contains.
    actual = min(target, available)
    if actual >= target - 1e-6:
        return []          # the declared reserve is fully funded -- no action

    have_months = months_covered(actual, annual_living_costs=living_costs,
                                  annual_debt_service=debt_service)
    short_months = max(0.0, target_months - have_months)

    return [Action(
        what=(f"Top up the emergency reserve in '{held_in}': you are "
              f"{short_months:.1f} months short of the {target_months:.0f} "
              f"months you declared (${target - actual:,.0f} to close the gap)."),
        deadline=None,
        value=None,
        value_note=(
            "The value of closing this gap is the forced-sale cost it avoids, "
            "which depends on the shock -- it is priced by the "
            "emergency_reserve_months sweep (expected terminal wealth against "
            "the probability and cost of a forced liquidation), not by a single "
            "figure that can be stated here."
        ),
        irreversible=False,
        why=(
            f"The reserve is drawn FIRST when required outflows exceed income "
            f"(issue #679's waterfall). It currently covers {have_months:.1f} "
            f"months of essential outflows (living costs + debt service); you "
            f"declared a target of {target_months:.0f}. Everything past it is "
            f"sold at a tax cost -- and a job loss and a market drawdown are "
            f"CORRELATED, so the sale tends to happen at the bottom."
        ),
    )]


def derive_decision_windows(contract: Dict[str, Any]) -> List[Action]:
    """All of source 3 (decision windows that expire), concatenated."""
    return (disbursement_segregation_actions(contract)
            + emergency_reserve_shortfall_actions(contract))


# ─────────────────────────────────────────────────────────────────────────
# Assembly, sorting, and rendering
# ─────────────────────────────────────────────────────────────────────────

def _sort_key(action: Action) -> Tuple[int, date]:
    """Ordered by DEADLINE, not by value (#662). Four buckets:

    0. Irreversible with no calendar deadline — a window that is closing
       *right now*, before any dated item on the list (e.g. "before
       disbursement"). Deadline=None normally means "not urgent"; an
       irreversible window is the one deliberate exception, because its
       urgency is real even though its exact date isn't known.
    1. UNDATABLE — the obligation exists but a date the document should
       carry is missing (#670). It sorts near the TOP, not the bottom:
       an obligation nobody can date could already be imminent, and
       burying it under everything dated is the same silent suppression
       #670 was filed for, one step removed.
    2. Has a deadline — sorted ascending, soonest first.
    3. Reversible with no calendar deadline — genuinely not urgent
       ("a decision you can revisit next year is not urgent"), sorts last.
    """
    if action.deadline_unknown:
        return (1, date.min)
    if action.deadline is not None:
        return (2, action.deadline)
    if action.irreversible:
        return (0, date.min)
    return (3, date.max)


def sort_actions(actions: List[Action]) -> List[Action]:
    return sorted(actions, key=_sort_key)


def derive_actions(
    contract: Dict[str, Any],
    *,
    as_of: Optional[date] = None,
    winning_decisions: Optional[List[Action]] = None,
) -> List[Action]:
    """The full, sorted NEXT ACTIONS list: the winning decision(s) the
    caller supplies (source 1), plus every dated obligation (source 2) and
    decision window (source 3) this module can derive on its own from the
    contract's dates.

    ``as_of`` defaults to the document's own ``as_of`` field (DP#1: the
    document is date-stamped; "today" is data, not an implicit assumption
    baked into the code).
    """
    if as_of is None:
        as_of = _parse_date(contract["as_of"])
    horizon_end = _horizon_end_date(contract)
    actions: List[Action] = list(winning_decisions or [])
    actions.extend(derive_dated_obligations(contract, as_of, horizon_end))
    actions.extend(derive_decision_windows(contract))
    return sort_actions(actions)


def _wrap(text: str, width: int = 70, indent: str = " " * 30) -> str:
    lines = textwrap.wrap(text, width=width) or [""]
    return ("\n" + indent).join(lines)


def format_actions(actions: List[Action]) -> str:
    """Render the NEXT ACTIONS block in the shape #662 illustrates:
    ``[!] before disbursement`` / ``by <date>`` prefix, wrapped why/value
    text, and an explicit IRREVERSIBLE tag."""
    lines = ["NEXT ACTIONS", ""]
    for action in actions:
        if action.deadline_unknown:
            label = "DEADLINE UNKNOWN"
        elif action.deadline is not None:
            label = f"by {action.deadline.isoformat()}"
        elif action.irreversible:
            label = "before disbursement"
        else:
            label = "no fixed deadline"
        marker = "[!]" if action.irreversible else "   "
        if action.deadline_unknown and not action.irreversible:
            marker = "[?]"
        lines.append(f"  {marker} {label:<22} {_wrap(action.what)}")
        lines.append(f"{'':30}{_wrap(action.why)}")
        if action.value is not None:
            lines.append(f"{'':30}Worth ${action.value:,.0f}.")
        elif action.value_note:
            lines.append(f"{'':30}{_wrap(action.value_note)}")
        if action.irreversible:
            lines.append(f"{'':30}IRREVERSIBLE.")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _action_to_jsonable(action: Action) -> Dict[str, Any]:
    return {
        "what": action.what,
        "deadline": action.deadline.isoformat() if action.deadline else None,
        "deadline_unknown": action.deadline_unknown,
        "value": action.value,
        "value_note": action.value_note,
        "irreversible": action.irreversible,
        "why": action.why,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive dated, costed, irreversibility-flagged next "
                    "actions from an input contract document (issue #662)."
    )
    parser.add_argument("--input", required=True, help="Path to a contract JSON document.")
    parser.add_argument("--as-of", default=None,
                        help="Override the document's as_of date (YYYY-MM-DD).")
    parser.add_argument("--json", action="store_true",
                        help="Print machine-readable JSON instead of the text report.")
    args = parser.parse_args(argv)

    with open(args.input) as f:
        contract = json.load(f)

    as_of = _parse_date(args.as_of) if args.as_of else None
    actions = derive_actions(contract, as_of=as_of)

    if args.json:
        print(json.dumps([_action_to_jsonable(a) for a in actions], indent=2))
    else:
        print(format_actions(actions), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
