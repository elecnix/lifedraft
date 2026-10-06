#!/usr/bin/env python3
"""QPIP (RQAP) benefit weeks and rates -- the Quebec Parental Insurance Plan,
issue #358.

The engine already computed the QPIP **premium** (``quebec_qpip_premium``); what
it never modelled was the other half of the deal -- the benefits a household
receives, which are the reason the plan exists. A household that declares a
birth got the payroll deduction and none of the income, so every projection of
a parental leave was wrong in the direction that makes Quebec's plan look like
a pure cost.

The law lives here (DP#10: one module per government program, in the
jurisdiction that owns it) and the fold only does the plumbing.

What is configuration and what is statute
-----------------------------------------
The **maximum insurable earnings** is indexed every year and is therefore
year-versioned data on the Quebec ``TaxYearData``
(``qpip_max_insurable_earnings``: 91,000 in 2023, 94,000 in 2024, 98,000 in 2025,
103,000 in 2026). The **weeks and replacement rates** have not moved since the
plan began in 2006 and are not indexed, so they are named constants here with
the source attached -- the same split issue #372 used for the Canada Training
Credit, where the money moved and the rates did not.

Source: Gouvernement du Québec, "Choice of Plan and Types of Benefits for a
Pregnancy or a Birth" --
https://www.quebec.ca/en/family-and-support-for-individuals/pregnancy-parenthood/financial-support-pregnant-women-families/quebec-parental-insurance-plan/pregnancy-childbirth/choice-plan
read for this change (not copied from the CFFP briefs, which are a derived
source).

The structure (all figures from that page)
------------------------------------------
Basic plan (long term) -- more weeks, lower benefits:

- maternity / exclusive to the person who gave birth: **18 weeks at 70%**
- paternity / exclusive to the parent who did not give birth: **5 weeks at 70%**
- shareable parental: **32 weeks -- the first 7 at 70%, the next 25 at 55%**

Special plan (short term) -- fewer weeks, higher benefits:

- maternity: **15 weeks at 75%**
- paternity: **3 weeks at 75%**
- shareable parental: **25 weeks at 75%**

A multiple birth adds 5 weeks (basic) or 3 weeks (special) to each parent's
exclusive benefit; a single parent on the act of birth likewise.

Both parents must choose the SAME plan, and the shareable weeks are split
between them by agreement -- which is why the split is a declared fact here and
not something this module invents. `validate_shares` refuses a household whose
declared shares do not add up to the plan's shareable weeks, because a document
that under-declares them would otherwise silently drop benefit weeks nobody
noticed were missing.
"""

from __future__ import annotations

from typing import Dict, Tuple

# ── Statute: weeks and rates (not indexed; source cited in the module docstring) ──
BASIC_RATE = 0.70
SPECIAL_RATE = 0.75
BASIC_PARENTAL_LONG_RATE = 0.55

MATERNITY_WEEKS = {"basic": 18, "special": 15}
PATERNITY_WEEKS = {"basic": 5, "special": 3}

# The shareable parental block: basic is 32 weeks split 7 @70% + 25 @55%;
# special is 25 weeks all at 75%.
PARENTAL_FIRST_RATE_WEEKS = {"basic": 7, "special": 0}
PARENTAL_LONG_RATE_WEEKS = {"basic": 25, "special": 0}
PARENTAL_SPECIAL_WEEKS = {"basic": 0, "special": 25}

# A multiple birth (or a sole parent on the act of birth) adds weeks to EACH
# parent's exclusive benefit.
MULTIPLE_BIRTH_EXTRA_WEEKS = {"basic": 5, "special": 3}

BIRTHING_ROLES = ("birthing", "other")
PLANS = ("basic", "special")


def weekly_insurable_earnings(annual_insurable_income: float) -> float:
    """Average weekly insurable earnings from the year's insurable income.

    QPIP pays a percentage of the claimant's average weekly earnings, and the
    contract declares annual employment income, so the week is the year divided
    by 52. Not a derived *stored* value (DP#1) -- computed from the year's
    income every time, like every other rate in this engine.
    """
    return max(0.0, annual_insurable_income) / 52.0


def weekly_benefit(weekly_insurable: float, rate: float,
                   max_insurable_earnings: float) -> float:
    """One week's benefit: ``rate`` of the claimant's weekly earnings, capped at
    the year's maximum insurable earnings divided by 52.

    The cap is the whole reason the acceptance test has a second case: earnings
    above it earn nothing extra, so a household at 100,000 and one at 200,000 of
    insurable income receive the same cheque. ``max_insurable_earnings <= 0``
    (a year whose data carries no cap) returns 0.0 rather than paying an
    uncapped benefit off a missing ceiling -- the same no-data-no-credit posture
    the accrual gates use (DP#32).
    """
    if max_insurable_earnings <= 0.0 or rate <= 0.0:
        return 0.0
    cap = max_insurable_earnings / 52.0
    return rate * min(max(0.0, weekly_insurable), cap)


def exclusive_entitlement(plan: str, parent_role: str,
                          multiple_birth: bool = False) -> Tuple[Tuple[int, float], ...]:
    """The weeks and rates exclusive to ONE parent: maternity for the person who
    gave birth, paternity for the parent who did not, plus the multiple-birth
    (or sole-parent) extension.

    Returns ``((weeks, rate), ...)``. Raises ``ValueError`` for a plan or role
    the statute does not have, rather than silently paying nothing -- a
    misspelled ``"Special"`` must not look like a household with no leave.
    """
    if plan not in PLANS:
        raise ValueError(f"unknown QPIP plan {plan!r}; expected one of {PLANS}")
    if parent_role not in BIRTHING_ROLES:
        raise ValueError(
            f"unknown QPIP parent_role {parent_role!r}; expected 'birthing' "
            f"(the person who gave birth, eligible for maternity) or 'other' "
            f"(eligible for paternity)")
    weeks = (MATERNITY_WEEKS[plan] if parent_role == "birthing"
             else PATERNITY_WEEKS[plan])
    rate = BASIC_RATE if plan == "basic" else SPECIAL_RATE
    out = [(weeks, rate)]
    if multiple_birth:
        out.append((MULTIPLE_BIRTH_EXTRA_WEEKS[plan], rate))
    return tuple(out)


def parental_entitlement(plan: str, first_rate_weeks: int,
                         long_rate_weeks: int) -> Tuple[Tuple[int, float], ...]:
    """The SHAREABLE parental weeks this parent takes, by portion.

    ``first_rate_weeks`` / ``long_rate_weeks`` are this parent's share of the
    basic plan's 7 @70% and 25 @55% portions; the special plan has a single
    25-week portion at 75%, declared through ``first_rate_weeks``.

    The two portions are separate arguments because the rate changes between
    them: treating the basic block as one 32-week benefit at 70% would overstate
    it by 25 weeks x 15 percentage points.
    """
    if plan not in PLANS:
        raise ValueError(f"unknown QPIP plan {plan!r}; expected one of {PLANS}")
    if first_rate_weeks < 0 or long_rate_weeks < 0:
        raise ValueError(
            "QPIP shareable parental weeks cannot be negative "
            f"(got first_rate={first_rate_weeks}, long_rate={long_rate_weeks})")
    if plan == "basic":
        if first_rate_weeks + long_rate_weeks == 0:
            return ()
        return ((first_rate_weeks, BASIC_RATE), (long_rate_weeks,
                                                 BASIC_PARENTAL_LONG_RATE))
    return ((first_rate_weeks, SPECIAL_RATE),)


def validate_shares(plan: str, shares: Dict[str, Dict[str, int]],
                    extra_weeks_per_parent: int = 0) -> None:
    """Refuse a household whose declared shareable weeks do not add up.

    ``shares`` maps a person id to that person's declared ``first_rate`` /
    ``long_rate`` portion of the shareable block. Both parents must choose the
    same plan and the shares must cover the plan's shareable weeks EXACTLY:
    under-declaring drops benefit weeks with no trace, and over-declaring
    invents weeks the plan does not pay. Both are the silent-wrong-number shape
    this engine refuses (DP#32).

    A couple where only one parent takes the whole shareable block is perfectly
    legal and sums correctly -- this checks the arithmetic, not the fairness.
    """
    if plan not in PLANS:
        raise ValueError(f"unknown QPIP plan {plan!r}; expected one of {PLANS}")
    first_total = sum(s.get("first_rate", 0) for s in shares.values())
    long_total = sum(s.get("long_rate", 0) for s in shares.values())
    want_first = PARENTAL_FIRST_RATE_WEEKS[plan] + (
        PARENTAL_SPECIAL_WEEKS[plan] if plan == "special" else 0)
    want_long = PARENTAL_LONG_RATE_WEEKS[plan]
    if first_total != want_first or long_total != want_long:
        raise ValueError(
            f"the declared QPIP {plan}-plan parental shares cover "
            f"{first_total} + {long_total} weeks, but the plan pays "
            f"{want_first} + {want_long} shareable weeks. Shareable weeks are "
            f"split between the parents by agreement, so the two declarations "
            f"must add up: declaring fewer drops benefit weeks silently, and "
            f"declaring more invents weeks the plan does not pay (issue #358).")


def gross_benefit(annual_insurable_income: float,
                  entitlement: Tuple[Tuple[int, float], ...],
                  max_insurable_earnings: float) -> float:
    """The gross benefit from a whole entitlement: every ``(weeks, rate)``
    portion priced at the claimant's capped weekly earnings.

    This is the figure the plan reports and CRA taxes; it is deliberately NOT
    net of any tax, because the caller decides where the tax lands.
    """
    weekly = weekly_insurable_earnings(annual_insurable_income)
    return sum(weeks * weekly_benefit(weekly, rate, max_insurable_earnings)
               for weeks, rate in entitlement)


def leave_weeks(entitlement: Tuple[Tuple[int, float], ...]) -> int:
    """Total paid weeks in an entitlement -- the length of the leave, which the
    caller needs to place the benefit on the calendar."""
    return sum(weeks for weeks, _ in entitlement)
