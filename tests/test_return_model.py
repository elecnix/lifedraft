#!/usr/bin/env python3
"""Unit tests for return_model.py - DP#21 pluggable return models.

Tests verify:
- FixedReturn: constant rate every year
- VariableReturn: per-year rates with fallback
- StochasticReturn: reproducible seeds (DP#23), different seeds diverge
- MeanRevertingReturn: rates stay within bounds, revert toward mean
- build_return_model factory function
- Return values are reasonable (no NaN, no infinity, within bounds)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from return_model import (
    ReturnModel, FixedReturn, VariableReturn,
    StochasticReturn, MeanRevertingReturn,
    build_return_model,
)


class TestFixedReturn(unittest.TestCase):
    def test_constant_rate(self):
        m = FixedReturn(0.07)
        self.assertAlmostEqual(m.return_for_year(0), 0.07)
        self.assertAlmostEqual(m.return_for_year(99), 0.07)

    def test_zero_rate(self):
        m = FixedReturn(0.0)
        self.assertAlmostEqual(m.return_for_year(0), 0.0)

    def test_negative_rate(self):
        m = FixedReturn(-0.05)
        self.assertAlmostEqual(m.return_for_year(0), -0.05)

    def test_default_rate(self):
        m = FixedReturn()
        self.assertAlmostEqual(m.return_for_year(0), 0.07)


class TestVariableReturn(unittest.TestCase):
    def test_per_year_rates(self):
        m = VariableReturn(rates=[0.05, 0.06, 0.08])
        self.assertAlmostEqual(m.return_for_year(0), 0.05)
        self.assertAlmostEqual(m.return_for_year(1), 0.06)
        self.assertAlmostEqual(m.return_for_year(2), 0.08)

    def test_fallback_after_list(self):
        m = VariableReturn(rates=[0.05], fallback=0.07)
        self.assertAlmostEqual(m.return_for_year(0), 0.05)
        self.assertAlmostEqual(m.return_for_year(1), 0.07)

    def test_empty_rates_uses_fallback(self):
        m = VariableReturn(rates=[], fallback=0.06)
        self.assertAlmostEqual(m.return_for_year(0), 0.06)


class TestStochasticReturn(unittest.TestCase):
    def test_reproducible_with_same_seed(self):
        """DP#23: same seed → same sequence."""
        m1 = StochasticReturn(mean=0.07, sigma=0.15, seed=42)
        m2 = StochasticReturn(mean=0.07, sigma=0.15, seed=42)
        for y in range(20):
            self.assertAlmostEqual(m1.return_for_year(y), m2.return_for_year(y))

    def test_different_seed_different_path(self):
        """DP#23: different seed → different sequence."""
        m1 = StochasticReturn(mean=0.07, sigma=0.15, seed=42)
        m2 = StochasticReturn(mean=0.07, sigma=0.15, seed=99)
        diffs = sum(1 for y in range(20) if m1.return_for_year(y) != m2.return_for_year(y))
        self.assertGreater(diffs, 0)

    def test_returns_within_bounds(self):
        m = StochasticReturn(mean=0.07, sigma=0.15, seed=42, n_years=100)
        for y in range(100):
            r = m.return_for_year(y)
            self.assertGreaterEqual(r, -0.30)
            self.assertLessEqual(r, 0.50)

    def test_beyond_n_years_falls_back_to_mean(self):
        m = StochasticReturn(mean=0.07, sigma=0.15, seed=42, n_years=5)
        for y in range(5):
            # Within range: should be pre-generated (may or may not equal mean)
            r = m.return_for_year(y)
            self.assertIsInstance(r, float)
        # Beyond range: falls back to mean
        self.assertAlmostEqual(m.return_for_year(100), 0.07)

    def test_higher_sigma_more_variance(self):
        m_low = StochasticReturn(mean=0.07, sigma=0.05, seed=42, n_years=100)
        m_high = StochasticReturn(mean=0.07, sigma=0.25, seed=42, n_years=100)
        range_low = max(m_low.return_for_year(y) for y in range(100)) - min(m_low.return_for_year(y) for y in range(100))
        range_high = max(m_high.return_for_year(y) for y in range(100)) - min(m_high.return_for_year(y) for y in range(100))
        self.assertGreater(range_high, range_low)


class TestMeanRevertingReturn(unittest.TestCase):
    def test_reproducible_with_same_seed(self):
        """DP#23: same seed → same sequence."""
        m1 = MeanRevertingReturn(seed=42)
        m2 = MeanRevertingReturn(seed=42)
        for y in range(20):
            self.assertAlmostEqual(m1.return_for_year(y), m2.return_for_year(y))

    def test_returns_within_bounds(self):
        m = MeanRevertingReturn(seed=42, n_years=100)
        for y in range(100):
            r = m.return_for_year(y)
            self.assertGreaterEqual(r, -0.15)
            self.assertLessEqual(r, 0.40)

    def test_reverts_toward_mean(self):
        """Over 50 years, the average should be near the long-term mean."""
        m = MeanRevertingReturn(long_term_mean=0.07, seed=42, n_years=100)
        avg = sum(m.return_for_year(y) for y in range(100)) / 100
        # Should be within 3% of mean (statistical test, may rarely fail)
        self.assertAlmostEqual(avg, 0.07, delta=0.03)


class TestBuildReturnModel(unittest.TestCase):
    def test_fixed(self):
        m = build_return_model('fixed', rate=0.05)
        self.assertIsInstance(m, ReturnModel)
        self.assertEqual(m.type, 'fixed')
        self.assertAlmostEqual(m.return_for_year(0), 0.05)

    def test_variable(self):
        m = build_return_model('variable', rates_list=[0.04, 0.06])
        self.assertIsInstance(m, ReturnModel)
        self.assertEqual(m.type, 'variable')
        self.assertAlmostEqual(m.return_for_year(0), 0.04)

    def test_stochastic(self):
        m = build_return_model('stochastic', mean=0.08, sigma=0.12, seed=100)
        self.assertIsInstance(m, ReturnModel)
        self.assertEqual(m.type, 'stochastic')

    def test_mean_reverting(self):
        m = build_return_model('mean_reverting', mean=0.06, seed=50)
        self.assertIsInstance(m, ReturnModel)
        self.assertEqual(m.type, 'mean_reverting')

    def test_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            build_return_model('nonexistent')


class TestReturnEngine(unittest.TestCase):
    """Test ReturnEngine dispatch (DP#8: data + engine pattern)."""

    def test_engine_fixed(self):
        from return_model import ReturnEngine
        model = ReturnModel(type="fixed", rate=0.06)
        self.assertAlmostEqual(ReturnEngine.return_for_year(model, 0), 0.06)
        self.assertAlmostEqual(ReturnEngine.return_for_year(model, 99), 0.06)

    def test_engine_variable(self):
        from return_model import ReturnEngine
        model = ReturnModel(type="variable", rates=[0.05, 0.06], fallback=0.07)
        self.assertAlmostEqual(ReturnEngine.return_for_year(model, 0), 0.05)
        self.assertAlmostEqual(ReturnEngine.return_for_year(model, 2), 0.07)

    def test_engine_stressed(self):
        from return_model import ReturnEngine
        model = ReturnModel(type="stressed", rate=0.07, crash_year=1, crash_pct=-0.40, recovery_years=5)
        self.assertAlmostEqual(ReturnEngine.return_for_year(model, 0), 0.07)
        self.assertAlmostEqual(ReturnEngine.return_for_year(model, 1), -0.40)

    def test_engine_unknown_type_raises(self):
        from return_model import ReturnEngine
        model = ReturnModel(type="invalid_type")
        with self.assertRaises(ValueError):
            ReturnEngine.return_for_year(model, 0)


class TestReturnModelRoundTrip(unittest.TestCase):
    """Test to_dict/from_dict round-trip (DP#24)."""

    def test_fixed_round_trip(self):
        original = ReturnModel(type="fixed", rate=0.065)
        restored = ReturnModel.from_dict(original.to_dict())
        self.assertEqual(restored.type, "fixed")
        self.assertAlmostEqual(restored.rate, 0.065)

    def test_stressed_round_trip(self):
        original = ReturnModel(type="stressed", rate=0.07, crash_year=3, crash_pct=-0.30, recovery_years=4)
        restored = ReturnModel.from_dict(original.to_dict())
        self.assertEqual(restored.type, "stressed")
        self.assertEqual(restored.crash_year, 3)

    def test_stochastic_round_trip(self):
        original = ReturnModel(type="stochastic", mean=0.08, sigma=0.12, seed=99, n_years=30)
        d = original.to_dict()
        restored = ReturnModel.from_dict(d)
        self.assertEqual(restored.type, "stochastic")
        self.assertAlmostEqual(restored.mean, 0.08)


if __name__ == '__main__':
    unittest.main()