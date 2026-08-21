#!/usr/bin/env python3
"""Tests for Issue #95: Deprecate SimulationConfig.investment_return (DP#21).

Per DP#21, investment_return is deprecated in favor of return_model_data.
The float field should not permanently coexist with the ReturnModel field.
When both are provided, return_model_data takes precedence.

DP#9: Remove investment_return in v2.0.
"""

import unittest
import warnings

from simulation_config import SimulationConfig
from return_model import FixedReturn, VariableReturn, build_return_model_from_config


class TestInvestmentReturnDeprecation(unittest.TestCase):
    """Test that investment_return is deprecated in favor of return_model_data."""

    def test_default_investment_return_no_warning(self):
        """Default investment_return (0.07) should not trigger a deprecation warning."""
        # No return_model_data, default investment_return — no conflict
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", DeprecationWarning)
            cfg = SimulationConfig()
            self.assertEqual(cfg.investment_return, 0.07)
            self.assertEqual(len(w), 0)

    def test_both_fields_no_warning_return_model_authoritative(self):
        """#260: investment_return is now a thin shim; return_model is the single
        source of truth. Setting both is no longer a conflict and must not warn —
        return_model_data is simply authoritative.
        """
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", DeprecationWarning)
            cfg = SimulationConfig(
                investment_return=0.05,
                return_model_data={'type': 'fixed', 'rate': 0.06},
            )
            self.assertEqual(len(w), 0)
            self.assertEqual(cfg.return_model_data['rate'], 0.06)

    def test_return_model_data_alone_no_warning(self):
        """Using return_model_data without custom investment_return should not warn."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", DeprecationWarning)
            cfg = SimulationConfig(
                return_model_data={'type': 'fixed', 'rate': 0.06},
            )
            self.assertEqual(len(w), 0)

    def test_custom_investment_return_alone_no_warning(self):
        """Custom investment_return without return_model_data should not warn.

        This is to avoid noisy warnings in existing code that hasn't migrated yet.
        The deprecation is documented in the field comment and will be enforced
        in v2.0 when the field is removed.
        """
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always", DeprecationWarning)
            cfg = SimulationConfig(investment_return=0.05)
            self.assertEqual(len(w), 0)


class TestReturnModelDataPrecedence(unittest.TestCase):
    """Test that return_model_data takes precedence over investment_return."""

    def test_simulation_prefers_return_model_data(self):
        """FamilySimulation should use return_model_data when available."""
        from simulation import FamilySimulation

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", DeprecationWarning)
            cfg = SimulationConfig(
                investment_return=0.05,
                return_model_data={'type': 'fixed', 'rate': 0.08},
                house_value=500000,
                mortgage_balance=200000,
                margin_available=50000,
            )
        sim = FamilySimulation(cfg)
        # Should use 0.08 from return_model_data, not 0.05 from investment_return
        self.assertAlmostEqual(sim.return_model.return_for_year(0), 0.08)

    def test_simulation_falls_back_to_investment_return(self):
        """FamilySimulation should fall back to investment_return when no return_model_data."""
        from simulation import FamilySimulation

        cfg = SimulationConfig(
            investment_return=0.05,
            house_value=500000,
            mortgage_balance=200000,
            margin_available=50000,
        )
        sim = FamilySimulation(cfg)
        # Should fall back to 0.05 from investment_return
        self.assertAlmostEqual(sim.return_model.return_for_year(0), 0.05)

    def test_optimizer_prefers_return_model_data(self):
        """GridOptimizer should use return_model_data when available."""
        from optimizer import GridOptimizer

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always", DeprecationWarning)
            cfg = SimulationConfig(
                investment_return=0.05,
                return_model_data={'type': 'fixed', 'rate': 0.09},
                house_value=500000,
                mortgage_balance=200000,
                margin_available=50000,
            )
        opt = GridOptimizer(cfg)
        self.assertAlmostEqual(opt.return_model.return_for_year(0), 0.09)

    def test_optimizer_falls_back_to_investment_return(self):
        """GridOptimizer should fall back to investment_return when no return_model_data."""
        from optimizer import GridOptimizer

        cfg = SimulationConfig(
            investment_return=0.05,
            house_value=500000,
            mortgage_balance=200000,
            margin_available=50000,
        )
        opt = GridOptimizer(cfg)
        self.assertAlmostEqual(opt.return_model.return_for_year(0), 0.05)


if __name__ == '__main__':
    unittest.main()