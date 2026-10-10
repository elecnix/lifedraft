"""Claiming-age adjustment factors for the CPP and the QPP (issue #361).

The two plans differ in two ways that matter to a household deciding when to
claim, and the engine priced both with the CPP rules:

**Deferral.** CPP rises 0.7% per month after 65 to a maximum at **70**
(+42%). The QPP rises at the same monthly rate but runs to **72** (+58.8%),
because Retraite Quebec has allowed a claim at 71 or 72 since 2024.

**Early reduction.** CPP is a flat 0.6% per month (36% at 60). The QPP is
**not flat**: per Retraite Quebec, "the adjustment factor varies between -0.5%
and -0.6% per month of anticipation, **in proportion to the level of the base
plan pension**". So the QPP reduction runs from 0.5%/month for a small pension
to 0.6%/month at the maximum, and a Quebec member below the maximum is
understated by up to six points of their pension. At 60 that is a 30% to 36%
reduction depending on the pension, not a flat 36%.

Both rates are quoted in the issue from the primary sources and are NOT taken
from memory (per #273):

- Retraite Quebec, "What age should you apply for your retirement pension":
  "It will increase by 0.7% for each month that has passed since you turned 65,
  up to a maximum of 58.8% for a pension that begins when you turn 72" and
  "Your pension will be decreased by 0.5% to 0.6% for each month before you
  turn 65".
- Retraite Quebec, "Calculation of your retirement pension": the adjustment
  factor "is determined by comparing a retirement pension at age 65 and a
  maximum retirement pension".

The proportional reading is what the acceptance tests assert: at the maximum
pension the reduction is 36% at 60, and for a small pension it approaches 30%.
"""
from __future__ import annotations

from typing import Optional

# ── CPP ────────────────────────────────────────────────────────────────────
# The CPP rates and window are NOT restated here: ``cpp_sharing`` already owns
# them, and a second copy of a statutory rate is exactly the divergence DP#9
# forbids (clone-detection flagged the duplication the first time this module
# was written). One definition; this module adds the QPP's differences on top.
from countries.canada.cpp_sharing import (  # noqa: E402
    CPP_EARLIEST_START_AGE as CPP_MIN_AGE,
    CPP_EARLY_PENALTY_PER_MONTH as CPP_EARLY_RATE,
    CPP_LATE_BONUS_PER_MONTH as CPP_LATE_RATE,
    CPP_LATEST_START_AGE as CPP_MAX_AGE,
)

CPP_REFERENCE_AGE = 65

# ── QPP ────────────────────────────────────────────────────────────────────
QPP_EARLY_RATE_AT_MAX = 0.006   # 0.6% per month, at the maximum pension
QPP_EARLY_RATE_AT_MIN = 0.005   # 0.5% per month, for a small pension
QPP_LATE_RATE = 0.007           # 0.7% per month after 65, through 72
QPP_MIN_AGE = 60
QPP_MAX_AGE = 72
QPP_REFERENCE_AGE = 65


def max_claim_age(plan: str) -> int:
    """The latest age the plan allows a retirement pension to start."""
    return QPP_MAX_AGE if plan == 'qpp' else CPP_MAX_AGE


def min_claim_age(plan: str) -> int:
    return QPP_MIN_AGE if plan == 'qpp' else CPP_MIN_AGE


def qpp_early_monthly_rate(pension_at_65: float,
                           max_pension_at_65: Optional[float]) -> float:
    """The QPP's monthly early-reduction rate for THIS pension.

    Linear in the pension's level, which is what "in proportion to the level of
    the base plan pension" means and what the two published endpoints pin:
    0.5%/month at a small pension, 0.6%/month at the maximum.

    ``max_pension_at_65`` is required to place the pension on that scale. An
    absent or non-positive maximum is NOT a licence to assume the worst rate:
    the caller passes it from the year-versioned table, and a missing table
    value is a data gap that must fail loudly rather than silently tax a small
    pensioner at the maximum rate (DP#32).
    """
    if max_pension_at_65 is None or max_pension_at_65 <= 0.0:
        raise ValueError(
            "The QPP early-reduction rate is proportional to the pension's "
            "level, so it cannot be computed without the maximum retirement "
            "pension at 65 for the year. Pass it from the year-versioned "
            "table (Retraite Quebec / CQLR c R-9); defaulting here would "
            "silently apply the maximum 0.6% rate to every pension."
        )
    ratio = pension_at_65 / max_pension_at_65
    # Clamp the RATIO, not the rate: a pension above the maximum is impossible,
    # and a negative one is malformed input rather than a smaller pension.
    if ratio < 0.0:
        ratio = 0.0
    if ratio > 1.0:
        ratio = 1.0
    return QPP_EARLY_RATE_AT_MIN + ratio * (
        QPP_EARLY_RATE_AT_MAX - QPP_EARLY_RATE_AT_MIN)


def claiming_age_factor(start_age: int, plan: str = 'cpp',
                        pension_at_65: Optional[float] = None,
                        max_pension_at_65: Optional[float] = None) -> float:
    """The multiplier applied to the pension at 65 for a given ``start_age``.

    Pure (DP#3): a function of the age, the plan, and (for the QPP's early
    side) the pension's level against the year's maximum. ``plan='cpp'`` with
    the defaults reproduces the CPP rules the engine used before this change,
    so every non-Quebec household is byte-identical.
    """
    if plan == 'qpp':
        lo, hi = QPP_MIN_AGE, QPP_MAX_AGE
    else:
        lo, hi = CPP_MIN_AGE, CPP_MAX_AGE

    # Clamp the AGE to the plan's own window. The caller is expected to refuse
    # an out-of-window claim at the contract boundary (a non-Quebec claim at 71
    # or 72 is invalid, not silently a 70); clamping here only keeps the
    # arithmetic total for the ages each plan genuinely allows.
    age = max(lo, min(hi, start_age))
    reference = CPP_REFERENCE_AGE

    if age < reference:
        months_early = (reference - age) * 12
        if plan == 'qpp':
            rate = qpp_early_monthly_rate(pension_at_65, max_pension_at_65)
        else:
            rate = CPP_EARLY_RATE
        return 1.0 - months_early * rate
    if age > reference:
        months_late = (age - reference) * 12
        return 1.0 + months_late * QPP_LATE_RATE if plan == 'qpp' \
            else 1.0 + months_late * CPP_LATE_RATE
    return 1.0