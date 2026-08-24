#!/usr/bin/env python3
"""Issue #258 — Cross-engine consistency tests.

optimize.py and simulate.py enumerate scenarios differently but score every
scenario through the SAME function (optimize.evaluate_strategy_with_simulation /
compute_net_benefit). For one canonical refinance scenario they must therefore
agree on the recorded debt and net benefit.

Issue #259 unified the two paths: optimize.run_optimization now builds its
scenario config through the SAME authoritative build_overlay_config/apply_overlay
machinery that simulate.py uses, so a refinance cash-out is recorded as debt
exactly once and its proceeds invested. Driving both engines with the identical
refinance overlay (_refi_overlay), they agree on future_value, total_debt, and
net_benefit. These tests are the regression lock for that agreement (they
previously xfail-tracked the #250 divergence, now resolved by #259).
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

import pytest

from optimize import run_optimization
from simulate import evaluate_overlay
from scenario_discovery import discover_anchors
from scenario_overlay import ScenarioOverlay


# ---------------------------------------------------------------------------
# Canonical scenario — round numbers, no personal data (DP#4/DP#15).
# margin_available=0 so the ONLY borrowed lump sum is the refinance cash-out,
# isolating the #250 cash-out-as-debt question.
# ---------------------------------------------------------------------------

def _canonical_cfg() -> dict:
    return {
        "family": {
            "members": [
                {"role": "primary", "gross_income": 150000, "birth_year": 1990,
                 "rrsp_room_accumulated": 60000, "tfsa_room_accumulated": 40000},
                {"role": "spouse", "gross_income": 70000, "birth_year": 1988,
                 "rrsp_room_accumulated": 25000, "tfsa_room_accumulated": 35000},
            ],
            "children": [],
        },
        "property": {
            "house_value": 800000,
            "mortgage_balance": 300000,
            "mortgage_rate": 0.05,
            "margin_available": 0,
            "ltv_max": 0.80,
        },
        "accounts": {"resp_current_balance": 0, "rrsp_annual_max": 33810},
        "assumptions": {
            "investment_return": 0.07,
            "salary_growth": 0.02,
            "projection_years": 10,
        },
        "savings": {"rate": 0.20},
    }


# Both engines run discover_anchors / discover_strategies on the same config, so
# they enumerate the SAME named strategies. We pin one ("balanced", plain — no
# Smith Manoeuvre, no deduct-later) and compare it across engines, so any
# divergence is in the engine accounting, not in the strategy choice.
_MATCH_STRATEGY = "balanced"


def _refi_overlay(cfg: dict) -> ScenarioOverlay:
    """The canonical full-LTV refinance as a single overlay both engines consume.

    Issue #259: optimize.py now models a refinance cash-out through the SAME
    authoritative overlay path (build_overlay_config) as simulate.py, so both
    engines must be driven with the identical overlay to compare like-for-like.
    """
    prop = cfg["property"]
    cashout = prop["house_value"] * prop["ltv_max"] - prop["mortgage_balance"]
    return ScenarioOverlay(label="refi-80", cash_out=cashout,
                           mortgage_rate=prop["mortgage_rate"],
                           use_readvanceable=False, deduct_later=False,
                           # #655: a fabricated round new-loan term -- this
                           # canonical scenario has no declared
                           # decisions.mortgage.refinance_options, so the
                           # comparison itself picks the placeholder rather
                           # than silently inheriting the incumbent schedule.
                           refinance_amortization_years=25)


def _optimize_matched(cfg: dict) -> dict:
    """optimize.py runs the full-LTV refinance through the authoritative overlay
    path (issue #259) and ranks the discovered strategies. Return the named match."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        results = run_optimization(cfg, "canonical", overlay=_refi_overlay(cfg))
    matched = [r for r in results if r.get("strategy") == _MATCH_STRATEGY]
    assert matched, (
        f"optimize.py produced no {_MATCH_STRATEGY!r} strategy; "
        f"got {[r.get('strategy') for r in results]}"
    )
    return matched[0]


def _simulate_refinance(cfg: dict) -> dict:
    """simulate.py path for the SAME full-LTV refinance, expressed as an overlay
    cash-out with the SAME named strategy allocation, so both engines model the
    identical economic action."""
    anchors = discover_anchors(cfg)
    strategy_alloc = next(
        (s for s in anchors.get("strategy", []) if s.get("id") == _MATCH_STRATEGY),
        None,
    )
    assert strategy_alloc is not None, f"no {_MATCH_STRATEGY!r} in discovered strategies"
    overlay = _refi_overlay(cfg)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return evaluate_overlay(cfg, overlay, strategy_alloc)


def test_cross_engine_future_value_matches():
    """Both engines deploy the same lump sum, so the (pre-debt) invested
    future_value must agree. This holds on main and confirms the two engines
    really are modelling the same action — making the debt divergence below a
    genuine accounting bug, not a different scenario."""
    cfg = _canonical_cfg()
    opt = _optimize_matched(cfg)
    sim = _simulate_refinance(cfg)
    assert opt["future_value"] == pytest.approx(sim["future_value"], rel=1e-6)


def test_cross_engine_refinance_debt_matches():
    """Money-conservation across engines: for one canonical refinance the
    recorded total_debt must agree between optimize.py and simulate.py."""
    cfg = _canonical_cfg()
    opt = _optimize_matched(cfg)
    sim = _simulate_refinance(cfg)
    assert opt["total_debt"] == pytest.approx(sim["total_debt"], rel=1e-3)


def test_cross_engine_refinance_net_benefit_matches():
    """The headline net_benefit must agree between the two engines for the same
    canonical scenario. This is the exact #250 regression lock."""
    cfg = _canonical_cfg()
    opt = _optimize_matched(cfg)
    sim = _simulate_refinance(cfg)
    assert opt["net_benefit"] == pytest.approx(sim["net_benefit"], rel=1e-3)
