#!/usr/bin/env python3
"""Issue #474: risk-aware asset-allocation recommendation.

#473 decided WHICH account holds WHICH asset class; #474 decides HOW MUCH of
each asset class to hold and WHAT KIND of product to buy. These tests assert the
three things the issue and the playbook ask for:

- the recommended equity/fixed-income mix RESPONDS to the household's declared
  risk tolerance and to its horizon (glide path),
- the recommendation is SURFACED (product-category label per bucket + Monte
  Carlo P10/P50/P90), and
- absence is a strict NO-OP: a household that declares no risk tolerance gets no
  recommendation, and the golden trajectory is byte-identical (the module never
  touches the simulation).
"""
import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import risk_allocation
import optimize
from risk_allocation import (
    recommend_allocation, recommended_mix, format_allocation,
)
from test_golden_trajectory_581 import golden_household_config, _run


def _household_with_risk(risk_tolerance, retirement_age=65):
    cfg = copy.deepcopy(golden_household_config())
    cfg["portfolio"]["risk_tolerance"] = risk_tolerance
    for m in cfg["family"]["members"]:
        if m["role"] == "primary":
            m["retirement_age"] = retirement_age
    return cfg


class TestMixRespondsToRiskTolerance:
    def test_more_aggressive_holds_more_equity(self):
        # Same horizon, more aggressive tolerance -> strictly more equity.
        equities = [recommended_mix(rt, years_to_retirement=30)["equity_pct"]
                    for rt in ("conservative", "balanced", "growth", "aggressive")]
        assert equities == sorted(equities)
        assert equities[0] < equities[-1]

    def test_mix_sums_to_one(self):
        mix = recommended_mix("balanced", years_to_retirement=25)
        assert mix["equity_pct"] + mix["fixed_income_pct"] == pytest.approx(1.0)

    def test_unknown_risk_tolerance_fails_loudly(self):
        # DP#32: an undeclared-category value is refused, never coerced.
        with pytest.raises(ValueError):
            recommended_mix("yolo", years_to_retirement=20)


class TestMixRespondsToHorizon:
    def test_nearer_retirement_glides_into_fixed_income(self):
        # Same risk tolerance, shorter horizon -> less equity (glide path).
        far = recommended_mix("growth", years_to_retirement=30)["equity_pct"]
        near = recommended_mix("growth", years_to_retirement=1)["equity_pct"]
        assert near < far

    def test_recommendation_reads_horizon_from_config(self):
        near = recommend_allocation(_household_with_risk("growth", retirement_age=60))
        far = recommend_allocation(_household_with_risk("growth", retirement_age=90))
        assert (near["recommended_mix"]["equity_pct"]
                < far["recommended_mix"]["equity_pct"])


@pytest.fixture(scope="module")
def rec():
    return recommend_allocation(_household_with_risk("balanced"))


class TestRecommendationSurfaced:
    def test_reports_mc_percentiles(self, rec):
        m = rec["risk_metrics"]
        # Acceptance: the mix is reported with P10/P50/P90 (terminal multiple).
        assert m["p10"] < m["p50"] < m["p90"]
        assert "probability_of_loss" in m

    def test_higher_equity_has_higher_median_and_wider_spread(self):
        cons = recommend_allocation(_household_with_risk("conservative"))["risk_metrics"]
        aggr = recommend_allocation(_household_with_risk("aggressive"))["risk_metrics"]
        assert aggr["p50"] > cons["p50"]
        assert (aggr["p90"] - aggr["p10"]) > (cons["p90"] - cons["p10"])

    def test_names_a_product_category_per_bucket(self, rec):
        buckets = {b["bucket"]: b for b in rec["product_categories"]}
        assert "equity" in buckets and "fixed_income" in buckets
        assert "equity index" in buckets["equity"]["category"]
        assert "bond" in buckets["fixed_income"]["category"].lower()

    def test_console_block_shows_mix_and_category(self, rec):
        block = format_allocation(rec)
        assert "RISK-AWARE ALLOCATION" in block
        assert "% equity" in block
        assert "equity index" in block

    def test_per_account_mix_covers_declared_accounts(self, rec):
        # A recommended mix is reported for each portfolio account.
        assert set(rec["per_account"]) == set(
            _household_with_risk("balanced")["portfolio"]["accounts"])


class TestDeclaredMixAnnotates:
    """DP#33: a declared composition is carried as an annotation, not used to
    replace the recommended search."""

    def test_declared_mix_reported_alongside_recommendation(self):
        rec = recommend_allocation(_household_with_risk("balanced"))
        # The golden household declares non_reg 60/40 -> surfaced as declared_mix,
        # distinct from the recommended mix (annotation, not override).
        assert rec["declared_mix"] == pytest.approx(
            {"equity_pct": 0.6, "fixed_income_pct": 0.4})
        assert rec["recommended_mix"] != rec["declared_mix"]

    def test_declared_mix_none_when_no_composition(self):
        cfg = _household_with_risk("balanced")
        cfg["portfolio"]["accounts"] = {}
        assert recommend_allocation(cfg)["declared_mix"] is None


class TestAbsenceIsNoOp:
    def test_no_risk_tolerance_is_a_no_op(self):
        # Golden household declares no risk tolerance -> no recommendation.
        assert recommend_allocation(golden_household_config()) is None

    def test_missing_horizon_is_a_no_op(self):
        # Risk declared but the primary's dating fields are absent -> None, not a
        # coerced glide (DP#32).
        cfg = _household_with_risk("balanced")
        for m in cfg["family"]["members"]:
            m.pop("retirement_age", None)
        assert recommend_allocation(cfg) is None

    def test_no_primary_member_is_a_no_op(self):
        # Risk declared but no member carries the 'primary' role -> the horizon
        # cannot be dated, so no recommendation (DP#32), never a coerced glide.
        cfg = _household_with_risk("balanced")
        for m in cfg["family"]["members"]:
            m["role"] = "spouse"
        assert recommend_allocation(cfg) is None

    def test_golden_invariant_unchanged(self):
        assert _run(golden_household_config())[-1].total_assets == 9709753.139463063


class TestMainSurfaceRecording:
    """optimize._record_risk_allocation records + prints when a recommendation
    exists, and is a silent no-op when it does not."""

    def test_records_and_prints_when_recommendation_exists(self, monkeypatch, capsys):
        rec = {
            "risk_tolerance": "balanced", "years_to_retirement": 20,
            "recommended_mix": {"equity_pct": 0.6, "fixed_income_pct": 0.4},
            "declared_mix": None,
            "product_categories": [
                {"bucket": "equity", "pct": 0.6, "etf_type": "international",
                 "category": "broad-market global equity index ETF"},
                {"bucket": "fixed_income", "pct": 0.4, "etf_type": "bonds",
                 "category": "aggregate-bond ETF or GIC ladder"}],
            "risk_metrics": {"horizon_years": 30, "p10": 1.5, "p50": 3.0,
                             "p90": 6.0, "probability_of_loss": 0.02},
            "per_account": {},
        }
        monkeypatch.setattr(risk_allocation, "recommend_allocation",
                            lambda *a, **k: rec)
        cfg = {}
        out = optimize._record_risk_allocation(cfg)
        assert out is rec
        assert cfg["assumptions"]["risk_allocation"] is rec
        assert "RISK-AWARE ALLOCATION" in capsys.readouterr().out

    def test_silent_no_op_when_no_recommendation(self, monkeypatch, capsys):
        monkeypatch.setattr(risk_allocation, "recommend_allocation",
                            lambda *a, **k: None)
        cfg = {}
        assert optimize._record_risk_allocation(cfg) is None
        assert "assumptions" not in cfg
        assert capsys.readouterr().out == ""


class TestReturnBeliefsArePluggable:
    """DP#21 (#993): the sleeve return beliefs (equity/fixed-income mean & sigma)
    are pluggable INPUT, not hardcoded constants. A user who disagrees with 6.8%
    equity can override via ``assumptions.return_beliefs`` (or the parameter)
    WITHOUT editing source -- and the defaults are preserved byte-for-byte so a
    household that declares nothing sees exactly the number it saw before."""

    def test_defaults_preserve_blended_mean_byte_exact(self):
        # No return_beliefs declared -> the blended mean equals the value
        # computed from the pre-#993 module constants (0.068 / 0.030), for any
        # mix. This is the byte-exact-preservation guard.
        from risk_allocation import _DEFAULT_RETURN_BELIEFS, _mc_risk_metrics
        mix = {"equity_pct": 0.6, "fixed_income_pct": 0.4}
        metrics = _mc_risk_metrics(mix, years_to_horizon=5)
        expected_mean = (0.6 * _DEFAULT_RETURN_BELIEFS["equity_mean"]
                         + 0.4 * _DEFAULT_RETURN_BELIEFS["fixed_income_mean"])
        assert metrics["blended_mean"] == pytest.approx(expected_mean)
        # And specifically the historical 0.068/0.030 literals:
        assert metrics["blended_mean"] == pytest.approx(0.6 * 0.068 + 0.4 * 0.030)

    def test_override_via_config_changes_blended_mean(self):
        # Declaring assumptions.return_beliefs flows through to the metrics.
        cfg = _household_with_risk("balanced")
        baseline = recommend_allocation(cfg)["risk_metrics"]["blended_mean"]
        cfg["assumptions"]["return_beliefs"] = {
            "equity_mean": 0.10, "fixed_income_mean": 0.04,
            "equity_sigma": 0.20, "fixed_income_sigma": 0.06,
        }
        overridden = recommend_allocation(cfg)["risk_metrics"]["blended_mean"]
        assert overridden != pytest.approx(baseline)
        mix = recommend_allocation(cfg)["recommended_mix"]
        assert overridden == pytest.approx(
            mix["equity_pct"] * 0.10 + mix["fixed_income_pct"] * 0.04)

    def test_override_via_parameter_changes_blended_mean(self):
        # The return_beliefs parameter overrides even when the config declares
        # nothing (and the config-declared value wins when the param is None).
        from risk_allocation import _mc_risk_metrics
        mix = {"equity_pct": 0.5, "fixed_income_pct": 0.5}
        default_mean = _mc_risk_metrics(mix, years_to_horizon=5)["blended_mean"]
        custom = _mc_risk_metrics(
            mix, years_to_horizon=5,
            return_beliefs={"equity_mean": 0.12, "fixed_income_mean": 0.02,
                            "equity_sigma": 0.18, "fixed_income_sigma": 0.04})
        assert custom["blended_mean"] == pytest.approx(0.5 * 0.12 + 0.5 * 0.02)
        assert custom["blended_mean"] != pytest.approx(default_mean)

    def test_explicit_zero_belief_is_honoured_not_coerced(self):
        # DP#32: an explicit 0.0 equity_mean is a real value, never coerced back
        # to the 0.068 default. No ``x or DEFAULT``.
        from risk_allocation import _mc_risk_metrics
        mix = {"equity_pct": 1.0, "fixed_income_pct": 0.0}
        metrics = _mc_risk_metrics(
            mix, years_to_horizon=3,
            return_beliefs={"equity_mean": 0.0, "fixed_income_mean": 0.0,
                            "equity_sigma": 0.0, "fixed_income_sigma": 0.0})
        assert metrics["blended_mean"] == 0.0

    def test_partial_override_keeps_undeclared_beliefs(self):
        # A partial override (only equity_mean) does not silently drop the other
        # three beliefs to 0 -- absent keys in an explicit override fall back to
        # the module constants, not to a coerced zero (DP#32).
        from risk_allocation import (_DEFAULT_RETURN_BELIEFS, _mc_risk_metrics,
                                     _resolve_return_beliefs)
        cfg = {"assumptions": {"return_beliefs": {"equity_mean": 0.09}}}
        resolved = _resolve_return_beliefs(cfg)
        assert resolved["equity_mean"] == 0.09
        assert resolved["equity_sigma"] == _DEFAULT_RETURN_BELIEFS["equity_sigma"]
        assert resolved["fixed_income_mean"] == _DEFAULT_RETURN_BELIEFS["fixed_income_mean"]
        assert resolved["fixed_income_sigma"] == _DEFAULT_RETURN_BELIEFS["fixed_income_sigma"]
        mix = {"equity_pct": 1.0, "fixed_income_pct": 0.0}
        metrics = _mc_risk_metrics(mix, years_to_horizon=3,
                                   return_beliefs=resolved)
        assert metrics["blended_mean"] == pytest.approx(0.09)

    def test_no_beliefs_declared_falls_back_to_module_constants(self):
        # A config with no assumptions.return_beliefs resolves to the module
        # constants exactly (byte-exact default path).
        from risk_allocation import _DEFAULT_RETURN_BELIEFS, _resolve_return_beliefs
        cfg = _household_with_risk("balanced")
        assert "return_beliefs" not in cfg.get("assumptions", {})
        assert _resolve_return_beliefs(cfg) == _DEFAULT_RETURN_BELIEFS
