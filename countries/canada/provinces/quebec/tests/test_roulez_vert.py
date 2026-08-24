#!/usr/bin/env python3
"""Unit tests for Quebec's Roulez vert new-vehicle rebate.

DP#17: both sides of every threshold -- each rung of the declining schedule,
both edges of the 2025 suspension window, the MSRP cap, and the end of the
priced range.

The sharpest assertion in this file is `test_2027_is_unavailable_not_zero`:
Quebec has announced that the amount FALLS on 2027-01-01 without publishing what
it falls to, and the module must refuse to price that rather than extrapolate.
"""

from datetime import date, timedelta

import pytest

from countries.canada.provinces.quebec.roulez_vert import (
    LAST_PRICED_DATE,
    ROULEZ_VERT_MSRP_CAP,
    SCHEDULE_START,
    SUSPENSION_END,
    SUSPENSION_START,
    RoulezVertResult,
    compute_roulez_vert_rebate,
    is_suspended,
    scheduled_amount,
)

CHEAP = 40000.0  # comfortably under the cap (DP#4/DP#15: fabricated round number)


def _rebate(**over):
    base = dict(acquisition_date=date(2026, 6, 1), msrp=CHEAP)
    base.update(over)
    return compute_roulez_vert_rebate(**base)


# --------------------------------------------------------------------------
# The declining schedule (DP#20): each rung, and the day before it.
# --------------------------------------------------------------------------

def test_day_before_schedule_starts_is_unpriced():
    result = _rebate(acquisition_date=SCHEDULE_START - timedelta(days=1))
    assert result.amount == 0.0
    assert result.amount_unavailable
    assert "begins" in result.ineligibility_reasons[0]


def test_2024_rung_pays_7000():
    assert scheduled_amount(date(2024, 1, 1)) == 7000.0
    assert _rebate(acquisition_date=date(2024, 6, 1)).amount == 7000.0


def test_last_day_of_2024_still_pays_7000():
    assert _rebate(acquisition_date=date(2024, 12, 31)).amount == 7000.0


def test_2025_rung_steps_down_to_4000():
    # 2025-01-01 is before the suspension window, so the rung is observable.
    assert _rebate(acquisition_date=date(2025, 1, 1)).amount == 4000.0


def test_2026_rung_steps_down_to_2000():
    assert _rebate(acquisition_date=date(2026, 1, 1)).amount == 2000.0


def test_last_priced_day_still_pays():
    result = _rebate(acquisition_date=LAST_PRICED_DATE)
    assert result.eligible
    assert result.amount == 2000.0


def test_2027_is_unavailable_not_zero():
    """The single most important behaviour in this module.

    Quebec published that the amount decreases on 2027-01-01 but not the new
    amount. Interpolating 7000 -> 4000 -> 2000 into "presumably 1000" would be
    exactly the confident, plausible, unverifiable number this codebase exists
    to refuse. The refusal IS the correct output (DP#32).
    """
    result = _rebate(acquisition_date=LAST_PRICED_DATE + timedelta(days=1))
    assert result.amount == 0.0
    assert not result.eligible
    # amount_unavailable is what distinguishes "not published" from "not eligible".
    assert result.amount_unavailable
    assert "has not published" in result.ineligibility_reasons[0]


def test_scheduled_amount_returns_none_not_zero_when_unpublished():
    """None and 0.0 must stay distinguishable at the pure-function layer too."""
    assert scheduled_amount(date(2027, 1, 1)) is None
    assert scheduled_amount(date(2023, 1, 1)) is None
    assert scheduled_amount(date(2026, 6, 1)) == 2000.0


# --------------------------------------------------------------------------
# The 2025 suspension: both edges, inclusive.
# --------------------------------------------------------------------------

def test_day_before_suspension_is_paid():
    result = _rebate(acquisition_date=SUSPENSION_START - timedelta(days=1))
    assert result.eligible
    assert result.amount == 4000.0


def test_first_day_of_suspension_pays_nothing():
    result = _rebate(acquisition_date=SUSPENSION_START)
    assert result.amount == 0.0
    assert not result.eligible
    assert "suspended" in result.ineligibility_reasons[0]


def test_last_day_of_suspension_pays_nothing():
    result = _rebate(acquisition_date=SUSPENSION_END)
    assert result.amount == 0.0
    assert "suspended" in result.ineligibility_reasons[0]


def test_day_after_suspension_is_paid_again():
    result = _rebate(acquisition_date=SUSPENSION_END + timedelta(days=1))
    assert result.eligible
    assert result.amount == 4000.0


def test_suspension_zero_is_not_the_unavailable_zero():
    """Two zeros, two different facts: suspended (eligible date, no money) is not
    unpriced (no published amount). Conflating them is the DP#32 defect."""
    suspended = _rebate(acquisition_date=SUSPENSION_START)
    unpriced = _rebate(acquisition_date=date(2027, 6, 1))
    assert suspended.amount == unpriced.amount == 0.0
    assert not suspended.amount_unavailable
    assert unpriced.amount_unavailable


def test_is_suspended_edges():
    assert not is_suspended(SUSPENSION_START - timedelta(days=1))
    assert is_suspended(SUSPENSION_START)
    assert is_suspended(SUSPENSION_END)
    assert not is_suspended(SUSPENSION_END + timedelta(days=1))


# --------------------------------------------------------------------------
# MSRP cap: both sides.
# --------------------------------------------------------------------------

def test_msrp_just_below_cap_is_eligible():
    result = _rebate(msrp=ROULEZ_VERT_MSRP_CAP - 1)
    assert result.eligible


def test_msrp_exactly_at_cap_is_ineligible():
    result = _rebate(msrp=ROULEZ_VERT_MSRP_CAP)
    assert not result.eligible
    assert "cap" in result.ineligibility_reasons[0]
    assert not result.amount_unavailable


def test_negative_msrp_refuses():
    with pytest.raises(ValueError, match="msrp must be non-negative"):
        _rebate(msrp=-1.0)


# --------------------------------------------------------------------------
# Jurisdiction and propulsion gates.
# --------------------------------------------------------------------------

def test_non_resident_is_ineligible():
    result = _rebate(is_quebec_resident=False)
    assert not result.eligible
    assert "not resident" in result.ineligibility_reasons[0]


def test_phev_is_refused_by_name_not_paid_the_bev_amount():
    """A plug-in hybrid is paid by Quebec on a schedule this module does not
    encode. Paying it the battery-electric amount would be a fabricated number;
    refusing it by name tells the household exactly what is missing."""
    result = _rebate(propulsion="phev")
    assert result.amount == 0.0
    assert not result.eligible
    assert "not priced by this module" in result.ineligibility_reasons[0]


def test_hydrogen_is_refused_by_name():
    result = _rebate(propulsion="hydrogen")
    assert not result.eligible
    assert "not priced by this module" in result.ineligibility_reasons[0]


def test_battery_electric_is_the_priced_case():
    assert _rebate(propulsion="battery_electric").eligible


def test_reasons_accumulate():
    """Non-resident AND over the cap AND a phev: all three are reported."""
    result = _rebate(msrp=90000.0, propulsion="phev", is_quebec_resident=False)
    assert len(result.ineligibility_reasons) == 3


# --------------------------------------------------------------------------
# DP#32 as a structural property.
# --------------------------------------------------------------------------

def test_no_bare_zero():
    dates = [date(2023, 6, 1), date(2024, 1, 1), date(2024, 12, 31),
             date(2025, 1, 1), SUSPENSION_START, SUSPENSION_END,
             date(2025, 4, 1), date(2026, 1, 1), LAST_PRICED_DATE,
             date(2027, 1, 1)]
    for d in dates:
        for msrp in (CHEAP, ROULEZ_VERT_MSRP_CAP, 90000.0):
            for prop_ in ("battery_electric", "phev"):
                for resident in (True, False):
                    r = compute_roulez_vert_rebate(
                        acquisition_date=d, msrp=msrp,
                        propulsion=prop_, is_quebec_resident=resident)
                    if r.amount == 0.0:
                        assert r.ineligibility_reasons, f"bare zero for {d} {msrp} {prop_}"
                        assert not r.eligible
                    else:
                        assert r.eligible
                        assert not r.ineligibility_reasons
                        assert not r.amount_unavailable


def test_default_result_is_an_ineligible_zero():
    """The dataclass default must not read as a paid rebate."""
    assert RoulezVertResult().amount == 0.0
    assert not RoulezVertResult().eligible
