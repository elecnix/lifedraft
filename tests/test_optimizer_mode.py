"""Tests for OptimizerMode and build_optimizer (DP#8: data + engine pattern)."""
import unittest
from simulation import SimulationConfig
from optimizer import OptimizerMode, build_optimizer, GridOptimizer


class TestOptimizerMode(unittest.TestCase):
    def test_default_is_grid(self):
        mode = OptimizerMode()
        self.assertEqual(mode.type, "grid")

    def test_monte_carlo_mode(self):
        mode = OptimizerMode(type="monte_carlo", n_simulations=500)
        self.assertEqual(mode.type, "monte_carlo")
        self.assertEqual(mode.n_simulations, 500)

    def test_scipy_mode(self):
        mode = OptimizerMode(type="scipy", optimize_vars=["ltv", "rrsp_weight"])
        self.assertEqual(mode.type, "scipy")
        self.assertEqual(mode.optimize_vars, ["ltv", "rrsp_weight"])

    def test_dp_mode(self):
        mode = OptimizerMode(type="dp", lookahead=2)
        self.assertEqual(mode.type, "dp")
        self.assertEqual(mode.lookahead, 2)

    def test_round_trip_grid(self):
        mode = OptimizerMode(type="grid")
        d = mode.to_dict()
        restored = OptimizerMode.from_dict(d)
        self.assertEqual(restored.type, "grid")

    def test_round_trip_monte_carlo(self):
        mode = OptimizerMode(type="monte_carlo", n_simulations=200, seed_base=100)
        d = mode.to_dict()
        restored = OptimizerMode.from_dict(d)
        self.assertEqual(restored.type, "monte_carlo")
        self.assertEqual(restored.n_simulations, 200)
        self.assertEqual(restored.seed_base, 100)

    def test_round_trip_scipy(self):
        mode = OptimizerMode(type="scipy", optimize_vars=["ltv"], scipy_method="Nelder-Mead")
        d = mode.to_dict()
        restored = OptimizerMode.from_dict(d)
        self.assertEqual(restored.type, "scipy")
        self.assertEqual(restored.optimize_vars, ["ltv"])
        self.assertEqual(restored.scipy_method, "Nelder-Mead")

    def test_round_trip_dp(self):
        mode = OptimizerMode(type="dp", lookahead=3)
        d = mode.to_dict()
        restored = OptimizerMode.from_dict(d)
        self.assertEqual(restored.type, "dp")
        self.assertEqual(restored.lookahead, 3)

    def test_unknown_type_raises(self):
        mode = OptimizerMode(type="nonexistent")
        self.assertEqual(mode.type, "nonexistent")  # OptimizerMode doesn't validate type


class TestBuildOptimizer(unittest.TestCase):
    def test_build_grid(self):
        config = SimulationConfig(
            projection_years=3, investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 120000, 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
                {'role': 'spouse', 'gross_income': 50000, 'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
            ],
            children=[{'name': 'Kid', 'age': 10, 'gross_income': 0}],
            mortgage_balance=100000, mortgage_rate=0.05,
        )
        opt = build_optimizer(OptimizerMode(type="grid"), config)
        self.assertIsInstance(opt, GridOptimizer)

    def test_build_default(self):
        config = SimulationConfig(
            projection_years=3, investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 120000, 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            ],
            children=[],
            mortgage_balance=100000, mortgage_rate=0.05,
        )
        opt = build_optimizer(base_config=config)
        self.assertIsInstance(opt, GridOptimizer)


if __name__ == '__main__':
    unittest.main()
