#!/usr/bin/env python3
"""Issue #241 (DP#8/DP#10): core reads province and FHSA/CESG limits from
config/data, never from hardcoded literals in jurisdiction-agnostic logic.

Each test locks one fact and one fact only:
- province comes from config, with the historical Quebec default preserved;
- core engine modules no longer carry FHSA/CESG dollar literals in their logic;
- the FHSA/CESG figures used by core are the ones owned by the Canada package.
"""

import ast
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _logic_source(filename: str) -> str:
    """Source of a module with string/byte literals (docstrings, comments
    stripped by the tokenizer would be hard; we strip docstrings + comments)."""
    path = os.path.join(ROOT, filename)
    with open(path) as f:
        src = f.read()
    tree = ast.parse(src)
    # Drop module/function/class docstrings so prose mentions don't trip the lock.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, 'body', [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    getattr(body[0], 'value', None), ast.Constant) and isinstance(
                    body[0].value.value, str):
                body[0].value.value = ''
    # ast.unparse drops comments entirely — exactly what we want for logic.
    return ast.unparse(tree)


class TestProvinceFromConfig(unittest.TestCase):
    """province is config data (DP#8), not a hardcoded literal in core."""

    def test_config_default_preserves_quebec(self):
        from simulation_config import SimulationConfig
        cfg = SimulationConfig()
        self.assertEqual(cfg.province, 'quebec')
        self.assertEqual(cfg.country, 'canada')

    def test_config_reads_province_from_tax_section(self):
        from simulation_config import SimulationConfig
        cfg = SimulationConfig.from_dict({'tax': {'province': 'on', 'country': 'canada'}})
        self.assertEqual(cfg.province, 'on')

    def test_province_round_trips_through_to_dict(self):
        from simulation_config import SimulationConfig
        cfg = SimulationConfig.from_dict({'tax': {'province': 'on'}})
        self.assertEqual(cfg.to_dict()['tax']['province'], 'on')


class TestNoHardcodedProvinceInCoreLogic(unittest.TestCase):
    """Core engine logic must not branch on the literal 'quebec'."""

    def test_simulation_logic_has_no_quebec_literal(self):
        src = _logic_source('simulation.py')
        self.assertNotIn("'quebec'", src)
        self.assertNotIn('"quebec"', src)

    def test_simulation_state_logic_has_no_quebec_literal(self):
        src = _logic_source('simulation_state.py')
        self.assertNotIn("'quebec'", src)
        self.assertNotIn('"quebec"', src)


class TestNoHardcodedFhsaCesgInCoreLogic(unittest.TestCase):
    """Core engine logic must not carry FHSA/CESG dollar literals."""

    def _numeric_literals(self, filename):
        path = os.path.join(ROOT, filename)
        with open(path) as f:
            tree = ast.parse(f.read())
        return {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        }

    def test_simulation_state_has_no_fhsa_dollar_literals(self):
        lits = self._numeric_literals('simulation_state.py')
        self.assertNotIn(8000, lits)
        self.assertNotIn(40000, lits)

    def test_strategy_has_no_cesg_or_fhsa_dollar_literals(self):
        lits = self._numeric_literals('strategy.py')
        self.assertNotIn(2500, lits)
        self.assertNotIn(8000, lits)


class TestLimitsSourcedFromCanadaPackage(unittest.TestCase):
    """The figures core falls back to are the ones owned by the Canada package."""

    def test_simulation_state_fhsa_fallback_matches_canada(self):
        import simulation_state
        from countries.canada.fhsa import (
            FHSA_ANNUAL_LIMIT, FHSA_CARRY_FORWARD_MAX, FHSA_LIFETIME_LIMIT,
        )
        self.assertEqual(
            simulation_state._canada_fhsa_limits(),
            (FHSA_ANNUAL_LIMIT, FHSA_CARRY_FORWARD_MAX, FHSA_LIFETIME_LIMIT),
        )

    def test_strategy_cesg_match_max_matches_canada(self):
        import strategy
        from countries.canada.resp_rules import get_cesg_contribution_max
        self.assertEqual(
            strategy._cesg_contribution_match_max(2026),
            get_cesg_contribution_max(2026),
        )

    def test_strategy_resp_match_max_prefers_config_value(self):
        import strategy
        from strategy import FamilyState
        state = FamilyState(resp_contribution_match_max=1234.0)
        self.assertEqual(strategy._resp_match_max(state), 1234.0)


if __name__ == '__main__':
    unittest.main()
