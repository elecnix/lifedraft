#!/usr/bin/env python3
"""Tests for issue #665: decisions.income[] is parsed, mapped, and then
silently dropped -- job-loss and salary-cut scenarios never reached the
optimizer.

Before this fix, ``input_contract.py`` mapped ``decisions.income[]`` onto
``cfg['scenarios']['income']`` and NOTHING downstream ever read it:
``optimize.py``'s CLI pipeline ran a single hand-rolled optimization pass on
the base config's income, and ``GridOptimizer.optimize()`` (a separate,
production-disconnected engine) defaulted ``income_overrides`` to
``[None]`` whenever a caller forgot to pass it -- silently. A contract
declaring three income scenarios produced a ranked table containing none of
them, with no warning that the scenarios were ignored.

This file covers:
  1. GridOptimizer.optimize() now fails loudly (DP#13/DP#32) instead of
     silently defaulting when income_overrides is omitted.
  2. scenario_discovery._convert_income_scenarios no longer zeroes out an
     earner's income just because a scenario didn't mention them (a real,
     adjacent bug this fix surfaced: a "primary loses their job" scenario
     that only overrides the primary earner must not also silently zero the
     spouse's income).
  3. optimize.py's production CLI pipeline (run_income_scenario_exploration)
     actually runs N income scenarios x M discovered strategies -- the
     scenarios reach the real optimizer, not just input_contract.py's leaf
     read.
  4. The "does the recommendation change under a different income scenario"
     reporting logic (winners_by_income_scenario) correctly flags a change
     when one occurs and correctly does not when it doesn't.

All data is fabricated: round numbers, role-based names (DP#4, DP#15).
"""
import sys
import os
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimizer import GridOptimizer
from simulation import SimulationConfig
from countries.canada.strategies import STRATEGY_BALANCED
from scenario_discovery import _convert_income_scenarios
import optimize


def _fixture_cfg(income_scenarios=None):
    """Minimal two-earner household with a readvanceable facility -- same
    shape as tests/architecture/test_dp18_dead_write.py's fixture (this
    engine's established pattern for a runnable legacy-shape config)."""
    return {
        "family": {
            "members": [
                {
                    "role": "primary",
                    "name": "Pat",
                    "gross_income": 150000,
                    "rrsp_room_accumulated": 30000,
                    "tfsa_room_accumulated": 40000,
                    "fhsa_first_time_buyer_since": None,
                    "fhsa_room_accumulated": 0,
                    "pension_adjustment": 0,
                },
                {
                    "role": "spouse",
                    "name": "Sam",
                    "gross_income": 70000,
                    "rrsp_room_accumulated": 20000,
                    "tfsa_room_accumulated": 40000,
                    "fhsa_first_time_buyer_since": None,
                    "fhsa_room_accumulated": 0,
                    "pension_adjustment": 0,
                },
            ],
            "children": [],
        },
        "property": {
            "house_value": 800000,
            "mortgage_balance": 300000,
            "mortgage_rate": 0.05,
            "margin_available": 50000,
            "ltv_max": 0.80,
            "heloc_readvance": True,
        },
        "accounts": {
            "resp_current_balance": 0,
        },
        "assumptions": {
            "investment_return": 0.07,
            "inflation": 0.02,
            "projection_years": 10,
            "heloc_rate": 0.05,
            "capital_gains_inclusion": 0.5,
            "resp_eap_tax_rate": 0.15,
        },
        "return_model": {
            "type": "fixed",
            "rate": 0.07,
        },
        "scenarios": {"income": income_scenarios or []},
        "savings": {"rate": 0.2},
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. GridOptimizer.optimize() requires income_overrides explicitly
# ═══════════════════════════════════════════════════════════════════════════

class TestGridOptimizerFailsLoudlyOnMissingIncomeOverrides(unittest.TestCase):
    def test_optimize_without_income_overrides_raises(self):
        """DP#13/DP#32: no more `income_overrides or [None]` silent default.
        A caller that means base income only must say so explicitly."""
        config = SimulationConfig.from_dict(_fixture_cfg())
        opt = GridOptimizer(config)
        with self.assertRaises(ValueError) as ctx:
            opt.optimize(strategies=[STRATEGY_BALANCED])
        self.assertIn("income_overrides", str(ctx.exception))

    def test_optimize_with_explicit_none_still_works(self):
        """The deliberate replacement for the old default: [None] passed
        explicitly means 'base income only', and still works."""
        config = SimulationConfig.from_dict(_fixture_cfg())
        opt = GridOptimizer(config)
        results = opt.optimize(strategies=[STRATEGY_BALANCED], income_overrides=[None])
        self.assertGreater(len(results), 0)


# ═══════════════════════════════════════════════════════════════════════════
# 2. scenario_discovery._convert_income_scenarios: None, not 0, for an
#    earner a scenario doesn't mention
# ═══════════════════════════════════════════════════════════════════════════

class TestConvertIncomeScenariosPreservesUnmentionedEarner(unittest.TestCase):
    def test_partial_override_leaves_other_earner_as_none(self):
        """A 'primary loses their job' scenario that overrides only the
        primary must not silently zero the spouse's income -- None means
        'no override', matching ScenarioOverlay's own convention
        (scenario_overlay.apply_overlay: 'if overlay.spouse_income is not
        None')."""
        scenarios = [{
            "id": "job_loss",
            "label": "Primary job loss, EI only",
            "members": [{"role": "primary", "gross_income": 24000,
                         "kind": "ei", "from": "2026-01-01", "to": None}],
        }]
        converted = _convert_income_scenarios(scenarios)
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0]["primary_income"], 24000)
        self.assertEqual(
            converted[0]["primary_segments"],
            [{"kind": "ei", "amount": 24000, "from": "2026-01-01",
              "to": None, "expenses_annual": None}],
        )
        self.assertIsNone(converted[0]["spouse_income"],
                           "spouse_income must be None (no override), not 0 "
                           "(silently zeroed) -- #665's adjacent finding.")

    def test_no_members_means_both_none(self):
        """An empty overrides list ('stay at current job') means NEITHER
        earner is overridden -- both None, not both 0."""
        scenarios = [{"id": "stay", "label": "Stay at current job", "members": []}]
        converted = _convert_income_scenarios(scenarios)
        self.assertIsNone(converted[0]["primary_income"])
        self.assertIsNone(converted[0]["spouse_income"])


# ═══════════════════════════════════════════════════════════════════════════
# 3. optimize.py's production pipeline: N income scenarios reach the
#    optimizer for real (N x M ranked entries), with materially different
#    output per scenario -- not just N copies of the same numbers.
# ═══════════════════════════════════════════════════════════════════════════

class TestIncomeScenariosReachTheOptimizer(unittest.TestCase):
    INCOME_SCENARIOS = [
        {"id": "stay", "label": "Stay at current job", "members": []},
        {"id": "salary_cut", "label": "Salary cut to $90k",
         "members": [{"role": "primary", "gross_income": 90000,
                      "kind": "employment", "from": "2026-01-01", "to": None}]},
        {"id": "job_loss", "label": "Job loss, EI only",
         "members": [{"role": "primary", "gross_income": 24000,
                      "kind": "ei", "from": "2026-01-01", "to": None}]},
    ]

    def test_n_scenarios_produce_n_times_strategies_ranked_entries(self):
        """A contract declaring N income scenarios must produce N x
        (number of discovered strategies) ranked entries -- proof the
        scenarios reach the optimizer, not that they merely parse."""
        cfg = _fixture_cfg(self.INCOME_SCENARIOS)
        results = optimize.run_income_scenario_exploration(cfg)

        counts = Counter(r["income_scenario_id"] for r in results)
        self.assertEqual(set(counts), {"stay", "salary_cut", "job_loss"},
                          "every declared income scenario must appear in the "
                          "ranked results -- issue #665's exact symptom was a "
                          "ranked table containing NONE of the declared scenarios.")
        # Every scenario must be run against the SAME strategy grid (N x M).
        strategy_counts = set(counts.values())
        self.assertEqual(len(strategy_counts), 1,
                          f"each income scenario should produce the same number "
                          f"of ranked entries (M strategies), got {counts}")
        self.assertGreater(next(iter(strategy_counts)), 0)

    def test_income_scenarios_produce_materially_different_output(self):
        """Not just tagged with a label -- the engine actually ran on the
        overridden income. Net benefit under 'job loss' must be
        substantially lower than under 'stay' (same strategy grid, real
        income drop feeding marginal rate / RRSP room / cash flow)."""
        cfg = _fixture_cfg(self.INCOME_SCENARIOS)
        results = optimize.run_income_scenario_exploration(cfg)

        def best_net_benefit(scenario_id):
            rows = [r for r in results if r["income_scenario_id"] == scenario_id]
            return max(r.get("net_benefit", 0) for r in rows)

        stay_best = best_net_benefit("stay")
        job_loss_best = best_net_benefit("job_loss")
        self.assertLess(
            job_loss_best, stay_best * 0.9,
            "job-loss scenario's best net_benefit should be materially lower "
            "than the full-income scenario's -- if it isn't, the income "
            "override never reached the simulation engine."
        )

    def test_declaring_no_income_scenarios_still_runs_exactly_once(self):
        """DP#13/DP#32: absence of decisions.income[] is an explicit,
        single-scenario run (the auto-discovered 'current income' entry),
        never an implicit multiplication or a crash."""
        cfg = _fixture_cfg(income_scenarios=[])
        results = optimize.run_income_scenario_exploration(cfg)
        scenario_ids = set(r["income_scenario_id"] for r in results)
        self.assertEqual(len(scenario_ids), 1)

    def test_partial_override_does_not_zero_spouse_in_full_pipeline(self):
        """End-to-end version of the _convert_income_scenarios unit test
        above: a scenario overriding only the primary's income must leave
        the spouse's income-driven contribution room intact. Verified by
        comparing against the OLD buggy behaviour (spouse also zeroed) --
        the two must differ, proving the spouse's $70k is actually still in
        the simulation."""
        scenario_primary_only = [
            {"id": "primary_job_loss", "label": "Primary job loss",
             "members": [{"role": "primary", "gross_income": 0,
                          "kind": "ei", "from": "2026-01-01", "to": None}]},
        ]
        correct_cfg = _fixture_cfg(scenario_primary_only)
        correct_results = optimize.run_income_scenario_exploration(correct_cfg)
        correct_best = max(r.get("net_benefit", 0) for r in correct_results)

        # Reconstruct the PRE-#665-fix behaviour directly: both earners
        # zeroed, as _convert_income_scenarios used to do for any role not
        # explicitly mentioned in a scenario's members.
        buggy_cfg = _fixture_cfg(income_scenarios=[])
        for m in buggy_cfg["family"]["members"]:
            if m["role"] in ("primary", "spouse"):
                m["gross_income"] = 0
        buggy_results = optimize.run_income_scenario_exploration(buggy_cfg)
        buggy_best = max(r.get("net_benefit", 0) for r in buggy_results)

        self.assertGreater(
            correct_best, buggy_best,
            "a scenario overriding only the primary's income must score "
            "better than one where BOTH earners are zeroed -- if it doesn't, "
            "the spouse's $70,000 income silently disappeared too."
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. winners_by_income_scenario: the "does the recommendation change"
#    detection logic, tested directly against synthetic ranked results (not
#    dependent on any particular household's numbers happening to flip).
# ═══════════════════════════════════════════════════════════════════════════

class TestWinnersByIncomeScenario(unittest.TestCase):
    def test_flags_a_change_when_the_winning_strategy_differs(self):
        results = [
            {"income_scenario_id": "stay", "income_scenario_label": "Stay",
             "strategy": "readvance_priority", "net_benefit": 500000, "deduct_later": False},
            {"income_scenario_id": "stay", "income_scenario_label": "Stay",
             "strategy": "no_readvance", "net_benefit": 400000, "deduct_later": False},
            {"income_scenario_id": "job_loss", "income_scenario_label": "Job loss",
             "strategy": "no_readvance", "net_benefit": 150000, "deduct_later": False},
            {"income_scenario_id": "job_loss", "income_scenario_label": "Job loss",
             "strategy": "readvance_priority", "net_benefit": 90000, "deduct_later": False},
        ]
        winners = optimize.winners_by_income_scenario(results)
        self.assertEqual(len(winners), 2)
        self.assertEqual(winners[0]["strategy"], "readvance_priority")
        self.assertFalse(winners[0]["changed_from_base"])
        self.assertEqual(winners[1]["strategy"], "no_readvance")
        self.assertTrue(
            winners[1]["changed_from_base"],
            "the winning strategy under 'job loss' differs from 'stay' -- "
            "this MUST be flagged (issue #665's core reporting requirement)."
        )

    def test_does_not_flag_a_change_when_the_winner_is_the_same(self):
        results = [
            {"income_scenario_id": "stay", "income_scenario_label": "Stay",
             "strategy": "readvance_priority", "net_benefit": 500000, "deduct_later": False},
            {"income_scenario_id": "job_loss", "income_scenario_label": "Job loss",
             "strategy": "readvance_priority", "net_benefit": 200000, "deduct_later": False},
        ]
        winners = optimize.winners_by_income_scenario(results)
        self.assertFalse(winners[1]["changed_from_base"])

    def test_single_scenario_has_no_comparison(self):
        results = [
            {"income_scenario_id": "current", "income_scenario_label": "Current income",
             "strategy": "readvance_priority", "net_benefit": 500000, "deduct_later": False},
        ]
        winners = optimize.winners_by_income_scenario(results)
        self.assertEqual(len(winners), 1)
        self.assertFalse(winners[0]["changed_from_base"])


if __name__ == "__main__":
    unittest.main()
