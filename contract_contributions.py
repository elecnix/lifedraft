"""``registered_contributions[]`` -> the internal dated-contribution list (#488).

A dated, one-off contribution into a declared registered account, with its own
deduction election. The adapter resolves each entry to the engine's slot --
which pot it lands in, and which ledger role claims its deduction -- and
refuses (DP#32) everything it cannot book faithfully, rather than dropping it:

* an account kind the engine cannot book a one-off contribution into. Only
  ``rrsp``, ``spousal_rrsp`` and ``tfsa`` are accepted. ``fhsa`` is refused
  because the engine models no FHSA deduction at all (the savings-flow FHSA is
  not deducted either), so the contribution would land and its deduction would
  silently vanish; ``resp`` is refused because the engine splits the household
  RESP allocation evenly across every RESP child, so a contribution for one
  beneficiary would be credited to the others, and the RESP is not in terminal
  wealth (#489);
* a contributor who cannot contribute to that account (an rrsp/tfsa paid by
  someone other than its owner; a spousal_rrsp paid by anyone but the primary,
  since the engine's single spousal pot is the primary's contribution to the
  spouse's plan);
* a date before ``as_of`` (the contribution is already in the balance; its
  undeducted part is ``people[].room.rrsp.undeducted_contributions``) or after
  the horizon (it would never happen in the run);
* a missing deduction election on a deductible kind, or any election on a
  tfsa.

Internal entry shape::

    {'id', 'year' (calendar), 'plan' ('rrsp'|'tfsa'),
     'role' ('primary'|'spousal'|'spouse'), 'amount',
     'deduct_from_year' (calendar; == year when claimed in the contribution
     year; None for a tfsa)}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from contract_errors import ContractAdaptationError

#: The account kinds a one-off contribution may target, and its plan.
BOOKABLE_KINDS = {'rrsp': 'rrsp', 'spousal_rrsp': 'rrsp', 'tfsa': 'tfsa'}

_REFUSED_KINDS = {
    'fhsa': ("the engine models no FHSA deduction (the savings-flow FHSA "
             "contribution is not deducted either), so the contribution would "
             "be booked and its deduction silently dropped"),
    'resp': ("the engine splits the household RESP allocation evenly across "
             "every RESP beneficiary, so a contribution for one beneficiary "
             "would be credited to the others; and the RESP is not yet in "
             "terminal wealth or the estate (#489)"),
}


def map_registered_contributions(doc: Dict, primary_id: str,
                                 spouse_id: Optional[str],
                                 horizon_end: Optional[int]) -> List[Dict[str, Any]]:
    """Map ``registered_contributions[]``; empty when none are declared."""
    accounts = {a['id']: a for a in doc['accounts']}
    as_of = doc['as_of']
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for entry in doc.get('registered_contributions', []):
        cid = entry['id']
        where = f"registered_contributions[{cid!r}]"
        if cid in seen:
            raise ContractAdaptationError(
                f"registered_contributions declares id={cid!r} more than once; "
                f"a duplicate would double-book a contribution, so it is "
                f"refused rather than deduplicated (DP#32).")
        seen.add(cid)

        acc = accounts.get(entry['account'])
        if acc is None:
            raise ContractAdaptationError(
                f"{where} targets account {entry['account']!r}, which is not a "
                f"declared account (DP#32).")
        kind = acc['kind']
        if kind in _REFUSED_KINDS:
            raise ContractAdaptationError(
                f"{where} targets a {kind} account: refused because "
                f"{_REFUSED_KINDS[kind]} (#488, DP#32).")
        if kind not in BOOKABLE_KINDS:
            raise ContractAdaptationError(
                f"{where} targets account {acc['id']!r} of kind {kind!r}; only "
                f"rrsp, spousal_rrsp and tfsa accept a declared one-off "
                f"contribution (#488, DP#32).")
        plan = BOOKABLE_KINDS[kind]
        role = _contributor_role(where, kind, acc['owner'], entry['contributor'],
                                 primary_id, spouse_id)

        date = entry['date']
        if date < as_of:
            raise ContractAdaptationError(
                f"{where} is dated {date}, before as_of {as_of}. A contribution "
                f"already made is in the account's balance; declare its "
                f"undeducted part as people[].room.rrsp.undeducted_contributions "
                f"instead (#488, DP#32).")
        year = int(date[:4])
        if horizon_end is not None and year > horizon_end:
            raise ContractAdaptationError(
                f"{where} is dated {date}, after the horizon's last year "
                f"{horizon_end}; it would never happen in the run (DP#32).")

        out.append({
            'id': cid, 'year': year, 'plan': plan, 'role': role,
            'amount': float(entry['amount']),
            'deduct_from_year': _deduct_from_year(where, plan, entry, year),
        })
    return out


def _contributor_role(where: str, kind: str, owner: Any, contributor: str,
                      primary_id: str, spouse_id: Optional[str]) -> str:
    """The engine slot a contribution is booked under (and, for an RRSP, the
    ledger role that claims its deduction)."""
    if kind == 'spousal_rrsp':
        if spouse_id is None or owner != spouse_id or contributor != primary_id:
            raise ContractAdaptationError(
                f"{where}: a spousal_rrsp contribution must be paid by the "
                f"primary into the spouse's plan -- the engine's one spousal "
                f"pot is that direction only (got contributor {contributor!r}, "
                f"owner {owner!r}) (DP#32).")
        return 'spousal'
    if contributor != owner:
        raise ContractAdaptationError(
            f"{where}: a {kind} contribution is paid by its owner {owner!r}, "
            f"but the contributor is {contributor!r} (DP#32).")
    if contributor == primary_id:
        return 'primary'
    if spouse_id is not None and contributor == spouse_id:
        return 'spouse'
    raise ContractAdaptationError(
        f"{where}: contributor {contributor!r} is neither the primary nor the "
        f"spouse; the engine books a declared contribution only for the couple "
        f"(DP#32).")


def _deduct_from_year(where: str, plan: str, entry: Dict, year: int) -> Optional[int]:
    timing = entry.get('deduction_timing')
    if plan == 'tfsa':
        if timing is not None:
            raise ContractAdaptationError(
                f"{where}: a tfsa contribution is not deductible, so it takes "
                f"no deduction_timing (DP#32).")
        return None
    if timing is None:
        raise ContractAdaptationError(
            f"{where}: an RRSP contribution must declare deduction_timing "
            f"(contribution_year or deferred); when to claim the deduction is "
            f"the household's election and is not defaulted (DP#32).")
    if timing == 'contribution_year':
        if 'deduct_from' in entry:
            raise ContractAdaptationError(
                f"{where}: deduct_from is only meaningful with "
                f"deduction_timing=deferred (DP#32).")
        return year
    if 'deduct_from' not in entry:
        raise ContractAdaptationError(
            f"{where}: deduction_timing=deferred requires deduct_from, the "
            f"first tax year the deduction may be claimed (DP#32).")
    deduct_year = int(entry['deduct_from'][:4])
    if deduct_year <= year:
        raise ContractAdaptationError(
            f"{where}: deduct_from {entry['deduct_from']} is not in a later tax "
            f"year than the contribution ({year}); a deferral must defer "
            f"(DP#32).")
    return deduct_year
