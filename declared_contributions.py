"""Declared one-off registered contributions in the fold (issue #488).

``config.registered_contributions`` (mapped by ``contract_contributions``) is a
list of dated contributions into a registered pot, funded from OUTSIDE the
modelled savings flow. This module is the fold's one reading of it:

* :func:`declared_for_year` -- the year's amounts per allocation slot, and the
  RRSP slices whose deduction the contributor deferred (each claimable from a
  later projection year, enforced by ``rrsp_ledger``);
* :func:`check_declared_contributions_runnable` -- the run-level refusals the
  adapter cannot make (a contribution past the configured projection, and the
  monthly time step, which does not book them).

Pure functions over explicit inputs (DP#3).
"""

from __future__ import annotations

from typing import Dict, List, Tuple

#: Allocation slot each (plan, role) books into -- the keys of the
#: ``allocations`` dict ``simulate_year`` hands the pure step.
_SLOT = {
    ('rrsp', 'primary'): 'primary_rrsp',
    ('rrsp', 'spousal'): 'spousal_rrsp',
    ('rrsp', 'spouse'): 'spouse_rrsp',
    ('tfsa', 'primary'): 'primary_tfsa',
    ('tfsa', 'spouse'): 'spouse_tfsa',
}
SLOTS = tuple(_SLOT.values())


def declared_for_year(entries: List[Dict], sim_year: int,
                      start_year: int) -> Tuple[Dict[str, float], List[Dict]]:
    """``(amounts_by_slot, deferred)`` for calendar year ``sim_year``.

    ``deferred`` lists ``{'role', 'amount', 'claimable_from'}`` for each RRSP
    contribution whose deduction was deferred, ``claimable_from`` being the
    0-based projection year of its ``deduct_from_year`` (the ledger's year
    convention). An unknown (plan, role) raises ``KeyError`` -- the adapter
    produces only the five pairs above (DP#32)."""
    amounts = {slot: 0.0 for slot in SLOTS}
    deferred: List[Dict] = []
    for e in entries:
        if e['year'] != sim_year:
            continue
        amounts[_SLOT[(e['plan'], e['role'])]] += e['amount']
        if e['plan'] == 'rrsp' and e['deduct_from_year'] > e['year']:
            deferred.append({'role': e['role'], 'amount': e['amount'],
                             'claimable_from': e['deduct_from_year'] - start_year})
    return amounts, deferred


def check_declared_contributions_runnable(entries: List[Dict], start_year: int,
                                          projection_years: int,
                                          time_step: str) -> None:
    """Refuse a run that would silently drop a declared contribution."""
    if not entries:
        return
    if time_step == 'monthly':
        raise ValueError(
            "registered_contributions[] is booked by the annual fold only; the "
            "monthly time step would silently drop every declared contribution "
            "(issue #488, DP#32). Run with the annual time step.")
    last_year = start_year + projection_years - 1
    for e in entries:
        if not start_year <= e['year'] <= last_year:
            raise ValueError(
                f"registered_contributions[{e['id']!r}] is in {e['year']}, "
                f"outside the projection {start_year}-{last_year}; it would "
                f"never be booked (issue #488, DP#32).")
