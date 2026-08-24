#!/usr/bin/env python3
"""Issue #143: trading friction — bid/ask spreads and commissions on the
dollars the engine actually turns over.

Before this feature the engine priced every asset transition as frictionless:
contributions bought at par, drawdowns sold at par. Any strategy comparison
involving different TURNOVER was biased toward the higher-turnover option by
construction. The fix is a DECLARED per-transaction cost model owned by
``trading_friction.py`` (DP#3: one spelling), attached at the seams where the
engine really trades:

  - the year-0 borrowed lump deployment (one countable ``fill_room`` event:
    bps + flat fee, netted out of the lump before allocation);
  - each year's savings deployment into the pots (bps only — an aggregate
    pot's ticket count is unobservable; the base is netted by the exact
    haircut ``gross / (1 + r)``).

Deliberately NOT priced (disclosed via model_fidelity, not silently dropped):
sale-side events (drawdown/waterfall gross draws) and substitute-switch
tracking error (no engine event carries a switch notional yet).

DP#32 is asserted both ways:

  - a household declaring no ``trading_friction`` runs to the golden terminal
    total_assets bit-for-bit;
  - a household that DOES declare friction pays it, and the drag compounds.

DP#4/DP#15: every figure below is fabricated and round; every name is
role-based. No real household's data appears anywhere.
"""
import json
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
import trading_friction as tf
from contract_schema import validate_contract
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from test_dp_income_scenario_reaches_engine import _two_generation_subset
from test_golden_trajectory_581 import (
    golden_household_config, _run as _run_golden,
)
import contract_errors  # noqa: F401
import contract_schema

TERMINAL_TOTAL_ASSETS = 9709753.139463063


def _load_doc():
    with open(contract_schema.EXAMPLE_PATH) as fh:
        return _two_generation_subset(json.load(fh))


def _run(cfg_dict):
    """Run a config-dict-shaped household through the yearly fold."""
    cfg = SimulationConfig.from_dict(cfg_dict)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim.run()


# ============================================================================
# 1. The pure module: the ONE spelling of per-transaction friction
# ============================================================================

class TestPureFrictionModel(unittest.TestCase):
    """DP#3: the arithmetic lives in one pure module, tested directly."""

    def test_absent_declaration_is_frictionless(self):
        self.assertTrue(tf.TradingFrictionModel().is_frictionless)
        self.assertTrue(tf.TradingFrictionModel.from_decl(None).is_frictionless)
        self.assertTrue(tf.TradingFrictionModel.from_decl({}).is_frictionless)

    def test_bps_cost_is_proportional(self):
        # Fabricated round numbers: 5 bps on $100,000 turned over = $50.
        m = tf.TradingFrictionModel(rebalance_bps=5)
        self.assertAlmostEqual(tf.round_trip_cost(100_000, m), 50.0, places=9)

    def test_flat_fee_counts_only_counted_events(self):
        # A $9.95 commission applies once per COUNTED event, never per dollar.
        m = tf.TradingFrictionModel(per_trade_fee=9.95)
        self.assertAlmostEqual(
            tf.round_trip_cost(100_000, m, count_events=1), 9.95, places=9)
        self.assertEqual(tf.round_trip_cost(100_000, m, count_events=0), 0.0)

    def test_bps_and_fee_compose(self):
        m = tf.TradingFrictionModel(rebalance_bps=5, per_trade_fee=9.95)
        self.assertAlmostEqual(
            tf.round_trip_cost(100_000, m, count_events=1), 59.95, places=9)

    def test_zero_notional_charges_nothing(self):
        # DP#32: absence is zero — no trade, no fabricated fee.
        m = tf.TradingFrictionModel(rebalance_bps=5, per_trade_fee=9.95)
        self.assertEqual(tf.round_trip_cost(0, m, count_events=1), 0.0)

    def test_tracking_drift_arithmetic(self):
        # Owned ahead of its consumer: 10 bps drift on a $50,000 rotation
        # = $50. No engine event calls this yet (#141 declares statute
        # facts, not rotation trades) — this pins the spelling.
        m = tf.TradingFrictionModel(swap_tracking_error_bps=10)
        self.assertAlmostEqual(
            tf.tracking_drift_cost(50_000, m), 50.0, places=9)
        self.assertEqual(tf.tracking_drift_cost(0, m), 0.0)

    def test_negative_friction_refused_loudly(self):
        # A negative spread PAYS the household to trade — refuse loudly,
        # never coerce (DP#32).
        for kwargs in ({'rebalance_bps': -1}, {'per_trade_fee': -0.01},
                       {'swap_tracking_error_bps': -5}):
            with self.assertRaises(ValueError):
                tf.TradingFrictionModel(**kwargs)

    def test_unknown_key_refused_loudly(self):
        with self.assertRaises(ValueError):
            tf.TradingFrictionModel.from_decl({'reballance_bps': 5})  # typo

    def test_non_numeric_refused(self):
        with self.assertRaises(ValueError):
            tf.TradingFrictionModel(rebalance_bps='5')


# ============================================================================
# 2. Golden no-op: absent declaration -> byte-identical trajectory
# ============================================================================

class TestGoldenNoOp(unittest.TestCase):

    def test_golden_terminal_assets_byte_identical(self):
        # Method: run the 46-year golden fixture (which declares no
        # trading_friction) and compare the terminal total_assets exactly.
        results = _run_golden(golden_household_config())
        self.assertEqual(results[-1].total_assets, TERMINAL_TOTAL_ASSETS)

    def test_no_friction_key_reported_without_declaration(self):
        cfg = golden_household_config()
        results = _run(cfg)
        for r in results:
            self.assertEqual(r.trading_friction_cost, 0.0)


# ============================================================================
# 3. The annual deployment seam: the bps haircut reaches the pots
# ============================================================================

class TestAnnualDeploymentFriction(unittest.TestCase):

    def test_friction_reduces_terminal_assets_and_compounds(self):
        base = _run(golden_household_config())
        cfg = golden_household_config()
        cfg['portfolio']['trading_friction'] = {'rebalance_bps': 5}
        charged = _run(cfg)
        self.assertLess(charged[-1].total_assets, base[-1].total_assets)
        # A higher rate must cost strictly more (the bias direction #143
        # exists to kill: turnover now has a price).
        cfg['portfolio']['trading_friction'] = {'rebalance_bps': 25}
        charged_more = _run(cfg)
        self.assertLess(charged_more[-1].total_assets,
                        charged[-1].total_assets)

    def test_year1_cost_equals_exact_bps_haircut_of_savings(self):
        # Exactness: the year's deployment base IS the traded notional, so
        # the reported cost equals base x bps exactly. Fabricated figures
        # from the golden fixture itself (income x a declared 20% savings
        # rate; the golden children earn nothing, so the adult base equals
        # the gross savings).
        cfg = golden_household_config()
        cfg['portfolio']['trading_friction'] = {'rebalance_bps': 5}
        results = _run(cfg)
        gross_savings_y1 = results[0].annual_savings
        expected = gross_savings_y1 * 5 / 10_000
        self.assertAlmostEqual(results[0].trading_friction_cost, expected,
                               places=6)

    def test_explicit_zero_block_is_a_real_zero_not_an_absence(self):
        # DP#32: a DECLARED zero-cost venue is a fact. It must behave like
        # frictionless (no charge) while still reaching the engine.
        cfg_zero = golden_household_config()
        cfg_zero['portfolio']['trading_friction'] = {
            'rebalance_bps': 0, 'per_trade_fee': 0}
        results = _run(cfg_zero)
        for r in results:
            self.assertEqual(r.trading_friction_cost, 0.0)
        self.assertEqual(results[-1].total_assets, TERMINAL_TOTAL_ASSETS)


# ============================================================================
# 4. The year-0 lump seam: one countable event, bps + flat fee
# ============================================================================

HOUSE_VALUE = 900_000
SURPLUS = 100_000


def _lump_household():
    """A single-adult household with HELOC margin room and no registered
    room, so a lump sum lands in the non-reg pot through fill_room."""
    return {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
                 'retirement_age': 65,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            'children': [],
        },
        'accounts': {'rrsp_annual_max': 0},
        'assumptions': {
            'start_year': 2026, 'horizon_age': 60,
            'investment_return': 0.06, 'salary_growth': 0.0,
            'inflation': 0.0, 'frozen_brackets': True,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': 0, 'cost_basis': 0,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.02, 'interest': 0.0},
                },
            },
        },
        'property': {
            'house_value': HOUSE_VALUE,
            'mortgage_balance': 0,
            'mortgage_rate': 0.05,
            'heloc_rate': 0.07,
            'margin_available': SURPLUS,
            'heloc_readvance': False,
            'amortization_years': 25,
        },
        'household_budget': {'annual_living_costs': 60_000},
    }


def _run_lump(friction=None):
    cfg = _lump_household()
    if friction is not None:
        cfg['portfolio']['trading_friction'] = friction
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False,
                           lump_sum=SURPLUS)
    return sim.run()


class TestLumpDeploymentFriction(unittest.TestCase):

    def test_lump_pays_spread_plus_one_counted_fee_in_year_0(self):
        # The traded notional is the borrowed lump (net of the #137 carry,
        # zero here: no lag declared), so the cost is L x bps + ONE fee.
        bps, fee = 5, 25.0
        results = _run_lump({'rebalance_bps': bps, 'per_trade_fee': fee})
        cost = results[0].trading_friction_cost
        self.assertAlmostEqual(cost, SURPLUS * bps / 10_000 + fee, places=6)
        self.assertGreater(cost, 0.0)

    def test_frictional_lump_deploys_less_than_frictionless(self):
        # The post-friction lump is strictly smaller, so every balance that
        # grows off it is smaller at the horizon.
        base = _run_lump(None)
        charged = _run_lump({'rebalance_bps': 5, 'per_trade_fee': 25})
        self.assertLess(charged[0].non_reg_balance, base[0].non_reg_balance)
        self.assertLess(charged[-1].total_assets, base[-1].total_assets)

    def test_monthly_fold_prices_the_same_friction(self):
        # DP#9: both folds must agree. The MONTHLY time-step prices the same
        # declared friction at its own seams. The lump's round trip applies
        # pre-projection (its result object is not appended, exactly like
        # #137's deployment-lag cost on this path); the annual seam's charge
        # surfaces per year, and terminal assets fall below baseline.
        cfg = _lump_household()
        cfg['savings'] = {'rate': 0.10}
        cfg['assumptions']['time_step'] = 'monthly'
        sim_cfg = SimulationConfig.from_dict(cfg)
        base = FamilySimulation(
            sim_cfg, adapter=CanadaAdapter(sim_cfg), use_readvanceable=False,
            deduct_later=False, lump_sum=SURPLUS).run()
        cfg['portfolio']['trading_friction'] = {
            'rebalance_bps': 5, 'per_trade_fee': 25}
        sim_cfg = SimulationConfig.from_dict(cfg)
        charged = FamilySimulation(
            sim_cfg, adapter=CanadaAdapter(sim_cfg), use_readvanceable=False,
            deduct_later=False, lump_sum=SURPLUS).run()
        self.assertGreater(charged[1].trading_friction_cost, 0.0)
        self.assertAlmostEqual(
            charged[1].trading_friction_cost,
            charged[1].annual_savings * 5 / 10_000, places=6)
        self.assertLess(charged[-1].total_assets, base[-1].total_assets)


# ============================================================================
# 5. The contract boundary: declarable, validated, transported, round-tripped
# ============================================================================

class TestContractMapping(unittest.TestCase):

    def test_wire_document_validates_with_friction_block(self):
        doc = _load_doc()
        doc['trading_friction'] = {'rebalance_bps': 5}
        validate_contract(doc)  # must not raise

    def test_negative_or_unknown_fields_refused_at_load(self):
        doc = _load_doc()
        doc['trading_friction'] = {'rebalance_bps': -5}
        with self.assertRaises(Exception):
            validate_contract(doc)
        doc2 = _load_doc()
        doc2['trading_friction'] = {'rebate_bps': 5}  # typo
        with self.assertRaises(Exception):
            validate_contract(doc2)

    def test_declared_block_rides_the_portfolio_transport(self):
        # The ONE internal dict that round-trips wholesale through
        # config_serde (untouched, DP#24) AND reaches the engine.
        doc = _load_doc()
        doc['trading_friction'] = {'rebalance_bps': 5, 'per_trade_fee': 9.95}
        internal = ic.to_internal_config(doc)
        self.assertEqual(internal['portfolio']['trading_friction'],
                         {'rebalance_bps': 5, 'per_trade_fee': 9.95})
        # And the engine reads it back off the config field.
        cfg = SimulationConfig.from_dict(internal)
        from trading_friction import TradingFrictionModel
        rebuilt = TradingFrictionModel.from_decl(
            (cfg.portfolio_data or {}).get('trading_friction'))
        self.assertFalse(rebuilt.is_frictionless)
        self.assertEqual(rebuilt.rebalance_bps, 5)

    def test_absent_block_leaves_no_trace(self):
        doc = _load_doc()
        internal = ic.to_internal_config(doc)
        portfolio = internal.get('portfolio') or {}
        self.assertIsNone(portfolio.get('trading_friction'))

    def test_friction_only_portfolio_does_not_flip_the_growth_model(self):
        # The friction block rides the internal `portfolio` dict, which also
        # switches the non-reg growth model when it declares COMPOSITION.
        # A household declaring friction alone must keep today's growth
        # physics exactly: an empty-account PortfolioConfig degrades to the
        # flat-rate fallback (no WHT drag, configurable default yield), so
        # declaring friction never silently re-prices returns.
        from countries.canada.portfolio import PortfolioConfig
        import simulation as sim_mod
        friction_only = PortfolioConfig.from_dict(
            {'trading_friction': {'rebalance_bps': 5}})
        self.assertFalse(friction_only.has_data)
        self.assertEqual(friction_only.accounts, {})
        self.assertIsNone(sim_mod._registered_wht_drag_for(friction_only))
        # And the after-tax non-reg rate equals the portfolio-less fallback.
        r_with = sim_mod._non_reg_after_tax_return_for(
            0, 0.40, 0.07, portfolio=friction_only,
            non_reg_yield_rate=0.02, province='qc')
        r_without = sim_mod._non_reg_after_tax_return_for(
            0, 0.40, 0.07, portfolio=None,
            non_reg_yield_rate=0.02, province='qc')
        self.assertEqual(r_with, r_without)


# ============================================================================
# 6. Disclosure: the approximation registers itself when friction is declared
# ============================================================================

class TestDisclosure(unittest.TestCase):

    def test_caveat_fires_only_for_friction_declarers(self):
        import model_fidelity
        active_plain = {a.id for a in model_fidelity.active_approximations({})}
        active_friction = {a.id for a in model_fidelity.active_approximations(
            {'portfolio': {'trading_friction': {'rebalance_bps': 5}}})}
        self.assertNotIn('trading_friction_annual_step', active_plain)
        self.assertIn('trading_friction_annual_step', active_friction)


if __name__ == '__main__':
    unittest.main()
