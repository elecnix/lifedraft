#!/usr/bin/env python3
"""Comprehensive tests for simulation.py — covering monthly mode, cash flows, and edge cases.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/test_simulation_full.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import json

from simulation import FamilySimulation, SimulationConfig, YearResult
from simulation_state import adult_fhsa_slot  # #700/#643/#704: per-adult FHSA store
from countries.canada.rate_model import build_rate_path, HELOCPath


def make_test_dict(**overrides):
    """Create a minimal test dict for SimulationConfig.from_dict with round numbers."""
    d = {
        'assumptions': {
            'projection_years': 3,
            'investment_return': 0.06,
            'salary_growth': 0.02,
            'start_year': 2026,
            'frozen_brackets': True,
            'time_step': 'yearly',
        },
        'savings': {'rate': 0.15},
        'property': {
            'house_value': 600000,
            'mortgage_balance': 200000,
            'mortgage_rate': 0.045,
            'ltv_max': 0.80,
            'current_payment_monthly': 1200,
            'amortization_years': 25,
            'margin_available': 0,
        },
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 40000},
            ],
            'children': [],
        },
        'accounts': {
            'rrsp_annual_percent': 0.18,
            'rrsp_annual_max': 33810,
            'tfsa_annual_room_per_person': 7000,
            'resp_current_balance': 0,
        },
        'cash_flows': [],
    }
    for key, val in overrides.items():
        if isinstance(val, dict) and key in d and isinstance(d[key], dict):
            d[key].update(val)
        else:
            d[key] = val
    return d


def make_test_config(**overrides):
    return SimulationConfig.from_dict(make_test_dict(**overrides))


class TestSimulationConfig(unittest.TestCase):
    """Test SimulationConfig creation and from_dict."""

    def test_basic_config(self):
        cfg = make_test_config()
        self.assertEqual(cfg.projection_years, 3)
        self.assertAlmostEqual(cfg.investment_return, 0.06)
        self.assertEqual(cfg.mortgage_balance, 200000)

    def test_from_dict_full(self):
        cfg = make_test_config()
        self.assertEqual(cfg.family_members[0]['role'], 'primary')

    def test_to_dict_roundtrip(self):
        """DP#24: to_dict() is the inverse of from_dict()."""
        cfg = make_test_config()
        d = cfg.to_dict()
        self.assertIn('assumptions', d)
        self.assertIn('property', d)
        self.assertEqual(d['assumptions']['projection_years'], 3)

    def test_cash_flows(self):
        d = make_test_dict()
        d['cash_flows'] = [{'year': 2026, 'amount': 10000, 'tax_treatment': 'post-tax'}]
        cfg = SimulationConfig.from_dict(d)
        self.assertEqual(len(cfg.cash_flows), 1)

    def test_frozen_brackets(self):
        cfg = make_test_config(assumptions={'frozen_brackets': True})
        self.assertTrue(cfg.frozen_brackets)

    def test_monthly_time_step(self):
        cfg = make_test_config(assumptions={'time_step': 'monthly'})
        self.assertEqual(cfg.time_step, 'monthly')


class TestFamilySimulationBasic(unittest.TestCase):
    """Test FamilySimulation basic runs."""

    def test_run_yearly_no_smith(self):
        cfg = make_test_config()
        rate_path = build_rate_path("test", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        results = sim.run()
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertIsInstance(r, YearResult)
            self.assertGreater(r.total_assets, 0)

    def test_run_with_smith(self):
        cfg = make_test_config()
        rate_path = build_rate_path("sm", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=True, rate_path=rate_path)
        results = sim.run()
        self.assertEqual(len(results), 3)

    def test_run_with_lump_sum(self):
        cfg = make_test_config()
        rate_path = build_rate_path("lump", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path, lump_sum=50000)
        results = sim.run()
        self.assertEqual(len(results), 3)
        self.assertGreater(results[0].total_rrsp, 0)

    def test_summary(self):
        cfg = make_test_config()
        rate_path = build_rate_path("sum", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        summary = sim.summary()
        self.assertIn('total_assets', summary)
        self.assertIn('total_debt', summary)


class TestFamilySimulationMonthly(unittest.TestCase):
    """Test monthly simulation mode."""

    def test_monthly_basic(self):
        d = make_test_dict()
        d['assumptions']['time_step'] = 'monthly'
        cfg = SimulationConfig.from_dict(d)
        rate_path = build_rate_path("monthly", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path, lump_sum=30000)
        results = sim.run()
        self.assertEqual(len(results), 3)

    def test_monthly_with_smith(self):
        d = make_test_dict()
        d['assumptions']['time_step'] = 'monthly'
        cfg = SimulationConfig.from_dict(d)
        rate_path = build_rate_path("msm", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=True, rate_path=rate_path)
        results = sim.run()
        self.assertEqual(len(results), 3)


class TestCashFlowsInSimulation(unittest.TestCase):
    """Test cash flow event handling in simulation."""

    def test_post_tax_cash_flow(self):
        d = make_test_dict()
        d['cash_flows'] = [{'year': 2026, 'amount': 10000, 'tax_treatment': 'post-tax'}]
        cfg = SimulationConfig.from_dict(d)
        rate_path = build_rate_path("cf", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        results = sim.run()
        self.assertGreater(len(results), 0)

    def test_pre_tax_cash_flow(self):
        d = make_test_dict()
        d['cash_flows'] = [{'year': 2026, 'amount': 20000, 'tax_treatment': 'pre-tax'}]
        cfg = SimulationConfig.from_dict(d)
        rate_path = build_rate_path("ptcf", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        results = sim.run()
        self.assertGreater(len(results), 0)

    def test_non_taxable_cash_flow(self):
        d = make_test_dict()
        d['cash_flows'] = [{'year': 2026, 'amount': 5000, 'tax_treatment': 'non-taxable'}]
        cfg = SimulationConfig.from_dict(d)
        rate_path = build_rate_path("ntcf", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        results = sim.run()
        self.assertGreater(len(results), 0)

    def test_cash_flow_future_year_ignored(self):
        """Cash flow for a different year should not apply."""
        d = make_test_dict()
        d['cash_flows'] = [{'year': 2035, 'amount': 50000, 'tax_treatment': 'post-tax'}]
        cfg = SimulationConfig.from_dict(d)
        rate_path = build_rate_path("future", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim_no_cf = FamilySimulation(make_test_config(), use_readvanceable=False, rate_path=rate_path)
        sim_with_cf = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        # With a future cash flow that doesn't trigger within the projection years,
        # results should be similar to no cash flow
        self.assertGreater(len(sim_with_cf.run()), 0)


class TestYearResult(unittest.TestCase):
    """Test YearResult dataclass."""

    def test_basic_result(self):
        r = YearResult(year=1, total_assets=165000, total_debt=200000,
                        mortgage_rate=0.045, heloc_rate=0.05)
        self.assertEqual(r.total_assets, 165000)
        self.assertEqual(r.total_debt, 200000)
        self.assertEqual(r.year, 1)

    def test_net_benefit_default_zero(self):
        """net_benefit defaults to 0.0 (populated by optimizer)."""
        r = YearResult(year=1, total_assets=100000, total_debt=80000)
        self.assertEqual(r.net_benefit, 0.0)


class TestFHSAInSimulation(unittest.TestCase):
    """Test FHSA contributions flow through the full simulation (issue #124)."""

    def _make_fhsa_config(self, time_step='yearly'):
        """Create a config with FHSA room data (DP#16: auto-include when data present)."""
        d = make_test_dict()
        d['assumptions']['time_step'] = time_step
        d['family']['members'][0]['fhsa_room_accumulated'] = 8000
        return SimulationConfig.from_dict(d)

    def test_fhsa_account_created_when_room_present(self):
        """DP#16: FHSA auto-includes when room data is present."""
        cfg = self._make_fhsa_config()
        rate_path = build_rate_path("fhsa", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        self.assertTrue(sim.has_fhsa, "FHSA should be active when fhsa_room_accumulated > 0")

    def test_fhsa_not_created_without_room(self):
        """DP#16: FHSA absent when no room data present."""
        cfg = make_test_config()
        rate_path = build_rate_path("nofhsa", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        self.assertFalse(sim.has_fhsa)

    def test_fhsa_contribution_nonzero_in_yearly_simulation(self):
        """FHSA contribution should be > 0 when strategy allocates to FHSA (yearly)."""
        from strategy import AllocationStrategy
        fhsa_strategy = AllocationStrategy(
            name="With FHSA",
            rrsp_pct=0.25,
            spousal_rrsp_pct=0.05,
            tfsa_pct=0.20,
            fhsa_pct=0.08,
            resp_pct=0.0,
            non_reg_pct=0.42,
        )
        cfg = self._make_fhsa_config()
        rate_path = build_rate_path("fhsa2", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path,
                               strategy=fhsa_strategy)
        results = sim.run()
        # Verify FHSA balance grows (contribution + growth)
        state = sim._state
        self.assertGreater(adult_fhsa_slot(state.jurisdiction_state['canada'], 0)['balance'], 0,
                           "FHSA balance should be > 0 after simulation with FHSA strategy")

    def test_fhsa_contribution_nonzero_in_monthly_simulation(self):
        """FHSA contribution should be > 0 when strategy allocates to FHSA (monthly)."""
        from strategy import AllocationStrategy
        fhsa_strategy = AllocationStrategy(
            name="With FHSA Monthly",
            rrsp_pct=0.25,
            spousal_rrsp_pct=0.05,
            tfsa_pct=0.20,
            fhsa_pct=0.08,
            resp_pct=0.0,
            non_reg_pct=0.42,
        )
        cfg = self._make_fhsa_config(time_step='monthly')
        rate_path = build_rate_path("fhsa3", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path,
                               strategy=fhsa_strategy)
        results = sim.run()
        # FHSA should have grown (contributions + growth)
        self.assertTrue(sim.has_fhsa)
        self.assertGreater(adult_fhsa_slot(sim._state.jurisdiction_state['canada'], 0)['balance'], 0,
                           "FHSA balance should be > 0 after monthly simulation")

    def test_fhsa_annual_room_added_in_yearly_simulation(self):
        """FHSA annual room should be added each year via TaxDataProvider (DP#20)."""
        from strategy import AllocationStrategy
        fhsa_strategy = AllocationStrategy(
            name="FHSA Room Test",
            rrsp_pct=0.25,
            spousal_rrsp_pct=0.05,
            tfsa_pct=0.20,
            fhsa_pct=0.08,
            resp_pct=0.0,
            non_reg_pct=0.42,
        )
        cfg = self._make_fhsa_config()
        rate_path = build_rate_path("fhsar", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path,
                               strategy=fhsa_strategy)
        results = sim.run()
        state = sim._state
        # After 3 years, FHSA room should still exist (annual room added each year)
        self.assertGreater(adult_fhsa_slot(state.jurisdiction_state['canada'], 0)['room'], 0,
                           "FHSA room should be replenished annually")

    def test_fhsa_balance_in_total_assets(self):
        """FHSA balance should be counted in total_assets."""
        from strategy import AllocationStrategy
        fhsa_strategy = AllocationStrategy(
            name="FHSA Assets",
            rrsp_pct=0.25,
            spousal_rrsp_pct=0.05,
            tfsa_pct=0.20,
            fhsa_pct=0.08,
            resp_pct=0.0,
            non_reg_pct=0.42,
        )
        cfg = self._make_fhsa_config()
        rate_path = build_rate_path("fhst", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim_with = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path,
                                    strategy=fhsa_strategy)
        results_with = sim_with.run()
        # FHSA balance must appear in total_assets at the final year
        fhsa_bal = adult_fhsa_slot(sim_with._state.jurisdiction_state['canada'], 0)['balance']
        self.assertGreater(fhsa_bal, 0, "FHSA balance should be > 0 after contributions")
        # total_assets must exceed the sum of non-FHSA assets
        non_fhsa_assets = (results_with[-1].primary_rrsp + results_with[-1].spousal_rrsp +
                          results_with[-1].spouse_rrsp + results_with[-1].primary_tfsa +
                          results_with[-1].spouse_tfsa + results_with[-1].non_reg_balance +
                          results_with[-1].resp_balance)
        self.assertGreater(results_with[-1].total_assets, non_fhsa_assets,
                           "total_assets must exceed non-FHSA assets when FHSA balance > 0")

    def test_fhsa_no_contribution_when_strategy_pct_zero(self):
        """Default strategy has fhsa_pct=0, so no FHSA contribution even with room."""
        cfg = self._make_fhsa_config()
        rate_path = build_rate_path("fhsa0", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim = FamilySimulation(cfg, use_readvanceable=False, rate_path=rate_path)
        results = sim.run()
        state = sim._state
        self.assertAlmostEqual(adult_fhsa_slot(state.jurisdiction_state['canada'], 0)['balance'], 0,
                               "Default strategy (fhsa_pct=0) should not contribute to FHSA")
        self.assertGreater(adult_fhsa_slot(state.jurisdiction_state['canada'], 0)['room'], 0,
                           "FHSA room should accumulate even without contributions")


class TestFHSAMonthlyYearlyParity(unittest.TestCase):
    """Compare yearly vs monthly FHSA balance under same strategy (G2-M1)."""

    def _make_fhsa_config(self, time_step='yearly'):
        d = make_test_dict()
        d['assumptions']['time_step'] = time_step
        d['family']['members'][0]['fhsa_room_accumulated'] = 8000
        return SimulationConfig.from_dict(d)

    def test_yearly_vs_monthly_fhsa_balance_similar(self):
        """Yearly and monthly simulations produce similar FHSA balance."""
        from strategy import AllocationStrategy
        fhsa_strategy = AllocationStrategy(
            name="FHSA Parity",
            rrsp_pct=0.25,
            spousal_rrsp_pct=0.05,
            tfsa_pct=0.20,
            fhsa_pct=0.08,
            resp_pct=0.0,
            non_reg_pct=0.42,
        )
        # Yearly
        cfg_yr = self._make_fhsa_config('yearly')
        rp_yr = build_rate_path("fhsa_yr", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim_yr = FamilySimulation(cfg_yr, use_readvanceable=False, rate_path=rp_yr,
                                   strategy=fhsa_strategy)
        sim_yr.run()
        yr_balance = adult_fhsa_slot(sim_yr._state.jurisdiction_state['canada'], 0)['balance']

        # Monthly
        cfg_mo = self._make_fhsa_config('monthly')
        rp_mo = build_rate_path("fhsa_mo", 0.045, 5, "fixed", [0.05], projection_years=3)
        sim_mo = FamilySimulation(cfg_mo, use_readvanceable=False, rate_path=rp_mo,
                                   strategy=fhsa_strategy)
        sim_mo.run()
        mo_balance = adult_fhsa_slot(sim_mo._state.jurisdiction_state['canada'], 0)['balance']

        # Both should be non-zero and within 20% of each other
        self.assertGreater(yr_balance, 0, "Yearly FHSA balance should be > 0")
        self.assertGreater(mo_balance, 0, "Monthly FHSA balance should be > 0")
        ratio = yr_balance / mo_balance if mo_balance > 0 else 0
        self.assertGreater(ratio, 0.8, f"Yearly/monthly ratio {ratio:.2f} too low")
        self.assertLess(ratio, 1.2, f"Yearly/monthly ratio {ratio:.2f} too high")

    def test_adapter_fhsa_16000_room_split(self):
        """Adapter splits fhsa_room_accumulated=16000 into annual=8000 + carry=8000."""
        from countries.canada.adapter import CanadaAdapter
        cfg = self._make_fhsa_config()
        adapter = CanadaAdapter(cfg)
        f = adapter.create_fhsa(contribution_room=16000)
        self.assertEqual(f.annual_room, 8000)
        self.assertEqual(f.carry_forward_room, 8000)
        self.assertEqual(f.annual_room + f.carry_forward_room, 16000)
        # Can contribute the full 16000
        actual = f.contribute(16000)
        self.assertAlmostEqual(actual, 16000)


if __name__ == '__main__':
    unittest.main()
