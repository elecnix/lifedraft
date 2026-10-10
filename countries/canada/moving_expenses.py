"""Issue #376: the moving-expense deduction (ITA s.62 / TA s.348).

An individual who moves at least 40 km to start a job, a business, or
full-time post-secondary studies may deduct eligible moving costs against
INCOME EARNED AT THE NEW LOCATION in the year of the move. The deduction is
capped at that income, and the unused balance carries forward to later years
(CRA line 21900, Quebec TP-1 line 228).

Two design decisions worth stating plainly, because both are places where the
engine has less information than the statute does.

**The cap is the member's whole declared income for the year, not their
new-location income.** The statute caps the deduction at income earned *at the
new location*; this engine has no notion of where work is performed, so the
only income it can price the cap against is the member's total. That is an
UPPER BOUND on the statutory cap, so it can permit MORE deduction than the
statute would -- never less. Inventing a location split would be a fabricated
precision; the looser bound is at least truthful about which way it errs.

**The 40 km test is the taxpayer's to assert.** Distance is not a quantity the
engine holds: it does not know either endpoint's geography. The declaration is
therefore that the move QUALIFIED, and the schema says so -- a claim is an
assertion of eligibility, not a number the engine verified. A household that
declares a move it did not qualify for gets a deduction it should not have,
which is the taxpayer's own input error, not an engine default.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

#: The CRA's minimum one-way distance for a move to qualify (line 21900).
MINIMUM_MOVING_KM = 40


def claimed_moving_expenses(member: Dict[str, Any], sim_year: int) -> float:
    """The moving expenses this member DECLARES for calendar ``sim_year``.

    Reads ``member['moving_expenses']``, a list of ``{year, amount}`` records.
    An absent block is zero -- a household that moved and declared nothing owes
    no deduction, and one that never moved never reaches here. A year with no
    matching record contributes zero rather than the previous year's amount:
    the deduction is claimed in the year of the move, not every year after it.
    """
    records = member.get('moving_expenses')
    if records is None:
        return 0.0
    total = 0.0
    for record in records:
        # Explicit `is not None` on both sides (DP#32): a year of 0 is a real
        # record the schema permits, and an amount of 0 is a real claim, so
        # neither may be skipped by truthiness.
        if record.get('year') == sim_year and record.get('amount') is not None:
            total += float(record['amount'])
    return total


def moving_expense_deduction(
        claimed: float, income: float, carry_forward: float = 0.0
) -> Tuple[float, float]:
    """The deductible amount for one member-year, and what carries forward.

    ``min(claimed + carry_forward, income)`` -- the eligible expenses plus
    anything left unused last year, limited to the income available to shelter
    them. The remainder carries forward, which is what makes the second case of
    the acceptance test work: $30,000 of expenses against $25,000 of income
    deducts $25,000 now and $5,000 next year.

    Returns ``(deduction, remaining_carry_forward)``. Pure (DP#3): a function
    of its three arguments, nothing read off any instance.
    """
    available = claimed + carry_forward
    if available <= 0.0:
        return 0.0, max(carry_forward, 0.0)
    if income <= 0.0:
        # No income to shelter them: the expenses stay carried forward rather
        # than being deducted (and are not lost -- the balance is returned).
        return 0.0, available
    deduction = min(available, income)
    return deduction, available - deduction