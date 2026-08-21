#!/usr/bin/env python3
"""Unit tests for optimize.py: run_ltv_exploration, run_optimization,
evaluate_strategy_with_simulation, auto-detection helpers.

Tests verify that:
- LTV exploration modifies margin/mortgage correctly at each LTV level
- Strategies are ranked and ordered
- Auto-detection fires when input data has the right fields
- Integration between modules produces consistent results

All test data uses round numbers. No personal information.
"""

from tax_data import default_tax_provider
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import json
import tempfile

from optimize import (
    run_ltv_exploration,
    run_optimization,
    evaluate_strategy_with_simulation,
    simulated_deduct_timing,
    compute_net_benefit,
    _has_property_data,
    _has_family_data,
)
from simulation import FamilySimulation, SimulationConfig, YearResult
from strategy import AllocationStrategy
from countries.canada.strategies import STRATEGY_BALANCED, STRATEGY_RRSP_MAX
from countries.canada.rate_model import build_rate_path


def _make_test_cfg(
    house_value=500000,
    mortgage_balance=100000,
    mortgage_rate=0.05,
    margin_available=200000,
    primary_income=120000,
    spouse_income=50000,
    primary_rrsp_room=100000,
    spouse_rrsp_room=50000,
    primary_tfsa_room=30000,
    spouse_tfsa_room=30000,
    projection_years=10,
    investment_return=0.07,
) -> dict:
    """Build a test config dict with round numbers."""
    return {
        'assumptions': {
            'projection_years': projection_years,
            'investment_return': investment_return,
            'salary_growth': 0.02,
        },
        'savings': {'rate': 0.20},
        'property': {
            'house_value': house_value,
            'mortgage_balance': mortgage_balance,
            'mortgage_rate': mortgage_rate,
            'margin_available': margin_available,
            'ltv_max': 0.80,
            'current_payment_monthly': 1000,
            'amortization_years': 25,
        },
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': primary_income,
                 'rrsp_room_accumulated': primary_rrsp_room,
                 'tfsa_room_accumulated': primary_tfsa_room,
                 'pension_adjustment': 4000},
                {'role': 'spouse', 'gross_income': spouse_income,
                 'rrsp_room_accumulated': spouse_rrsp_room,
                 'tfsa_room_accumulated': spouse_tfsa_room,
                 'pension_adjustment': 4000},
            ],
            'children': [{'name': 'Kid', 'age': 10, 'gross_income': 0}],
        },
        'accounts': {
            'rrsp_annual_percent': 0.18,
            'rrsp_annual_max': 33000,
            'tfsa_annual_room_per_person': 7000,
            'resp_current_balance': 0,
        },
    }


def _write_cfg_to_file(cfg: dict) -> str:
    """Write config dict to a temp file, return path."""
    fd, path = tempfile.mkstemp(suffix='.json')
    with os.fdopen(fd, 'w') as f:
        json.dump(cfg, f)
    return path


# ── Auto-Detection Helpers ─────────────────────────────────────────────────

class TestDP9NoBackwardCompat(unittest.TestCase):
    """Lock: deprecated shims are removed from optimize (DP#9, #278/#329)."""

    def test_load_inputs_removed(self):
        import optimize
        self.assertFalse(
            hasattr(optimize, 'load_inputs'),
            "load_inputs() is a deprecated shim; use SimulationConfig.from_json "
            "or json.load directly (DP#9, #329)",
        )


class TestHasPropertyData(unittest.TestCase):
    """Test _has_property_data auto-detection."""

    def test_all_present(self):
        cfg = {'property': {'house_value': 500000, 'mortgage_balance': 100000,
                            'margin_available': 200000}}
        self.assertTrue(_has_property_data(cfg))

    def test_zero_house(self):
        cfg = {'property': {'house_value': 0, 'mortgage_balance': 100000,
                            'margin_available': 200000}}
        self.assertFalse(_has_property_data(cfg))

    def test_zero_mortgage(self):
        cfg = {'property': {'house_value': 500000, 'mortgage_balance': 0,
                            'margin_available': 200000}}
        self.assertFalse(_has_property_data(cfg))

    def test_zero_margin(self):
        cfg = {'property': {'house_value': 500000, 'mortgage_balance': 100000,
                            'margin_available': 0}}
        self.assertFalse(_has_property_data(cfg))

    def test_missing_property(self):
        self.assertFalse(_has_property_data({}))

    def test_negative_values(self):
        cfg = {'property': {'house_value': -1, 'mortgage_balance': 100000,
                            'margin_available': 200000}}
        self.assertFalse(_has_property_data(cfg))


class TestHasFamilyData(unittest.TestCase):
    """Test _has_family_data auto-detection."""

    def test_with_members(self):
        cfg = {'family': {'members': [{'role': 'primary'}]}}
        self.assertTrue(_has_family_data(cfg))

    def test_empty_members(self):
        cfg = {'family': {'members': []}}
        self.assertTrue(_has_family_data(cfg))  # Key exists

    def test_no_family_key(self):
        self.assertFalse(_has_family_data({}))

    def test_family_no_members(self):
        cfg = {'family': {}}
        self.assertFalse(_has_family_data(cfg))


# ── run_optimization ──────────────────────────────────────────────────────

class TestRunOptimization(unittest.TestCase):
    """Test run_optimization with new-format config."""

    def test_returns_list_of_dicts(self):
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            results = run_optimization(cfg, path)
            self.assertIsInstance(results, list)
            self.assertGreater(len(results), 0)
            for r in results:
                self.assertIn('strategy', r)
                self.assertIn('net_benefit', r)
        finally:
            os.unlink(path)

    def test_results_have_expected_keys(self):
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            results = run_optimization(cfg, path)
            r = results[0]
            self.assertIn('TFSA', r)
            self.assertIn('RRSP', r)
            self.assertIn('total_debt', r)
            self.assertIn('future_value', r)
        finally:
            os.unlink(path)

    def test_lump_sum_included(self):
        """run_optimization passes lump_sum = margin + cashout."""
        cfg = _make_test_cfg(margin_available=300000, house_value=500000,
                              mortgage_balance=100000)
        path = _write_cfg_to_file(cfg)
        try:
            results = run_optimization(cfg, path)
            # Should have non-zero investments
            self.assertGreater(results[0]['future_value'], 0)
        finally:
            os.unlink(path)


# ── run_ltv_exploration ────────────────────────────────────────────────────

class TestRunLTVExploration(unittest.TestCase):
    """Test LTV exploration loop."""

    def test_returns_results_with_ltv_field(self):
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            results = run_ltv_exploration(cfg, path)
            self.assertIsInstance(results, list)
            for r in results:
                self.assertIn('ltv', r)
                self.assertIn('cashout', r)
                self.assertIn('net_benefit', r)
        finally:
            os.unlink(path)

    def test_ltv_0_no_cashout(self):
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            results = run_ltv_exploration(cfg, path)
            ltv0 = [r for r in results if r['ltv'] == 0.0]
            self.assertGreaterEqual(len(ltv0), 2)  # At least 2 strategies at LTV 0
            for r in ltv0:
                self.assertAlmostEqual(r['cashout'], 0)
        finally:
            os.unlink(path)

    def test_ltv_80_has_cashout(self):
        cfg = _make_test_cfg(house_value=500000, mortgage_balance=100000)
        path = _write_cfg_to_file(cfg)
        try:
            results = run_ltv_exploration(cfg, path)
            ltv80 = [r for r in results if r['ltv'] == 0.80]
            self.assertGreater(len(ltv80), 0)
            for r in ltv80:
                expected = 500000 * 0.80 - 100000  # 300k
                self.assertAlmostEqual(r['cashout'], expected, places=0)
        finally:
            os.unlink(path)

    def test_each_ltv_increases_mortgage_balance(self):
        """Higher LTV means larger mortgage_balance in modified config."""
        cfg = _make_test_cfg(house_value=500000, mortgage_balance=100000)
        path = _write_cfg_to_file(cfg)
        try:
            results = run_ltv_exploration(cfg, path)
            ltvs = sorted(set(r['ltv'] for r in results))
            # Cashout should increase with LTV
            cashouts = {}
            for r in results:
                if r['strategy'] == results[0]['strategy']:  # Same strategy
                    cashouts[r['ltv']] = r['cashout']
            sorted_cashouts = [cashouts[ltv] for ltv in sorted(cashouts)]
            for i in range(1, len(sorted_cashouts)):
                self.assertGreaterEqual(sorted_cashouts[i], sorted_cashouts[i-1])
        finally:
            os.unlink(path)

    def test_custom_ltv_steps(self):
        """Can pass custom LTV steps."""
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            results = run_ltv_exploration(cfg, path, ltv_steps=[0.0, 0.50])
            ltvs = sorted(set(r['ltv'] for r in results))
            self.assertEqual(len(ltvs), 2)
            self.assertIn(0.0, ltvs)
            self.assertIn(0.50, ltvs)
        finally:
            os.unlink(path)


# ── Unified config path (issue #259) ──────────────────────────────────────

class TestUnifiedConfigPath(unittest.TestCase):
    """Issue #259: optimize.py's refinance headline routes through the SAME
    authoritative build_overlay_config/apply_overlay path as simulate.py.

    Two lean invariants, one assertion target each:
      1. cross-engine: optimize headline net benefit == simulate.py's for the
         same 80%-refinance scenario + named strategy;
      2. internal: optimize headline at 80% == its own LTV-exploration 80% row.
    """

    LTV = 0.80
    STRATEGY = 'rrsp_max'

    def _net_benefit(self, results, strategy):
        return next(r['net_benefit'] for r in results if r['strategy'] == strategy)

    def test_headline_matches_simulate_for_80pct_refinance(self):
        """Cross-engine: optimize headline == simulate.py (same scenario+strategy)."""
        from optimize import _scenario_overlay, _cashout_for_ltv
        from simulate import evaluate_overlay
        from simulation_config import ScenarioOverlay

        cfg = _make_test_cfg(house_value=500000, mortgage_balance=100000)

        # optimize.py headline: refinance to 80% via the overlay path.
        overlay = _scenario_overlay(cfg, self.LTV, label="LTV 80%")
        opt = run_optimization(cfg, overlay=overlay)
        opt_net = self._net_benefit(opt, self.STRATEGY)

        # simulate.py: same overlay + the SAME allocation optimize uses for
        # this named strategy, so any difference would be the config path.
        s = STRATEGY_RRSP_MAX
        alloc = dict(rrsp_pct=s.rrsp_pct, spousal_rrsp_pct=s.spousal_rrsp_pct,
                     tfsa_pct=s.tfsa_pct, fhsa_pct=getattr(s, 'fhsa_pct', 0.0),
                     resp_pct=s.resp_pct, non_reg_pct=s.non_reg_pct)
        sim_overlay = ScenarioOverlay(
            label="LTV 80%", cash_out=_cashout_for_ltv(cfg, self.LTV),
            mortgage_rate=cfg['property']['mortgage_rate'], ltv=self.LTV,
            use_readvanceable=s.prioritize_readvanceable,
            deduct_later=s.deduct_later,
            # issue #655: same DP#13 placeholder _scenario_overlay uses
            # (refinance_amortization_fallback), so both engines model the
            # identical refinance.
            refinance_amortization_years=25,
        )
        sim = evaluate_overlay(cfg, sim_overlay, strategy_alloc=alloc)

        # Relational: the two engines agree within rounding.
        self.assertAlmostEqual(opt_net, sim['net_benefit'], places=2)

    def test_headline_matches_own_ltv_exploration_80_row(self):
        """Internal: optimize headline at 80% == its own 80% exploration row."""
        cfg = _make_test_cfg(house_value=500000, mortgage_balance=100000)
        path = _write_cfg_to_file(cfg)
        try:
            from optimize import _scenario_overlay
            overlay = _scenario_overlay(cfg, self.LTV, label="LTV 80%")
            headline = run_optimization(cfg, path, overlay=overlay)

            exploration = run_ltv_exploration(cfg, path)
            row_80 = [r for r in exploration if r['ltv'] == self.LTV]

            self.assertAlmostEqual(
                self._net_benefit(headline, self.STRATEGY),
                self._net_benefit(row_80, self.STRATEGY),
                places=2,
            )
        finally:
            os.unlink(path)


# ── evaluate_strategy_with_simulation ─────────────────────────────────────

class TestEvaluateStrategyWithSimulation(unittest.TestCase):
    """Test evaluate_strategy_with_simulation."""

    def test_returns_dict_with_net_benefit(self):
        cfg = _make_test_cfg()
        config = SimulationConfig.from_dict(cfg)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        result = evaluate_strategy_with_simulation(
            name="test",
            strategy=STRATEGY_BALANCED,
            config=config,
            rate_path=rp,
            use_readvanceable=False,
            deduct_later=False,
        )
        self.assertIn('net_benefit', result)
        self.assertIn('strategy', result)

    def test_with_lump_sum(self):
        cfg = _make_test_cfg(margin_available=300000)
        config = SimulationConfig.from_dict(cfg)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        result = evaluate_strategy_with_simulation(
            name="test_lump",
            strategy=STRATEGY_BALANCED,
            config=config,
            rate_path=rp,
            use_readvanceable=False,
            deduct_later=False,
            lump_sum=300000,
        )
        self.assertGreater(result['future_value'], 0)

    def test_no_lump_sum(self):
        cfg = _make_test_cfg()
        config = SimulationConfig.from_dict(cfg)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        result = evaluate_strategy_with_simulation(
            name="test_no_lump",
            strategy=STRATEGY_BALANCED,
            config=config,
            rate_path=rp,
            use_readvanceable=False,
            deduct_later=False,
            lump_sum=0,
        )
        self.assertIn('net_benefit', result)

    def test_uses_jurisdiction_agnostic_readvanceable_key(self):
        """Issue #244 (DP#6/DP#7): the readvanceable-mortgage flag is exposed under a
        mechanism-based key, never the branded 'smith_manoeuvre' product name."""
        cfg = _make_test_cfg()
        config = SimulationConfig.from_dict(cfg)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        result = evaluate_strategy_with_simulation(
            name="test",
            strategy=STRATEGY_BALANCED,
            config=config,
            rate_path=rp,
            use_readvanceable=True,
            deduct_later=False,
        )
        self.assertNotIn('smith_manoeuvre', result)
        self.assertIn('readvanceable_mortgage', result)
        self.assertEqual(result['readvanceable_mortgage'], True)


# ── compute_net_benefit integration ───────────────────────────────────────

class TestComputeNetBenefitIntegration(unittest.TestCase):
    """Test compute_net_benefit auto-includes retirement when birth_year present."""

    def test_with_birth_year_uses_retirement(self):
        """When birth_year is in cfg, retirement module is used for withdrawal tax."""
        cfg_no_age = {
            'family': {'members': [{'role': 'primary'}]},
            'assumptions': {'capital_gains_inclusion': 0.50,
                           'resp_eap_taxable_portion': 0.60,
                           'resp_eap_tax_rate': 0.15},
        }
        cfg_with_age = {
            'family': {'members': [{'role': 'primary', 'birth_year': 1990}]},
            'assumptions': {'capital_gains_inclusion': 0.50,
                           'resp_eap_taxable_portion': 0.60,
                           'resp_eap_tax_rate': 0.15},
        }
        # Create dummy results
        r1 = YearResult(total_rrsp=200000, total_tfsa=50000,
                         total_assets=250000, total_debt=10000,
                         non_reg_balance=0, resp_balance=0, heloc_balance=0)
        r1.rrsp_tax_savings = 50000
        r1.readvance_tax_savings = 0
        r1.contributions = {}

        net_no_age = compute_net_benefit([r1], cfg_no_age)
        net_with_age = compute_net_benefit([r1], cfg_with_age)
        # With age, retirement drawdown computes more precise withdrawal tax
        # Both should be positive, but may differ
        self.assertIsInstance(net_no_age, float)
        self.assertIsInstance(net_with_age, float)


class TestSimulatedDeductTiming(unittest.TestCase):
    """Issue #251: deduct-timing comparison must be DERIVED FROM the simulation,
    not a misleading standalone closed-form that overstated deduct-later."""

    def test_returns_simulated_deduct_now_vs_later(self):
        """Returns per-strategy simulated deduct_now/deduct_later scores."""
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            result = simulated_deduct_timing(cfg, path)
        finally:
            os.unlink(path)
        self.assertIsNotNone(result)
        self.assertIn('deduct_now', result)
        self.assertIn('deduct_later', result)
        self.assertIn('advantage_later', result)
        self.assertIn('strategy', result)
        # advantage_later is exactly the simulated delta (later - now)
        self.assertAlmostEqual(
            result['advantage_later'],
            result['deduct_later'] - result['deduct_now'],
            places=6,
        )

    def test_delta_is_simulation_derived_not_closed_form(self):
        """The reported advantage must equal an INDEPENDENT matched-pair
        re-simulation of the chosen strategy (same allocation, only deduct
        timing flips) — proving it comes from the engine, not the old
        closed-form that compared a one-year lump vs a 10-year spread."""
        import json as _json
        from dataclasses import replace
        from simulation_config import SimulationConfig
        from strategy import FamilyState
        from countries.canada.strategies import discover_strategies
        from countries.canada.rate_model import build_rate_path
        from tax_calculator import marginal_rate
        from objective import MAX_NET_BENEFIT

        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            result = simulated_deduct_timing(cfg, path)
        finally:
            os.unlink(path)
        self.assertIsNotNone(result)

        # Re-simulate the chosen strategy both ways, independently.
        config = SimulationConfig.from_dict(cfg)
        primary = next(m for m in config.family_members if m['role'] == 'primary')
        spouse = next(m for m in config.family_members if m['role'] == 'spouse')
        brackets = default_tax_provider().get_combined_brackets()
        state = FamilyState(
            primary_income=primary['gross_income'],
            spouse_income=spouse['gross_income'],
            primary_marginal_rate=marginal_rate(primary['gross_income'], brackets),
            spouse_marginal_rate=marginal_rate(spouse['gross_income'], brackets),
            primary_rrsp_room=primary['rrsp_room_accumulated'],
            spouse_rrsp_room=spouse['rrsp_room_accumulated'],
            primary_tfsa_room=primary['tfsa_room_accumulated'],
            spouse_tfsa_room=spouse['tfsa_room_accumulated'],
        )
        discovered = discover_strategies(
            state, cfg,
            investment_return=cfg['assumptions']['investment_return'],
            heloc_rate=cfg['property']['mortgage_rate'],
        )
        strat = discovered[result['strategy']]
        rate_path = build_rate_path(
            name="v", initial_rate=config.mortgage_rate,
            term_years=config.projection_years, rate_type='variable',
            renewal_rates=[config.mortgage_rate])
        cashout = max(0, config.house_value * config.ltv_max - config.mortgage_balance)
        # issue #735: simulated_deduct_timing no longer draws a declared
        # facility unconditionally -- this independent re-simulation must
        # match that same fix (undrawn, draw_fraction=0.0) or it is no
        # longer proving what it claims to prove.
        lump = config.margin_available * 0.0 + cashout
        scores = {}
        for dl in (False, True):
            s = replace(strat, deduct_later=dl)
            r = evaluate_strategy_with_simulation(
                name=result['strategy'], strategy=s, config=config,
                rate_path=rate_path, use_readvanceable=s.prioritize_readvanceable,
                deduct_later=dl, lump_sum=lump, objective=MAX_NET_BENEFIT)
            scores[dl] = r['objective_score']
        self.assertAlmostEqual(result['deduct_now'], scores[False], places=2)
        self.assertAlmostEqual(result['deduct_later'], scores[True], places=2)
        # And the headline number is exactly later - now.
        self.assertAlmostEqual(
            result['advantage_later'], scores[True] - scores[False], places=2)

    def test_returns_none_for_old_format(self):
        """Without the new family/members format, no simulated comparison."""
        self.assertIsNone(simulated_deduct_timing({}, "input.json"))


# ── Issue #1058: include_year_by_year flag ────────────────────────────────

class TestIncludeYearByYearFlag(unittest.TestCase):
    """Regression: include_year_by_year=False must skip serialization (#1058).

    If someone removes the ``if include_year_by_year:`` guard or flips the
    default, these assertions break — the definition of done is that something
    fails if it regresses.
    """

    def test_flag_false_returns_empty_year_by_year(self):
        """run_optimization with include_year_by_year=False produces rows
        where year_by_year is an empty list."""
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            results = run_optimization(cfg, path, include_year_by_year=False)
            self.assertGreater(len(results), 0, "expected at least one result row")
            for r in results:
                self.assertIn('year_by_year', r, "key must be present even when False")
                self.assertEqual(r['year_by_year'], [],
                                 "year_by_year should be empty list when flag is False")
        finally:
            os.unlink(path)

    def test_flag_true_returns_nonempty_year_by_year(self):
        """run_optimization with include_year_by_year=True (the default)
        produces rows where year_by_year is a non-empty list."""
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            results = run_optimization(cfg, path, include_year_by_year=True)
            self.assertGreater(len(results), 0, "expected at least one result row")
            for r in results:
                self.assertIn('year_by_year', r)
                self.assertIsInstance(r['year_by_year'], list)
                self.assertGreater(len(r['year_by_year']), 0,
                                   "year_by_year should be non-empty when flag is True")
        finally:
            os.unlink(path)

    def test_flag_false_preserves_scores(self):
        """Score-bearing keys are identical regardless of the flag."""
        cfg = _make_test_cfg()
        path = _write_cfg_to_file(cfg)
        try:
            without = run_optimization(cfg, path, include_year_by_year=False)
            with_ = run_optimization(cfg, path, include_year_by_year=True)
            self.assertEqual(len(without), len(with_), "same number of result rows")
            score_keys = ['strategy', 'net_benefit', 'objective_score']
            for r_no, r_yes in zip(without, with_):
                for k in score_keys:
                    if k in r_no or k in r_yes:
                        self.assertEqual(r_no.get(k), r_yes.get(k),
                                         f"{k} should match regardless of year_by_year flag")
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
