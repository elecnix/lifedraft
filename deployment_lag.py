"""Deployment-lag carry cost: the opportunity cost of borrowed money sitting idle.

The engine's default assumption is that borrowed money is deployed the instant
it is borrowed -- a year-0 refinance cash-out lump is invested in that same
simulated year, so a household that takes months to actually move the money
gets a projection byte-identical to one that moved it same-day. The delay's
cost is silently modelled as $0.

A household that DECLARES ``decisions.mortgage.refinance_options[]
.deployment_lag_months`` opts into pricing that cost. The economically correct
lag cost is the **opportunity cost** of the idle window: the lump could have
been invested earning the portfolio's year-0 return, but instead it sits in
cash earning the declared ``parking_rate``. The carry is the foregone
investment return minus the parking earnings::

    carry = lump * (investment_return - parking_rate) * (months / 12)

This is NOT the spread between the debt's interest rate and the parking rate.
The borrowed lump is already inside ``mortgage_balance`` and the mortgage
accrues interest on it regardless of when the money is deployed, so charging
``(borrowing_rate - parking_rate)`` would double-count the debt cost -- the
mortgage already pays that interest. The only thing the lag changes is what
the lump EARNS during the window (parking_rate instead of the investment
return it would have earned deployed), so the carry is the foregone
investment return net of parking earnings.

This module owns the pure arithmetic (DP#3: same inputs -> same output, no
hidden state, no globals). The engine applies the returned carry as a
year-0-equivalent reduction of the deployable principal (see
``FamilySimulation``'s year-0 ``fill_room`` call), so it flows into every
objective the way other one-time year-0 costs do. Absence (``lag_months == 0``,
the default) is a hard zero -- no lag declared, year-0 deployment exactly as
today (DP#32: zero is a value, not a fallback; the no-lag path is byte-for-byte
the pre-feature behaviour). A declared ``deployment_lag_months: 0`` round-trips
to absent by design -- 0 is the canonical no-lag state, so a load->modify->save
cycle emits no key for it (the same absence-safe convention
``refinance_amortization_years`` uses), and the byte-identical no-lag path is
preserved.

Validation is loud (DP#32): a negative month count raises rather than producing
a plausible sign-flipped carry, and a net rate of return at or below -100%
(parking earning 100%+ more than the investment return -- an absurd scenario
where the "cost" is a huge fabricated gain) raises rather than silently
inventing money. A declared zero-lag and a zero lump are hard zeros, never
coerced.
"""

from __future__ import annotations


def deployment_lag_cost(
    lump: float,
    months: int,
    investment_return: float,
    parking_rate: float,
) -> float:
    """The year-0-equivalent opportunity cost of a deployment lag.

    During the lag window the borrowed ``lump`` sits in cash earning
    ``parking_rate`` instead of being invested at ``investment_return`` (the
    portfolio's year-0 return -- the same return model the deployed lump would
    compound at). The foregone return net of parking earnings is
    ``lump * (investment_return - parking_rate) * (months / 12)``. The engine
    applies the returned value as a reduction of the deployable principal at
    year 0, so the cost flows into every objective.

    This is the opportunity cost of the idle window, NOT the debt's interest
    spread: the borrowed lump is already on the mortgage and accrues interest
    there regardless of deployment timing, so the borrowing rate is not an
    input here (charging it would double-count the debt cost the mortgage
    already pays).

    Pure function (DP#3): no fold state, no globals, same inputs always yield
    the same output. Loud validation (DP#32):

    * ``months < 0`` raises ``ValueError`` -- a negative lag is not a "lag in the
      other direction," it is a bad input, and a plausible sign-flipped carry
      would be a confident wrong number.
    * ``(investment_return - parking_rate) <= -1.0`` raises ``ValueError`` -- a
      net rate at or below -100% (parking earns 100%+ more than the investment
      return) is an absurd scenario whose "cost" would be a large fabricated
      gain; refusing it stops the engine from silently inventing money.
    * ``months == 0`` (no lag declared) and ``lump == 0`` are hard zeros -- the
      carry IS zero, never a default that masks a missing input. A household
      that declares no lag is byte-for-byte the pre-feature behaviour.
    * ``lump < 0`` raises ``ValueError`` -- a negative borrowed lump is not a
      carry to price, it is a bad input.

    A NEGATIVE carry (``parking_rate > investment_return``) is a real,
    representable scenario the household may declare (idle money earning more
    than the portfolio's return): it is returned as-is, not floored at zero.
    The engine CAPS the deployable principal at the borrowed lump (a negative
    carry does not let the household invest MORE than it borrowed -- that
    would be solvency inflation); the parking-earnings excess is not routed
    into invested principal. See ``FamilySimulation``'s year-0 deployment.
    The ``<= -1.0`` guard is the absurd extreme of the negative direction, not
    the normal case.

    Args:
        lump: the borrowed lump subject to the lag (>= 0). For a refinance
            cash-out this is the cash-out advance; the margin draw (a separate
            facility) is deployed same-day and carries no lag.
        months: the declared lag in whole months (>= 0). 0 means no lag
            declared; the carry is a hard zero (and round-trips to absent).
        investment_return: the annual rate the lump would have earned had it
            been deployed immediately -- the portfolio's year-0 return (the same
            return model the deployed lump compounds at). NOT the borrowing
            rate: the debt's interest accrues on the mortgage regardless of the
            lag, so it is not an input to the opportunity cost.
        parking_rate: the annual rate the idle lump earns while waiting
            (declared ``parking_rate``; default 0 -- idle cash earning
            nothing).

    Returns:
        The carry cost. ``0.0`` when no lag is declared (``months == 0``) or
        there is no lump to lag (``lump == 0``) -- the no-op path that keeps the
        golden trajectory byte-identical (DP#32).
    """
    if months < 0:
        raise ValueError(
            f"deployment_lag_cost: months={months} is negative. A deployment "
            f"lag is a non-negative whole-month window; a negative value is a "
            f"bad input, not a lag in the other direction (DP#32)."
        )
    if lump < 0:
        raise ValueError(
            f"deployment_lag_cost: lump={lump} is negative. The borrowed lump "
            f"subject to a deployment lag is a non-negative amount; a negative "
            f"lump is a bad input, not a carry to price (DP#32)."
        )
    # Hard zeros: no lag declared, or no lump to lag. Both are the no-op path
    # (byte-for-byte the pre-feature behaviour), never a default that masks a
    # missing input (DP#32: zero is a value, not a fallback -- but a zero lag
    # genuinely carries no cost, so 0.0 IS the correct value here). A declared
    # deployment_lag_months: 0 round-trips to absent by design (0 is the
    # canonical no-lag state).
    if months == 0 or lump == 0:
        return 0.0
    rate_spread = investment_return - parking_rate
    if rate_spread <= -1.0:
        raise ValueError(
            f"deployment_lag_cost: the net rate (investment_return - parking_rate "
            f"= {rate_spread}) is at or below -100%. Parking earning 100%+ more "
            f"than the investment return is an absurd scenario whose 'cost' would "
            f"be a large fabricated gain; refusing rather than silently "
            f"inventing money (DP#32). investment_return={investment_return}, "
            f"parking_rate={parking_rate}."
        )
    return lump * rate_spread * (months / 12.0)