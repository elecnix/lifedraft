#!/usr/bin/env python3
"""
Federal iZEV (Incentives for Zero-Emission Vehicles) Program

Per DP#10 this module owns ONE government program: Transport Canada's iZEV
point-of-sale incentive. Quebec's provincial Roulez vert rebate is a DIFFERENT
program in a DIFFERENT jurisdiction and lives in
``countries/canada/provinces/quebec/roulez_vert.py``.

## Why a purchase event is in scope at all (DP#30)

DP#30 puts vehicle valuation and "should I buy this car" squarely OUT of scope,
and this module does not answer that question. It answers the one question a
tax/benefit engine is entitled to answer about a vehicle: **a dated government
program pays the household a defined amount, if and only if a dated eligibility
test passes.** That is the same warrant under which ``first_home_purchases``
(#704) models a home purchase: not because the engine values houses, but because
the FHSA and the HBP attach to the event. Here the iZEV incentive attaches.

The engine therefore models the INCENTIVE, not the vehicle. See "What this does
NOT model" below -- that boundary is the whole reason this module is allowed to
exist.

## The program is CLOSED, and that is the point (DP#28)

iZEV is not a live program with a rate that happens to be zero today. It opened
2019-05-01, exhausted its funds, was paused 2025-01-12, and closed
**2025-03-31**. DP#28 requires that programs enter *and exit* on a schedule, and
a household modelling a 2026 purchase must be told the incentive is $0 **because
the program ended**, not handed a silent zero indistinguishable from "you were
not eligible" or "we did not implement this."

That distinction is the reason ``ZEVIncentiveResult`` carries
``ineligibility_reasons`` rather than just an amount (DP#32): a zero this module
returns always arrives with the sentence that explains it.

## Amounts (year-versioned per DP#20)

While the program was open the incentive did not vary by year:

  * battery-electric, hydrogen fuel-cell, or long-range PHEV -> $5,000
  * short-range PHEV (electric range < 50 km)                -> $2,500

A LEASE receives a prorated share of the purchase incentive, by term: a 48-month
or longer lease receives the full amount, and shorter terms receive the
proportion of 48 months they run. Transport Canada published this as a table of
four rungs (12/24/36/48 months); ``_lease_fraction`` reproduces the table's
proportionality rule rather than the table, so an off-table term (a 30-month
lease) is priced instead of rejected.

## MSRP caps

Eligibility is capped on the MSRP of the vehicle's BASE trim, with a higher
ceiling allowed for higher trims of an otherwise-eligible model:

  * cars                          -> base trim < $55,000, trims up to $65,000
  * larger vehicles (SUV/van/truck) -> base trim < $60,000, trims up to $70,000

Both numbers are required: a household that supplies only the trim price it paid
cannot have its base trim inferred, and this module refuses rather than guessing
(DP#32).

## What this does NOT model

* **The vehicle.** Not as an asset, not as a depreciating balance, not as a
  liability if financed. The household's vehicle does not appear on the balance
  sheet, and nothing here ranks buying against keeping.
* **The purchase outflow.** Only the incentive INFLOW is booked. A household
  that wants the cash cost of the vehicle in its projection expresses it through
  ``household_budget`` or ``cash_flows`` like any other spending -- this module
  does not invent a second channel for it.
* **Operating cost.** Fuel, electricity, insurance, and maintenance are not
  modelled and are not this module's business.
* **Sales tax.** The incentive is applied to the purchase price before tax in
  some provinces and after in others; the engine does not model vehicle sales
  tax at all, so it does not model that interaction either.

References:
    Transport Canada, Incentives for Zero-Emission Vehicles (iZEV) Program
    https://tc.canada.ca/en/road-transportation/innovative-technologies/electric-vehicles/incentives-zero-emission-vehicles-izev
    Pause announcement, 2025-01-10 (effective 2025-01-12)
    https://www.canada.ca/en/transport-canada/news/2025/01/pause-of-the-incentives-for-zero-emission-vehicles-program.html
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

# Program window (DP#28: a program enters AND exits on a schedule).
IZEV_PROGRAM_START = date(2019, 5, 1)
IZEV_PROGRAM_PAUSED = date(2025, 1, 12)
IZEV_PROGRAM_END = date(2025, 3, 31)

# Incentive amounts while the program was open.
IZEV_FULL_INCENTIVE = 5000.0
IZEV_SHORT_RANGE_PHEV_INCENTIVE = 2500.0

# A PHEV at or above this electric-only range receives the full incentive.
PHEV_LONG_RANGE_MIN_KM = 50

# Lease proration: a term of this many months or longer receives the full amount.
IZEV_FULL_LEASE_TERM_MONTHS = 48

# MSRP caps by vehicle class: (base trim ceiling, higher trim ceiling).
IZEV_MSRP_CAPS = {
    "car": (55000.0, 65000.0),
    "larger": (60000.0, 70000.0),
}

# Propulsion types that receive the full incentive.
FULL_INCENTIVE_PROPULSIONS = frozenset({"battery_electric", "hydrogen", "phev"})


@dataclass(frozen=True)
class ZEVPurchase:
    """A dated acquisition of a zero-emission vehicle.

    DP#1: stores the actual acquisition date, never a year or an age.
    DP#7: describes the MECHANISM (propulsion, class, range) that the statute
    tests, never a make or model -- no field here can name a brand.
    """

    acquisition_date: date
    # MSRP of the vehicle's BASE trim, and of the trim actually acquired.
    base_msrp: float
    trim_msrp: float
    # Statutory vehicle classes, not marketing segments: 'car' or 'larger'.
    vehicle_class: str = "car"
    # 'battery_electric' | 'hydrogen' | 'phev'
    propulsion: str = "battery_electric"
    # Electric-only range in km. Required for a PHEV (it selects the amount);
    # ignored for battery-electric and hydrogen.
    electric_range_km: Optional[float] = None
    is_lease: bool = False
    lease_term_months: Optional[int] = None

    def __post_init__(self) -> None:
        # DP#32: absence and nonsense fail loudly here, at the edge, rather than
        # flowing into an amount the household cannot tell apart from a real one.
        if self.base_msrp < 0:
            raise ValueError(f"base_msrp must be non-negative, got {self.base_msrp}")
        if self.trim_msrp < 0:
            raise ValueError(f"trim_msrp must be non-negative, got {self.trim_msrp}")
        if self.trim_msrp < self.base_msrp:
            raise ValueError(
                f"trim_msrp {self.trim_msrp} is below base_msrp {self.base_msrp}: "
                "a trim cannot cost less than the base trim it is a trim of"
            )
        if self.vehicle_class not in IZEV_MSRP_CAPS:
            raise ValueError(
                f"unknown vehicle_class {self.vehicle_class!r}; "
                f"expected one of {sorted(IZEV_MSRP_CAPS)}"
            )
        if self.propulsion not in FULL_INCENTIVE_PROPULSIONS:
            raise ValueError(
                f"unknown propulsion {self.propulsion!r}; "
                f"expected one of {sorted(FULL_INCENTIVE_PROPULSIONS)}"
            )
        if self.propulsion == "phev" and self.electric_range_km is None:
            raise ValueError(
                "electric_range_km is required for a PHEV: it selects between the "
                f"${IZEV_FULL_INCENTIVE:.0f} and ${IZEV_SHORT_RANGE_PHEV_INCENTIVE:.0f} "
                "incentive and cannot be inferred"
            )
        if self.is_lease and self.lease_term_months is None:
            raise ValueError(
                "lease_term_months is required for a lease: it prorates the incentive "
                "and cannot be inferred"
            )
        if self.lease_term_months is not None and self.lease_term_months <= 0:
            raise ValueError(
                f"lease_term_months must be positive, got {self.lease_term_months}"
            )


@dataclass(frozen=True)
class ZEVIncentiveResult:
    """Outcome of one iZEV eligibility test.

    ``amount`` of 0.0 is only ever returned alongside a non-empty
    ``ineligibility_reasons`` (DP#32): this module never returns a bare zero.
    """

    amount: float = 0.0
    eligible: bool = False
    full_amount_before_lease_proration: float = 0.0
    lease_fraction: float = 1.0
    ineligibility_reasons: List[str] = field(default_factory=list)


def izev_program_status(acquisition_date: date) -> Optional[str]:
    """Why the program cannot pay on this date, or None if it was open.

    DP#28: the window is computed from the date, and both edges are real. The
    pause and the close are reported as DIFFERENT sentences because they are
    different facts: a vehicle ordered during the pause with a pre-approved
    application was still paid, whereas nothing after the close was.
    """
    if acquisition_date < IZEV_PROGRAM_START:
        return (
            f"iZEV did not exist on {acquisition_date.isoformat()}: "
            f"the program opened {IZEV_PROGRAM_START.isoformat()}"
        )
    if acquisition_date > IZEV_PROGRAM_END:
        return (
            f"iZEV closed {IZEV_PROGRAM_END.isoformat()}: "
            f"no incentive is payable on a {acquisition_date.isoformat()} acquisition"
        )
    if acquisition_date >= IZEV_PROGRAM_PAUSED:
        return (
            f"iZEV was paused {IZEV_PROGRAM_PAUSED.isoformat()} when its funds were "
            f"fully committed; only applications pre-approved before the pause were "
            f"paid through the {IZEV_PROGRAM_END.isoformat()} close"
        )
    return None


def izev_base_amount(purchase: ZEVPurchase) -> float:
    """The un-prorated incentive the propulsion type earns.

    Pure function (DP#3). A PHEV is split on its electric-only range; battery
    electric and hydrogen always earn the full amount.
    """
    if purchase.propulsion != "phev":
        return IZEV_FULL_INCENTIVE
    # A PHEV's range is guaranteed non-None by ZEVPurchase.__post_init__.
    if purchase.electric_range_km >= PHEV_LONG_RANGE_MIN_KM:
        return IZEV_FULL_INCENTIVE
    return IZEV_SHORT_RANGE_PHEV_INCENTIVE


def _lease_fraction(lease_term_months: int) -> float:
    """Share of the purchase incentive a lease of this term receives.

    Transport Canada published four rungs (12/24/36/48 months) of a single
    proportional rule. Reproducing the RULE rather than the table means a
    30-month lease is priced at 30/48 instead of falling off the table into a
    zero that would look exactly like ineligibility (DP#32, DP#33).
    """
    if lease_term_months >= IZEV_FULL_LEASE_TERM_MONTHS:
        return 1.0
    return lease_term_months / IZEV_FULL_LEASE_TERM_MONTHS


def _msrp_reasons(purchase: ZEVPurchase) -> List[str]:
    """Reasons the MSRP caps disqualify this vehicle.

    Both edges are tested: the base trim against the base ceiling, and the trim
    actually acquired against the higher-trim ceiling. A model whose base trim
    is over the ceiling is ineligible in every trim.
    """
    base_cap, trim_cap = IZEV_MSRP_CAPS[purchase.vehicle_class]
    reasons = []
    if purchase.base_msrp >= base_cap:
        reasons.append(
            f"base trim MSRP ${purchase.base_msrp:,.0f} is not below the "
            f"${base_cap:,.0f} cap for class {purchase.vehicle_class!r}"
        )
    if purchase.trim_msrp > trim_cap:
        reasons.append(
            f"acquired trim MSRP ${purchase.trim_msrp:,.0f} exceeds the "
            f"${trim_cap:,.0f} higher-trim cap for class {purchase.vehicle_class!r}"
        )
    return reasons


def compute_izev_incentive(purchase: ZEVPurchase) -> ZEVIncentiveResult:
    """Federal iZEV incentive payable on one acquisition.

    Pure function (DP#3): same inputs always produce the same output.

    Every zero carries its reason (DP#32). Reasons accumulate rather than
    short-circuiting, so a household that is ineligible on two counts is told
    about both and does not fix one only to be refused again.
    """
    reasons: List[str] = []

    closed = izev_program_status(purchase.acquisition_date)
    if closed is not None:
        reasons.append(closed)
    reasons.extend(_msrp_reasons(purchase))

    if reasons:
        return ZEVIncentiveResult(
            amount=0.0,
            eligible=False,
            full_amount_before_lease_proration=0.0,
            lease_fraction=1.0,
            ineligibility_reasons=reasons,
        )

    base = izev_base_amount(purchase)
    fraction = (
        _lease_fraction(purchase.lease_term_months) if purchase.is_lease else 1.0
    )
    return ZEVIncentiveResult(
        amount=round(base * fraction, 2),
        eligible=True,
        full_amount_before_lease_proration=base,
        lease_fraction=fraction,
        ineligibility_reasons=[],
    )


def zev_purchase_from_dict(entry: Dict[str, Any]) -> ZEVPurchase:
    """Build a ``ZEVPurchase`` from one mapped ``zev_purchases[]`` entry.

    DP#32: every field this program tests is read with ``[]``, not ``.get()``
    with a default. A document missing ``base_msrp`` is a document whose
    eligibility nobody can compute, and it fails here rather than being priced
    against an invented zero. The optional fields are optional in the STATUTE
    (a purchase has no lease term), not merely absent from the document.
    """
    lease_term = entry.get("lease_term_months")
    return ZEVPurchase(
        acquisition_date=date.fromisoformat(entry["acquisition_date"]),
        base_msrp=float(entry["base_msrp"]),
        trim_msrp=float(entry["trim_msrp"]),
        vehicle_class=entry["vehicle_class"],
        propulsion=entry["propulsion"],
        electric_range_km=(
            float(entry["electric_range_km"])
            if entry.get("electric_range_km") is not None
            else None
        ),
        is_lease=entry["is_lease"],
        lease_term_months=int(lease_term) if lease_term is not None else None,
    )
