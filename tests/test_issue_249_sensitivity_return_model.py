#!/usr/bin/env python3
"""Regression tests for issue #249: return-rate sensitivity sweep is inert.

The sweep wrote the swept rate only into the deprecated scalar
``assumptions.investment_return``. Since the engine prefers the modern
``return_model_data`` dict (DP#21) when a ``return_model`` block is present,
the swept value was silently discarded and every swept rate compounded at the
fixed base-model rate. The result: identical net benefit across 4%/7%/10%.

These tests assert that ``apply_overlay`` propagates ``investment_return``
into the engine's preferred source of truth (the ``return_model``), so two
overlays differing only in ``investment_return`` produce different results.

All data is fabricated with round numbers — no personal data (DP#4, DP#15).
"""

from simulation_config import apply_overlay, ScenarioOverlay, SimulationConfig
from simulate import evaluate_overlay


def _fixture_cfg_with_fixed_return_model():
    """Minimal valid config that carries a *fixed* return_model block.

    The presence of the return_model block is what triggered the bug: the
    engine prefers it over the deprecated investment_return scalar.
    """
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
        "accounts": {
            "resp_current_balance": 0,
        },
        "assumptions": {
            "investment_return": 0.07,
            "inflation": 0.02,
            "projection_years": 10,
            "heloc_rate": 0.05,
            "capital_gains_inclusion": 0.5,
            "resp_eap_tax_rate": 0.15,
        },
        # DP#21: modern, engine-preferred return model. This is the field the
        # sweep failed to update.
        "return_model": {
            "type": "fixed",
            "rate": 0.07,
        },
        "scenarios": {},
        "savings": {"rate": 0.2},
    }


def _overlay(ret):
    return ScenarioOverlay(
        label=f"r={ret:.0%}",
        cash_out=0.0,
        resp_cash_out=0.0,
        mortgage_rate=0.05,
        investment_return=ret,
        ltv=0.0,
    )


def test_apply_overlay_propagates_swept_rate_into_fixed_return_model():
    """apply_overlay must write the swept rate into the return_model — the single
    source of truth (#260) — so the engine reflects it."""
    base = _fixture_cfg_with_fixed_return_model()

    derived_low = apply_overlay(base, _overlay(0.04))
    derived_high = apply_overlay(base, _overlay(0.10))

    # The return_model is the single source of truth and carries the swept rate
    assert derived_low["return_model"]["rate"] == 0.04
    assert derived_high["return_model"]["rate"] == 0.10

    # And SimulationConfig.from_dict surfaces it via return_model_data
    cfg_low = SimulationConfig.from_dict(derived_low)
    cfg_high = SimulationConfig.from_dict(derived_high)
    assert cfg_low.return_model_data["rate"] == 0.04
    assert cfg_high.return_model_data["rate"] == 0.10

    # apply_overlay must not mutate the base config
    assert base["return_model"]["rate"] == 0.07


def test_sensitivity_sweep_yields_monotonic_results_with_fixed_return_model():
    """Two overlays differing only in investment_return must produce different
    total_assets / net_benefit when the config has a fixed return_model.

    Higher return -> higher result (monotonic), over a 10-year horizon.
    This is the end-to-end reproduction of issue #249.
    """
    base = _fixture_cfg_with_fixed_return_model()

    res_low = evaluate_overlay(base, _overlay(0.04))
    res_mid = evaluate_overlay(base, _overlay(0.07))
    res_high = evaluate_overlay(base, _overlay(0.10))

    # Net benefit must strictly increase with return rate.
    assert res_low["net_benefit"] < res_mid["net_benefit"] < res_high["net_benefit"], (
        "Return-rate sweep is inert: net benefit identical/non-monotonic across "
        f"4/7/10% — {res_low['net_benefit']} / {res_mid['net_benefit']} / "
        f"{res_high['net_benefit']}"
    )

    # Final total assets (future_value) must also increase with return rate.
    assert (
        res_low["future_value"] < res_mid["future_value"] < res_high["future_value"]
    )


def test_non_fixed_return_model_is_substituted_at_swept_rate():
    """For non-fixed return models the sweep substitutes a fixed model at the
    swept rate (no silent no-op), so the override is honored unambiguously."""
    base = _fixture_cfg_with_fixed_return_model()
    base["return_model"] = {
        "type": "variable",
        "rates": [0.07, 0.08, 0.06],
        "fallback": 0.07,
    }

    derived = apply_overlay(base, _overlay(0.04))

    assert derived["return_model"]["type"] == "fixed"
    assert derived["return_model"]["rate"] == 0.04
    # Base untouched
    assert base["return_model"]["type"] == "variable"


def test_investment_return_shim_materializes_return_model():
    """#260: a config carrying ONLY the deprecated assumptions.investment_return
    (no return_model block) must still drive the engine's return_model at that
    rate — proving the scalar is a load-time shim, never silently ignored.

    Relational: the model the engine builds from return_model_data returns exactly
    the deprecated scalar's rate.
    """
    from return_model import build_return_model_from_config, ReturnEngine

    cfg = _fixture_cfg_with_fixed_return_model()
    del cfg["return_model"]
    cfg["assumptions"]["investment_return"] = 0.09

    config = SimulationConfig.from_dict(cfg)
    model = build_return_model_from_config(config.return_model_data)

    assert ReturnEngine.return_for_year(model, year=0) == 0.09
