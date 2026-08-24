#!/usr/bin/env python3
"""Regression tests for issue #591: --overlay <preset> sensitivity sweeps are
a silent no-op whenever the config has a ``return_model`` block (which
``input_schema.json`` ships).

Two overlay mechanisms wrote the swept return rate to two different places:

  * ``scenario_overlay.apply_overlay`` (the ``ScenarioOverlay`` path, fixed
    for issue #260/#249) writes into ``return_model`` -- the place the engine
    (``simulation.py``) actually reads (DP#21).
  * ``optimize.apply_preset``, ``optimize.apply_sensitivity_overlay`` and
    ``stress_scenarios.run_stress_test`` wrote the deprecated scalar
    ``assumptions.investment_return`` instead. Since ``SimulationConfig
    .from_dict`` only materializes that scalar into ``return_model`` when NO
    ``return_model`` block is present, and the schema ships one, the write
    landed in a key nothing reads. The run completed, tests stayed green, and
    a confident (wrong) number was printed.

These tests prove the sweep actually reaches ``return_model`` -- the single
source of truth the engine consumes -- and that ``sensitivity_overlay_presets
.*.inflation`` (declared in the schema, never applied) is now honoured too.

All data is fabricated with round numbers -- no personal data (DP#4, DP#15).
"""

from unittest.mock import patch

from optimize import (
    DEFAULT_SENSITIVITY_OVERLAYS,
    FORECAST_PRESETS,
    apply_preset,
    apply_sensitivity_overlay,
    compose_preset,
)
from simulation_config import SimulationConfig
from stress_scenarios import StressPath, run_stress_test


def _fixture_cfg_with_fixed_return_model():
    """Minimal valid config that carries a *fixed* return_model block.

    The presence of the return_model block is what triggers the bug: the
    engine prefers it over the deprecated investment_return scalar (DP#21).
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
        # overlay/preset sweeps failed to update.
        "return_model": {
            "type": "fixed",
            "rate": 0.07,
        },
        "scenarios": {},
        "savings": {"rate": 0.2},
    }


class TestApplySensitivityOverlayTargetsReturnModel:
    """optimize.apply_sensitivity_overlay must write the swept rate where the
    engine actually reads it."""

    def test_conservative_and_aggressive_diverge_in_return_model(self):
        base = _fixture_cfg_with_fixed_return_model()

        conservative = apply_sensitivity_overlay(base, 'conservative')
        aggressive = apply_sensitivity_overlay(base, 'aggressive')

        conservative_rate = SimulationConfig.from_dict(conservative).return_model_data.get('rate')
        aggressive_rate = SimulationConfig.from_dict(aggressive).return_model_data.get('rate')

        assert conservative_rate == DEFAULT_SENSITIVITY_OVERLAYS['conservative']['investment_return']
        assert aggressive_rate == DEFAULT_SENSITIVITY_OVERLAYS['aggressive']['investment_return']
        assert conservative_rate != aggressive_rate, (
            "--overlay is a no-op for the return dimension: conservative and "
            "aggressive overlays produced the SAME engine-facing return_model "
            f"rate ({conservative_rate})."
        )

        # Base config must not be mutated (DP#18: overlays modify a copy).
        assert base['return_model']['rate'] == 0.07

    def test_applies_inflation_overlay(self):
        """sensitivity_overlay_presets.*.inflation is declared in the schema
        and shipped in DEFAULT_SENSITIVITY_OVERLAYS, but was never applied."""
        base = _fixture_cfg_with_fixed_return_model()
        result = apply_sensitivity_overlay(base, 'conservative')
        assert result['assumptions']['inflation'] == DEFAULT_SENSITIVITY_OVERLAYS['conservative']['inflation']
        assert base['assumptions']['inflation'] == 0.02  # base untouched


class TestApplyPresetTargetsReturnModel:
    """optimize.apply_preset must write the swept rate where the engine reads it."""

    def test_conservative_and_aggressive_diverge_in_return_model(self):
        base = _fixture_cfg_with_fixed_return_model()

        conservative = apply_preset(base, 'conservative')
        aggressive = apply_preset(base, 'aggressive')

        conservative_rate = SimulationConfig.from_dict(conservative).return_model_data.get('rate')
        aggressive_rate = SimulationConfig.from_dict(aggressive).return_model_data.get('rate')

        assert conservative_rate == FORECAST_PRESETS['conservative']['investment_return']
        assert aggressive_rate == FORECAST_PRESETS['aggressive']['investment_return']
        assert conservative_rate != aggressive_rate


class TestRunStressTestTargetsReturnModel:
    """stress_scenarios.run_stress_test's investment-return override must land
    in return_model, not the deprecated assumptions.investment_return scalar.

    Issue #727 (DP#21): the override is now a PER-YEAR `variable` ReturnModel
    whose `rates` are the stress path (each year's return applied in that
    year), not a single averaged `fixed` `rate`. The #591 invariant it tests
    -- the override lands in return_model, the engine's single source of
    truth -- still holds; the SHAPE of what lands changed (averaged scalar
    -> per-year variable model)."""

    def test_stress_path_lands_in_return_model_as_per_year_variable(self):
        base = _fixture_cfg_with_fixed_return_model()
        crash = StressPath("crash", [-0.20, -0.10, 0.05] + [0.07] * 7,
                            [0.05] * 10)
        n_years = base['assumptions']['projection_years']
        expected_path = crash.fill_returns(n_years)

        captured = {}

        def _fake_run_optimization(mod_cfg, *args, **kwargs):
            captured['cfg'] = mod_cfg
            return [{'net_benefit': 0}]

        with patch('optimize.run_optimization', _fake_run_optimization):
            run_stress_test(base, crash)

        rm = captured['cfg']['return_model']
        # The override lands in return_model (the #591 invariant), as a
        # per-year `variable` model -- NOT a fixed `rate` at the average.
        assert rm['type'] == 'variable'
        assert rm['rates'] == expected_path
        assert 'rate' not in rm
        # The averaged scalar is NOT what the engine consumes (it is a display
        # label only); the per-year path is.
        assert rm['rates'] != [crash.average_return(n_years)] * n_years
        # Base config must not be mutated.
        assert base['return_model']['rate'] == 0.07
        assert base['return_model']['type'] == 'fixed'


class TestOverlaySweepChangesSimulatedResult:
    """End-to-end: with a return_model block present, --overlay conservative
    must actually change the simulated result, not just print a config that
    was never fed to the engine (this is the observable symptom of #591).

    A FIXED AllocationStrategy is used for both runs (rather than letting
    ``run_optimization`` discover strategies independently for each overlay)
    so the only variable between the two runs is the return the engine
    compounds at -- not which strategy ``discover_strategies`` happens to
    pick. That heuristic reads ``assumptions.investment_return`` too (a
    separate, legitimate read site -- see ``resolve_return_rate``), so
    comparing ``run_optimization``'s *best* result across overlays can differ
    for the wrong reason (a different discovered strategy) even when the
    engine's compounding is unaffected by the sweep. Pinning the strategy
    isolates the actual bug: does the engine's return_model change.
    """

    @staticmethod
    def _simulate_total_assets(cfg):
        from countries.canada.adapter import CanadaAdapter
        from countries.canada.rate_model import build_rate_path
        from simulation import FamilySimulation
        from strategy import AllocationStrategy

        config = SimulationConfig.from_dict(cfg)
        adapter = CanadaAdapter(config)
        rate_path = build_rate_path(
            name="fixed",
            initial_rate=config.mortgage_rate,
            term_years=config.projection_years,
            rate_type='variable',
            renewal_rates=[config.mortgage_rate],
        )
        sim = FamilySimulation(
            config=config,
            adapter=adapter,
            strategy=AllocationStrategy(name="fixed-strategy"),
            rate_path=rate_path,
            use_readvanceable=False,
            deduct_later=False,
            lump_sum=config.margin_available,
            free_cash=0.0,
        )
        results = sim.run()
        return results[-1].total_assets

    def test_conservative_vs_aggressive_overlay_changes_engine_compounding(self):
        base = _fixture_cfg_with_fixed_return_model()

        conservative_cfg = compose_preset(base, overlay_name='conservative')
        aggressive_cfg = compose_preset(base, overlay_name='aggressive')

        # Isolate the return dimension: conservative/aggressive also differ in
        # salary_growth, which independently changes total_assets (that write
        # path was never broken). Pin salary_growth equal so investment_return
        # is the only variable between the two runs.
        conservative_cfg['assumptions']['salary_growth'] = 0.02
        aggressive_cfg['assumptions']['salary_growth'] = 0.02

        conservative_assets = self._simulate_total_assets(conservative_cfg)
        aggressive_assets = self._simulate_total_assets(aggressive_cfg)

        assert conservative_assets != aggressive_assets, (
            "--overlay conservative (5%) vs --overlay aggressive (9%) produced "
            f"IDENTICAL simulated total_assets ({conservative_assets:,.2f}) for "
            "the SAME fixed strategy -- the swept return never reached the "
            "engine's return_model (issue #591)."
        )
        # A higher swept return should compound to a higher terminal balance.
        assert conservative_assets < aggressive_assets


class TestScenarioOverlayExtractReadsReturnModel:
    """Issue #990 (DP#18/DP#24): ScenarioOverlay.extract must detect a
    return-rate overlay by reading return_model (the engine's single source
    of truth, DP#21), not the deprecated assumptions.investment_return
    scalar -- apply_overlay writes the swept rate via set_return_rate into
    return_model and never touches the scalar, so the old extract read a dead
    key and a return-rate overlay round-tripped through extract as "no
    change" (DP#24 broken)."""

    def test_extract_recovers_return_rate_overlay_applied_to_fixed_model(self):
        """apply_overlay(investment_return=0.05) over a fixed return_model
        base must extract back to investment_return=0.05, not None."""
        from scenario_overlay import ScenarioOverlay, apply_overlay

        base = _fixture_cfg_with_fixed_return_model()
        overlay = ScenarioOverlay(
            label="r05", investment_return=0.05,
            refinance_amortization_years=25,
        )
        derived = apply_overlay(base, overlay)
        recovered = ScenarioOverlay.extract(base, derived)
        assert recovered.investment_return == 0.05, (
            "extract read the dead assumptions.investment_return scalar (which "
            "apply_overlay never writes) instead of return_model, so a return-"
            "rate overlay round-tripped as None (issue #990)."
        )

    def test_extract_recovers_return_rate_overlay_over_scalar_only_base(self):
        """A base carrying only the deprecated scalar (no return_model block)
        still resolves via _materialize_return_model_data; the overlay lands a
        fixed return_model and extract must recover it."""
        from scenario_overlay import ScenarioOverlay, apply_overlay

        base = _fixture_cfg_with_fixed_return_model()
        del base["return_model"]  # leave only assumptions.investment_return
        overlay = ScenarioOverlay(
            label="r06", investment_return=0.06,
            refinance_amortization_years=25,
        )
        derived = apply_overlay(base, overlay)
        recovered = ScenarioOverlay.extract(base, derived)
        assert recovered.investment_return == 0.06

    def test_extract_no_return_overlay_yields_none(self):
        """An overlay that does not touch the return rate must extract to
        investment_return=None (no spurious detection from the resolve
        fallback default)."""
        from scenario_overlay import ScenarioOverlay, apply_overlay

        base = _fixture_cfg_with_fixed_return_model()
        overlay = ScenarioOverlay(label="noop", refinance_amortization_years=25)
        derived = apply_overlay(base, overlay)
        recovered = ScenarioOverlay.extract(base, derived)
        assert recovered.investment_return is None
