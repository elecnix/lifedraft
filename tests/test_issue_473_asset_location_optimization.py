#!/usr/bin/env python3
"""Issue #473: asset-location placement is an OPTIMIZABLE dimension.

The optimizer scored each account's composition verbatim; it never decided
*which asset class belongs in which account* for tax efficiency. #641 made the
per-account composition reach the return engine (foreign-withholding-tax drag
differs by shelter -- US equity is treaty-exempt in an RRSP but fully withheld
in a TFSA), so a placement choice now produces a DIFFERENT simulated number.
This is what made #473's earlier PRs unfalsifiable -- now fixed.

These tests assert the two things #473 asks for:
- the optimizer PREFERS the tax-efficient placement (foreign equity in the RRSP
  over the TFSA) and the two placements' objectives genuinely differ (falsifiable
  via #641), and
- the chosen placement is SURFACED in the recommendation/output.

Absence is a strict no-op: the golden household declares composition only on
non_reg, so there is no placement decision and the recommendation is ``None`` --
the golden terminal total_assets is byte-identical.
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asset_location_optimize
import optimize
from asset_location_optimize import (
    discover_placements, rank_placements, recommend_asset_location,
    format_asset_location,
)
from objective import MAX_NET_BENEFIT
from test_golden_trajectory_581 import golden_household_config, _run

# A strongly-foreign registered sleeve (US/intl dividends attract WHT) and a
# domestic sleeve (Cdn dividends + interest, no WHT leak).
FOREIGN = {"composition": {"us_equity_pct": 0.7, "intl_equity_pct": 0.3},
           "yield": {"foreign_income": 0.03}}
DOMESTIC = {"composition": {"cdn_equity_pct": 0.6, "fixed_income_pct": 0.4},
            "yield": {"eligible_dividends": 0.015, "interest": 0.01}}


def _household_declaring_foreign_in(kind: str) -> dict:
    """The golden household with EQUAL registered pots, declaring the foreign
    sleeve in ``kind`` and the domestic sleeve in the other registered account.

    Equal rrsp/tfsa balances isolate the per-unit WHT-drag difference (the tax
    lever #641 unblocked) from pot-size effects, so the tax-efficient direction
    (RRSP) is unambiguous.
    """
    cfg = copy.deepcopy(golden_household_config())
    for m in cfg["family"]["members"]:
        if m["role"] == "primary":
            m["rrsp_balance"] = 200_000
            m["tfsa_balance"] = 200_000
        else:
            m["rrsp_balance"] = 0
            m["tfsa_balance"] = 0
    other = "tfsa" if kind == "rrsp" else "rrsp"
    cfg["portfolio"]["accounts"][kind] = copy.deepcopy(FOREIGN)
    cfg["portfolio"]["accounts"][other] = copy.deepcopy(DOMESTIC)
    return cfg


@pytest.fixture(scope="module")
def recommendation() -> dict:
    """Recommendation for a household that DECLARED the sub-optimal placement
    (foreign in the TFSA). Computed once (two optimizer passes)."""
    cfg = _household_declaring_foreign_in("tfsa")
    return recommend_asset_location(cfg)


class TestOptimizerPrefersTaxEfficientPlacement:
    def test_two_distinct_placements_discovered(self):
        cfg = _household_declaring_foreign_in("tfsa")
        placements = discover_placements(cfg)
        assert len(placements) == 2
        # DP#33: the declared arrangement is annotated among the candidates,
        # not replaced by the search.
        assert sum(1 for p in placements if p["declared"]) == 1

    def test_optimizer_picks_foreign_in_rrsp(self, recommendation):
        # The winner shelters the foreign sleeve in the RRSP (treaty-exempt),
        # not the TFSA (unrecoverable WHT) the household happened to declare.
        assert recommendation["foreign_kind"] == "rrsp"
        assert recommendation["chosen"]["declared"] is False

    def test_placements_objectives_differ_and_better_wins(self, recommendation):
        rows = recommendation["ranking"]
        scores = [r["objective_score"] for r in rows]
        # Falsifiable via #641: the two placements are NOT the same number.
        assert scores[0] > scores[1]
        assert recommendation["after_tax_benefit"] > 0


class TestChosenPlacementSurfaced:
    def test_recommendation_carries_reused_drag_comparison(self, recommendation):
        # Reuses asset_location.light_vs_ludicrous (DP#9): the bps tax-drag saved
        # is part of the surfaced recommendation.
        assert "savings_bps" in recommendation["drag_comparison"]

    def test_console_block_names_the_chosen_account(self, recommendation):
        block = format_asset_location(recommendation)
        assert "RRSP" in block
        assert "ASSET LOCATION" in block


class TestAbsenceIsNoOp:
    def test_no_registered_composition_is_a_no_op(self):
        # Golden household declares composition only on non_reg -> nothing to
        # place -> a single arrangement and no recommendation.
        cfg = golden_household_config()
        assert len(discover_placements(cfg)) == 1
        assert recommend_asset_location(cfg) is None

    def test_golden_invariant_unchanged(self):
        assert _run(golden_household_config())[-1].total_assets == 9709753.139463063


class TestDegenerateArrangementsAreNoOps:
    """The other absence paths: two registered slots exist, but there is no
    genuine placement CHOICE to make -- so a single (declared) arrangement is
    returned and no optimizer pass is spent."""

    def _two_registered(self, rrsp: dict, tfsa: dict) -> dict:
        cfg = copy.deepcopy(golden_household_config())
        cfg["portfolio"]["accounts"]["rrsp"] = copy.deepcopy(rrsp)
        cfg["portfolio"]["accounts"]["tfsa"] = copy.deepcopy(tfsa)
        return cfg

    def test_two_slots_but_no_foreign_sleeve_is_a_no_op(self):
        # Both registered accounts hold only domestic assets: nothing whose
        # shelter matters, so no arrangement beats another.
        cfg = self._two_registered(DOMESTIC, DOMESTIC)
        placements = discover_placements(cfg)
        assert len(placements) == 1
        assert placements[0]["declared"] is True

    def test_identical_foreign_profiles_collapse_to_one_arrangement(self):
        # Both accounts hold the SAME foreign sleeve: every permutation is the
        # same decision, so the alternatives dedupe away to the declared one.
        cfg = self._two_registered(FOREIGN, FOREIGN)
        placements = discover_placements(cfg)
        assert len(placements) == 1
        assert placements[0]["declared"] is True


class TestRankingHandlesEmptyOptimizerRuns:
    """A placement whose optimization yields no ranked strategy contributes no
    row; if none do, there is nothing to compare and the recommendation is
    None -- never a fabricated single-placement 'winner' (DP#32)."""

    def test_empty_runs_drop_rows_and_yield_no_recommendation(self, monkeypatch):
        cfg = _household_declaring_foreign_in("tfsa")
        # A real placement choice is discovered (two arrangements)...
        assert len(discover_placements(cfg)) == 2
        # ...but every optimizer pass comes back empty (e.g. a household the
        # search cannot place): each placement is skipped.
        monkeypatch.setattr(optimize, "run_optimization", lambda *a, **k: [])
        assert rank_placements(cfg) == []
        assert recommend_asset_location(cfg) is None


class TestMainSurfaceRecording:
    """optimize._record_asset_location records + prints when a recommendation
    exists, and is a silent no-op when it does not."""

    def test_records_and_prints_when_recommendation_exists(self, monkeypatch, capsys):
        rec = {"foreign_kind": "rrsp", "after_tax_benefit": 12345.0,
               "ranking": [{"placement_label": "Foreign equity in RRSP",
                            "objective_score": 2.0, "declared": False},
                           {"placement_label": "As declared",
                            "objective_score": 1.0, "declared": True}],
               "drag_comparison": {"savings_bps": 7.5}}
        monkeypatch.setattr(asset_location_optimize,
                            "recommend_asset_location", lambda *a, **k: rec)
        cfg = {}
        out = optimize._record_asset_location(cfg, "input.json", MAX_NET_BENEFIT)
        assert out is rec
        assert cfg["assumptions"]["asset_location"] is rec
        assert "ASSET LOCATION" in capsys.readouterr().out

    def test_silent_no_op_when_no_recommendation(self, monkeypatch, capsys):
        monkeypatch.setattr(asset_location_optimize,
                            "recommend_asset_location", lambda *a, **k: None)
        cfg = {}
        assert optimize._record_asset_location(cfg, "input.json", MAX_NET_BENEFIT) is None
        assert "assumptions" not in cfg
        assert capsys.readouterr().out == ""
