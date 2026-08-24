"""Deployment lag — the cost of borrowed money idling before it is invested.

Issue #137: the engine assumed borrowed money deploys the instant it is
borrowed — the year-0 leveraged lump sum (``ctx.lump_sum``, a HELOC margin
draw plus a mortgage cash-out) was always invested in the same simulated
year, so a household that took 3 months to move the money got a projection
byte-identical to one that moved it the same day. The delay cost was silently
modelled as zero (DP#32's founding defect, in miniature).

This module is the ONE spelling of that cost (DP#9). It is deliberately pure
(DP#3): it takes the lump sum, the declared lag, and the two rates and returns
a dollar figure — the **carry cost** of the undeployed advance over the lag.

The physical model: during the lag, the borrowed money sits in a parking
sleeve earning the *parking rate* while the debt it financed accrues at the
*financing rate*. The spread — financing minus parking — is what each month
of waiting actually costs. Over `lag_months` of a year it is:

    carry = lump_sum × (financing_rate − idle_rate) × (lag_months / 12)

Exactly the issue's framing ("a $100,000 advance at a 5% borrowing rate,
idling in a non-interest-bearing account, carries ~$410/month" —
$100,000 × 0.05 / 12 ≈ $417/month, the difference from the issue's $410
being rounding).

A zero lag, or an absent one, is a strict DP#32 no-op (returns 0.0), so the
golden trajectory — which declares no lag — is byte-identical.
"""

from __future__ import annotations

from typing import Optional

MONTHS_PER_YEAR = 12


def deployment_lag_years(lag_months: Optional[int]) -> float:
    """The fraction of a year `lag_months` represents, or 0.0 when absent or
    nonpositive (a household that declares no lag, or an explicit 0, does not
    move — a strict no-op, DP#32)."""
    if not lag_months or lag_months <= 0:
        return 0.0
    return lag_months / MONTHS_PER_YEAR


def lump_financing_rate(
    lump_sum: float,
    margin_available: float,
    heloc_rate: float,
    mortgage_rate: float,
) -> float:
    """The blended borrowing rate of a year-0 lump sum's two legs (#137).

    The lump is funded by exactly two borrowings (issue #850): the HELOC
    margin draw and the mortgage cash-out. `margin_draw_for_lump_sum` is
    already THE ONE rule that says how much of a lump is which (DP#9 — this
    reuses it rather than re-deriving the split, so the financing rate cannot
    disagree with the debt booked on the balance sheet). The blended rate is
    the balance-weighted average of the two legs' rates:

        (margin_draw × heloc_rate + advance × mortgage_rate) / lump_sum

    A pure-HELOC or pure-cash-out lump reduces to that leg's own rate. A
    nonpositive lump has no financing to price and returns 0.0 (DP#32).
    """
    if lump_sum <= 0:
        return 0.0
    from simulation_state import margin_draw_for_lump_sum
    margin_draw = margin_draw_for_lump_sum(lump_sum, margin_available)
    advance = lump_sum - margin_draw
    if margin_draw <= 0:
        return float(mortgage_rate)
    if advance <= 0:
        return float(heloc_rate)
    return ((margin_draw * heloc_rate + advance * mortgage_rate) / lump_sum)


def deployment_carry_cost(
    lump_sum: float,
    lag_months: Optional[int],
    financing_rate: float,
    idle_rate: float = 0.0,
) -> float:
    """The dollar cost of the undeployed period (issue #137).

    Pure function: the spread between the financed debt and the idle parking
    sleeve, applied to the undeployed principal over the lag. 0.0 (DP#32 no-op)
    when nothing was borrowed, no lag was declared, or the declared lag is
    zero — the household moved nothing late, so there is nothing to price.

    `idle_rate` defaults to 0.0 (today's assumption: the money sat in a
    non-interest-bearing chequing account while it waited — exactly the
    issue's round-number headline).
    """
    if lump_sum <= 0:
        return 0.0
    years = deployment_lag_years(lag_months)
    if years <= 0:
        return 0.0
    return lump_sum * (financing_rate - idle_rate) * years