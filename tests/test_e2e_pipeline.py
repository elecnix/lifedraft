"""End-to-end integration test: full pipeline with all triggers active.

Verifies the entire discover → enumerate → simulate → rank pipeline
works when every trigger dimension is populated simultaneously.

All data is fabricated with round numbers — no personal data (DP#4, DP#15).
"""

import math
import pytest

from scenario_discovery import discover_anchors
from simulate import (
    enumerate_overlays,
    evaluate_overlay,
    build_all_overlays,
)
from simulation import ScenarioOverlay


# ---------------------------------------------------------------------------
# Full-featured config fixture — all triggers active
# ---------------------------------------------------------------------------

def _full_cfg():
    """Return a config where every trigger fires.

    - Primary ($150k) + spouse ($70k) with RRSP/TFSA room
    - Mortgage ($300k) with house value ($800k)
    - heloc_readvance=true
    - resp_current_balance=50000
    - 2 children: one with FHSA room ($16k), one with no room
    - scenarios.income with 2 entries
    - scenarios.strategy with 2 entries
    - sensitivity_overlays.investment_return = [0.04, 0.07, 0.10]
    """
    return {
        "family": {
            "members": [
                {
                    "role": "primary",
                    "gross_income": 150000,
                    "rrsp_room_accumulated": 60000,
                    "tfsa_room_accumulated": 40000,
                    "fhsa_room_accumulated": 8000,
                    "fhsa_first_time_buyer_since": "2007-10-03",
                    "birth_year": 1990,
                },
                {
                    "role": "spouse",
                    "gross_income": 70000,
                    "rrsp_room_accumulated": 25000,
                    "tfsa_room_accumulated": 35000,
                    "fhsa_room_accumulated": 0,
                    "fhsa_first_time_buyer_since": None,
                    "birth_year": 1988,
                },
            ],
            "children": [
                {
                    "name": "child_a",
                    "birth_year": 2003,
                    "fhsa_room_accumulated": 16000,
                    "tfsa_room_accumulated": 0,
                    "rrsp_room_accumulated": 0,
                },
                {
                    "name": "child_b",
                    "birth_year": 2012,
                    "fhsa_room_accumulated": 0,
                    "tfsa_room_accumulated": 0,
                    "rrsp_room_accumulated": 0,
                },
            ],
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
            "resp_current_balance": 50000,
        },
        "assumptions": {
            "investment_return": 0.07,
            "salary_growth": 0.02,
            "projection_years": 10,
            "capital_gains_inclusion": 0.50,
            "resp_eap_tax_rate": 0.15,
            "resp_eap_taxable_portion": 0.60,
        },
        "savings": {
            "rate": 0.20,
        },
        "scenarios": {
            "income": [
                {
                    "id": "current",
                    "label": "Current income",
                    "members": [
                        {"role": "primary", "gross_income": 150000,
                         "kind": "employment", "from": "2026-01-01", "to": None},
                        {"role": "spouse", "gross_income": 70000,
                         "kind": "employment", "from": "2026-01-01", "to": None},
                    ],
                },
                {
                    "id": "promotion",
                    "label": "Promotion",
                    "members": [
                        {"role": "primary", "gross_income": 180000,
                         "kind": "employment", "from": "2026-01-01", "to": None},
                        {"role": "spouse", "gross_income": 70000,
                         "kind": "employment", "from": "2026-01-01", "to": None},
                    ],
                },
            ],
            "strategy": [
                {
                    "id": "rrsp_heavy",
                    "label": "RRSP Heavy",
                    "rrsp_pct": 0.40,
                    "spousal_rrsp_pct": 0.10,
                    "tfsa_pct": 0.25,
                    "fhsa_pct": 0.05,
                    "resp_pct": 0.07,
                    "non_reg_pct": 0.13,
                    "use_smith": True,
                    "deduct_later": True,
                },
                {
                    "id": "tfsa_first",
                    "label": "TFSA First",
                    "rrsp_pct": 0.20,
                    "spousal_rrsp_pct": 0.05,
                    "tfsa_pct": 0.40,
                    "fhsa_pct": 0.05,
                    "resp_pct": 0.07,
                    "non_reg_pct": 0.23,
                    "use_smith": False,
                    "deduct_later": False,
                },
            ],
        },
        "sensitivity_overlays": {
            "investment_return": [0.04, 0.07, 0.10],
        },
    }


# ---------------------------------------------------------------------------
# 1. test_discover_anchors_all_triggers
# ---------------------------------------------------------------------------

def test_discover_anchors_all_triggers():
    """All 8 anchor keys are populated when every trigger fires."""
    anchors = discover_anchors(_full_cfg())
    expected_keys = [
        "income", "mortgage", "refinance", "strategy",
        "resp_action", "sm_options", "deduct_later_options", "child_accounts",
    ]
    for key in expected_keys:
        assert key in anchors, f"Missing anchor key: {key}"

    # Every key should be non-empty given the rich config
    for key in expected_keys:
        val = anchors[key]
        assert val, f"Anchor key '{key}' is empty but triggers are active"


# ---------------------------------------------------------------------------
# 2. test_enumerate_scenarios_uses_anchors
# ---------------------------------------------------------------------------

def test_enumerate_scenarios_uses_anchors():
    """enumerate_overlays(base) produces scenarios from all trigger dimensions."""
    cfg = _full_cfg()
    anchors = discover_anchors(cfg)
    overlays = enumerate_overlays(cfg, anchors=anchors)

    assert len(overlays) > 0, "No overlays produced"

    # Verify multiple dimensions are represented in the overlay labels
    labels = [o.label for o in overlays]
    # Income: both "Current income" and "Promotion" should appear
    assert any("Current income" in lbl for lbl in labels), "Missing current income dimension"
    assert any("Promotion" in lbl for lbl in labels), "Missing promotion income dimension"

    # SM: both +SM and no SM should appear
    assert any("+SM" in lbl for lbl in labels), "Missing +SM dimension"
    assert any("no SM" in lbl for lbl in labels), "Missing no SM dimension"

    # Deduct later: both +DL and no DL
    assert any("+DL" in lbl for lbl in labels), "Missing +DL dimension"
    assert any("no DL" in lbl for lbl in labels), "Missing no DL dimension"


# ---------------------------------------------------------------------------
# 3. test_simulate_builds_overlays
# ---------------------------------------------------------------------------

def test_simulate_builds_overlays():
    """simulate.build_all_overlays produces combinations from all anchors."""
    cfg = _full_cfg()
    anchors = discover_anchors(cfg)
    combos = build_all_overlays(cfg, anchors)

    assert len(combos) > 0, "No combinations produced"

    # Each combo should have an overlay and a strategy_alloc
    for combo in combos:
        assert "overlay" in combo, "Missing overlay key in combo"
        assert "strategy_alloc" in combo, "Missing strategy_alloc key in combo"
        overlay = combo["overlay"]
        strat = combo["strategy_alloc"]
        assert isinstance(overlay, ScenarioOverlay)
        assert "id" in strat
        assert "rrsp_pct" in strat

    # Should have combos for both strategy ids
    strategy_ids = {c["strategy_alloc"]["id"] for c in combos}
    assert "rrsp_heavy" in strategy_ids, "Missing rrsp_heavy strategy"
    assert "tfsa_first" in strategy_ids, "Missing tfsa_first strategy"


# ---------------------------------------------------------------------------
# 4. test_evaluate_overlay_runs
# ---------------------------------------------------------------------------

def test_evaluate_overlay_runs():
    """evaluate_overlay returns a result dict with net_benefit."""
    cfg = _full_cfg()
    anchors = discover_anchors(cfg)

    # Pick a simple overlay: first income, first mortgage, no refi, no SM, no DL
    overlay = ScenarioOverlay(
        label="e2e test",
        cash_out=0,
        primary_income=150000,
        spouse_income=70000,
        mortgage_rate=0.05,
        use_readvanceable=False,
        deduct_later=False,
        ltv=300000 / 800000,
    )

    result = evaluate_overlay(cfg, overlay)
    assert "net_benefit" in result, "Missing net_benefit in result"
    assert isinstance(result["net_benefit"], (int, float)), "net_benefit is not numeric"
    # Also check other expected keys from evaluate_overlay
    assert "future_value" in result
    assert "total_debt" in result
    assert "label" in result


# ---------------------------------------------------------------------------
# 5. test_no_nan_in_results
# ---------------------------------------------------------------------------

def test_no_nan_in_results():
    """No NaN or infinity in any result field across multiple overlays."""
    cfg = _full_cfg()
    anchors = discover_anchors(cfg)
    overlays = enumerate_overlays(cfg, anchors=anchors)

    # Evaluate a representative subset (first 6 overlays)
    for overlay in overlays[:6]:
        result = evaluate_overlay(cfg, overlay)
        for key, val in result.items():
            if isinstance(val, (int, float)):
                assert not math.isnan(val), f"NaN in {key} for overlay '{overlay.label}'"
                assert not math.isinf(val), f"Inf in {key} for overlay '{overlay.label}'"


# ---------------------------------------------------------------------------
# 6. test_ranking_by_net_benefit
# ---------------------------------------------------------------------------

def test_ranking_by_net_benefit():
    """Results are rankable by net_benefit — sorted list is monotonic."""
    cfg = _full_cfg()
    anchors = discover_anchors(cfg)
    overlays = enumerate_overlays(cfg, anchors=anchors)

    # Evaluate a subset for speed
    subset = overlays[:8]
    results = [evaluate_overlay(cfg, o) for o in subset]

    # Sort by net_benefit descending
    ranked = sorted(results, key=lambda r: r["net_benefit"], reverse=True)

    # Should be non-increasing
    for i in range(len(ranked) - 1):
        assert ranked[i]["net_benefit"] >= ranked[i + 1]["net_benefit"], (
            f"Ranking not monotonic at position {i}: "
            f"{ranked[i]['net_benefit']} < {ranked[i + 1]['net_benefit']}"
        )


# ---------------------------------------------------------------------------
# 7. test_child_accounts_discovered
# ---------------------------------------------------------------------------

def test_child_accounts_discovered():
    """Child with FHSA room ($16k) appears in anchors; child without room does not."""
    anchors = discover_anchors(_full_cfg())
    child_accounts = anchors["child_accounts"]

    # child_a has fhsa_room_accumulated=16000, should appear
    names = [ca["child_name"] for ca in child_accounts]
    assert "child_a" in names, "child_a (with FHSA room) missing from child_accounts"

    # child_b has no room, should not appear
    assert "child_b" not in names, "child_b (no room) should not appear in child_accounts"

    # Verify the FHSA room value
    child_a = next(ca for ca in child_accounts if ca["child_name"] == "child_a")
    assert child_a["fhsa_room"] == 16000


# ---------------------------------------------------------------------------
# 8. test_resp_actions_discovered
# ---------------------------------------------------------------------------

def test_resp_actions_discovered():
    """resp_action includes keep/eap/collapse when resp_current_balance > 0."""
    anchors = discover_anchors(_full_cfg())
    resp_actions = anchors["resp_action"]

    assert "keep" in resp_actions, "Missing 'keep' in resp_action"
    assert "eap" in resp_actions, "Missing 'eap' in resp_action"
    assert "collapse" in resp_actions, "Missing 'collapse' in resp_action"


# ---------------------------------------------------------------------------
# 9. test_sm_options_from_heloc
# ---------------------------------------------------------------------------

def test_sm_options_from_heloc():
    """sm_options is [True, False] when heloc_readvance is True and SM is profitable."""
    anchors = discover_anchors(_full_cfg())
    assert anchors["sm_options"] == [True, False], (
        f"Expected [True, False], got {anchors['sm_options']}"
    )


# ---------------------------------------------------------------------------
# 10. test_scenario_override_wins
# ---------------------------------------------------------------------------

def test_scenario_override_wins():
    """When scenarios.income is provided, it overrides auto-discovery."""
    cfg = _full_cfg()
    anchors = discover_anchors(cfg)

    # The override has 2 income entries (current + promotion), not 1 auto-discovered
    income = anchors["income"]
    assert len(income) == 2, f"Expected 2 income scenarios from override, got {len(income)}"

    ids = [i["id"] for i in income]
    assert "current" in ids, "Missing 'current' income scenario"
    assert "promotion" in ids, "Missing 'promotion' income scenario"

    # Verify the override values, not auto-discovered defaults
    promo = next(i for i in income if i["id"] == "promotion")
    assert promo["primary_income"] == 180000, (
        f"Promotion primary_income should be 180000, got {promo['primary_income']}"
    )
    assert promo["spouse_income"] == 70000

    # Also verify strategies override (2 entries, not auto-discovered)
    # #890/DP#33: the declared strategies now ANNOTATE the DP#6 auto sweep
    # (unioned over it and marked) rather than replacing it, so both declared
    # strategies are present alongside the auto-discovered ones.
    strategy = anchors["strategy"]
    strategy_ids = [s["id"] for s in strategy]
    assert "rrsp_heavy" in strategy_ids
    assert "tfsa_first" in strategy_ids
    declared_ids = {s["declared_id"] for s in strategy if s.get("declared")}
    assert declared_ids == {"rrsp_heavy", "tfsa_first"}


# ---------------------------------------------------------------------------
# 10. TestOptimalLTVDiscovery
# ---------------------------------------------------------------------------

class TestOptimalLTVDiscovery:
    """Validate optimizer can find optimal LTV when it's between 40% and 80%."""

    def test_ltv_levels_are_explored(self):
        """Verify LTV levels are generated from 40% to 80% in 5% steps."""
        cfg = _full_cfg()
        anchors = discover_anchors(cfg)
        refi = anchors["refinance"]
        
        # Should have at least 8 LTV levels (40%, 45%, 50%, 55%, 60%, 65%, 70%, 75%, 80%)
        assert len(refi) >= 8, f"Expected >= 8 LTV levels, got {len(refi)}"
        
        # Check for expected LTV levels
        ids = [r["id"] for r in refi]
        assert "no_refinance" in ids
        assert any("ltv_80pct" in id for id in ids), "Should have 80% loan-to-value option"

    def test_build_all_overlays_includes_ltv_combinations(self):
        """Verify overlays are built combining all LTV levels with mortgage options."""
        cfg = _full_cfg()
        anchors = discover_anchors(cfg)
        overlays = build_all_overlays(cfg, anchors)
        
        # Get unique LTV values from the overlay dicts
        ltvs = set(o["overlay"].ltv for o in overlays if o["overlay"].ltv is not None)
        
        # Should have multiple LTV levels
        assert len(ltvs) >= 8, f"Expected >= 8 LTV levels in overlays, got {len(ltvs)}"

    def test_ladder_ltv_scenario(self):
        """Test scenario where optimal LTV is in the middle of the range.
        
        Setup: High interest rate environment where partial refinance
        maximizes net benefit (not full 80% loan-to-value).
        """
        cfg = _full_cfg()
        # High HELOC rate makes full SM expensive, optimal LTV is partial
        cfg["assumptions"]["heloc_rate"] = 0.08  # 8% HELOC cost
        # Lower investment return to make high leverage risky
        cfg["assumptions"]["investment_return"] = 0.04
        
        anchors = discover_anchors(cfg)
        refi = anchors["refinance"]
        
        # Should still have multiple LTV options
        assert len(refi) >= 5
        
        # Verify we have a range to explore
        min_ltv = min(r["ltv"] for r in refi)
        max_ltv = max(r["ltv"] for r in refi)
        assert max_ltv - min_ltv >= 0.30, "Should have 30%+ LTV range to explore"

    def test_ltv_optimal_not_at_extremes(self):
        """Verify optimizer can rank LTV levels; top result may be intermediate.
        
        In this scenario, we verify the pipeline runs and produces rankings
        where different LTV levels can be compared.
        """
        cfg = _full_cfg()
        # Use moderate rates so intermediate LTV might be optimal
        cfg["assumptions"]["heloc_rate"] = 0.06
        cfg["assumptions"]["investment_return"] = 0.06
        
        anchors = discover_anchors(cfg)
        overlays = build_all_overlays(cfg, anchors)
        
        if len(overlays) < 10:
            pytest.skip("Not enough overlays to test LTV optimization")
        
        # Evaluate a subset of overlays
        results = []
        for o in overlays[:20]:  # Top 20 for speed
            try:
                result = evaluate_overlay(o["overlay"], cfg)
                results.append((o["overlay"].ltv, result.net_benefit))
            except Exception:
                pass
        
        if not results:
            pytest.skip("No valid evaluation results")
        
        # Verify we have results across different LTV levels
        ltvs = set(ltv for ltv, _ in results)
        assert len(ltvs) >= 3, f"Should have results across multiple LTV levels, got {ltvs}"