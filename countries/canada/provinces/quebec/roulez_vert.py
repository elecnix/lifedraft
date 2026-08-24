#!/usr/bin/env python3
"""
Roulez vert -- Quebec's rebate for a NEW zero-emission vehicle

Per DP#10 this is a provincial program in its own jurisdiction module, separate
from the federal iZEV program in ``countries/canada/zev_incentive.py``. The two
are independent: a household can receive both, one, or neither, and each answers
its own dated eligibility test. Nothing here reads the federal module.

## The declining schedule is the whole modelling problem (DP#20, DP#28)

Roulez vert does not pay a rate; it pays an amount that steps DOWN on published
dates, and it is legislated to end. A household weighing a purchase across a
year boundary is choosing between two different amounts, so the amount must be
year-versioned and computed from the acquisition date, never from "the current
amount."

The schedule also contains a **suspension**: vehicles registered between
2025-02-01 and 2025-03-31 receive nothing at all, even though the program was
neither finished before that window nor finished after it. A zero inside that
window and a zero after 2026 are different facts and this module reports them as
different sentences (DP#32).

## The 2027 amount is deliberately absent

Quebec has published that the criteria change on 2027-01-01 and that the amount
decreases -- but not what it decreases TO. This module therefore prices nothing
on or after that date and says so. Interpolating the trend (7000 -> 4000 -> 2000
"so presumably 1000") would produce exactly the confident, plausible, unverifiable
number this codebase exists to refuse. When Quebec publishes the figure, add the
rung; until then the refusal IS the correct output (DP#32).

## What this does NOT model

* The vehicle, its price beyond the eligibility cap, its depreciation, or its
  operating cost -- see the same section in ``zev_incentive.py`` and DP#30.
* The USED-vehicle rebate, which is a separate amount under a separate set of
  conditions. Absent here rather than approximated by the new-vehicle amount.
* The home charging station rebate, which is a separate application with its own
  cap and its own dates.
* Plug-in hybrids, hydrogen vehicles and electric motorcycles, which Quebec pays
  on a different (and differently-scheduled) basis. A non-battery-electric
  acquisition is refused by name here rather than silently paid the BEV amount.

References:
    Roulez vert -- amount of financial assistance for a new electric vehicle
    https://www.quebec.ca/en/transports/electric-transportation/financial-assistance-electric-vehicle/new-vehicle/amount-financial-assistance
    Program conditions and the 2025 suspension
    https://www.quebec.ca/transports/transport-electrique/aide-financiere-vehicule-electrique/programme-roulez-vert
"""

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional, Tuple

# MSRP ceiling for a new battery-electric vehicle.
ROULEZ_VERT_MSRP_CAP = 65000.0

# Temporary suspension: nothing registered inside this window is payable.
SUSPENSION_START = date(2025, 2, 1)
SUSPENSION_END = date(2025, 3, 31)

# Last date this module can price. Quebec has announced that the criteria change
# and the amount falls on 2027-01-01 but has not published the new amount, so the
# schedule stops here on purpose rather than being extrapolated.
LAST_PRICED_DATE = date(2026, 12, 31)

# Declining amount schedule for a new battery-electric vehicle, as
# (first date the amount applies, amount). Sorted ascending; the applicable rung
# is the last one whose date is on or before the acquisition date (DP#20).
ROULEZ_VERT_SCHEDULE: Tuple[Tuple[date, float], ...] = (
    (date(2024, 1, 1), 7000.0),
    (date(2025, 1, 1), 4000.0),
    (date(2026, 1, 1), 2000.0),
)

# The schedule above starts here; earlier acquisitions are not priced because
# the pre-2024 amounts are not encoded (this engine projects forward).
SCHEDULE_START = ROULEZ_VERT_SCHEDULE[0][0]


@dataclass(frozen=True)
class RoulezVertResult:
    """Outcome of one Roulez vert eligibility test.

    A zero ``amount`` always arrives with a non-empty ``ineligibility_reasons``
    (DP#32). ``amount_unavailable`` distinguishes the two kinds of zero that
    would otherwise look identical: "you are not eligible" (False) versus "the
    program's amount for this date has not been published, so this engine
    declines to price it" (True).
    """

    amount: float = 0.0
    eligible: bool = False
    amount_unavailable: bool = False
    ineligibility_reasons: List[str] = field(default_factory=list)


def scheduled_amount(acquisition_date: date) -> Optional[float]:
    """The scheduled rebate for a date, or None when no rung is published.

    Pure function (DP#3). Returning None rather than 0.0 is load-bearing: 0.0 is
    a rebate of zero dollars, None is the absence of a published rebate, and the
    caller must be able to tell them apart (DP#32).
    """
    if acquisition_date < SCHEDULE_START or acquisition_date > LAST_PRICED_DATE:
        return None
    applicable = None
    for effective_from, amount in ROULEZ_VERT_SCHEDULE:
        if acquisition_date >= effective_from:
            applicable = amount
    return applicable


def is_suspended(acquisition_date: date) -> bool:
    """Whether the acquisition falls inside the 2025 suspension window.

    Both edges are inclusive: Quebec excluded vehicles registered *between*
    February 1 and March 31, 2025.
    """
    return SUSPENSION_START <= acquisition_date <= SUSPENSION_END


def compute_roulez_vert_rebate(
    acquisition_date: date,
    msrp: float,
    propulsion: str = "battery_electric",
    is_quebec_resident: bool = True,
) -> RoulezVertResult:
    """Quebec Roulez vert rebate payable on one new-vehicle acquisition.

    Pure function (DP#3). Reasons accumulate rather than short-circuiting, so a
    household that fails two tests learns about both.

    Args:
        acquisition_date: the date the vehicle was registered (DP#1).
        msrp: manufacturer's suggested retail price, tested against the cap.
        propulsion: only ``battery_electric`` is priced; anything else is
            refused by name rather than paid the battery-electric amount.
        is_quebec_resident: the program is provincial and pays residents.
    """
    if msrp < 0:
        raise ValueError(f"msrp must be non-negative, got {msrp}")

    reasons: List[str] = []

    if not is_quebec_resident:
        reasons.append("Roulez vert is a Quebec program and the household is not resident")

    if propulsion != "battery_electric":
        reasons.append(
            f"propulsion {propulsion!r} is not priced by this module: Quebec pays "
            "plug-in hybrids, hydrogen vehicles and electric motorcycles on a "
            "different schedule that is not modelled here"
        )

    if msrp >= ROULEZ_VERT_MSRP_CAP:
        reasons.append(
            f"MSRP ${msrp:,.0f} is not below the ${ROULEZ_VERT_MSRP_CAP:,.0f} cap"
        )

    if is_suspended(acquisition_date):
        reasons.append(
            f"the program was suspended from {SUSPENSION_START.isoformat()} to "
            f"{SUSPENSION_END.isoformat()}: a vehicle registered "
            f"{acquisition_date.isoformat()} receives nothing"
        )
        return RoulezVertResult(
            amount=0.0, eligible=False, amount_unavailable=False,
            ineligibility_reasons=reasons,
        )

    amount = scheduled_amount(acquisition_date)
    if amount is None:
        if acquisition_date > LAST_PRICED_DATE:
            reasons.append(
                f"Quebec has announced that the criteria change and the amount falls on "
                f"2027-01-01 but has not published the new amount; this engine declines "
                f"to price a {acquisition_date.isoformat()} acquisition rather than "
                f"extrapolate the schedule"
            )
        else:
            reasons.append(
                f"no Roulez vert amount is encoded for {acquisition_date.isoformat()}: "
                f"the schedule in this module begins {SCHEDULE_START.isoformat()}"
            )
        return RoulezVertResult(
            amount=0.0, eligible=False, amount_unavailable=True,
            ineligibility_reasons=reasons,
        )

    if reasons:
        return RoulezVertResult(
            amount=0.0, eligible=False, amount_unavailable=False,
            ineligibility_reasons=reasons,
        )

    return RoulezVertResult(
        amount=round(amount, 2), eligible=True, amount_unavailable=False,
        ineligibility_reasons=[],
    )
