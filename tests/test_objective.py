#!/usr/bin/env python3
"""Unit tests for objective.py — DP#22 pluggable objective functions.

Tests verify:
- Each objective returns a scalar score
- MAX_NET_BENEFIT correctly sums assets - debt + tax savings
- MAX_TERMINAL_WEALTH returns assets - debt
- MIN_RETIREMENT_GAP returns net - target
- MIN_YEARS_TO_MORTGAGE_FREE returns negative years
- Custom objectives work via ObjectiveFunction constructor
- get_objective lookups work
- Empty results return 0
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from simulation import YearResult
from objective import (
    ObjectiveFunction,
    MAX_NET_BENEFIT, MAX_TERMINAL_WEALTH, MAX_TAX_SAVINGS,
    MIN_RETIREMENT_GAP, MIN_YEARS_TO_MORTGAGE_FREE, MAX_SM_SAVINGS,
    OBJECTIVES, get_objective,
)


def _make_results(**overrides) -> list:
    """Build a list of YearResult with fabricated data (DP#4)."""
    base = YearResult(total_assets=500000, total_debt=100000,
                      rrsp_tax_savings=20000, readvance_tax_savings=5000,
                      mortgage_balance=100000, readvance_interest=10000)
    for k, v in overrides.items():
        setattr(base, k, v)
    return [base]


def _make_multi_year(n_years=5) -> list:
    """Build n years of results with increasing assets."""
    results = []
    for i in range(n_years):
        yr = YearResult(
            year=i + 1,
            total_assets=300000 + i * 50000,
            total_debt=200000 - i * 20000,
            rrsp_tax_savings=5000 + i * 1000,
            readvance_tax_savings=1000,
            mortgage_balance=200000 - i * 20000,
            readvance_interest=5000,
        )
        results.append(yr)
    return results


class TestObjectiveFunction(unittest.TestCase):
    def test_custom_objective(self):
        obj = ObjectiveFunction(
            name="test",
            fn=lambda results, cfg: results[-1].total_assets,
        )
        score = obj.evaluate(_make_results())
        self.assertEqual(score, 500000)
    
    def test_empty_results(self):
        obj = ObjectiveFunction(name="test", fn=lambda r, c: 999)
        score = obj.evaluate([], {})
        self.assertEqual(score, 0.0)


class TestMaxNetBenefit(unittest.TestCase):
    def test_basic(self):
        results = _make_results()
        score = MAX_NET_BENEFIT.evaluate(results)
        # assets(500k) - debt(100k) + rrsp_savings(20k) + sm_savings(5k)
        expected = 500000 - 100000 + 20000 + 5000
        self.assertAlmostEqual(score, expected)
    
    def test_multi_year(self):
        results = _make_multi_year(5)
        score = MAX_NET_BENEFIT.evaluate(results)
        self.assertIsInstance(score, (int, float))
        self.assertGreater(score, 0)


class TestMaxTerminalWealth(unittest.TestCase):
    def test_basic(self):
        results = _make_results()
        score = MAX_TERMINAL_WEALTH.evaluate(results)
        self.assertAlmostEqual(score, 500000 - 100000)  # assets - debt


class TestMaxTaxSavings(unittest.TestCase):
    def test_basic(self):
        results = _make_results()
        score = MAX_TAX_SAVINGS.evaluate(results)
        self.assertAlmostEqual(score, 20000 + 5000)  # rrsp + sm


class TestMinRetirementGap(unittest.TestCase):
    def test_surplus(self):
        results = _make_results(total_assets=600000, total_debt=100000)
        score = MIN_RETIREMENT_GAP.evaluate(results, {'retirement_target': 400000})
        self.assertAlmostEqual(score, 100000)  # 600k - 100k - 400k
    
    def test_gap(self):
        results = _make_results(total_assets=300000, total_debt=100000)
        score = MIN_RETIREMENT_GAP.evaluate(results, {'retirement_target': 500000})
        self.assertAlmostEqual(score, -300000)  # 300k - 100k - 500k
    
    def test_default_target(self):
        results = _make_results()
        score = MIN_RETIREMENT_GAP.evaluate(results, {})
        # Default target 500k; net = 500k - 100k = 400k; gap = -100k
        self.assertAlmostEqual(score, -100000)


class TestMinYearsToMortgageFree(unittest.TestCase):
    def test_paid_off_year_3(self):
        results = []
        for i in range(5):
            yr = YearResult(year=i + 1, mortgage_balance=100000 - i * 50000)
            results.append(yr)
        score = MIN_YEARS_TO_MORTGAGE_FREE.evaluate(results)
        # Year 1: 100k, Year 2: 50k, Year 3: 0  → paid off in year 3
        self.assertEqual(score, -3)
    
    def test_never_paid_off(self):
        results = [YearResult(year=i + 1, mortgage_balance=200000) for i in range(5)]
        score = MIN_YEARS_TO_MORTGAGE_FREE.evaluate(results)
        self.assertEqual(score, -5)


class TestMaxSMSavings(unittest.TestCase):
    def test_basic(self):
        results = _make_results()
        score = MAX_SM_SAVINGS.evaluate(results)
        self.assertAlmostEqual(score, 5000)


class TestGetObjective(unittest.TestCase):
    def test_known_objective(self):
        obj = get_objective('max_net_benefit')
        self.assertEqual(obj.name, 'max_net_benefit')
    
    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            get_objective('nonexistent')
    
    def test_all_objectives_callable(self):
        for name, obj in OBJECTIVES.items():
            score = obj.evaluate(_make_results())
            self.assertIsInstance(score, (int, float), f"{name} returned {type(score)}")


if __name__ == '__main__':
    unittest.main()