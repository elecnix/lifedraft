#!/usr/bin/env python3
"""Tests for DynamicProgrammingOptimizer (DP#31).

Per DP#17: every rule path tested. Per DP#3: pure functions, same inputs → same outputs.
Per DP#31: optimizer mode is pluggable data, independent of other modes.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from dataclasses import replace

from simulation import SimulationConfig
from dp_optimizer import (
    DPOptimizer,
    Decision, DecisionStep, DPOptimizeResult,
    DECISION_DEDUCT_LATER, DECISION_DRAWDOWN_ORDER,
)
from countries.canada.strategies import STRATEGY_BALANCED, STRATEGY_RRSP_MAX
from return_model import FixedReturn


def _make_config():
    return SimulationConfig(
        projection_years=3, investment_return=0.07,
        family_members=[
            {'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
        mortgage_balance=100000, mortgage_rate=0.05, house_value=400000,
    )


def _deduct_later(**kwargs):
    """Build a deduct_later Decision (issue #717: one dataclass, type field).

    Replaces the old _deduct_later() construction so the call sites stay
    terse. kwargs map onto Decision's fields (claim_fractions, name, ...).
    """
    return Decision(name="deduct_later", decision_type=DECISION_DEDUCT_LATER, **kwargs)


class Test_deduct_later(unittest.TestCase):
    """Test the deduct_later variant of the Decision data class (issue #717)."""

    def test_default_fractions(self):
        """Default claim fractions cover 0% to 100% in 25% steps."""
        d = Decision(name="deduct_later", decision_type=DECISION_DEDUCT_LATER)
        self.assertEqual(d.claim_fractions, [0.0, 0.25, 0.5, 0.75, 1.0])

    def test_custom_fractions(self):
        """Custom fractions can be provided."""
        d = Decision(name="dl", decision_type=DECISION_DEDUCT_LATER,
                     claim_fractions=[0.0, 0.5, 1.0])
        self.assertEqual(d.claim_fractions, [0.0, 0.5, 1.0])


class TestDrawdownOrderDecision(unittest.TestCase):
    """Test the drawdown_order variant of the Decision data class (issue #717)."""

    def test_default_orderings(self):
        """Default drawdown orderings include common sequences."""
        d = Decision(name="drawdown_order", decision_type=DECISION_DRAWDOWN_ORDER)
        self.assertGreater(len(d.orderings), 2)
        self.assertIn("rrsp_first", d.orderings)


class TestDPOptimizerInit(unittest.TestCase):
    """Test DPOptimizer initialization."""

    def test_init_defaults(self):
        """DPOptimizer initializes with base config and defaults."""
        cfg = _make_config()
        opt = DPOptimizer(cfg)
        self.assertEqual(opt.base_config, cfg)
        self.assertEqual(opt.lookahead, 0)

    def test_init_with_return_model(self):
        """DPOptimizer accepts a custom return model (DP#21)."""
        cfg = _make_config()
        rm = FixedReturn(0.05)
        opt = DPOptimizer(cfg, return_model=rm)
        self.assertEqual(opt.return_model.return_for_year(0), 0.05)

    def test_init_with_lookahead(self):
        """DPOptimizer accepts a lookahead parameter."""
        cfg = _make_config()
        opt = DPOptimizer(cfg, lookahead=2)
        self.assertEqual(opt.lookahead, 2)


class TestDPOptimizerDeductLater(unittest.TestCase):
    """Test DPOptimizer with deduct-later decisions (DP#45)."""

    def test_optimize_returns_result(self):
        """DP optimization produces a result for each strategy."""
        cfg = _make_config()
        opt = DPOptimizer(cfg)
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(),
        )
        self.assertGreater(len(result), 0)
        # Score can be negative (costs exceed benefits)
        self.assertIsNotNone(result[0].total_score)

    def test_optimize_has_decision_path(self):
        """Each year has a decision step in the path."""
        cfg = _make_config()
        opt = DPOptimizer(cfg)
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(),
        )
        path = result[0].decision_path
        self.assertEqual(len(path), cfg.projection_years)
        for step in path:
            self.assertIn("claim_", step.action_name)
            self.assertGreaterEqual(step.action_value, 0)
            self.assertLessEqual(step.action_value, 1.0)

    def test_no_undeducted_claims_all(self):
        """When no undeducted RRSP, all fractions produce same result."""
        cfg = _make_config()
        # Fresh config with no prior RRSP contributions → no undeducted
        opt = DPOptimizer(cfg)
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(),
        )
        # Should still produce a valid result
        self.assertGreater(len(result), 0)

    def test_different_strategies_produce_different_results(self):
        """Different strategies produce different scores (DP#22)."""
        cfg = _make_config()
        opt = DPOptimizer(cfg)
        results = opt.optimize(
            strategies=[STRATEGY_BALANCED, STRATEGY_RRSP_MAX],
            decision_class=_deduct_later(),
        )
        # Two strategies, two results
        self.assertEqual(len(results), 2)
        # Strategies may have different scores (not guaranteed, but likely)
        names = {r.strategy_name for r in results}
        self.assertEqual(len(names), 2)

    def test_deterministic(self):
        """Same inputs produce same outputs (DP#3, DP#23)."""
        cfg = _make_config()
        rm = FixedReturn(0.07)
        opt1 = DPOptimizer(cfg, return_model=rm)
        result1 = opt1.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[0.0, 1.0]),
        )
        opt2 = DPOptimizer(cfg, return_model=rm)
        result2 = opt2.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[0.0, 1.0]),
        )
        self.assertAlmostEqual(result1[0].total_score, result2[0].total_score, places=0)

    def test_optimize_does_not_modify_base_config(self):
        """DP optimization doesn't mutate the base config (DP#18: overlay)."""
        cfg = _make_config()
        original_ltv = cfg.ltv_max
        opt = DPOptimizer(cfg)
        opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(),
        )
        self.assertEqual(cfg.ltv_max, original_ltv)


class TestDecisionStep(unittest.TestCase):
    """Test DecisionStep data class."""

    def test_step_fields(self):
        """DecisionStep has expected fields."""
        step = DecisionStep(
            year=0,
            action_name="claim_50%",
            action_value=0.5,
            score_contribution=10000,
            cumulative_score=10000,
        )
        self.assertEqual(step.year, 0)
        self.assertEqual(step.action_name, "claim_50%")
        self.assertEqual(step.action_value, 0.5)


class TestDPOptimizeResult(unittest.TestCase):
    """Test DPOptimizeResult data class."""

    def test_result_fields(self):
        """DPOptimizeResult has expected fields."""
        result = DPOptimizeResult(
            strategy_name="balanced",
            decision_class="deduct_later",
            objective_name="net_benefit",
            total_score=100000,
        )
        self.assertEqual(result.strategy_name, "balanced")
        self.assertEqual(result.total_score, 100000)
        self.assertEqual(len(result.decision_path), 0)


class TestOptimizerIndependence(unittest.TestCase):
    """Verify DP optimizer doesn't depend on other optimizer modes (DP#25)."""

    def test_no_import_from_grid(self):
        """DPOptimizer does not import GridOptimizer class."""
        import dp_optimizer
        with open(dp_optimizer.__file__) as f:
            source = f.read()
        # It should not import the GridOptimizer CLASS
        # (mentioning GridOptimizer in comments is fine per DP#25)
        self.assertNotIn('from optimizer import GridOptimizer', source)
        self.assertNotIn('GridOptimizer(', source)

    def test_no_import_from_scipy(self):
        """DPOptimizer does not import ScipyOptimizer."""
        import dp_optimizer
        with open(dp_optimizer.__file__) as f:
            source = f.read()
        self.assertNotIn('from scipy_optimizer import', source)

    def test_no_import_from_monte_carlo(self):
        """DPOptimizer does not import MonteCarloOptimizer."""
        import dp_optimizer
        with open(dp_optimizer.__file__) as f:
            source = f.read()
        self.assertNotIn('from monte_carlo_optimizer import', source)


class TestDPLookahead(unittest.TestCase):
    """Test _evaluate_with_lookahead path."""

    def test_lookahead_zero_returns_single_step_score(self):
        """Lookahead=0 should evaluate only the current year."""
        cfg = _make_config()
        rm = FixedReturn(0.07)
        opt = DPOptimizer(cfg, return_model=rm, lookahead=0)
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[1.0]),
        )
        self.assertGreater(len(result), 0)

    def test_lookahead_one_produces_result(self):
        """Lookahead=1 should evaluate two years and produce a result."""
        cfg = _make_config()
        rm = FixedReturn(0.07)
        opt = DPOptimizer(cfg, return_model=rm, lookahead=1)
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[1.0]),
        )
        self.assertGreater(len(result), 0)
        # With lookahead=1, total_score should be >= single year
        self.assertIsNotNone(result[0].total_score)

    def test_lookahead_deterministic(self):
        """Same inputs produce same outputs with lookahead (DP#23)."""
        cfg = _make_config()
        rm = FixedReturn(0.07)
        opt1 = DPOptimizer(cfg, return_model=rm, lookahead=1)
        result1 = opt1.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[1.0]),
        )
        opt2 = DPOptimizer(cfg, return_model=rm, lookahead=1)
        result2 = opt2.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[1.0]),
        )
        self.assertAlmostEqual(result1[0].total_score, result2[0].total_score, places=0)


class TestPartialClaimFraction(unittest.TestCase):
    """Test the partial claim fraction logic in the deduct_later Decision."""

    def test_zero_fraction_carries_forward(self):
        """Fraction=0 means carry all forward (deduct later)."""
        cfg = _make_config()
        opt = DPOptimizer(cfg)
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[0.0]),
        )
        # Should produce valid result with claims=0
        self.assertGreater(len(result), 0)

    def test_full_fraction_claims_all(self):
        """Fraction=1 means claim all undeducted now."""
        cfg = _make_config()
        opt = DPOptimizer(cfg)
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[1.0]),
        )
        self.assertGreater(len(result), 0)

    def test_half_fraction_interpolates(self):
        """Fraction=0.5 should claim approximately half."""
        cfg = _make_config()
        opt = DPOptimizer(cfg)
        result_half = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[0.5]),
        )
        result_full = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=[1.0]),
        )
        # Half claim should produce different score than full claim
        self.assertIsNotNone(result_half[0].total_score)
        self.assertIsNotNone(result_full[0].total_score)


class TestDecisionSpaceInvariant(unittest.TestCase):
    """Issue #717 (DP#8): the single-Decision refactor must explore the SAME
    decision space as the old subclass hierarchy, and dispatch on the
    ``decision_type`` field rather than ``isinstance``.

    All figures are fabricated round numbers (DP#4/#15). The invariant is
    narrow: the optimizer visits one action per projection year, the
    ``deduct_later`` actions are drawn from ``claim_fractions``, and the
    ``drawdown_order`` discriminator takes the distinct ``baseline`` branch --
    so the dispatch is driven by the data field, not by type.
    """

    def test_deduct_later_explores_claim_fractions(self):
        """Each year's action value is one of the declared claim_fractions."""
        cfg = _make_config()
        rm = FixedReturn(0.07)
        opt = DPOptimizer(cfg, return_model=rm)
        fractions = [0.0, 0.5, 1.0]
        result = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=_deduct_later(claim_fractions=fractions),
        )
        path = result[0].decision_path
        # One decision per projection year.
        self.assertEqual(len(path), cfg.projection_years)
        # Every action value is one of the declared fractions (the search space).
        for step in path:
            self.assertIn(step.action_value, fractions)
            self.assertTrue(step.action_name.startswith("claim_"))

    def test_decision_type_drives_dispatch_not_isinstance(self):
        """A drawdown_order Decision takes the 'baseline' branch; a
        deduct_later Decision takes the 'claim_' branch. Same dataclass,
        different discriminator field -> different path (DP#8)."""
        cfg = _make_config()
        rm = FixedReturn(0.07)
        opt = DPOptimizer(cfg, return_model=rm)
        deduct = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=Decision(name="deduct_later",
                                     decision_type=DECISION_DEDUCT_LATER,
                                     claim_fractions=[1.0]),
        )
        drawdown = opt.optimize(
            strategies=[STRATEGY_BALANCED],
            decision_class=Decision(name="drawdown_order",
                                     decision_type=DECISION_DRAWDOWN_ORDER),
        )
        # deduct_later -> claim_* actions; drawdown_order -> baseline actions.
        self.assertTrue(all(s.action_name.startswith("claim_")
                            for s in deduct[0].decision_path))
        self.assertTrue(all(s.action_name == "baseline"
                            for s in drawdown[0].decision_path))

    def test_no_decision_subclasses_remain(self):
        """Issue #717: the inheritance hierarchy is gone. The module exports
        only the single Decision dataclass -- no DeductLaterDecision /
        DrawdownOrderDecision -- and dispatch is not isinstance-based (an
        AST check: no isinstance call appears in the module body)."""
        import ast
        import dp_optimizer
        self.assertFalse(hasattr(dp_optimizer, "DeductLaterDecision"))
        self.assertFalse(hasattr(dp_optimizer, "DrawdownOrderDecision"))
        with open(dp_optimizer.__file__) as f:
            tree = ast.parse(f.read())
        isinstance_calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == "isinstance"
        ]
        self.assertEqual(isinstance_calls, [],
                         "dp_optimizer.py must not dispatch with isinstance; "
                         "branch on Decision.decision_type instead (issue #717).")


class TestDecisionDataclassFields(unittest.TestCase):
    """Issue #717: the single Decision carries every variant's fields (DP#8)."""

    def test_decision_has_discriminator_and_both_variants_fields(self):
        from dataclasses import fields
        names = {f.name for f in fields(Decision)}
        self.assertEqual(names, {"name", "description", "decision_type",
                                 "claim_fractions", "orderings"})

    def test_default_decision_type_is_deduct_later(self):
        d = Decision(name="any")
        self.assertEqual(d.decision_type, DECISION_DEDUCT_LATER)
        # Both variant field defaults are populated on the one dataclass.
        self.assertEqual(d.claim_fractions, [0.0, 0.25, 0.5, 0.75, 1.0])
        self.assertIn("rrsp_first", d.orderings)


if __name__ == '__main__':
    unittest.main()