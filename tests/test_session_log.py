#!/usr/bin/env python3
"""Unit tests for session_log.py — JSONL session store (issue #239).

Verifies that:
- build_session_record captures timestamp, git commit, full input, and scenarios
- year-by-year YearResult data is serialized into each scenario record
- save_session appends one JSON line per run (append-only)
- load_session round-trips the records
- default_session_path places the file next to input.json

All test data uses round numbers. No personal information.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import tempfile
import unittest
from pathlib import Path

from session_log import (
    build_session_record,
    default_session_path,
    load_session,
    save_session,
    year_result_to_dict,
)
from year_result import YearResult


def _make_year_results(n: int = 3):
    """Build a list of YearResult objects with round numbers."""
    results = []
    for i in range(n):
        yr = YearResult(year=i + 1)
        yr.primary_income = 100000 * (1 + i)
        yr.total_assets = 500000 + i * 10000
        yr.total_debt = 100000 - i * 1000
        yr.contributions = {"primary_rrsp": 10000 + i * 100, "non_reg": 5000}
        yr.rrsp_tax_savings = 4000 + i * 100
        results.append(yr)
    return results


class TestYearResultToDict(unittest.TestCase):
    """Test year_result_to_dict serializes a YearResult dataclass."""

    def test_serializes_fields(self):
        yr = YearResult(year=1, total_assets=500000, total_debt=100000)
        yr.contributions = {"primary_rrsp": 10000}
        d = year_result_to_dict(yr)
        self.assertEqual(d["year"], 1)
        self.assertEqual(d["total_assets"], 500000)
        self.assertEqual(d["total_debt"], 100000)
        self.assertEqual(d["contributions"], {"primary_rrsp": 10000})

    def test_includes_all_yearresult_fields(self):
        import dataclasses
        yr = YearResult(year=2)
        d = year_result_to_dict(yr)
        expected = {f.name for f in dataclasses.fields(YearResult)}
        self.assertEqual(set(d.keys()), expected)


class TestBuildSessionRecord(unittest.TestCase):
    """Test build_session_record produces a complete record."""

    def test_record_has_required_top_level_keys(self):
        cfg = {"property": {"house_value": 500000}}
        results = [
            {"strategy": "balanced", "net_benefit": 1000000,
             "year_results": _make_year_results(2)},
        ]
        record = build_session_record(
            input_path="/tmp/input.json",
            input_cfg=cfg,
            results=results,
            objective_name="max_net_benefit",
        )
        for key in ("timestamp", "git_commit", "input_path", "input",
                    "objective", "scenarios"):
            self.assertIn(key, record, f"missing top-level key: {key}")

    def test_record_contains_full_input(self):
        cfg = {"family": {"members": [{"role": "primary"}]},
               "property": {"house_value": 500000, "mortgage_balance": 100000}}
        record = build_session_record(
            input_path="input.json",
            input_cfg=cfg,
            results=[],
            objective_name="",
        )
        self.assertEqual(record["input"], cfg)
        # The input must be the verbatim config, not a reference
        record["input"]["property"]["house_value"] = 0
        self.assertEqual(cfg["property"]["house_value"], 500000)

    def test_scenario_includes_year_results(self):
        year_results = _make_year_results(3)
        results = [
            {"strategy": "balanced", "net_benefit": 1000000,
             "year_results": year_results},
        ]
        record = build_session_record(
            input_path="input.json",
            input_cfg={},
            results=results,
            objective_name="max_net_benefit",
        )
        scenario = record["scenarios"][0]
        self.assertIn("year_results", scenario)
        self.assertEqual(len(scenario["year_results"]), 3)
        self.assertEqual(scenario["year_results"][0]["year"], 1)
        self.assertEqual(scenario["year_results"][0]["total_assets"], 500000)
        self.assertEqual(scenario["year_results"][0]["contributions"]["primary_rrsp"], 10000)

    def test_scenario_without_year_results_has_empty_list(self):
        results = [{"strategy": "balanced", "net_benefit": 1000000}]
        record = build_session_record(
            input_path="input.json",
            input_cfg={},
            results=results,
        )
        self.assertEqual(record["scenarios"][0]["year_results"], [])

    def test_scenario_name_from_strategy(self):
        results = [{"strategy": "rrsp_max", "net_benefit": 500}]
        record = build_session_record(
            input_path="input.json",
            input_cfg={},
            results=results,
        )
        self.assertEqual(record["scenarios"][0]["name"], "rrsp_max")

    def test_extra_keys_merged_in(self):
        record = build_session_record(
            input_path="input.json",
            input_cfg={},
            results=[],
            extra={"anchor": "renew_current", "overlay": "moderate"},
        )
        self.assertEqual(record["anchor"], "renew_current")
        self.assertEqual(record["overlay"], "moderate")

    def test_optimizer_mode_included_when_provided(self):
        record = build_session_record(
            input_path="input.json",
            input_cfg={},
            results=[],
            optimizer_mode={"type": "grid"},
        )
        self.assertEqual(record["optimizer_mode"], {"type": "grid"})

    def test_ranked_scenario_serialization(self):
        """A RankedScenario (from optimizer.optimize()) serializes correctly."""
        from optimizer import RankedScenario, RiskMeasures
        year_results = _make_year_results(2)
        scenario = RankedScenario(
            scenario_name="balanced_sm1_dl0",
            score=1234567.0,
            objective_name="max_net_benefit",
            config_overrides={"ltv": 0.0, "use_readvanceable": True},
            results=year_results,
            risk_measures=RiskMeasures(expected_value=1234567.0, n_simulations=1),
        )
        record = build_session_record(
            input_path="input.json",
            input_cfg={},
            results=[scenario],
        )
        out = record["scenarios"][0]
        self.assertEqual(out["name"], "balanced_sm1_dl0")
        self.assertEqual(out["score"], 1234567.0)
        self.assertEqual(out["config_overrides"]["ltv"], 0.0)
        self.assertEqual(len(out["year_results"]), 2)
        self.assertIn("summary", out)
        self.assertEqual(out["summary"]["net_benefit"], 1234567.0)
        self.assertIn("risk_measures", out)
        self.assertEqual(out["risk_measures"]["n_simulations"], 1)

    def test_git_commit_is_string_or_none(self):
        record = build_session_record(
            input_path="input.json",
            input_cfg={},
            results=[],
        )
        commit = record["git_commit"]
        self.assertTrue(commit is None or isinstance(commit, str))


class TestSaveAndLoadSession(unittest.TestCase):
    """Test save_session / load_session JSONL round-trip."""

    def test_save_appends_one_line_per_record(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "optimisations.jsonl")
            rec1 = build_session_record("a.json", {"v": 1}, [])
            rec2 = build_session_record("b.json", {"v": 2}, [])
            save_session(rec1, path)
            save_session(rec2, path)
            with open(path) as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            self.assertEqual(len(lines), 2)
            loaded = load_session(path)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["input_path"], "a.json")
            self.assertEqual(loaded[1]["input_path"], "b.json")

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nested", "dir", "optimisations.jsonl")
            save_session(build_session_record("a.json", {}, []), path)
            self.assertTrue(os.path.exists(path))

    def test_load_skips_blank_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "optimisations.jsonl")
            rec = build_session_record("a.json", {"v": 1}, [])
            with open(path, "w") as f:
                f.write(json.dumps(rec) + "\n\n  \n")
            loaded = load_session(path)
            self.assertEqual(len(loaded), 1)

    def test_round_trip_preserves_year_results(self):
        results = [{"strategy": "balanced", "net_benefit": 1000,
                    "year_results": _make_year_results(2)}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "optimisations.jsonl")
            rec = build_session_record("input.json", {}, results)
            save_session(rec, path)
            loaded = load_session(path)
            self.assertEqual(len(loaded[0]["scenarios"][0]["year_results"]), 2)
            self.assertEqual(loaded[0]["scenarios"][0]["year_results"][0]["year"], 1)


class TestDefaultSessionPath(unittest.TestCase):
    """Test default_session_path places the file next to input.json."""

    def test_next_to_input_json(self):
        with tempfile.TemporaryDirectory() as d:
            input_path = os.path.join(d, "scenario", "input.json")
            os.makedirs(os.path.dirname(input_path))
            Path(input_path).touch()
            path = default_session_path(input_path)
            self.assertEqual(Path(path).name, "optimisations.jsonl")
            self.assertEqual(Path(path).parent, Path(input_path).resolve().parent)


if __name__ == "__main__":
    unittest.main()
