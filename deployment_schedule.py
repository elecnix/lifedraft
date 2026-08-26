"""Staggered-deployment schedule cost: the opportunity cost of dripping a
borrowed lump into the market over N years instead of deploying it all at
year 0.

The engine's default (and the ``deployment_lag`` layer's short-months-window
carry) assumes the cash-out advance is deployed as a year-0 lump.  A household
that wants to DCA / stagger the advance over several years -- parking the
undeployed portion and dripping it in equal annual tranches -- could not ask
whether that changes the outcome: there was no time-spread decision variable.

Declaring ``decisions.mortgage.refinance_options[]
.deployment_schedule_years`` opts into pricing that.  During the stagger
window the undeployed tranches sit at the declared ``parking_rate`` instead of
being invested at the portfolio's year-0 return.  The opportunity cost is the
foregone investment return net of parking earnings, summed linearly over the
N-1 years of staggering::

    cost = lump * (return_rate - parking_rate) * (years - 1) / 2

The ``(years - 1) / 2`` factor is the average number of years a tranche waits
undeployed: the first tranche (year 0) waits 0 years, the last (year N-1)
waits N-1 years, and the average is (N-1)/2.  The per-year undeployed balance
declines linearly from (N-1)/N * lump at year 0 to 0 at year N-1, and the sum
of those per-year balances times the spread simplifies to the closed form
above (verified against explicit per-year summation).

This is the SAME economic concept as the deployment-lag carry
(``deployment_lag.deployment_lag_cost``) -- the opportunity cost of delayed
deployment -- but at a longer timescale (years, not months) and with a
linear-decline undeployed balance rather than a flat one.  The borrowed lump
is already inside ``mortgage_balance`` and the mortgage accrues interest on
it regardless of deployment timing, so the borrowing rate is NOT an input
(the debt cost the mortgage already pays is not double-counted).  The cost is
applied as a year-0-equivalent reduction of the deployable principal (the
SAME seam ``deployment_lag_cost`` and ``transaction_cost_year0`` use), so it
flows into every objective the way other one-time year-0 costs do.

This is a LINEAR (non-compounded) estimate: the true compounded opportunity
cost over the window is slightly larger (the foregone return itself would have
compounded).  The linear estimate is consistent with the deployment-lag
carry's own linear model and is applied as a single year-0 reduction (not a
per-year tranche deployment), so it understates the true cost when the return
exceeds parking -- the same shape the deployment-lag carry carries, and the
honest trade-off for reusing the existing one-time-year-0-cost seam rather
than threading per-year tranche deployment through the fold's state.

A deployment SCHEDULE (years) and a deployment LAG (months) are rival timings
of the same borrowed money: declaring both on the same refinance option (or
having both carried across options) is refused loudly at the contract mapping
boundary (``contract_decisions.map_mortgage_decisions``), because both costs
would fire and double-count the same opportunity cost (DP#32/DP#5).  Absence
of either never conflicts.

This module owns the pure arithmetic (DP#3: same inputs -> same output, no
hidden state, no globals).  Absence (``years <= 1``, the default) is a hard
zero -- 1 tranche = today's year-0 lump behaviour, byte-for-byte the
pre-feature path (DP#32).  A declared ``deployment_schedule_years: 0`` or
``: 1`` round-trips to absent by design (0/1 is the canonical no-schedule
state).
"""

from __future__ import annotations


def schedule_cost(
    lump: float,
    years: int,
    parking_rate: float,
    return_rate: float,
) -> float:
    """The year-0-equivalent opportunity cost of staggering a lump over
    ``years`` equal annual tranches instead of deploying it all at year 0.

    During the stagger window the undeployed tranches sit at ``parking_rate``
    instead of being invested at ``return_rate`` (the portfolio's year-0
    return -- the same return model the deployed lump compounds at).  The
    foregone return net of parking earnings, summed linearly over the window,
    is::

        lump * (return_rate - parking_rate) * (years - 1) / 2

    The ``(years - 1) / 2`` factor is the average wait of a tranche: 0 years
    for the first (deployed at year 0), N-1 years for the last (deployed at
    year N-1), average (N-1)/2.  Verified against explicit per-year summation.

    This is the opportunity cost of the stagger, NOT the debt's interest
    spread: the borrowed lump is already on the mortgage and accrues interest
    there regardless of deployment timing, so the borrowing rate is not an
    input here (charging it would double-count the debt cost the mortgage
    already pays) -- the same reasoning ``deployment_lag_cost`` uses.

    Pure function (DP#3): no fold state, no globals, same inputs always yield
    the same output.  Loud validation (DP#32):

    * ``years < 0`` raises ``ValueError`` -- a negative schedule is a bad
      input, not a "schedule in the other direction."
    * ``(return_rate - parking_rate) <= -1.0`` raises ``ValueError`` -- a
      net rate at or below -100% (parking earns 100%+ more than the investment
      return) is an absurd scenario whose "cost" would be a large fabricated
      gain; refusing it stops the engine from silently inventing money. The
      SAME threshold the sibling ``deployment_lag_cost`` uses.
    * ``lump < 0`` raises ``ValueError`` -- a negative borrowed lump is a bad
      input.
    * ``years <= 1`` (no schedule declared, or a single tranche = immediate
      deployment) and ``lump == 0`` are hard zeros -- the cost IS zero, never
      a default that masks a missing input.  A household that declares no
      schedule is byte-for-byte the pre-feature behaviour.

    A NEGATIVE cost (``parking_rate > return_rate``) is a real, representable
    scenario (idle money earning more than the portfolio's return): it is
    returned as-is, not floored at zero.  The engine CAPS the deployable
    principal at the borrowed lump (a negative cost does not let the
    household invest MORE than it borrowed -- that would be solvency
    inflation); the parking-earnings excess is not routed into invested
    principal (the same cap ``deployment_lag_cost`` applies to a negative
    carry).

    Args:
        lump: the borrowed lump subject to the schedule (>= 0).  For a
            refinance cash-out this is the cash-out advance; the margin draw
            (a separate facility) is deployed same-day and carries no
            schedule.
        years: the number of equal annual tranches (>= 0).  0 or 1 means no
            staggering (the whole lump deploys at year 0 -- today's
            behaviour); the cost is a hard zero.  >= 2 staggers the lump over
            that many years.
        parking_rate: the annual rate the undeployed tranches earn while
            waiting (declared ``parking_rate``; default 0 -- idle cash
            earning nothing).
        return_rate: the annual rate the lump would have earned had it been
            deployed immediately -- the portfolio's year-0 return.  The net
            spread (return_rate - parking_rate) at or below -100% raises
            (DP#32).  NOT the borrowing rate.

    Returns:
        The schedule cost.  ``0.0`` when no schedule is declared (``years
        <= 1``) or there is no lump to schedule (``lump == 0``) -- the no-op
        path that keeps the golden trajectory byte-identical (DP#32).
    """
    if years < 0:
        raise ValueError(
            f"schedule_cost: years={years} is negative. A deployment schedule "
            f"is a non-negative whole-year count; a negative value is a bad "
            f"input, not a schedule in the other direction (DP#32)."
        )
    if lump < 0:
        raise ValueError(
            f"schedule_cost: lump={lump} is negative. The borrowed lump "
            f"subject to a deployment schedule is a non-negative amount; a "
            f"negative lump is a bad input, not a cost to price (DP#32)."
        )
    # Hard zeros: no schedule declared (years <= 1 = single tranche = today's
    # year-0 lump), or no lump to schedule. Both are the no-op path
    # (byte-for-byte the pre-feature behaviour), never a default that masks a
    # missing input (DP#32). A declared deployment_schedule_years: 0 or : 1
    # round-trips to absent by design (0/1 is the canonical no-schedule state).
    # These short-circuit BEFORE the rate_spread <= -1.0 check so a household
    # that declares no schedule is byte-identical even under an extreme
    # negative spread (a stress-test crash scenario with no schedule declared
    # must not raise here -- the schedule is inert, and the spread validation
    # only applies when a schedule is actually being priced).
    if years <= 1 or lump == 0:
        return 0.0
    rate_spread = return_rate - parking_rate
    if rate_spread <= -1.0:
        raise ValueError(
            f"schedule_cost: the net rate (return_rate - parking_rate "
            f"= {rate_spread}) is at or below -100%. Parking earning 100%+ "
            f"more than the investment return is an absurd scenario whose "
            f"'cost' would be a large fabricated gain; refusing rather than "
            f"silently inventing money (DP#32). return_rate={return_rate}, "
            f"parking_rate={parking_rate}."
        )
    return lump * rate_spread * (years - 1) / 2.0