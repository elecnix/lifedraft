#!/usr/bin/env python3
"""Unit tests for the federal iZEV incentive module.

DP#17: every rule path, and BOTH SIDES of every threshold. The thresholds here
are the three program-window edges (open / pause / close), the PHEV range split,
the two MSRP caps per vehicle class, and the lease proration boundary.

DP#32 is asserted structurally rather than by spot-check: `test_no_bare_zero`
sweeps a grid and proves that this module never returns a zero amount without a
reason attached.
"""

from datetime import date, timedelta

import pytest

from countries.canada.zev_incentive import (
    IZEV_FULL_INCENTIVE,
    IZEV_FULL_LEASE_TERM_MONTHS,
    IZEV_PROGRAM_END,
    IZEV_PROGRAM_PAUSED,
    IZEV_PROGRAM_START,
    IZEV_SHORT_RANGE_PHEV_INCENTIVE,
    PHEV_LONG_RANGE_MIN_KM,
    ZEVPurchase,
    compute_izev_incentive,
    izev_base_amount,
    izev_program_status,
    zev_purchase_from_dict,
)

# A vehicle comfortably inside every cap, so a test that moves ONE variable is
# testing that variable. Fabricated round numbers (DP#4/DP#15).
OPEN_DAY = date(2024, 6, 1)


def _purchase(**over):
    base = dict(
        acquisition_date=OPEN_DAY,
        base_msrp=45000.0,
        trim_msrp=50000.0,
        vehicle_class="car",
        propulsion="battery_electric",
    )
    base.update(over)
    return ZEVPurchase(**base)


# --------------------------------------------------------------------------
# Program window (DP#28): open, paused, closed -- both sides of each edge.
# --------------------------------------------------------------------------

def test_day_before_program_start_is_ineligible():
    result = compute_izev_incentive(
        _purchase(acquisition_date=IZEV_PROGRAM_START - timedelta(days=1))
    )
    assert result.amount == 0.0
    assert not result.eligible
    assert "opened" in result.ineligibility_reasons[0]


def test_first_day_of_program_is_eligible():
    result = compute_izev_incentive(_purchase(acquisition_date=IZEV_PROGRAM_START))
    assert result.eligible
    assert result.amount == IZEV_FULL_INCENTIVE


def test_day_before_pause_is_eligible():
    day_before = IZEV_PROGRAM_PAUSED - timedelta(days=1)
    result = compute_izev_incentive(_purchase(acquisition_date=day_before))
    assert result.eligible
    assert result.amount == IZEV_FULL_INCENTIVE


def test_pause_day_itself_is_ineligible_and_says_paused_not_closed():
    result = compute_izev_incentive(_purchase(acquisition_date=IZEV_PROGRAM_PAUSED))
    assert result.amount == 0.0
    assert not result.eligible
    # The pause and the close are DIFFERENT facts and must read differently.
    reason = result.ineligibility_reasons[0]
    assert "paused" in reason
    assert "pre-approved" in reason


def test_last_day_of_program_reports_the_pause_not_silence():
    result = compute_izev_incentive(_purchase(acquisition_date=IZEV_PROGRAM_END))
    assert result.amount == 0.0
    assert result.ineligibility_reasons


def test_day_after_close_reports_the_close():
    day_after = IZEV_PROGRAM_END + timedelta(days=1)
    result = compute_izev_incentive(_purchase(acquisition_date=day_after))
    assert result.amount == 0.0
    assert "closed" in result.ineligibility_reasons[0]


def test_program_status_returns_none_while_open():
    assert izev_program_status(OPEN_DAY) is None


# --------------------------------------------------------------------------
# PHEV range threshold: both sides of 50 km.
# --------------------------------------------------------------------------

def test_phev_just_below_range_threshold_gets_half_incentive():
    p = _purchase(propulsion="phev", electric_range_km=PHEV_LONG_RANGE_MIN_KM - 1)
    assert izev_base_amount(p) == IZEV_SHORT_RANGE_PHEV_INCENTIVE
    assert compute_izev_incentive(p).amount == IZEV_SHORT_RANGE_PHEV_INCENTIVE


def test_phev_exactly_at_range_threshold_gets_full_incentive():
    p = _purchase(propulsion="phev", electric_range_km=PHEV_LONG_RANGE_MIN_KM)
    assert izev_base_amount(p) == IZEV_FULL_INCENTIVE
    assert compute_izev_incentive(p).amount == IZEV_FULL_INCENTIVE


def test_hydrogen_gets_full_incentive_and_ignores_range():
    p = _purchase(propulsion="hydrogen")
    assert izev_base_amount(p) == IZEV_FULL_INCENTIVE


# --------------------------------------------------------------------------
# MSRP caps: both sides, both classes, both ceilings.
# --------------------------------------------------------------------------

def test_car_base_msrp_just_below_cap_is_eligible():
    result = compute_izev_incentive(_purchase(base_msrp=54999.0, trim_msrp=54999.0))
    assert result.eligible


def test_car_base_msrp_exactly_at_cap_is_ineligible():
    # The statute says "below $55,000", so $55,000 itself fails.
    result = compute_izev_incentive(_purchase(base_msrp=55000.0, trim_msrp=55000.0))
    assert not result.eligible
    assert "base trim MSRP" in result.ineligibility_reasons[0]


def test_larger_vehicle_gets_the_higher_base_cap():
    # 58,000 fails as a car and passes as a larger vehicle: the class is what moved.
    assert not compute_izev_incentive(
        _purchase(base_msrp=58000.0, trim_msrp=58000.0, vehicle_class="car")).eligible
    assert compute_izev_incentive(
        _purchase(base_msrp=58000.0, trim_msrp=58000.0, vehicle_class="larger")).eligible


def test_trim_at_higher_ceiling_is_eligible():
    result = compute_izev_incentive(_purchase(base_msrp=50000.0, trim_msrp=65000.0))
    assert result.eligible


def test_trim_above_higher_ceiling_is_ineligible():
    result = compute_izev_incentive(_purchase(base_msrp=50000.0, trim_msrp=65001.0))
    assert not result.eligible
    assert "higher-trim cap" in result.ineligibility_reasons[0]


def test_two_failures_are_both_reported():
    """Reasons accumulate: fixing one must not reveal the other as a surprise."""
    result = compute_izev_incentive(
        _purchase(acquisition_date=date(2026, 1, 1), base_msrp=90000.0, trim_msrp=90000.0)
    )
    assert len(result.ineligibility_reasons) >= 2


# --------------------------------------------------------------------------
# Lease proration: both sides of the 48-month boundary.
# --------------------------------------------------------------------------

def test_lease_at_full_term_gets_full_incentive():
    result = compute_izev_incentive(
        _purchase(is_lease=True, lease_term_months=IZEV_FULL_LEASE_TERM_MONTHS))
    assert result.amount == IZEV_FULL_INCENTIVE
    assert result.lease_fraction == 1.0


def test_lease_longer_than_full_term_is_not_more_than_full():
    result = compute_izev_incentive(
        _purchase(is_lease=True, lease_term_months=IZEV_FULL_LEASE_TERM_MONTHS + 12))
    assert result.amount == IZEV_FULL_INCENTIVE


def test_lease_one_month_short_is_prorated_not_zeroed():
    months = IZEV_FULL_LEASE_TERM_MONTHS - 1
    result = compute_izev_incentive(_purchase(is_lease=True, lease_term_months=months))
    assert result.amount == pytest.approx(
        IZEV_FULL_INCENTIVE * months / IZEV_FULL_LEASE_TERM_MONTHS, abs=0.01)
    assert result.eligible


def test_off_table_lease_term_is_priced_not_refused():
    """A 30-month lease is not on Transport Canada's four-rung table. Pricing the
    RULE means it is prorated; reproducing the TABLE would have zeroed it, and
    that zero would have been indistinguishable from ineligibility (DP#32)."""
    result = compute_izev_incentive(_purchase(is_lease=True, lease_term_months=30))
    assert result.amount == pytest.approx(IZEV_FULL_INCENTIVE * 30 / 48, abs=0.01)


def test_purchase_is_not_prorated():
    result = compute_izev_incentive(_purchase(is_lease=False))
    assert result.lease_fraction == 1.0
    assert result.amount == IZEV_FULL_INCENTIVE


# --------------------------------------------------------------------------
# DP#32: absence fails loudly at construction, never silently.
# --------------------------------------------------------------------------

def test_phev_without_range_refuses():
    with pytest.raises(ValueError, match="electric_range_km is required"):
        _purchase(propulsion="phev")


def test_lease_without_term_refuses():
    with pytest.raises(ValueError, match="lease_term_months is required"):
        _purchase(is_lease=True)


def test_zero_lease_term_refuses():
    with pytest.raises(ValueError, match="must be positive"):
        _purchase(is_lease=True, lease_term_months=0)


def test_negative_base_msrp_refuses():
    with pytest.raises(ValueError, match="base_msrp must be non-negative"):
        _purchase(base_msrp=-1.0)


def test_negative_trim_msrp_refuses():
    with pytest.raises(ValueError, match="trim_msrp must be non-negative"):
        _purchase(base_msrp=0.0, trim_msrp=-1.0)


def test_trim_below_base_refuses():
    with pytest.raises(ValueError, match="cannot cost less than"):
        _purchase(base_msrp=50000.0, trim_msrp=40000.0)


def test_unknown_vehicle_class_refuses():
    with pytest.raises(ValueError, match="unknown vehicle_class"):
        _purchase(vehicle_class="motorcycle")


def test_unknown_propulsion_refuses():
    with pytest.raises(ValueError, match="unknown propulsion"):
        _purchase(propulsion="diesel")


def test_no_bare_zero():
    """A zero amount ALWAYS carries a reason, across a grid of every dimension.

    This is the DP#32 property the module exists to hold, asserted structurally
    rather than trusted one example at a time.
    """
    dates = [date(2018, 1, 1), IZEV_PROGRAM_START, date(2024, 6, 1),
             IZEV_PROGRAM_PAUSED, IZEV_PROGRAM_END, date(2026, 5, 5)]
    msrps = [(30000.0, 30000.0), (54999.0, 65000.0), (55000.0, 55000.0),
             (50000.0, 70000.0)]
    for d in dates:
        for base, trim in msrps:
            for cls in ("car", "larger"):
                for prop_, rng in (("battery_electric", None), ("phev", 20.0),
                                   ("phev", 80.0), ("hydrogen", None)):
                    r = compute_izev_incentive(_purchase(
                        acquisition_date=d, base_msrp=base, trim_msrp=trim,
                        vehicle_class=cls, propulsion=prop_, electric_range_km=rng))
                    if r.amount == 0.0:
                        assert r.ineligibility_reasons, (
                            f"bare zero for {d} {base}/{trim} {cls} {prop_} {rng}")
                        assert not r.eligible
                    else:
                        assert r.eligible
                        assert not r.ineligibility_reasons


# --------------------------------------------------------------------------
# The dict adapter.
# --------------------------------------------------------------------------

def test_from_dict_round_trips_a_full_entry():
    entry = {
        "id": "ev_a",
        "acquisition_date": "2024-06-01",
        "base_msrp": 45000.0,
        "trim_msrp": 50000.0,
        "vehicle_class": "car",
        "propulsion": "phev",
        "electric_range_km": 60.0,
        "is_lease": True,
        "lease_term_months": 36,
    }
    p = zev_purchase_from_dict(entry)
    assert p.acquisition_date == date(2024, 6, 1)
    assert p.propulsion == "phev"
    assert p.electric_range_km == 60.0
    assert p.lease_term_months == 36
    assert compute_izev_incentive(p).amount == pytest.approx(
        IZEV_FULL_INCENTIVE * 36 / 48, abs=0.01)


def test_from_dict_omits_optional_statutory_fields():
    """A purchase has no lease term and a BEV has no relevant range: both are
    optional in the STATUTE, and their absence is legitimate, not a defect."""
    p = zev_purchase_from_dict({
        "id": "ev_b",
        "acquisition_date": "2024-06-01",
        "base_msrp": 45000.0,
        "trim_msrp": 45000.0,
        "vehicle_class": "larger",
        "propulsion": "battery_electric",
        "is_lease": False,
    })
    assert p.lease_term_months is None
    assert p.electric_range_km is None


def test_from_dict_refuses_a_missing_required_field():
    """DP#32: a document without base_msrp cannot have eligibility computed, and
    fails here rather than being priced against an invented zero."""
    with pytest.raises(KeyError):
        zev_purchase_from_dict({
            "id": "ev_c",
            "acquisition_date": "2024-06-01",
            "trim_msrp": 45000.0,
            "vehicle_class": "car",
            "propulsion": "battery_electric",
            "is_lease": False,
        })
