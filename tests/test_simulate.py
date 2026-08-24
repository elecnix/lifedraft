#!/usr/bin/env python3
"""Integration tests for simulate.py — single entry point (discover → simulate → rank).

Tests verify:
- simulate module imports cleanly
- discover_anchors is called by the pipeline
- build_all_overlays produces expected combo count from anchors
- No-mortgage configs don't crash and produce no refi scenarios
- No-RESP configs produce no resp cash-out overlays
- Income override flows through to overlays
- CSV export produces valid output
- JSON export produces valid output

All data is fabricated with round numbers — no personal data (DP#4, DP#15).
"""

import csv
import json
import os
import tempfile

import pytest

from scenario_discovery import discover_anchors
from simulation import ScenarioOverlay


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fixture_cfg():
    """Return a minimal valid config for testing with fabricated round numbers.

    Includes enough triggers (mortgage, HELOC readvance, bracket gap, RESP balance)
    to generate anchor scenarios across all dimensions.
    """
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
            "resp_current_balance": 20000,
            "resp_composition": {
                "total_contributions": 10000,
                "total_cesg_received": 2000,
                "total_qesi_received": 1000,
                "investment_earnings": 7000,
            },
        },
        "assumptions": {
            "investment_return": 0.07,
            "inflation": 0.02,
            "projection_years": 25,
            "heloc_rate": 0.05,
            "capital_gains_inclusion": 0.5,
            "resp_eap_tax_rate": 0.15,
        },
        "scenarios": {},
        "savings": {
            "rate": 0.2,
        },
    }


# ---------------------------------------------------------------------------
# 1. test_simulate_imports
# ---------------------------------------------------------------------------

class TestSimulateImports:
    """Verify simulate module can be imported without errors."""

    def test_simulate_imports(self):
        """import simulate works and exposes key functions."""
        import simulate
        assert hasattr(simulate, "main")
        assert hasattr(simulate, "build_all_overlays")
        assert hasattr(simulate, "evaluate_overlay")
        assert hasattr(simulate, "export_csv")
        assert hasattr(simulate, "export_json")


# ---------------------------------------------------------------------------
# 2. test_discover_anchors_called
# ---------------------------------------------------------------------------

class TestDiscoverAnchorsCalled:
    """Verify simulate.py calls discover_anchors to produce anchors."""

    def test_discover_anchors_called(self):
        """discover_anchors(cfg) returns dict with required keys used by simulate."""
        cfg = _fixture_cfg()
        anchors = discover_anchors(cfg)

        # simulate.build_all_overlays expects these keys
        required_keys = [
            "income", "mortgage", "refinance", "strategy",
            "sm_options", "deduct_later_options", "resp_action",
        ]
        for key in required_keys:
            assert key in anchors, f"Missing anchor key: {key}"

        # Anchors should produce non-empty lists for a config with triggers
        assert len(anchors["income"]) >= 1
        assert len(anchors["mortgage"]) >= 1
        assert len(anchors["strategy"]) >= 1


# ---------------------------------------------------------------------------
# 3. test_build_overlays_generates_combos
# ---------------------------------------------------------------------------

class TestBuildOverlaysGeneratesCombos:
    """Verify build_all_overlays produces expected combo count from anchors."""

    def test_build_overlays_generates_combos(self):
        """build_all_overlays produces correct number of combinations."""
        import simulate

        cfg = _fixture_cfg()
        anchors = discover_anchors(cfg)
        combinations = simulate.build_all_overlays(cfg, anchors)

        # Each combo must have 'overlay' and 'strategy_alloc'
        assert len(combinations) > 0
        for combo in combinations:
            assert "overlay" in combo
            assert "strategy_alloc" in combo
            assert isinstance(combo["overlay"], ScenarioOverlay)

        # Verify combo count = product of all anchor dimensions
        n_income = len(anchors["income"])
        n_mortgage = len(anchors["mortgage"])
        n_refinance = len(anchors["refinance"])
        n_strategy = len(anchors["strategy"])
        n_sm = len(anchors["sm_options"])
        n_dl = len(anchors["deduct_later_options"])
        n_resp = len(anchors["resp_action"])

        expected = n_income * n_mortgage * n_refinance * n_strategy * n_sm * n_dl * n_resp
        assert len(combinations) == expected

    def test_overlay_labels_are_unique(self):
        """All overlay labels in combinations should be unique."""
        import simulate

        cfg = _fixture_cfg()
        anchors = discover_anchors(cfg)
        combinations = simulate.build_all_overlays(cfg, anchors)

        labels = [c["overlay"].label for c in combinations]
        assert len(labels) == len(set(labels)), "Duplicate overlay labels found"

    def test_missing_retirement_age_key_defaults_to_single_none_pass(self):
        """DP#32 (#606): a genuinely ABSENT 'retirement_age' anchor key still
        gets the [None] single-pass placeholder (no dimension configured)."""
        import simulate

        cfg = _fixture_cfg()
        anchors = discover_anchors(cfg)
        anchors.pop("retirement_age", None)
        combinations = simulate.build_all_overlays(cfg, anchors)
        assert len(combinations) > 0

    def test_explicit_empty_retirement_age_list_is_not_overridden(self):
        """DP#32 (#606): an explicit retirement_age=[] must NOT silently
        revert to the [None] single-pass placeholder -- it is a value
        ("sweep nothing"), so build_all_overlays legitimately produces zero
        combinations rather than falling through to a hidden default."""
        import simulate

        cfg = _fixture_cfg()
        anchors = discover_anchors(cfg)
        anchors["retirement_age"] = []
        combinations = simulate.build_all_overlays(cfg, anchors)
        assert combinations == []


# ---------------------------------------------------------------------------
# 4. test_no_mortgage_no_crash
# ---------------------------------------------------------------------------

class TestNoMortgageNoCrash:
    """Config with no mortgage should not crash and should produce no refi scenarios."""

    def test_no_mortgage_no_crash(self):
        """Zero mortgage balance → discover_anchors returns empty refinance list."""
        cfg = _fixture_cfg()
        # Remove mortgage entirely
        cfg["property"]["mortgage_balance"] = 0
        cfg["property"]["margin_available"] = 0
        cfg["property"]["heloc_readvance"] = False

        anchors = discover_anchors(cfg)

        # No mortgage + no margin → no refinance scenarios
        assert anchors["refinance"] == []

        # build_all_overlays should still work (just 0 combos if no refinance)
        import simulate
        combinations = simulate.build_all_overlays(cfg, anchors)
        # With no refinance, no combos can be built (refinance is a required dimension)
        assert len(combinations) == 0


# ---------------------------------------------------------------------------
# 5. test_no_resp_no_resp_scenarios
# ---------------------------------------------------------------------------

class TestNoRespNoRespScenarios:
    """Config with resp=0 should produce no resp cash-out overlays."""

    def test_no_resp_no_resp_scenarios(self):
        """Zero RESP balance → no EAP or collapse in resp_action."""
        cfg = _fixture_cfg()
        cfg["accounts"]["resp_current_balance"] = 0
        # Remove composition since there's no balance
        cfg["accounts"].pop("resp_composition", None)

        anchors = discover_anchors(cfg)

        # No RESP balance → only 'keep' action
        assert anchors["resp_action"] == ["keep"]

        # No resp cash-out overlays in combinations
        import simulate
        combinations = simulate.build_all_overlays(cfg, anchors)
        resp_cash_outs = [c["overlay"].resp_cash_out for c in combinations]
        assert all(rco == 0.0 for rco in resp_cash_outs), (
            f"Expected all resp_cash_out=0 but found {set(resp_cash_outs)}"
        )


# ---------------------------------------------------------------------------
# 6. test_income_override_used
# ---------------------------------------------------------------------------

class TestIncomeOverrideUsed:
    """Verify that scenarios.income override values flow into overlays."""

    def test_income_override_used(self):
        """When scenarios.income provides custom incomes, overlays use those values."""
        cfg = _fixture_cfg()
        # Provide explicit income override scenarios
        cfg["scenarios"]["income"] = [
            {
                "id": "boost",
                "label": "Income boost",
                "members": [
                    {"role": "primary", "gross_income": 200000,
                     "kind": "employment", "from": "2026-01-01", "to": None},
                    {"role": "spouse", "gross_income": 90000,
                     "kind": "employment", "from": "2026-01-01", "to": None},
                ],
            }
        ]

        anchors = discover_anchors(cfg)

        # Should have exactly 1 income scenario with overridden values
        assert len(anchors["income"]) == 1
        assert anchors["income"][0]["primary_income"] == 200000
        assert anchors["income"][0]["spouse_income"] == 90000

        import simulate
        combinations = simulate.build_all_overlays(cfg, anchors)

        # All overlays should use the overridden income
        for combo in combinations:
            assert combo["overlay"].primary_income == 200000, (
                f"Expected primary_income=200000, got {combo['overlay'].primary_income}"
            )
            assert combo["overlay"].spouse_income == 90000, (
                f"Expected spouse_income=90000, got {combo['overlay'].spouse_income}"
            )


# ---------------------------------------------------------------------------
# 7. test_csv_export
# ---------------------------------------------------------------------------

class TestCsvExport:
    """Verify --csv produces valid CSV output."""

    def test_csv_export(self):
        """export_csv writes a valid CSV file with expected headers."""
        import simulate

        # Fabricate minimal results list (as produced by evaluate_scenario)
        results = [
            {
                "label": "Test scenario A",
                "strategy_id": "balanced",
                "cash_out": 0,
                "resp_cash_out": 0,
                "use_readvanceable": False,
                "deduct_later": False,
                "ltv": 0.375,
                "net_benefit": 50000,
                "future_value": 200000,
                "total_debt": 100000,
                "RRSP": 60000,
                "Spousal_RRSP": 20000,
                "TFSA": 40000,
                "Non_Reg": 30000,
                "RESP": 10000,
            },
            {
                "label": "Test scenario B",
                "strategy_id": "smith",
                "cash_out": 100000,
                "resp_cash_out": 0,
                "use_readvanceable": True,
                "deduct_later": True,
                "ltv": 0.50,
                "net_benefit": 80000,
                "future_value": 300000,
                "total_debt": 200000,
                "RRSP": 70000,
                "Spousal_RRSP": 25000,
                "TFSA": 50000,
                "Non_Reg": 45000,
                "RESP": 10000,
            },
        ]

        fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_simulate_")
        os.close(fd)
        try:
            simulate.export_csv(results, path)
            assert os.path.exists(path)

            with open(path, "r", newline="") as f:
                reader = csv.reader(f)
                rows = list(reader)

            # Header row
            header = rows[0]
            assert "rank" in header
            assert "label" in header
            assert "net_benefit" in header

            # Data rows (sorted by net_benefit desc, so B first, then A)
            assert len(rows) == 3  # header + 2 data rows
            # Row 1 should be the higher net_benefit scenario
            assert int(rows[1][0]) == 1  # rank=1
            assert rows[1][1] == "Test scenario B"

            # Verify CSV is parseable and values are present
            for row in rows[1:]:
                assert len(row) == len(header)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# 8. test_json_export
# ---------------------------------------------------------------------------

class TestJsonExport:
    """Verify --json produces valid JSON output."""

    def test_json_export(self):
        """export_json writes a valid JSON file with expected structure."""
        import simulate

        cfg = _fixture_cfg()
        results = [
            {
                "label": "Test scenario A",
                "strategy_id": "balanced",
                "cash_out": 0,
                "resp_cash_out": 0,
                "use_readvanceable": False,
                "deduct_later": False,
                "ltv": 0.375,
                "net_benefit": 50000,
                "future_value": 200000,
                "total_debt": 100000,
                "RRSP": 60000,
                "Spousal_RRSP": 20000,
                "TFSA": 40000,
                "Non_Reg": 30000,
                "RESP": 10000,
            },
            {
                "label": "Test scenario B",
                "strategy_id": "smith",
                "cash_out": 100000,
                "resp_cash_out": 0,
                "use_readvanceable": True,
                "deduct_later": True,
                "ltv": 0.50,
                "net_benefit": 80000,
                "future_value": 300000,
                "total_debt": 200000,
                "RRSP": 70000,
                "Spousal_RRSP": 25000,
                "TFSA": 50000,
                "Non_Reg": 45000,
                "RESP": 10000,
            },
        ]

        fd, path = tempfile.mkstemp(suffix=".json", prefix="test_simulate_")
        os.close(fd)
        try:
            simulate.export_json(results, cfg, path, run_id="test-run-001")
            assert os.path.exists(path)

            with open(path, "r") as f:
                data = json.load(f)

            # Top-level structure
            assert "title" in data
            assert "count" in data
            assert "results" in data
            assert data["count"] == 2
            assert data["metadata"]["run_id"] == "test-run-001"

            # Results sorted by net_benefit desc
            assert len(data["results"]) == 2
            assert data["results"][0]["net_benefit"] == 80000
            assert data["results"][1]["net_benefit"] == 50000

            # Each result should be JSON-serializable (no non-serializable types)
            for r in data["results"]:
                assert isinstance(r, dict)
                assert "label" in r
                assert "net_benefit" in r
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# 9. Year-by-year breakdown (issue #248)
# ---------------------------------------------------------------------------

class TestYearByYear:
    """Verify the per-year series is threaded through to all output formats.

    Per DP#8/DP#25 this is a presentation-layer change: evaluate_overlay
    already runs the simulation; we assert it now surfaces the existing
    List[YearResult] (serialized) instead of discarding it.
    """

    def test_evaluate_overlay_includes_year_by_year(self):
        """evaluate_overlay surfaces one serialized YearResult per projection year."""
        import simulate
        from scenario_overlay import ScenarioOverlay

        cfg = _fixture_cfg()
        projection_years = cfg["assumptions"]["projection_years"]
        overlay = ScenarioOverlay(label="YBY test", cash_out=0, mortgage_rate=0.05)

        result = simulate.evaluate_overlay(cfg, overlay)

        assert "year_by_year" in result
        yby = result["year_by_year"]
        assert len(yby) == projection_years
        # Each entry is a dict carrying every concern's columns.
        for key in [
            "year", "mortgage_payment", "mortgage_interest", "mortgage_principal",
            "mortgage_balance", "primary_marginal", "rrsp_tax_savings",
            "readvance_tax_savings", "sm_qc_deductible", "total_family_income",
            "annual_savings", "contributions", "total_assets", "total_debt",
        ]:
            assert key in yby[0], f"missing column {key}"
        # Years are sequential starting at 1.
        assert [y["year"] for y in yby] == list(range(1, projection_years + 1))

    def test_json_export_surfaces_top_year_by_year(self):
        """export_json exposes the #1 scenario's per-year series at top level."""
        import simulate

        cfg = _fixture_cfg()
        n = cfg["assumptions"]["projection_years"]
        results = [
            {"label": "Top", "net_benefit": 80000,
             "year_by_year": [{"year": y, "mortgage_balance": 1000 * y} for y in range(1, n + 1)]},
            {"label": "Runner-up", "net_benefit": 50000,
             "year_by_year": [{"year": y} for y in range(1, n + 1)]},
        ]
        fd, path = tempfile.mkstemp(suffix=".json", prefix="test_yby_")
        os.close(fd)
        try:
            simulate.export_json(results, cfg, path)
            with open(path) as f:
                data = json.load(f)
            assert len(data["year_by_year"]) == n
            # Top-level series belongs to the highest net_benefit scenario.
            assert data["results"][0]["label"] == "Top"
            assert len(data["results"][0]["year_by_year"]) == n
        finally:
            os.unlink(path)

    def test_csv_export_writes_long_format_sibling(self):
        """export_csv writes a tidy long-format <name>_year_by_year.csv sibling."""
        import simulate

        cfg = _fixture_cfg()
        n = 5
        results = [
            {"label": "Top", "net_benefit": 80000, "strategy_id": "balanced",
             "year_by_year": [
                 {"year": y, "mortgage_balance": 1000 * y, "primary_marginal": 0.5}
                 for y in range(1, n + 1)
             ]},
        ]
        fd, path = tempfile.mkstemp(suffix=".csv", prefix="test_yby_")
        os.close(fd)
        sibling = path[:-4] + "_year_by_year.csv"
        try:
            simulate.export_csv(results, path)
            assert os.path.exists(sibling)
            with open(sibling, newline="") as f:
                rows = list(csv.DictReader(f))
            assert len(rows) == n  # one row per year
            assert "scenario_rank" in rows[0]
            assert "scenario_label" in rows[0]
            assert "mortgage_balance" in rows[0]
            assert [int(r["year"]) for r in rows] == list(range(1, n + 1))
        finally:
            os.unlink(path)
            if os.path.exists(sibling):
                os.unlink(sibling)