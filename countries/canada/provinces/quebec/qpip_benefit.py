"""QPIP / RQAP benefits -- Quebec parental insurance (#358).

The engine modelled only the QPIP *premium*. Nothing modelled the maternity,
paternity and parental benefits that replace salary in a Quebec household's
birth years, so a household having a child was mispriced by the whole benefit.

## What the program does

A dated leave on a person (birth or adoption), a chosen plan, and a split of the
shareable parental weeks between the parents. Weekly benefit is the claimant's
average weekly insurable earnings -- capped at that YEAR's maximum insurable
earnings divided by 52 -- times the plan's replacement rate, summed over the
weeks that apply to that parent.

Two plans, from the official Quebec description (quebec.ca, "Quebec Parental
Insurance Plan"):

- **basic**: maternity 18 weeks at 70%, paternity 5 at 70%, parental 32 weeks
  (the first 7 at 70%, the remaining 25 at 55%).
- **special**: maternity 15 at 75%, paternity 3 at 75%, parental 25 at 75%.

Rates, week counts and each year's maximum insurable earnings come from that
official source, NOT from the CFFP scenario tables (which are a worked example,
not the program).

## Three properties that are easy to get wrong

1. The benefits are **taxable**, so they are income.
2. They add **nothing** to RRSP earned income.
3. They carry **no QPP, QPIP or EI premium** -- a benefit is not insurable
   earnings.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Tuple

# Official week counts and rates (quebec.ca, QPIP).
# Each tuple is (weeks, replacement rate).
BASIC_PLAN = {
    "maternity": ((18, 0.70),),
    "paternity": ((5, 0.70),),
    # Parental is progressive under the basic plan: 7 weeks at 70%, then 25 at 55%.
    "parental": ((7, 0.70), (25, 0.55)),
}
SPECIAL_PLAN = {
    "maternity": ((15, 0.75),),
    "paternity": ((3, 0.75),),
    "parental": ((25, 0.75),),
}
PLANS = {"basic": BASIC_PLAN, "special": SPECIAL_PLAN}

# Maximum insurable earnings by year (QPIP). Year-versioned (DP#20); a year with
# no value refuses rather than borrowing another year's ceiling (DP#32).
QPIP_MAX_INSURABLE_EARNINGS = {
    2024: 94_000.0,
    2025: 98_000.0,
    2026: 100_000.0,
}

WEEKS_PER_YEAR = 52.0


@dataclass
class QPIPLeave:
    """A dated leave declared on one person."""

    person_id: str
    event_date: Optional[date]     # birth or adoption
    plan: str
    weekly_insurable_earnings: float
    is_birthing_parent: bool = False
    # How many of the SHAREABLE parental weeks this person takes. The two
    # parents' shares must not exceed the plan's parental total.
    parental_weeks_taken: float = 0.0

    @property
    def year(self) -> Optional[int]:
        return self.event_date.year if self.event_date else None


@dataclass
class QPIPBenefit:
    """The benefit for one parent's leave."""

    maternity: float = 0.0
    paternity: float = 0.0
    parental: float = 0.0

    @property
    def total(self) -> float:
        return self.maternity + self.paternity + self.parental

    @property
    def taxable(self) -> bool:
        """QPIP benefits are taxable income."""
        return True


def _weekly_cap(year: int) -> float:
    mie = QPIP_MAX_INSURABLE_EARNINGS.get(year)
    if mie is None:
        raise ValueError(
            f"no QPIP maximum insurable earnings for {year}. The ceiling is "
            f"year-versioned (DP#20); borrowing another year's would be a "
            f"plausible wrong number, so this refuses (DP#32)."
        )
    return mie / WEEKS_PER_YEAR


def _capped_weekly(leave: QPIPLeave, year: int) -> float:
    cap = _weekly_cap(year)
    return min(leave.weekly_insurable_earnings, cap)


def qpip_benefit(leave: QPIPLeave) -> QPIPBenefit:
    """The QPIP benefit for one parent's leave, in dollars."""
    year = leave.year
    if year is None:
        raise ValueError(
            "a QPIP leave must carry the birth or adoption date; the benefit "
            "depends on that year's maximum insurable earnings (DP#32)."
        )
    if leave.plan not in PLANS:
        raise ValueError(
            f"unknown QPIP plan {leave.plan!r}; expected one of "
            f"{sorted(PLANS)} (DP#32)."
        )
    if leave.weekly_insurable_earnings < 0:
        raise ValueError(
            f"weekly insurable earnings of {leave.weekly_insurable_earnings!r} "
            f"are negative (DP#32)."
        )

    plan = PLANS[leave.plan]
    base = _capped_weekly(leave, year)
    out = QPIPBenefit()

    if leave.is_birthing_parent:
        out.maternity = sum(w * r * base for w, r in plan["maternity"])
    else:
        out.paternity = sum(w * r * base for w, r in plan["paternity"])

    taken = min(leave.parental_weeks_taken, sum(w for w, _ in plan["parental"]))
    remaining = taken
    parental = 0.0
    for weeks, rate in plan["parental"]:
        if remaining <= 0:
            break
        use = min(weeks, remaining)
        parental += use * rate * base
        remaining -= use
    out.parental = parental
    return out
