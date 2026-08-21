#!/usr/bin/env python3
"""Issue #859 Part B: asset location ACROSS family members.

Part A (#908) built the family balance sheet; #473 optimizes WHICH of ONE
member's accounts holds a given asset class. Part B extends that decision ACROSS
members: place a foreign-equity sleeve in the MEMBER (and their registered
account) where it is most tax-efficient for the family as a whole -- e.g. in the
lower-marginal-rate spouse's RRSP -- maximizing the FAMILY after-tax objective
(``_family_after_tax_networth``, Part A / #861).

Two levers, both reused (DP#9 -- no second spelling):
  - #641's ``PortfolioConfig.registered_wht_drag()``: the foreign sleeve leaks
    foreign withholding tax that differs by account KIND (treaty-exempt in an
    RRSP, unrecoverable in a TFSA), so the composition reaches the score; a
    domestic sleeve leaks nothing (falsifiable-via-#641).
  - #861's per-member deemed-disposition seam (``after_tax_networth_of_own_
    accounts``): each member's registered pot is taxed as ordinary income at
    their OWN terminal bracket, so a growth sleeve sheltered in a LOWER-bracket
    member's RRSP is taxed less at the horizon -- the cross-member decision.

Absence is a strict no-op: a household that declares no cross-member sleeve (the
golden household) has nothing to place, so the discovery yields a single
arrangement and the recommendation is ``None`` (DP#32) -- the golden terminal
total_assets is byte-identical.

SCOPE (#917): like #473 this operates at the internal-config / optimize-flow
level. The contract adapter threads only ``non_reg`` composition today, so this
is reachable + tested from an internally-constructed multi-member config but is
a no-op from a ``--input`` contract until #917 wires per-member composition.

DP#15: role labels and fabricated round numbers only -- no personal data.
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
    discover_member_placements,
    rank_member_placements,
    recommend_cross_member_location,
    format_cross_member_location,
)
from objective import MAX_FAMILY_AFTER_TAX_NETWORTH
from test_golden_trajectory_581 import golden_household_config, _run

# A strongly-foreign registered sleeve (US/intl dividends attract WHT) and an
# otherwise-identical domestic sleeve (no foreign income -> no WHT leak).
FOREIGN = {"composition": {"us_equity_pct": 0.6, "intl_equity_pct": 0.4},
           "yield": {"foreign_income": 0.02}}
DOMESTIC = {"composition": {"cdn_equity_pct": 0.6, "fixed_income_pct": 0.4},
            "yield": {"eligible_dividends": 0.015, "interest": 0.01}}


def _cfg(members, sleeve, home, kinds_note=None) -> dict:
    """A family config declaring a cross-member sleeve to place. ``members`` set
    each member's registered brackets; ``home`` is the declared placement."""
    return {
        "tax": {"province": "quebec", "year": 2026},
        "projection_years": 20,
        "investment_return": 0.05,
        "family": {"members": members},
        "asset_location": {
            "cross_member_sleeve": {**copy.deepcopy(sleeve), "balance": 100_000,
                                    "home": home},
        },
    }


# A high-bracket member (large base RRSP -> high marginal rate on incremental
# registered dollars) and a low-bracket member (no base RRSP), each declaring
# only an RRSP slot so the placement decision is purely CROSS-MEMBER.
HIGH = {"id": "high", "role": "primary", "registered": {"rrsp": 400_000}}
LOW = {"id": "low", "role": "spouse", "registered": {"rrsp": 0}}


class TestCrossMemberPlacementPrefersLowerBracketMember:
    """The headline: the same foreign sleeve is placed in the LOWER-bracket
    member's RRSP because that maximizes the FAMILY after-tax objective."""

    def test_two_distinct_member_placements_discovered(self):
        cands = discover_member_placements(_cfg([HIGH, LOW], FOREIGN,
                                                {"member": "high", "kind": "rrsp"}))
        # one per (member, kind) slot; DP#33: the declared placement is annotated.
        assert len(cands) == 2
        assert sum(1 for c in cands if c["declared"]) == 1

    def test_family_objective_differs_by_member_and_lower_wins(self):
        cfg = _cfg([HIGH, LOW], FOREIGN, {"member": "high", "kind": "rrsp"})
        rows = rank_member_placements(cfg)
        by_member = {r["member"]: r["objective_score"] for r in rows}
        # Falsifiable: the two members are NOT the same family number...
        assert by_member["low"] != by_member["high"]
        # ...and sheltering the growth in the low-bracket member's RRSP wins.
        assert by_member["low"] > by_member["high"]

    def test_optimizer_places_sleeve_in_the_low_bracket_member(self):
        cfg = _cfg([HIGH, LOW], FOREIGN, {"member": "high", "kind": "rrsp"})
        rec = recommend_cross_member_location(cfg)
        assert rec["chosen"]["member"] == "low"
        assert rec["chosen"]["kind"] == "rrsp"
        # the household DECLARED the sub-optimal (high) placement -> a real move.
        assert rec["chosen"]["declared"] is False
        assert rec["after_tax_benefit"] > 0

    def test_ranked_by_the_family_objective(self):
        cfg = _cfg([HIGH, LOW], FOREIGN, {"member": "high", "kind": "rrsp"})
        rec = recommend_cross_member_location(cfg)
        assert rec["objective"] == MAX_FAMILY_AFTER_TAX_NETWORTH.name


class TestFalsifiableViaComposition:
    """DP#18/#641: the sleeve's COMPOSITION reaches the score -- a foreign sleeve
    (WHT drag) yields a different family number than an identical domestic one."""

    def test_foreign_sleeve_scores_below_domestic_in_same_slot(self):
        home = {"member": "low", "kind": "rrsp"}
        foreign = rank_member_placements(_cfg([HIGH, LOW], FOREIGN, home))
        domestic = rank_member_placements(_cfg([HIGH, LOW], DOMESTIC, home))
        f_low = next(r["objective_score"] for r in foreign if r["member"] == "low")
        d_low = next(r["objective_score"] for r in domestic if r["member"] == "low")
        # The foreign sleeve's unrecoverable WHT drag compounds away growth the
        # domestic sleeve keeps -> strictly poorer. Composition reached the score.
        assert f_low < d_low

    def test_domestic_sleeve_has_no_wht_drag(self):
        # A domestic-only sleeve leaks no foreign WHT: its drag map is empty.
        assert asset_location_optimize._sleeve_drag(DOMESTIC) == {}
        assert asset_location_optimize._sleeve_drag(FOREIGN)["rrsp"] > 0


class TestAbsenceIsNoOp:
    def test_no_cross_member_sleeve_is_a_no_op(self):
        cfg = golden_household_config()
        assert len(discover_member_placements(cfg)) == 1
        assert recommend_cross_member_location(cfg) is None

    def test_golden_invariant_unchanged(self):
        assert _run(golden_household_config())[-1].total_assets == 9709753.139463063


class TestDegenerateArrangementsAreNoOps:
    """Two registered slots must exist across DISTINCT members, a sleeve must be
    declared, and the horizon must be projectable -- else a single (declared)
    arrangement is returned and no family valuation is spent (DP#32)."""

    def test_single_member_is_not_a_cross_member_decision(self):
        cfg = _cfg([HIGH], FOREIGN, {"member": "high", "kind": "rrsp"})
        assert len(discover_member_placements(cfg)) == 1
        assert recommend_cross_member_location(cfg) is None

    def test_absent_projection_horizon_is_a_no_op(self):
        cfg = _cfg([HIGH, LOW], FOREIGN, {"member": "high", "kind": "rrsp"})
        del cfg["projection_years"]
        assert len(discover_member_placements(cfg)) == 1
        assert recommend_cross_member_location(cfg) is None

    def test_absent_gross_return_is_a_no_op(self):
        cfg = _cfg([HIGH, LOW], FOREIGN, {"member": "high", "kind": "rrsp"})
        del cfg["investment_return"]
        assert len(discover_member_placements(cfg)) == 1
        assert recommend_cross_member_location(cfg) is None

    def test_member_without_registered_pots_is_skipped_in_scoring(self):
        """A family member declaring NO registered pots (e.g. a young child with
        only a RESP) is not a placement host and contributes nothing to the
        family after-tax total -- it is skipped when scoring, so adding it leaves
        every candidate's objective_score byte-identical to the two-earner family
        (asset_location_optimize.py:469, the `registered is None` continue)."""
        home = {"member": "high", "kind": "rrsp"}
        NO_REG = {"id": "child", "role": "child"}  # no 'registered' key at all
        without = rank_member_placements(_cfg([HIGH, LOW], FOREIGN, home))
        with_noreg = rank_member_placements(_cfg([HIGH, LOW, NO_REG], FOREIGN, home))
        # The no-registered member adds no host slot -> same two candidates...
        assert [r["member"] for r in with_noreg] == [r["member"] for r in without]
        # ...and adds 0 to every family total -> identical scores (it was skipped).
        for a, b in zip(with_noreg, without):
            assert a["objective_score"] == b["objective_score"]


class TestChosenPlacementSurfaced:
    def test_console_block_names_the_chosen_member(self):
        cfg = _cfg([HIGH, LOW], FOREIGN, {"member": "high", "kind": "rrsp"})
        block = format_cross_member_location(recommend_cross_member_location(cfg))
        assert "ASSET LOCATION" in block
        assert "low" in block


class TestMainSurfaceRecording:
    """optimize._record_cross_member_asset_location records + prints when a
    recommendation exists, and is a silent no-op when it does not."""

    def test_records_and_prints_when_recommendation_exists(self, monkeypatch, capsys):
        rec = {"chosen": {"member": "low", "kind": "rrsp", "declared": False,
                          "label": "Foreign sleeve in low's RRSP",
                          "objective_score": 2.0},
               "objective": MAX_FAMILY_AFTER_TAX_NETWORTH.name,
               "after_tax_benefit": 12345.0,
               "ranking": [{"member": "low", "label": "Foreign sleeve in low's RRSP",
                            "objective_score": 2.0, "declared": False},
                           {"member": "high", "label": "Foreign sleeve in high's RRSP",
                            "objective_score": 1.0, "declared": True}]}
        monkeypatch.setattr(asset_location_optimize,
                            "recommend_cross_member_location", lambda *a, **k: rec)
        cfg = {}
        out = optimize._record_cross_member_asset_location(cfg)
        assert out is rec
        assert cfg["assumptions"]["cross_member_asset_location"] is rec
        assert "ASSET LOCATION" in capsys.readouterr().out

    def test_silent_no_op_when_no_recommendation(self, monkeypatch, capsys):
        monkeypatch.setattr(asset_location_optimize,
                            "recommend_cross_member_location", lambda *a, **k: None)
        cfg = {}
        assert optimize._record_cross_member_asset_location(cfg) is None
        assert "assumptions" not in cfg
        assert capsys.readouterr().out == ""
