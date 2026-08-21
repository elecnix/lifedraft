"""Issue #303: generate & rank varying-retirement-age scenarios.

LEAN, single-responsibility, relational tests:
  1. discover_anchors yields the configured candidate ages (forced / long horizon).
  2. On a long horizon, two retirement ages produce DIFFERENT net benefit
     (retiring later ⇒ more accumulation), proving the dimension varies the sim.
  3. The default short-horizon run is NOT multiplied by retirement ages (gating).
"""

import json
from copy import deepcopy

import pytest

from scenario_discovery import (
    discover_anchors,
    DEFAULT_RETIREMENT_CANDIDATE_AGES,
)
from simulate import build_all_overlays, evaluate_overlay
from simulation_config import ScenarioOverlay


# ── Fixtures ────────────────────────────────────────────────────────────────

def _base_cfg(projection_years: int, start_year: int = 2026,
              primary_birth: int = 1979):
    """Minimal config sufficient for discovery + simulation."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 120000,
                 'birth_year': primary_birth, 'retirement_age': 65,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 60000,
                 'birth_year': 1980, 'retirement_age': 65,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
            'children': [],
        },
        'property': {
            'house_value': 600000,
            'mortgage_balance': 250000,
            'mortgage_rate': 0.05,
            'margin_available': 0,
            'ltv_max': 0.80,
        },
        'accounts': {'resp_current_balance': 0},
        # A non-zero savings rate drives annual contributions so retiring later
        # (more accumulation years) measurably outperforms retiring earlier.
        'savings': {'rate': 0.20},
        'assumptions': {
            'start_year': start_year,
            'projection_years': projection_years,
            'investment_return': 0.06,
        },
        'return_model': {'type': 'fixed', 'rate': 0.06},
        'retirement': {},
    }


# ── 1. discover_anchors yields the configured candidate ages ────────────────

def test_discover_anchors_yields_configured_candidate_ages_when_forced():
    cfg = _base_cfg(projection_years=10)  # short horizon, but forced
    anchors = discover_anchors(cfg, force_retirement_ages=True)
    assert anchors['retirement_age'] == DEFAULT_RETIREMENT_CANDIDATE_AGES


def test_discover_anchors_honors_custom_candidate_ages():
    cfg = _base_cfg(projection_years=10)
    cfg['retirement']['candidate_ages'] = [58, 60, 65]
    anchors = discover_anchors(cfg, force_retirement_ages=True)
    assert anchors['retirement_age'] == [58, 60, 65]


def test_long_horizon_auto_enumerates_candidate_ages():
    # Primary born 1979, start 2026, 25yr horizon ⇒ age 72 at end ⇒ reaches 60.
    cfg = _base_cfg(projection_years=25)
    anchors = discover_anchors(cfg)  # NOT forced — gating should open it
    assert anchors['retirement_age'] == DEFAULT_RETIREMENT_CANDIDATE_AGES


def test_explicit_empty_candidate_ages_is_not_a_default_sweep():
    """DP#32 (#606): candidate_ages=[] means "do not sweep retirement age" --
    it must NOT silently revert to DEFAULT_RETIREMENT_CANDIDATE_AGES. Forcing
    enumeration with an explicit empty set must yield no candidates (the
    caller falls back to the single baseline age), not the default sweep."""
    from scenario_discovery import _candidate_retirement_ages, _discover_retirement_ages

    cfg = _base_cfg(projection_years=25)
    cfg['retirement']['candidate_ages'] = []
    assert _candidate_retirement_ages(cfg) == []
    assert _candidate_retirement_ages(cfg) != DEFAULT_RETIREMENT_CANDIDATE_AGES

    # Forced enumeration with an explicit empty candidate set stays empty
    # (the caller's choice is honored, not silently replaced) -- but the
    # UNforced path still degrades to the single baseline retirement_age,
    # matching the un-swept single-pass shape used everywhere else.
    assert _discover_retirement_ages(cfg, force=True) == []
    assert _discover_retirement_ages(cfg, force=False) == [65]


# ── 2. Two retirement ages produce DIFFERENT net benefit ────────────────────

def test_retiring_later_accumulates_more_on_long_horizon():
    cfg = _base_cfg(projection_years=25)

    def net_benefit_for(age):
        overlay = ScenarioOverlay(label=f"retire@{age}", retirement_age=age)
        return evaluate_overlay(cfg, overlay)['net_benefit']

    nb_early = net_benefit_for(60)
    nb_late = net_benefit_for(70)

    # The dimension must actually move the simulation.
    assert nb_early != nb_late
    # Retiring later ⇒ more years of employment income & accumulation.
    assert nb_late > nb_early


# ── 3. Short horizon is NOT multiplied by retirement ages (gating) ──────────

def test_short_horizon_not_multiplied_by_retirement_ages():
    short = _base_cfg(projection_years=10)

    anchors_short = discover_anchors(short)  # default: gated OFF
    assert len(anchors_short['retirement_age']) == 1

    combos_short = build_all_overlays(short, anchors_short)

    # Compare against the SAME config but with the retirement dimension forced:
    # the forced run must produce strictly more combinations (one per extra age).
    anchors_forced = discover_anchors(short, force_retirement_ages=True)
    combos_forced = build_all_overlays(short, anchors_forced)

    assert len(combos_forced) == len(combos_short) * len(DEFAULT_RETIREMENT_CANDIDATE_AGES)

    # And the gated short run leaves the overlay's retirement_age untouched (None).
    assert all(c['overlay'].retirement_age is None for c in combos_short)
