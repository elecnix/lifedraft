"""The superficial-loss window primitive (issue #141, ITA s.53(1)(c)).

## The rule being modelled

ITA s.53(1)(c): when a taxpayer disposes of capital property at a loss and
-- within 30 calendar days ending 30 days after the disposition -- the
taxpayer OR AN AFFILIATED PERSON acquires property that is "the same or
identical" to it, the capital loss is DENIED for the year. The denied loss
is not destroyed: under s.53(1)(f) it is added to the adjusted cost base of
the repurchased property, so the loss resurfaces on a LATER, genuine
disposition. (ITA s.53(1)(g.1)'s stop-loss on deferred-interest/bond
premium dispositions is out of scope for this slice.)

## The abstraction (disclosed via model_fidelity)

The statutory window is 61 days wide. The engine's step is a YEAR, and the
engine cannot see intra-year timing. This module therefore evaluates the
window at two annual steps:

- A loss realized in step Y is checked against repurchases in step Y and in
  step Y+1. A repurchase in EITHER step denies the loss; a repurchase-free
  Y+1 releases it into Y+1's capital-loss settlement (one year late -- the
  cost of the step boundary, disclosed as a model_fidelity approximation).

Both directions of the abstraction err toward DENIAL (the conservative
direction): a January disposition followed by a December repurchase is more
than 61 days apart in reality but is treated as superficial, and a released
loss reaches the carry-forward pool one step late. The engine never
understates tax because of this abstraction.

"Repurchase" at engine granularity is household-level: any contribution to
the household's non-registered pot (or the SM sleeve's readvance) in the
window counts, which SUBSUMES the affiliated-person limb -- a spouse's
repurchase denies the primary's loss, exactly as s.251.1(1)(a)/(b) would.
The engine does not yet track per-security holdings, so identity of
property is evaluated by DECLARATION (see below), not by ticker matching.

## Identity of property: declared substitute pairs

Absent any declaration, a repurchase in the window is treated as identical
(the conservative default -- DP#32: absence must not silently grant the
favourable reading). The household can declare that its repurchases are of
a NON-identical substitute (e.g. XEQT vs VEQT -- different issuers, s.53's
"identical" limb not met) via ``decisions.superficial_loss.substitute_pairs``
(e.g. ``[["XEQT", "VEQT"]]``). At the engine's current granularity a
non-empty declaration means "repurchases in the window are of the declared
non-identical substitutes", so the loss is allowed: the declaration is an
assertion of fact about what the household actually bought, not an
optimization lever (DP#30: the simulator models consequences; the
declarative pair list annotates that fact).

Pure functions only (DP#3); no engine state is constructed here -- the
registered ``superficial_loss`` rule (rules_superficial_loss.py) reads and
writes YearWorkingState, and this module stays the arithmetic + summarizer.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

# A pending-loss entry: {'year': <calendar step the loss was realized in>,
# 'amount': <RAW dollars>} (raw = 100%-inclusion dollars, matching the
# capital_loss ledger's raw convention; #140).
PendingEntry = Dict


def classify_window(
    realized_loss_raw: float,
    repurchase_this_step: float,
    substitutes_declared: bool,
    opening_pending: Sequence[PendingEntry],
    year: int,
) -> Tuple[float, float, float, List[PendingEntry]]:
    """Resolve one step of the annualized superficial-loss window.

    Args:
        realized_loss_raw: this step's realized capital LOSS on security
            dispositions, as a POSITIVE raw-dollar magnitude (the caller
            negates the signed position; 0.0 when the year netted a gain).
        repurchase_this_step: this step's household repurchase dollars
            (non-reg contributions + SM readvance + reinvested principal
            proceeds). > 0 means the household re-acquired in the window.
        substitutes_declared: True when the household declared substitute
            pairs -- the declaration asserts the repurchase is NON-identical,
            so no denial arises (and nothing is ever pended: a loss with a
            non-identical repurchase is allowed in its own step).
        opening_pending: entries pended from the PRIOR step (each
            ``{'year': int, 'amount': raw>0}``). Resolved this step.
        year: the calendar step this classification runs in (stored on any
            pended entry; DP#1: the ledger records when the loss arose).

    Returns ``(denied, pended, released, new_pending)``:

    - ``denied``: raw dollars denied THIS step (same-step loss + any pended
      entry caught by this step's repurchase). The caller adds these to the
      repurchased property's ACB under s.53(1)(f).
    - ``pended``: raw dollars of this step's loss held over to the next step
      for its Y+1 window check (excluded from THIS step's settlement).
    - ``released``: raw dollars of pended entries resolved as NON-superficial
      this step -- they enter this step's settlement here (one step late).
    - ``new_pending``: the carried-forward list for the next step (empty
      unless ``pended`` > 0).

    Loud-boundary refusals (DP#32): a negative loss magnitude, a negative
    repurchase, or a pended entry that is not ``{'year', 'amount'}`` with a
    positive raw amount raises ValueError -- the caller's disagreement with
    the window's contract must fail loudly, never be coerced to zero.
    """
    if realized_loss_raw < 0.0:
        raise ValueError(
            f"superficial_loss: realized_loss_raw must be a positive "
            f"magnitude, got {realized_loss_raw!r}")
    if repurchase_this_step < 0.0:
        raise ValueError(
            f"superficial_loss: repurchase_this_step must be >= 0, got "
            f"{repurchase_this_step!r}")
    for entry in opening_pending:
        if (not isinstance(entry, dict) or 'year' not in entry
                or 'amount' not in entry):
            raise ValueError(
                f"superficial_loss: malformed pending entry {entry!r} -- "
                f"expected {{'year': int, 'amount': float}}")
        if entry['amount'] <= 0.0:
            raise ValueError(
                f"superficial_loss: pending entry {entry!r} carries a "
                f"non-positive raw amount -- a pended loss is a positive "
                f"magnitude by construction")

    denied = 0.0
    released = 0.0
    new_pending: List[PendingEntry] = []

    # Step 1: resolve the PRIOR step's pended entries against THIS step's
    # window tail. The engine cannot distinguish "identical" from
    # "non-identical" repurchases at the pot level; a declared substitute
    # pair is the household's assertion that its repurchase is not
    # identical, so it releases rather than denies (s.53(1)(c)'s limb is
    # not met).
    for entry in opening_pending:
        if repurchase_this_step > 0.0 and not substitutes_declared:
            denied += entry['amount']
        else:
            released += entry['amount']

    # Step 2: resolve THIS step's own loss. Denied outright when a
    # same-step identical repurchase exists; held over to the next step's
    # window check otherwise (the Y+1 limb of the abstraction); allowed in
    # its own step when the repurchase is declared non-identical.
    if realized_loss_raw > 0.0:
        if substitutes_declared:
            # Non-identical repurchase -> allowed now, nothing pended.
            pass
        elif repurchase_this_step > 0.0:
            # Same-step repurchase (the classic same-day tax-loss harvest
            # shape): denied this step. The caller adds the amount to the
            # repurchased property's ACB (s.53(1)(f)).
            denied += realized_loss_raw
        else:
            # No repurchase THIS step; the Y+1 limb is still open. Pend the
            # loss -- it is excluded from this step's settlement and
            # resolves next step (denied if next step repurchases,
            # released into next step's settlement otherwise).
            new_pending.append({'year': year, 'amount': realized_loss_raw})

    pended = sum(entry['amount'] for entry in new_pending)
    return denied, pended, released, new_pending





def summarize_superficial_loss(results) -> Dict:
    """Fold a trajectory's ``YearResult`` list into the superficial-loss
    facts a household needs (issue #141's model_fidelity disclosure).

    Returns::

        {
          'engaged':           bool,   # any year denied or held a loss
          'first_denied_year': int|None,
          'denied_total':      float, # raw dollars denied across the run
          'acb_added_total':   float, # s.53(1)(f) ACB additions (== denied)
          'pending_years':     int,   # steps a loss spent held over
        }

    ``first_denied_year`` is the PROJECTION index (the trajectory's own
    clock -- YearResult carries no calendar year; the caller's surface
    labels it accordingly, mirroring #170).

    Pure function (DP#3); the optimize caller records the worst-across-
    scenarios reduction onto ``assumptions.superficial_loss`` for the
    fidelity caveat -- the same bridge as #707/#170 (DP#9: one spelling).
    """
    engaged = False
    first_denied_year = None
    denied_total = 0.0
    acb_added_total = 0.0
    pending_years = 0
    for row in results:
        denied = getattr(row, 'superficial_loss_denied', 0.0)
        acb = getattr(row, 'superficial_loss_acb_added', 0.0)
        held = getattr(row, 'superficial_loss_pended', 0.0)
        if denied > 0.0 or acb > 0.0 or held > 0.0:
            engaged = True
        denied_total += denied
        acb_added_total += acb
        pending_years += 1 if held > 0.0 else 0
        if denied > 0.0 and first_denied_year is None:
            first_denied_year = getattr(row, 'year', None)
    return {
        'engaged': engaged,
        'first_denied_year': first_denied_year,
        'denied_total': denied_total,
        'acb_added_total': acb_added_total,
        'pending_years': pending_years,
    }


def worst_superficial_loss(rows) -> Dict:
    """Reduce ranked-scenario superficial-loss summaries to the ONE summary
    the run-wide caveat names: the scenario that denied the MOST raw loss
    dollars; ties break to the earliest first-denied year. An empty or
    all-clear row set returns the all-clear summary (DP#32: "nothing was
    denied" is a checked result, not an absence). Mirrors
    ``rules_contributions.worst_rrsp_refusal`` (#170)."""
    engaged = [r for r in rows
               if isinstance(r, dict) and r.get('engaged')]
    if not engaged:
        return {'engaged': False, 'first_denied_year': None,
                'denied_total': 0.0, 'acb_added_total': 0.0,
                'pending_years': 0}
    return max(
        engaged,
        key=lambda row: (row.get('denied_total', 0.0),
                         -(row.get('first_denied_year')
                           if row.get('first_denied_year') is not None
                           else float('inf'))))
