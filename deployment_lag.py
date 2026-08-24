"""Deployment lag & staggered deployment — the cost of borrowed money idling
before it is invested.

Issue #137: the engine assumed borrowed money deploys the instant it is
borrowed — the year-0 leveraged lump sum (``ctx.lump_sum``, a HELOC margin
draw plus a mortgage cash-out) was always invested in the same simulated
year, so a household that took 3 months to move the money got a projection
byte-identical to one that moved it the same day. The delay cost was silently
modelled as zero (DP#32's founding defect, in miniature).

Issue #74 extends the same pricing to a declared deployment SCHEDULE: spread
the lump over N equal annual tranches (deploy-over-1/2/3/4-years) instead of
a single fixed delay. A schedule is a SPREAD (#74), not a delay (#137): each
tranche k parks for exactly k years before deploying, so the household both
pays #137's parking carry on every undeployed dollar AND gives up k years of
compounding on tranche k.

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

## The schedule's cost is priced at the year-0 seam (#74)

The schedule keeps #137's single seam: the whole lump is borrowed at year 0
(the debt booking in ``initial_state_for_run`` does not move), and the
declared timing prices a **year-0-equivalent reduction** of the deployed
principal. Under the deterministic ranker's own flat return R that reduction
is EXACT in terminal wealth, tranche by tranche:

    tranche t_k = L / N deploys at year k. Against deploying everything at
    year 0 it costs, discounted to year 0:
      - forgone compounding:  t·(1 − (1+R)^−k)
      - parking carry (#137): t·(f − i)·k, worth t·(f − i)·k·(1+R)^−k at year 0
    X = Σ_k [ t·(1 − (1+R)^−k) + t·(f − i)·k·(1+R)^−k ]

Reducing the year-0 lump by X reproduces the schedule's terminal wealth
exactly when returns are flat — which is precisely the assumption the main
optimizer ranks under (issue #74 finding 2), so the ranking is internally
consistent, not approximated. What the deterministic price deliberately does
NOT credit is the stochastic half of DCA — variance/sequence-of-returns
mitigation needs part (b) of #74 (ranking on a return DISTRIBUTION), which
stays open as a follow-up; under a flat return staggering can only ever cost,
and this module prices that cost honestly instead of inventing an offsetting
benefit it cannot see.

Account-routing caveat, stated rather than hidden: the reduced lump still
deploys through ONE year-0 ``fill_room`` call, so registered-vs-non-reg
routing follows the lump-year-0 optimum rather than each tranche competing
for its own year's freshly accrued room. For the households this dimension
targets (a large borrowed cash-out against limited registered room — the
lump lands predominantly non-reg either way) the routing difference is nil;
a future increment that deploys tranches through per-year ``fill_room``
calls would pick up the routing effect.
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


def validate_deployment_dimensions(
    lag_months: Optional[int],
    schedule_years: Optional[int],
) -> None:
    """Refuse an ambiguous declaration (issue #74): a household may price a
    fixed LAG (#137) or a SPREAD (#74), never both at once — the two timings
    describe different physical movements of the same money, and composing
    them would silently double-price the parking carry. Absent/zero values
    are the no-ops and never conflict. Raises ValueError loudly (DP#32)
    rather than picking one interpretation for the caller.
    """
    if (lag_months or 0) > 0 and (schedule_years or 1) > 1:
        raise ValueError(
            f"Ambiguous deployment timing: deployment_lag_months={lag_months} "
            f"and deployment_schedule_years={schedule_years} are both active. "
            "Declare either a fixed lag (#137) or a staggered schedule (#74), "
            "not both — the engine refuses to guess which one you mean."
        )


def year0_deployment_cost(
    lump_sum: float,
    lag_months: Optional[int],
    schedule_years: Optional[int],
    financing_rate: float,
    idle_rate: float = 0.0,
    investment_return: float = 0.0,
) -> float:
    """THE one pricing of the year-0 borrowed lump's deployment timing
    (DP#9): #137's fixed lag, #74's staggered schedule, or — absent both —
    a strict 0.0. Every fold that deploys the year-0 lump (the yearly
    ``simulate_year`` path and the monthly pre-step) prices it through here,
    so the two engines cannot disagree on what delay costs (#258).
    """
    validate_deployment_dimensions(lag_months, schedule_years)
    if (schedule_years or 1) > 1:
        return deployment_schedule_cost(
            lump_sum, schedule_years, financing_rate, idle_rate,
            investment_return,
        )
    return deployment_carry_cost(lump_sum, lag_months, financing_rate, idle_rate)


def deployment_schedule_cost(
    lump_sum: float,
    schedule_years: Optional[int],
    financing_rate: float,
    idle_rate: float = 0.0,
    investment_return: float = 0.0,
) -> float:
    """The year-0-equivalent dollar cost of spreading `lump_sum` over N equal
    ANNUAL tranches deployed at years 0..N-1 (issue #74).

    Pure function (DP#3). Each tranche t = L/N parks for k = 0..N-1 years
    before deploying; against deploying the whole lump at year 0 it costs,
    discounted back to year 0 at the flat return R the ranker prices with:

        X = Σ_k [ t·(1 − (1+R)^−k)      forgone compounding on tranche k
                + t·(f − i)·k·(1+R)^−k ]  #137's parking carry on tranche k

    Reducing the year-0 deployed principal by X reproduces the schedule's
    terminal wealth EXACTLY under a flat R (see the module docstring) — and
    a flat R is exactly what the deterministic ranking assumes, so the cost
    the optimizer compares schedules by is the model's own truth, not an
    approximation of it.

    0.0 (DP#32 no-op) when nothing was borrowed or the schedule is absent,
    0/1 — a household deploying everything in year 0 gives up nothing.
    A return ≤ −100% makes the discount factor undefined; that is not priced
    as a cost, it raises (DP#32 — refuse loudly rather than fabricate).
    """
    if lump_sum <= 0:
        return 0.0
    n = schedule_years or 1
    if n <= 1:
        return 0.0
    growth = 1.0 + investment_return
    if growth <= 0:
        raise ValueError(
            f"deployment_schedule_cost needs a return > -100% to discount "
            f"tranches; got investment_return={investment_return}."
        )
    spread = financing_rate - idle_rate
    tranche = lump_sum / n
    cost = 0.0
    for k in range(n):
        discount = growth ** (-k)
        cost += tranche * (1.0 - discount)
        cost += tranche * spread * k * discount
    return cost
