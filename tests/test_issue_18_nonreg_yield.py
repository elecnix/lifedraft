#!/usr/bin/env python3
"""Test that non-reg investment yield is configurable, not hardcoded as 2%.

Issue #18: The Quebec interest deduction limit calculation used a hardcoded
0.02 yield, violating DP#2 (configuration belongs in input, not in code).

These tests verify:
1. SimulationConfig.non_reg_yield_rate is configurable
2. simulation_state uses config.non_reg_yield_rate instead of hardcoded 0.02
3. simulation.py uses cfg.non_reg_yield_rate instead of hardcoded 0.02
4. compute_sm_qc_benefit accepts non_reg_yield_rate parameter
5. Different yield rates produce different Quebec deduction amounts

Run with: python3 -m pytest tests/test_issue_18_nonreg_yield.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch, MagicMock

from simulation_config import SimulationConfig
from countries.canada.provinces.quebec.quebec_deduction import (
    compute_sm_qc_benefit,
    quebec_interest_deduction,
)


class TestNonRegYieldConfigurability(unittest.TestCase):
    """DP#2: Configuration belongs in input, not in code."""

    def test_config_has_non_reg_yield_rate_field(self):
        """SimulationConfig should have a non_reg_yield_rate field."""
        config = SimulationConfig()
        self.assertTrue(hasattr(config, 'non_reg_yield_rate'),
                        "SimulationConfig must have non_reg_yield_rate field")

    def test_config_default_yield_is_2_percent(self):
        """Default non_reg_yield_rate should be 0.02 (backward compatible)."""
        config = SimulationConfig()
        self.assertAlmostEqual(config.non_reg_yield_rate, 0.02)

    def test_config_yield_can_be_overridden(self):
        """non_reg_yield_rate should be configurable from dict."""
        config = SimulationConfig.from_dict({
            'assumptions': {'non_reg_yield_rate': 0.04}
        })
        self.assertAlmostEqual(config.non_reg_yield_rate, 0.04)

    def test_config_yield_roundtrip(self):
        """non_reg_yield_rate should round-trip through to_dict/from_dict."""
        config = SimulationConfig(non_reg_yield_rate=0.05)
        d = config.to_dict()
        self.assertAlmostEqual(d['assumptions']['non_reg_yield_rate'], 0.05)
        config2 = SimulationConfig.from_dict(d)
        self.assertAlmostEqual(config2.non_reg_yield_rate, 0.05)

    def test_different_yield_changes_deduction(self):
        """Higher yield rate should produce more Quebec deduction room.

        A $200k non-reg portfolio at 2% = $4k investment income,
        but at 4% = $8k. This directly affects how much HELOC interest
        can be deducted in Quebec.
        """
        # At 2% yield: $200k * 0.02 = $4k investment income
        result_2pct = compute_sm_qc_benefit(
            readvance_heloc_balance=200000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=200000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.02,
        )
        # At 4% yield: $200k * 0.04 = $8k investment income
        result_4pct = compute_sm_qc_benefit(
            readvance_heloc_balance=200000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=200000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.04,
        )
        # Higher yield should produce more QC deduction
        # (investment income limit is higher, so more interest is deductible)
        self.assertGreater(result_4pct['qc_deductible'], result_2pct['qc_deductible'])

    def test_sm_qc_benefit_default_yield_is_2_percent(self):
        """compute_sm_qc_benefit should default to 2% yield (backward compat)."""
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
        )
        # Default 2% yield on $100k = $2k investment income
        # HELOC interest = $100k * 5% = $5k
        # QC deductible = min($5k, $2k) = $2k
        self.assertAlmostEqual(result['qc_deductible'], 2000.0, places=0)

    def test_zero_yield_produces_zero_deduction(self):
        """At 0% yield, no investment income means no QC deduction."""
        result = compute_sm_qc_benefit(
            readvance_heloc_balance=100000,
            heloc_rate=0.05,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.0,
        )
        # 0% yield = $0 investment income = $0 QC deductible
        # All interest carries forward
        self.assertAlmostEqual(result['qc_deductible'], 0.0, places=0)
        self.assertGreater(result['qc_carry_forward'], 0)


class TestNoHardcodedYieldInSimulationModules(unittest.TestCase):
    """Verify that hardcoded 0.02 yield has been removed from simulation logic."""

    def test_no_hardcoded_yield_in_simulation_state(self):
        """simulation_state.py should not have '* 0.02' for yield."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'simulation_state.py')) as f:
            content = f.read()
        # The pattern "* 0.02" should NOT appear as a yield calculation
        # (We check for the specific pattern that was hardcoded)
        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            if '* 0.02' in line and 'new_sm_investment' in line:
                self.fail(f"Hardcoded yield found at line {i}: {line.strip()}")

    def test_no_hardcoded_yield_in_simulation(self):
        """simulation.py should not have '* 0.02' for yield in QC deduction."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'simulation.py')) as f:
            content = f.read()
        lines = content.split('\n')
        for i, line in enumerate(lines, 1):
            if '* 0.02' in line and 'non_reg_balance' in line:
                self.fail(f"Hardcoded yield found at line {i}: {line.strip()}")


if __name__ == '__main__':
    unittest.main()