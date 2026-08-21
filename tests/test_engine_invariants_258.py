#!/usr/bin/env python3
"""Issue #258 — Engine invariant / property tests.

These tests assert INVARIANTS and RELATIONSHIPS (monotonicity, conservation,
determinism) rather than specific dollar figures, so they survive legitimate
model refinements while still catching semantic/accounting regressions of the
#249 / #257 class.

Net worth here is the simple liquid figure final.total_assets - final.total_debt
(SimState.net_assets equivalent), NOT the after-tax compute_net_benefit, so the
relationships are clean and model-agnostic.

xfail tracking (see issue #258):
  - test_net_worth_increases_with_investment_return: xfail until #249 merges.
    With a return_model block present (the realistic input.json shape) the swept
    investment_return is inert, so net worth is flat — monotonicity is violated.
  - test_cash_out_conserves_debt_exactly: xfail until #257 merges. apply_overlay
    adds cash_out to BOTH mortgage_balance and margin_available, and SimState
    books margin_available as HELOC debt, so initial debt rises by ~2x cash_out.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

import pytest

from simulation_config import SimulationConfig, ScenarioOverlay, apply_overlay
from simulation import FamilySimulation
from simulation_state import SimState
from countries.canada.adapter import CanadaAdapter


# ---------------------------------------------------------------------------
# Fabricated round-number config — no personal data (DP#4/DP#15).
# ---------------------------------------------------------------------------

def _base_cfg(with_return_model: bool = False) -> dict:
    cfg = {
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
            "margin_available": 50000,
            "ltv_max": 0.80,
        },
        "accounts": {"resp_current_balance": 0, "rrsp_annual_max": 33810},
        "assumptions": {
            "investment_return": 0.07,
            "salary_growth": 0.02,
            "inflation": 0.025,
            "projection_years": 10,
        },
        "savings": {"rate": 0.20},
    }
    if with_return_model:
        cfg["return_model"] = {"type": "fixed", "rate": 0.05}
    return cfg


def _net_worth(cfg_dict: dict) -> float:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(config, adapter=CanadaAdapter(config))
    final = sim.run()[-1]
    return final.total_assets - final.total_debt


def _initial_debt(cfg_dict: dict) -> float:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SimulationConfig.from_dict(cfg_dict)
    return SimState.initial(config).total_debt()


# ===========================================================================
# MONOTONICITY
# ===========================================================================

def test_net_worth_increases_with_investment_return():
    """Higher investment_return ⇒ strictly higher net worth, all else equal.

    Uses the realistic config shape (return_model block present), which is the
    case #249 breaks. This is the monotonicity property the issue calls for; it
    would have caught the inert sensitivity sweep that the old 'it runs' /
    'output == $X' tests missed.
    """
    base = _base_cfg(with_return_model=True)
    nws = [
        _net_worth(apply_overlay(base, ScenarioOverlay(label=str(r), investment_return=r)))
        for r in (0.03, 0.06, 0.10)
    ]
    assert nws[0] < nws[1] < nws[2], f"net worth not strictly increasing: {nws}"


def test_net_worth_increases_with_investment_return_no_return_model():
    """Same monotonicity property via the deprecated scalar path (no
    return_model block). This path works on main and locks it in."""
    base = _base_cfg(with_return_model=False)
    nws = [
        _net_worth(apply_overlay(base, ScenarioOverlay(label=str(r), investment_return=r)))
        for r in (0.03, 0.06, 0.10)
    ]
    assert nws[0] < nws[1] < nws[2], f"net worth not strictly increasing: {nws}"


def test_net_worth_decreases_with_mortgage_rate():
    """Higher mortgage rate ⇒ strictly lower net worth, all else equal.

    Holds on main (rate path propagation works); guards against a regression
    that severs the rate channel.
    """
    base = _base_cfg()
    nws = [
        _net_worth(apply_overlay(base, ScenarioOverlay(label=str(r), mortgage_rate=r)))
        for r in (0.03, 0.06, 0.09)
    ]
    assert nws[0] > nws[1] > nws[2], f"net worth not strictly decreasing: {nws}"


# ===========================================================================
# MONEY CONSERVATION / DEBT ACCOUNTING
# ===========================================================================

@pytest.mark.parametrize("cash_out", [50000, 100000, 250000])
def test_cash_out_conserves_debt_exactly(cash_out):
    """Money conservation: a cash-out must raise recorded initial debt by
    EXACTLY cash_out versus a no-cash-out baseline.

    Δ(initial total_debt) == cash_out. On main the cash-out is booked twice
    (mortgage + HELOC margin), giving Δ == 2*cash_out. This is the #257
    double-count, invisible to any 'specific output number' test.
    """
    base = _base_cfg()
    d_baseline = _initial_debt(apply_overlay(base, ScenarioOverlay(label="none", cash_out=0)))
    d_cashout = _initial_debt(apply_overlay(
        base, ScenarioOverlay(label="co", cash_out=cash_out, refinance_amortization_years=25)))
    assert d_cashout - d_baseline == pytest.approx(cash_out)


@pytest.mark.parametrize("cash_out", [50000, 100000, 250000])
def test_cash_out_debt_is_monotone(cash_out):
    """Weaker, always-true companion to the conservation test: more cash-out ⇒
    strictly more recorded debt. This passes on main and ensures the cash-out
    channel never goes fully inert (the opposite failure mode from #257)."""
    base = _base_cfg()
    d_baseline = _initial_debt(apply_overlay(base, ScenarioOverlay(label="none", cash_out=0)))
    d_cashout = _initial_debt(apply_overlay(
        base, ScenarioOverlay(label="co", cash_out=cash_out, refinance_amortization_years=25)))
    assert d_cashout > d_baseline


# ===========================================================================
# DETERMINISM (pure-function guarantee)
# ===========================================================================

def _run_results(cfg_dict: dict):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(config, adapter=CanadaAdapter(config))
    return sim.run()


def test_simulation_is_deterministic():
    """Identical inputs ⇒ identical YearResult list (DP#3 pure-function guarantee).

    YearResult is a dataclass with value equality, so we compare the full
    per-year results, not just a summary scalar.
    """
    cfg = _base_cfg()
    r1 = _run_results(cfg)
    r2 = _run_results(cfg)
    assert r1 == r2


def test_simulation_deterministic_with_overlay():
    """Determinism also holds for a non-trivial overlaid scenario."""
    cfg = apply_overlay(
        _base_cfg(),
        ScenarioOverlay(label="d", cash_out=100000, mortgage_rate=0.06,
                        investment_return=0.08, salary_growth=0.03,
                        refinance_amortization_years=25),
    )
    assert _run_results(cfg) == _run_results(cfg)
