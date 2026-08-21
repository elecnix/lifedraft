#!/usr/bin/env python3
"""Tests for issue #548: mortgage / variable-rate sensitivity sweep.

The ``--sensitivity`` report swept only the investment return. This adds a
sweep over the *mortgage term rate* plus the break-even rate against a fixed
reference. Because ``build_rate_path`` holds a variable rate flat for the whole
term (see ``evaluate_overlay``), sweeping ``mortgage_rate`` is equivalent to
sweeping the *average realized rate over the term* — the functions and output
must say so.

These tests pin pure-function behavior with fabricated round-number data
(DP#3, DP#4, DP#15), asserting invariants rather than magic numbers:
- the sweep range is read from config, not hardcoded (DP#2);
- net benefit is monotonically non-increasing in the mortgage rate when the
  strategy borrows (a higher borrowing cost cannot help);
- the break-even rate is the rate at which the swept scenario's net benefit
  equals the fixed reference, and bisection brackets it correctly.
"""

import pytest

from simulate import (
    _mortgage_rate_range,
    break_even_mortgage_rate,
    mortgage_rate_sweep,
    run_mortgage_sensitivity,
)
from simulation_config import ScenarioOverlay


def _fixture_cfg():
    """Minimal leveraged config: Smith Manoeuvre is viable, so the mortgage
    rate matters to net benefit."""
    return {
        "family": {
            "members": [
                {
                    "role": "primary",
                    "name": "Pat",
                    "gross_income": 150000,
                    "rrsp_room_accumulated": 30000,
                    "tfsa_room_accumulated": 40000,
                    "fhsa_first_time_buyer_since": None,
                    "fhsa_room_accumulated": 0,
                    "pension_adjustment": 0,
                },
                {
                    "role": "spouse",
                    "name": "Sam",
                    "gross_income": 70000,
                    "rrsp_room_accumulated": 20000,
                    "tfsa_room_accumulated": 40000,
                    "fhsa_first_time_buyer_since": None,
                    "fhsa_room_accumulated": 0,
                    "pension_adjustment": 0,
                },
            ],
            "children": [],
        },
        "property": {
            "house_value": 800000,
            "mortgage_balance": 300000,
            "mortgage_rate": 0.05,
            "margin_available": 50000,
            "ltv_max": 0.80,
            "heloc_readvance": True,
        },
        "accounts": {"resp_current_balance": 0},
        "assumptions": {
            "investment_return": 0.07,
            "inflation": 0.02,
            "projection_years": 10,
            "heloc_rate": 0.05,
            "capital_gains_inclusion": 0.5,
            "resp_eap_tax_rate": 0.15,
        },
        "return_model": {"type": "fixed", "rate": 0.07},
        "scenarios": {},
        "savings": {"rate": 0.2},
    }


def _sm_overlay(rate):
    """A leveraged (Smith Manoeuvre) overlay differing only in mortgage rate."""
    return ScenarioOverlay(
        label=f"m={rate:.1%}",
        cash_out=0.0,
        resp_cash_out=0.0,
        mortgage_rate=rate,
        investment_return=0.07,
        use_readvanceable=True,
        ltv=0.0,
    )


# ── DP#2: sweep range is config, not code ─────────────────────────────────

def test_mortgage_rate_range_read_from_config():
    """The sweep range comes from sensitivity_overlays.mortgage_rates (DP#2)."""
    cfg = _fixture_cfg()
    cfg["sensitivity_overlays"] = {"mortgage_rates": [0.03, 0.05, 0.07]}
    assert _mortgage_rate_range(cfg) == [0.03, 0.05, 0.07]


def test_mortgage_rate_range_falls_back_to_round_numbers():
    """With no config range, fall back to round-number placeholders (DP#13)."""
    rng = _mortgage_rate_range(_fixture_cfg())
    assert rng == sorted(rng)
    assert len(rng) >= 3
    # Round-number placeholders only (DP#13).
    assert all(round(r * 1000) % 5 == 0 for r in rng)


def test_explicit_range_argument_overrides_config():
    cfg = _fixture_cfg()
    cfg["sensitivity_overlays"] = {"mortgage_rates": [0.03, 0.05]}
    assert _mortgage_rate_range(cfg, [0.09]) == [0.09]


# ── sweep semantics ───────────────────────────────────────────────────────

def test_sweep_returns_one_row_per_rate_with_swept_rate_recorded():
    cfg = _fixture_cfg()
    rates = [0.03, 0.05, 0.07]
    rows = mortgage_rate_sweep(
        cfg, anchor_overlay=_sm_overlay(0.05), strategy_alloc={}, rate_range=rates
    )
    assert [r["mortgage_rate"] for r in rows] == rates
    # Each row carries the swept rate as its average-rate-over-term label key.
    assert all(r["avg_rate_over_term"] == r["mortgage_rate"] for r in rows)


def test_net_benefit_monotonically_non_increasing_in_mortgage_rate():
    """A higher mortgage / HELOC borrowing rate cannot raise net benefit for a
    leveraged strategy (the borrowed dollar costs more)."""
    cfg = _fixture_cfg()
    rows = mortgage_rate_sweep(
        cfg,
        anchor_overlay=_sm_overlay(0.05),
        strategy_alloc={},
        rate_range=[0.03, 0.05, 0.07, 0.09],
    )
    nets = [r["net_benefit"] for r in rows]
    assert nets == sorted(nets, reverse=True), (
        f"net benefit not non-increasing in mortgage rate: {nets}"
    )


# ── break-even ────────────────────────────────────────────────────────────

def test_break_even_rate_equates_swept_scenario_to_fixed_reference():
    """The break-even rate is where the variable scenario's net benefit equals
    the fixed reference's. Evaluating the variable scenario at that rate must
    reproduce (within tolerance) the reference net benefit."""
    cfg = _fixture_cfg()
    # Fixed reference: the same leveraged strategy locked at 5.5%.
    reference = mortgage_rate_sweep(
        cfg, anchor_overlay=_sm_overlay(0.055), strategy_alloc={}, rate_range=[0.055]
    )[0]
    be = break_even_mortgage_rate(
        cfg,
        anchor_overlay=_sm_overlay(0.05),
        strategy_alloc={},
        reference_net_benefit=reference["net_benefit"],
    )
    # The break-even rate against an identical scenario is its own rate (5.5%).
    assert be == pytest.approx(0.055, abs=1e-3)


def test_break_even_rate_within_search_bounds():
    """When a reachable reference crosses inside [lo, hi], the break-even rate is
    returned within those bounds."""
    cfg = _fixture_cfg()
    # A reference that the anchor beats at low rates and falls below at high
    # rates — i.e. genuinely reachable within the range. Use the anchor's own
    # net benefit at 0.10 as the reference, so the crossing is at ~0.10.
    reference = mortgage_rate_sweep(
        cfg, anchor_overlay=_sm_overlay(0.10), strategy_alloc={}, rate_range=[0.10]
    )[0]["net_benefit"]
    be = break_even_mortgage_rate(
        cfg,
        anchor_overlay=_sm_overlay(0.05),
        strategy_alloc={},
        reference_net_benefit=reference,
        lo=0.0,
        hi=0.20,
    )
    assert be is not None
    assert 0.0001 <= be <= 0.20
    assert be == pytest.approx(0.10, abs=1e-3)


# ── unreachable reference / ZeroDivision regression (issue #548) ───────────

def test_unreachable_reference_returns_none_without_raising():
    """When even a near-zero mortgage rate cannot push the anchor's net benefit
    up to the reference, there is no break-even in [lo, hi]. The bisection must
    NOT collapse the rate toward 0 (which floating-point underflows the
    amortization annuity factor to 1.0 → ZeroDivisionError). It must return the
    explicit no-break-even sentinel (None) instead of a boundary value or a
    crash."""
    cfg = _fixture_cfg()
    # A reference no leveraged scenario could ever reach, even at rate ≈ 0.
    be = break_even_mortgage_rate(
        cfg,
        anchor_overlay=_sm_overlay(0.05),
        strategy_alloc={},
        reference_net_benefit=1e12,
        lo=0.0,
        hi=0.20,
    )
    assert be is None


def test_bisection_floor_never_evaluates_zero_rate():
    """Even at the very bottom of the search range the evaluated rate is clamped
    above zero, so the amortization annuity factor never underflows to 1.0.

    A reference that is barely reachable forces the search toward the floor; it
    must still complete without raising."""
    cfg = _fixture_cfg()
    # Reference equal to the net benefit at a very low rate → search drives lo
    # toward the floor but must stay non-zero and finite.
    low_rate_net = mortgage_rate_sweep(
        cfg, anchor_overlay=_sm_overlay(0.0001), strategy_alloc={}, rate_range=[0.0001]
    )[0]["net_benefit"]
    be = break_even_mortgage_rate(
        cfg,
        anchor_overlay=_sm_overlay(0.05),
        strategy_alloc={},
        reference_net_benefit=low_rate_net,
        lo=0.0,
        hi=0.20,
    )
    # Either a real break-even at/near the floor, or the no-break-even sentinel —
    # never a crash and never an exact-zero rate.
    assert be is None or be >= 0.0001


def _result_row(label, net_benefit, **over):
    """A minimal result dict shaped like evaluate_overlay output, with the keys
    run_mortgage_sensitivity reads."""
    row = {
        "label": label,
        "net_benefit": net_benefit,
        "resp_cash_out": 0.0,
        "cash_out": 0.0,
        "primary_income": None,
        "spouse_income": None,
        "mortgage_rate": 0.05,
        "use_readvanceable": True,
        "deduct_later": False,
        "investment_return": 0.07,
        "ltv": 0.0,
        "strategy_id": "balanced",
    }
    row.update(over)
    return row


def test_run_mortgage_sensitivity_worse_top_anchor_does_not_raise(capsys):
    """Production reproduction of the crash: the break-even is computed for each
    top scenario against the BEST of the others. When the top-ranked anchor is
    itself worse than the best alternative, that reference is unreachable even at
    rate ≈ 0, which used to drive the bisection to a near-zero rate and
    ZeroDivisionError inside the amortization annuity. It must complete and print
    a no-break-even message instead."""
    cfg = _fixture_cfg()
    anchors = {"strategy": [{"id": "balanced", "label": "Balanced"}]}
    # Three real scenarios produced by the sweep at different rates, so each
    # anchor is genuinely evaluable, and they differ in net benefit so the
    # "best alternative" reference is unreachable for the worse ones.
    results = [
        _result_row("anchor-A", net_benefit=10000.0, mortgage_rate=0.03),
        _result_row("anchor-B", net_benefit=5000.0, mortgage_rate=0.05),
        _result_row("anchor-C", net_benefit=1000.0, mortgage_rate=0.07),
    ]
    rows = run_mortgage_sensitivity(cfg, results, anchors)
    assert rows  # sweep produced rows
    out = capsys.readouterr().out
    # At least one anchor cannot reach its best alternative → explicit message.
    assert "no break-even" in out.lower()
