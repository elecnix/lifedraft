#!/usr/bin/env python3
"""Comprehensive tests for low-coverage modules: stress_scenarios, account_models, fhsa, strategy, attribution.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/test_remaining_coverage.py -v

Epic #603 Track C Phase 2b: this file used to also test
``module_registry.check_auto_includes``/``ModuleTrigger`` -- deleted along
with those symbols (zero production callers, operated on the legacy input
shape's auto-include triggers). See ``module_registry.py``'s module
docstring.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from stress_scenarios import (
    StressPath, STRESS_BASELINE, STRESS_2008_CRASH, STRESS_RATE_SPIKE,
    STRESS_STAGFLATION, STRESS_COMBINED, ALL_STRESS_PATHS
)
from countries.canada.account_models import RRSPAccount, TSFAccount, RESPAccount
from countries.canada.rate_model import ReadvanceableMortgage
from countries.canada.fhsa import FHSAAccount
from strategy import AllocationStrategy, AllocationResult, FamilyState, StrategyEngine
from countries.canada.attribution import attribution_planning_summary


# =============================================================================
# stress_scenarios.py
# =============================================================================

class TestStressPath(unittest.TestCase):
    def test_years_property(self):
        sp = StressPath(name="test", investment_return_path=[0.07]*5, heloc_rate_path=[0.05]*3)
        self.assertEqual(sp.years, 5)

    def test_fill_returns_short(self):
        sp = StressPath(name="test", investment_return_path=[-0.40, 0.15], heloc_rate_path=[0.05])
        filled = sp.fill_returns(10)
        self.assertEqual(len(filled), 10)
        self.assertAlmostEqual(filled[0], -0.40)
        self.assertAlmostEqual(filled[2], 0.07)

    def test_fill_returns_exact(self):
        sp = StressPath(name="test", investment_return_path=[0.07]*5, heloc_rate_path=[0.05]*5)
        self.assertEqual(len(sp.fill_returns(5)), 5)

    def test_fill_rates(self):
        sp = StressPath(name="test", investment_return_path=[0.07], heloc_rate_path=[0.072, 0.035])
        filled = sp.fill_rates(5, default_rate=0.0495)
        self.assertEqual(len(filled), 5)
        self.assertAlmostEqual(filled[2], 0.0495)

    def test_average_return(self):
        sp = StressPath(name="test", investment_return_path=[-0.40, 0.15, 0.10], heloc_rate_path=[0.05]*3)
        self.assertAlmostEqual(sp.average_return(3), (-0.40 + 0.15 + 0.10) / 3, places=4)

    def test_average_rate(self):
        sp = StressPath(name="test", investment_return_path=[0.07]*3, heloc_rate_path=[0.072, 0.035, 0.030])
        self.assertAlmostEqual(sp.average_rate(3), (0.072 + 0.035 + 0.030) / 3, places=4)


class TestPredefinedStressPaths(unittest.TestCase):
    def test_all_defined(self):
        self.assertEqual(len(ALL_STRESS_PATHS), 7)

    def test_baseline_positive(self):
        self.assertGreater(STRESS_BASELINE.average_return(), 0)

    def test_crash_negative_yr1(self):
        self.assertLess(STRESS_2008_CRASH.investment_return_path[0], 0)

    def test_rate_spike(self):
        self.assertGreater(STRESS_RATE_SPIKE.heloc_rate_path[2], STRESS_BASELINE.heloc_rate_path[2])

    def test_stagflation_low(self):
        self.assertLess(STRESS_STAGFLATION.average_return(), STRESS_BASELINE.average_return())

    def test_combined_worse(self):
        self.assertLess(STRESS_COMBINED.average_return(), STRESS_BASELINE.average_return())


# =============================================================================
# module_registry.py
# =============================================================================



# =============================================================================
# account_models.py
# =============================================================================

class TestRRSPAccountFull(unittest.TestCase):
    def test_contribute_within_room(self):
        acc = RRSPAccount(contribution_room=50000)
        actual, _ = acc.contribute(10000)
        self.assertAlmostEqual(actual, 10000)

    def test_contribute_exceeds_room(self):
        acc = RRSPAccount(contribution_room=5000)
        actual, _ = acc.contribute(10000)
        self.assertAlmostEqual(actual, 5000)

    def test_grow(self):
        acc = RRSPAccount(balance=10000)
        acc.grow(0.06)
        self.assertAlmostEqual(acc.balance, 10600, places=0)

    def test_add_annual_room(self):
        acc = RRSPAccount()
        acc.add_annual_room(120000)
        self.assertGreater(acc.contribution_room, 0)


class TestTSFAccountFull(unittest.TestCase):
    def test_contribute(self):
        acc = TSFAccount(contribution_room=40000)
        actual, _ = acc.contribute(5000)
        self.assertAlmostEqual(actual, 5000)

    def test_contribute_exceeds(self):
        acc = TSFAccount(contribution_room=3000)
        actual, _ = acc.contribute(5000)
        self.assertAlmostEqual(actual, 3000)

    def test_withdraw(self):
        acc = TSFAccount(balance=10000, contribution_room=0)
        actual = acc.withdraw(5000)
        self.assertAlmostEqual(actual, 5000)

    def test_withdraw_more(self):
        acc = TSFAccount(balance=3000, contribution_room=0)
        actual = acc.withdraw(5000)
        self.assertAlmostEqual(actual, 3000)

    def test_add_annual_room(self):
        acc = TSFAccount(annual_room=7000)
        acc.add_annual_room()
        self.assertGreater(acc.contribution_room, 0)


class TestRESPAccountFull(unittest.TestCase):
    def test_contribute(self):
        acc = RESPAccount(balance=0, contributions_total=0)
        actual, grants = acc.contribute(2500)
        self.assertAlmostEqual(actual, 2500)

    def test_eap_tax(self):
        acc = RESPAccount(balance=45000)
        tax = acc.eap_tax(0.15)
        self.assertGreater(tax, 0)


class TestReadvanceableMortgageFull(unittest.TestCase):
    def test_calculate_interest(self):
        sm = ReadvanceableMortgage(heloc_rate=0.05)
        sm.heloc_balance = 100000
        interest, _ = sm.calculate_interest(year=0)
        self.assertAlmostEqual(interest, 5000)

    def test_calculate_interest_uses_rate_path_when_supplied(self):
        # When a RatePath is supplied, the HELOC rate for the year comes from
        # the path, not the static heloc_rate (variable-rate HELOC scenario).
        # This is the readvanceable mortgage's rate_path branch (#723): the
        # class now lives in rate_model.py alongside the RatePath types it
        # consumes, so exercising the branch here keeps both covered.
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('heloc_variable', initial_rate=0.03,
                            term_years=5, rate_type='fixed')
        sm = ReadvanceableMortgage(heloc_rate=0.05)
        sm.heloc_balance = 100000
        interest, _ = sm.calculate_interest(year=0, rate_path=rp)
        self.assertAlmostEqual(interest, 3000.0)  # 100000 * 0.03, not 0.05

    def test_readvance(self):
        sm = ReadvanceableMortgage(heloc_rate=0.05)
        sm.readvance(5000)
        self.assertAlmostEqual(sm.heloc_balance, 5000)

    def test_grow_investment(self):
        sm = ReadvanceableMortgage(heloc_rate=0.05)
        sm.investment_balance = 10000
        sm.grow_investment(0.06)
        self.assertGreater(sm.investment_balance, 10000)

    def test_summary(self):
        sm = ReadvanceableMortgage(heloc_rate=0.05)
        sm.heloc_balance = 50000
        sm.investment_balance = 50000
        summary = sm.annual_summary()
        self.assertIn('heloc_balance', summary)


# =============================================================================
# fhsa.py
# =============================================================================

class TestFHSAAccountFull(unittest.TestCase):
    def test_contribute(self):
        f = FHSAAccount()
        f.annual_room = 8000
        f.carry_forward_room = 8000
        f.open_year = 2026
        actual = f.contribute(8000)
        self.assertAlmostEqual(actual, 8000)

    def test_contribute_exceeds_annual(self):
        f = FHSAAccount()
        f.annual_room = 8000
        f.carry_forward_room = 8000
        room_before = f.annual_room + f.carry_forward_room
        f.open_year = 2026
        actual = f.contribute(10000)
        # With contribution_room=16000 (8000 annual + 8000 carry-forward, capped),
        # can contribute up to total room before contribution
        self.assertLessEqual(actual, room_before)
        self.assertEqual(actual, 10000)

    def test_add_annual_room(self):
        f = FHSAAccount()
        f.add_annual_room()
        self.assertGreater(f.annual_room, 0)

    def test_grow(self):
        f = FHSAAccount()
        f.balance = 10000
        f.grow(0.06)
        self.assertGreater(f.balance, 10000)

    def test_qualifying_withdrawal(self):
        f = FHSAAccount()
        f.balance = 30000
        result = f.qualifying_withdrawal(2028)
        self.assertGreaterEqual(result['amount'], 0)

    def test_non_qualifying_withdrawal(self):
        f = FHSAAccount()
        f.balance = 30000
        result = f.non_qualifying_withdrawal(2029)
        self.assertIn('amount', result)

    def test_transfer_to_rrsp(self):
        f = FHSAAccount()
        f.balance = 15000
        self.assertAlmostEqual(f.transfer_to_rrsp(), 15000)

    def test_must_close(self):
        f = FHSAAccount()
        f.open_year = 2020
        self.assertTrue(f.must_close(2036, 1990))

    def test_tax_savings(self):
        f = FHSAAccount()
        savings = f.tax_savings(8000, 0.43)
        self.assertAlmostEqual(savings, 3440)

    def test_summary(self):
        f = FHSAAccount()
        f.balance = 20000
        s = f.summary()
        self.assertIn('balance', s)


# =============================================================================
# strategy.py
# =============================================================================

class TestAllocationResultFull(unittest.TestCase):
    def test_total_allocated(self):
        a = AllocationResult(primary_rrsp=5000, spousal_rrsp=2000,
                              spouse_rrsp=1000, primary_tfsa=3000,
                              spouse_tfsa=2000, resp=500, non_reg=1500)
        self.assertAlmostEqual(a.total_allocated, 15000)

    def test_as_dict(self):
        a = AllocationResult(primary_rrsp=5000, spousal_rrsp=2000,
                              spouse_rrsp=1000, primary_tfsa=3000,
                              spouse_tfsa=2000, resp=500, non_reg=1500)
        d = a.as_dict()
        self.assertEqual(d['primary_rrsp'], 5000)


class TestAllocationStrategyFull(unittest.TestCase):
    def test_total_pct(self):
        s = AllocationStrategy(name="test", rrsp_pct=0.40, spousal_rrsp_pct=0.10,
                               tfsa_pct=0.20, resp_pct=0.05, non_reg_pct=0.25)
        self.assertAlmostEqual(s.total_pct, 1.0)

    def test_validate_valid(self):
        s = AllocationStrategy(name="test", rrsp_pct=0.40, spousal_rrsp_pct=0.10,
                               tfsa_pct=0.20, resp_pct=0.05, non_reg_pct=0.25)
        errors = s.validate()
        self.assertEqual(len(errors), 0)

    def test_validate_over_100(self):
        s = AllocationStrategy(name="bad", rrsp_pct=0.60, spousal_rrsp_pct=0.30,
                               tfsa_pct=0.30, resp_pct=0.10, non_reg_pct=0.20)
        errors = s.validate()
        self.assertGreater(len(errors), 0)


class TestStrategyEngineFillRoomFull(unittest.TestCase):
    def test_fill_room(self):
        strat = AllocationStrategy(name="fill", rrsp_pct=0.40, spousal_rrsp_pct=0.10,
                                   tfsa_pct=0.20, resp_pct=0.05, non_reg_pct=0.25)
        engine = StrategyEngine(strat)
        state = FamilyState(
            primary_income=120000, spouse_income=50000,
            primary_marginal_rate=0.45, spouse_marginal_rate=0.30,
            primary_rrsp_room=50000, spouse_rrsp_room=20000,
            primary_tfsa_room=40000, spouse_tfsa_room=40000,
            resp_eligible_children=0, annual_savings=0,
            bracket_gap=0.15,
        )
        alloc = engine.fill_room(50000, state)
        total = alloc.total_allocated
        self.assertGreater(total, 0)
        self.assertLessEqual(total, 50000)


# =============================================================================
# attribution.py
# =============================================================================

class TestAttributionPlanningSummaryFull(unittest.TestCase):
    def test_basic_summary(self):
        result = attribution_planning_summary(
            spouse_age=46, child_ages=[10, 14],
            has_prescribed_rate_loan=True,
            interest_paid_on_time=True,
        )
        self.assertIn('spousal_attribution', result)
        self.assertIn('minor_child_attribution', result)


if __name__ == '__main__':
    unittest.main()