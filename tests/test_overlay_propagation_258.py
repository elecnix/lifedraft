#!/usr/bin/env python3
"""Issue #258 — Config-transformation propagation tests.

These tests assert that each ScenarioOverlay field LANDS WHERE THE ENGINE
ACTUALLY READS IT, not merely where apply_overlay happens to write it. This is
the test class that catches the DP#21 dual-field family of bugs (#249): an
overlay that writes the *deprecated* source of truth (assumptions.investment_return)
while the engine reads the *preferred* one (return_model_data), so the override
is silently discarded.

Each test runs an overlay through apply_overlay/build_overlay_config, builds the
real engine (FamilySimulation / SimState) from the derived config, and asserts on
the value the engine genuinely uses for compounding / rates / debt — never on a
hardcoded dollar figure.

xfail tracking (see issue #258):
  - test_investment_return_reaches_engine_with_return_model_block: xfail until #249
    merges. On main the overlay writes assumptions.investment_return, but the engine
    prefers return_model_data, so the swept rate never reaches compounding.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings

import pytest

from simulation_config import (
    SimulationConfig,
    ScenarioOverlay,
    apply_overlay,
    build_overlay_config,
)
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
            "projection_years": 5,
        },
        "savings": {"rate": 0.20},
    }
    if with_return_model:
        # Realistic input.json shape: a return_model block IS present, so the
        # engine prefers it over the deprecated investment_return scalar.
        cfg["return_model"] = {"type": "fixed", "rate": 0.05}
    return cfg


def _build_sim(cfg_dict: dict) -> FamilySimulation:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SimulationConfig.from_dict(cfg_dict)
    return FamilySimulation(config, adapter=CanadaAdapter(config))


# ---------------------------------------------------------------------------
# investment_return → effective return_model rate the engine compounds at
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rate", [0.03, 0.06, 0.10])
def test_investment_return_reaches_engine_no_return_model(rate):
    """When NO return_model block exists, the overlay rate reaches compounding.

    This path already works on main (the engine falls back to
    FixedReturn(config.investment_return)). It locks in that propagation so a
    future refactor cannot silently break it.
    """
    cfg = apply_overlay(_base_cfg(with_return_model=False),
                        ScenarioOverlay(label="r", investment_return=rate))
    sim = _build_sim(cfg)
    # The exact value the engine feeds to simulate_year_pure for compounding.
    assert sim.return_model.return_for_year(0) == pytest.approx(rate)


@pytest.mark.parametrize("rate", [0.03, 0.06, 0.10])
def test_investment_return_reaches_engine_with_return_model_block(rate):
    """DP#21 dual-field catch (#249): overlay rate must reach the engine EVEN
    when a return_model block is present.

    On main apply_overlay writes only assumptions.investment_return, but
    FamilySimulation prefers config.return_model_data. So the swept rate is
    inert and the sensitivity sweep is a silent no-op. This is the precise bug
    #249 fixes (and the kind of bug a 'specific output number' test cannot see).
    """
    cfg = apply_overlay(_base_cfg(with_return_model=True),
                        ScenarioOverlay(label="r", investment_return=rate))
    sim = _build_sim(cfg)
    assert sim.return_model.return_for_year(0) == pytest.approx(rate)


# ---------------------------------------------------------------------------
# mortgage_rate → rate path the engine actually uses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rate", [0.03, 0.066, 0.099])
def test_mortgage_rate_reaches_rate_path(rate):
    """The overlay mortgage_rate must reach the engine's rate path for year 0.

    This invariant holds on main; it guards the rate-propagation channel that a
    monotonicity-by-numbers test would not.
    """
    cfg = apply_overlay(_base_cfg(), ScenarioOverlay(label="m", mortgage_rate=rate))
    sim = _build_sim(cfg)
    assert sim.rate_path.get_rate(0) == pytest.approx(rate)


@pytest.mark.parametrize("rate", [0.03, 0.066, 0.099])
def test_mortgage_rate_reaches_year_result(rate):
    """The overlay mortgage_rate surfaces in the simulated YearResult.mortgage_rate."""
    cfg = apply_overlay(_base_cfg(), ScenarioOverlay(label="m", mortgage_rate=rate))
    sim = _build_sim(cfg)
    results = sim.run()
    assert results[0].mortgage_rate == pytest.approx(rate)


# ---------------------------------------------------------------------------
# cash_out → recorded debt at year 0
# ---------------------------------------------------------------------------

def _initial_debt(cfg_dict: dict) -> float:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SimulationConfig.from_dict(cfg_dict)
    return SimState.initial(config).total_debt()


@pytest.mark.parametrize("cash_out", [50000, 100000, 250000])
def test_cash_out_increases_recorded_debt(cash_out):
    """A cash-out must INCREASE the engine's recorded initial debt.

    Relational, not a magic number: more cash-out ⇒ strictly more debt. (The
    exact delta is checked by the conservation test in the engine-invariant
    module; here we only assert the channel is non-inert and monotone.)
    """
    base = _base_cfg()
    d_baseline = _initial_debt(apply_overlay(base, ScenarioOverlay(label="none", cash_out=0)))
    d_cashout = _initial_debt(apply_overlay(
        base, ScenarioOverlay(label="co", cash_out=cash_out, refinance_amortization_years=25)))
    assert d_cashout > d_baseline


# ---------------------------------------------------------------------------
# salary_growth → SimulationConfig field the engine reads in run()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("growth", [0.0, 0.03, 0.05])
def test_salary_growth_reaches_config(growth):
    """salary_growth overlay must land on SimulationConfig.salary_growth.

    FamilySimulation.run() reads config.salary_growth to compound incomes
    year-over-year, so this is where the engine actually consumes it.
    """
    cfg = apply_overlay(_base_cfg(), ScenarioOverlay(label="g", salary_growth=growth))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SimulationConfig.from_dict(cfg)
    assert config.salary_growth == pytest.approx(growth)


# ---------------------------------------------------------------------------
# resp_cash_out → RESP balance zeroed AND proceeds carried as free_cash
# ---------------------------------------------------------------------------

def test_resp_cash_out_zeroes_resp_balance():
    """resp_cash_out must zero the RESP balance the engine starts from."""
    base = _base_cfg()
    base["accounts"]["resp_current_balance"] = 80000
    cfg = apply_overlay(base, ScenarioOverlay(label="resp", resp_cash_out=80000))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = SimulationConfig.from_dict(cfg)
    assert config.resp_current_balance == 0


def test_resp_cash_out_recorded_as_free_cash():
    """resp_cash_out proceeds must be carried in property.free_cash for the
    engine's year-0 deployment (this is the input the cash-out double-count
    audit, #257, scrutinises)."""
    base = _base_cfg()
    base["accounts"]["resp_current_balance"] = 80000
    cfg = apply_overlay(base, ScenarioOverlay(label="resp", resp_cash_out=80000))
    assert cfg["property"]["free_cash"] == pytest.approx(80000)


# ---------------------------------------------------------------------------
# build_overlay_config is the documented alias of apply_overlay
# ---------------------------------------------------------------------------

def test_build_overlay_config_matches_apply_overlay():
    """DP#18: build_overlay_config delegates to apply_overlay; they must agree."""
    overlay = ScenarioOverlay(label="x", cash_out=100000, mortgage_rate=0.06,
                              investment_return=0.08, salary_growth=0.03,
                              refinance_amortization_years=25)
    base = _base_cfg()
    a = apply_overlay(base, overlay)
    b = build_overlay_config(base, overlay)
    assert a == b
