#!/usr/bin/env python3
"""Interest on a qualifying government student loan: the two non-refundable
credits (ITA s.118.62 federal; TP-1 line 385 Quebec), and the carry-forward
ledger both regimes use.

One module per government program (DP#10): the two credits are the SAME
interest base claimed TWICE -- once against federal tax, once against Quebec
provincial tax -- so they live together, and nothing here reads the household.

The federal credit (ITA s.118.62, CRA line 31900)
-------------------------------------------------
``interest x lowest_rate``, where ``lowest_rate`` is the federal lowest-bracket
rate from the year-versioned tax data (never a hardcoded 15%, DP#2/DP#20).

For a QUEBEC resident the credit is worth ``interest x lowest_rate x
(1 - abatement)``, because the Quebec abatement (ITA s.120(2)) is computed on
the tax remaining AFTER the non-refundable credits, so every federal credit is
worth 16.5% less to a Quebec resident (Revenu Quebec's own guidance, and
TaxTips.ca on s.120(2): "the tax credits are deducted from federal tax before
the abatement is calculated"). This engine models the abatement by scaling the
federal bracket rates (``tax_data._combine_brackets``), so its tax is ALREADY
abated: applying the credit at the unabated rate would make it 16.5% too
generous. Scaling the credit by ``(1 - abatement)`` reproduces the statutory
ordering against an abated base -- the two routes agree to the cent, which is
what the acceptance figures pin ($250 of interest -> $37.50 unabated, $31.3125
abated).

The Quebec credit (TP-1 line 385)
---------------------------------
``interest x qc_student_loan_credit_rate`` (20%), the rate carried as
year-versioned Quebec data beside ``qc_tuition_credit_rate`` (DP#20), never
hardcoded. Only interest on a loan made under the Act respecting financial
assistance for education expenses, the Canada Student Loans Act, the Canada
Student Financial Assistance Act, the Apprentice Loans Act, or another
province's equivalent post-secondary assistance law qualifies -- the contract
says so explicitly and a loan that does not declare it is refused there
(DP#32: never silently treated as qualifying).

The carry-forward (both regimes carry INTEREST, not credit dollars)
-------------------------------------------------------------------
Both regimes let the taxpayer carry forward interest that never produced a
credit, and both recompute the credit from that interest in a later year:

* **Federal** -- CRA line 31900: interest paid "in the year or in any of the
  five preceding years". So the eligible window is ``[sim_year - 5,
  sim_year]`` and interest older than that is lost.
* **Quebec** -- Revenu Quebec's line 385 page: interest paid from 1998 onward
  that has never been used to compute the credit "peut etre reporte a des
  annees futures", with no expiring window stated (Schedule M computes the
  cumulative amount). So the Quebec window is open-ended here.

That asymmetry is the reason the ledger below is a per-YEAR schedule of unused
INTEREST rather than a balance of unused credit dollars: the two regimes expire
different things, and a single credit balance could not tell them apart. What
is consumed when a credit is applied is INTEREST, FIFO from the oldest year --
the credit is recomputed from whatever interest remains, which is exactly what
Schedule M / line 31900 describe.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple


# CRA line 31900: "interest paid in the year or in any of the five preceding
# years". A window, not a ledger of credit -- so it bounds the INTEREST years.
FEDERAL_CARRY_FORWARD_YEARS = 5


def _quebec_rates(qc_data) -> Tuple[float, float]:
    """The ``(abatement, student_loan_credit_rate)`` the Quebec data carries.

    Explicit ``is None`` tests, never ``or`` (DP#32): both fields are real
    numbers that may legitimately be ``0.0`` -- a year whose data has not been
    sourced yet, or a jurisdiction with no abatement -- and ``x or DEFAULT``
    cannot tell that state from an absent attribute. An absent attribute or a
    ``None`` value yields ``0.0``, which means "no abatement" / "no provincial
    credit": the DP#32 direction (absence produces nothing, it is never guessed
    at 16.5% or 20%).
    """
    if qc_data is None:
        return 0.0, 0.0
    abatement = getattr(qc_data, 'provincial_abatement', None)
    qc_student_rate = getattr(qc_data, 'qc_student_loan_credit_rate', None)
    return (0.0 if abatement is None else float(abatement),
            0.0 if qc_student_rate is None else float(qc_student_rate))


def student_loan_interest_credit(
        interest: float, year: int = 2026, provider=None,
        province: Optional[str] = None) -> Tuple[float, float]:
    """The ``(federal, quebec)`` non-refundable credits on ``interest`` paid in
    ``year`` on a qualifying government student loan.

    Args:
        interest: Interest paid this year on a qualifying loan (>= 0).
        year: Tax year, for the year-versioned rates (DP#20).
        provider: Optional ``TaxDataProvider`` override.
        province: The claimant's province of residence. A Quebec resident gets
            the federal credit scaled by the abatement AND the Quebec credit;
            anyone else gets the federal credit at the full lowest rate and no
            provincial credit (DP#32: never credit a provincial portion at a
            guessed rate).

    Returns:
        ``(federal, quebec)``, both >= 0, both 0.0 for non-positive interest or
        for a year with no rate data (absence is loud: no data -> no credit,
        never a hardcoded rate).
    """
    if interest is None or interest <= 0.0:
        return 0.0, 0.0
    from tax_data import TaxDataProvider
    from countries.canada.tax_calc import _load_fed_data
    if provider is None:
        provider = TaxDataProvider()
    try:
        _, lowest_rate, _, _ = _load_fed_data(year, provider)
    except (ValueError, IndexError):
        return 0.0, 0.0  # no rate data -> no credit, not a guessed 15%

    is_quebec = province is not None and province.lower() in ('quebec', 'qc')
    abatement_rate = 0.0
    qc_rate = 0.0
    if is_quebec:
        try:
            qc_data = provider._load_year(year, 'canada', 'quebec')
        except (ValueError, IndexError, AttributeError):
            qc_data = None
        abatement, qc_student_rate = _quebec_rates(qc_data)
        abatement_rate, qc_rate = abatement, qc_student_rate

    federal = interest * lowest_rate * (1.0 - abatement_rate)
    quebec = interest * qc_rate if is_quebec else 0.0
    return federal, quebec


def eligible_interest(ledger: Dict[int, float], sim_year: int,
                      federal: bool = True) -> float:
    """The interest in ``ledger`` still claimable in ``sim_year``.

    ``ledger`` maps the CALENDAR YEAR the interest was paid to the amount never
    yet used to compute a credit. Federal eligibility is the CRA line 31900
    window -- the year itself and the five preceding years; older interest is
    lost. Quebec has no expiring window stated (Schedule M is cumulative), so
    ``federal=False`` sums the whole ledger.
    """
    if federal:
        oldest = sim_year - FEDERAL_CARRY_FORWARD_YEARS
        return sum(amount for y, amount in ledger.items()
                   if oldest <= y <= sim_year and amount > 0.0)
    return sum(amount for y, amount in ledger.items() if amount > 0.0)


def consume_interest_fifo(ledger: Dict[int, float],
                          consumed: float) -> Dict[int, float]:
    """Remove ``consumed`` dollars of interest from ``ledger``, oldest year
    first, and return the remaining schedule.

    FIFO because the federal window expires the OLDEST interest first: spending
    the oldest first is what keeps the household's claim as large as the
    statute allows, and it makes the expiry visible (the entries that survive a
    5-year window are the recent ones). A ``consumed`` beyond the ledger empties
    it rather than going negative -- the caller floors the credit at the
    interest available, so this cannot arise from the fold, and a schedule
    cannot hold a negative amount of interest.
    """
    remaining: Dict[int, float] = {y: amount for y, amount in ledger.items()}
    left = max(0.0, consumed)
    for y in sorted(remaining):
        if left <= 0.0:
            break
        take = min(remaining[y], left)
        remaining[y] -= take
        left -= take
    return {y: amount for y, amount in remaining.items() if amount > 0.0}
