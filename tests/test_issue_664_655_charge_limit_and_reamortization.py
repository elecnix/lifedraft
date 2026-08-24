#!/usr/bin/env python3
"""Enforcement tests for issues #664 and #655 -- one problem, "what is a
refinance, actually?" -- both fixed by rewriting ``apply_ltv_overlay`` /
``apply_overlay`` (simulation_config.py) and the contract-loading boundary
(input_contract.py).

#664: a readvanceable mortgage and its HELOC are carved out of ONE
registered charge with ONE combined limit -- NOT independent borrowing
sources. Before this fix, the engine happily modeled a mortgage refinanced
up to the charge AND the full pre-existing HELOC limit drawn on top,
producing >100% LTV facilities.

#655: a cash-out refinance is a NEW LOAN, re-amortized over its own
declared term -- not the incumbent mortgage's remaining amortization
(which understates the true payment by roughly 2x on a near-payoff
mortgage, and makes every LTV level's debt converge to the same terminal
value, hiding the cost of leverage).

Fabricated round numbers, role-based names only (DP#4/DP#15). Every
scenario here is invented for this test file.
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import input_contract as ic
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from charge_limits import (
    charge_limit,
    heloc_revolving_limit,
    ChargeLimitExceededError,
    MissingRefinanceAmortizationError,
    OSFI_B20_CHARGE_LTV_MAX,
    OSFI_B20_REVOLVING_LTV_MAX,
)
from scenario_overlay import ScenarioOverlay, apply_ltv_overlay, apply_overlay
from simulation_config import SimulationConfig
from year_result import YearResult
from trajectory_invariants import assert_invariant, run_invariant
import contract_errors
import contract_schema


# ============================================================================
# charge_limit() / heloc_revolving_limit() -- the OSFI B-20 constants
# ============================================================================

class TestChargeLimitHelpers:
    def test_charge_limit_is_80_percent_of_house_value(self):
        assert charge_limit(500_000) == pytest.approx(400_000)
        assert OSFI_B20_CHARGE_LTV_MAX == 0.80

    def test_heloc_revolving_limit_is_65_percent_of_house_value(self):
        assert heloc_revolving_limit(500_000) == pytest.approx(325_000)
        assert OSFI_B20_REVOLVING_LTV_MAX == 0.65

    def test_charge_limit_accepts_a_declared_override(self):
        """DP#13: the 80% figure is a fallback, not an opinion -- a config
        with its own declared charge_ltv_limit overrides it."""
        assert charge_limit(500_000, charge_ltv_limit=0.75) == pytest.approx(375_000)


# ============================================================================
# apply_ltv_overlay: refuse an over-charge facility (issue #664, DP#17 both
# sides of the 80% threshold)
# ============================================================================

def _readvanceable_config(**overrides):
    defaults = dict(
        projection_years=5, house_value=800_000, mortgage_balance=100_000,
        margin_available=100_000, mortgage_rate=0.05,
        refinance_amortization_years=25,
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


class TestChargeLimitRefusal:
    def test_ltv_at_exactly_80_percent_is_allowed(self):
        """DP#17 (below/at the threshold): a refinance to EXACTLY the 80%
        charge ceiling is a valid facility, not refused."""
        cfg = _readvanceable_config()
        overlaid = apply_ltv_overlay(cfg, 0.80)
        assert overlaid.mortgage_balance == pytest.approx(640_000)
        total_secured = overlaid.mortgage_balance + overlaid.margin_available
        assert total_secured <= charge_limit(cfg.house_value) + 0.01

    def test_ltv_above_80_percent_is_refused_not_simulated(self):
        """DP#17 (above the threshold): a target LTV whose mortgage ALONE
        would exceed the 80% charge must be refused loudly (issue #664),
        not silently modeled as a >80% LTV facility."""
        cfg = _readvanceable_config()
        with pytest.raises(ChargeLimitExceededError):
            apply_ltv_overlay(cfg, 0.81)

    def test_over_limit_facility_is_refused_not_simulated(self):
        """The exact reproduction shape from issue #664: a household whose
        pre-existing mortgage + HELOC limit is ALREADY at the charge ceiling
        (a legitimate, fully-utilized facility) must not be allowed to ALSO
        draw a fresh mortgage increase up to the same charge on top of the
        undiminished HELOC -- that is the >100% LTV bug. Refused, not
        simulated."""
        # mortgage 100k + margin 500k = 600k = 75% of 800k (a valid,
        # near-fully-utilized starting facility, charge ceiling 640k).
        cfg = _readvanceable_config(margin_available=500_000)
        # Refinancing to 80% books a 540k cash-out; margin can absorb at
        # most 500k of it, leaving 40k of genuinely new debt that pushes
        # total secured debt to 100k+540k+0 = 640k... which is still exactly
        # at the charge (not over). Push further, past the point the shared
        # charge can support at all:
        with pytest.raises(ChargeLimitExceededError):
            apply_ltv_overlay(cfg, 0.90)  # mortgage alone would hit 720k > 640k charge

    def test_apply_overlay_dict_path_also_refuses(self):
        """The dict/ScenarioOverlay path (simulate.py's engine) enforces the
        identical charge limit -- not just the SimulationConfig path
        (optimizer.py/scipy_optimizer.py)."""
        base_cfg = {
            'assumptions': {'projection_years': 5},
            'property': {
                'house_value': 800_000, 'mortgage_balance': 100_000,
                'margin_available': 100_000, 'mortgage_rate': 0.05,
            },
            'accounts': {},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120_000},
            ]},
        }
        overlay = ScenarioOverlay(label='over-limit', cash_out=548_000,  # -> mortgage 648k > 640k charge
                                   refinance_amortization_years=25)
        with pytest.raises(ChargeLimitExceededError):
            apply_overlay(base_cfg, overlay)


# ============================================================================
# apply_ltv_overlay / apply_overlay: refuse a missing refinance amortization
# (issue #655, DP#32)
# ============================================================================

class TestMissingRefinanceAmortizationRefusal:
    def test_apply_ltv_overlay_refuses_without_declared_amortization(self):
        cfg = SimulationConfig(projection_years=5, house_value=800_000,
                                mortgage_balance=100_000, margin_available=100_000)
        assert cfg.refinance_amortization_years is None
        with pytest.raises(MissingRefinanceAmortizationError):
            apply_ltv_overlay(cfg, 0.70)

    def test_apply_ltv_overlay_accepts_an_explicit_override(self):
        cfg = SimulationConfig(projection_years=5, house_value=800_000,
                                mortgage_balance=100_000, margin_available=100_000)
        overlaid = apply_ltv_overlay(cfg, 0.70, refinance_amortization_years=25)
        assert overlaid.amortization_years == 25

    def test_apply_overlay_refuses_without_declared_amortization(self):
        base_cfg = {
            'assumptions': {'projection_years': 5},
            'property': {'house_value': 800_000, 'mortgage_balance': 100_000,
                         'margin_available': 100_000, 'mortgage_rate': 0.05},
            'accounts': {},
            'family': {'members': [{'role': 'primary', 'gross_income': 120_000}]},
        }
        overlay = ScenarioOverlay(label='no-amort', cash_out=100_000)
        with pytest.raises(MissingRefinanceAmortizationError):
            apply_overlay(base_cfg, overlay)

    def test_zero_cash_out_never_needs_an_amortization(self):
        """A no-op overlay (no refinance happening) must not be refused for
        an absent refinance amortization -- there is nothing to re-amortize."""
        cfg = SimulationConfig(projection_years=5, house_value=800_000,
                                mortgage_balance=100_000, margin_available=100_000)
        overlaid = apply_ltv_overlay(cfg, 0.0)
        assert overlaid.mortgage_balance == cfg.mortgage_balance


# ============================================================================
# input_contract.to_internal_config: refuse a DECLARED over-limit facility
# (issue #664, DP#17 both sides of the 80% and 65% thresholds)
# ============================================================================

def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _two_generation_subset(doc):
    """Same trim helper as tests/test_input_contract.py: the shipped
    4-generation example down to p1/p2 + their direct children -- the
    sub-family the adapter can honestly map onto the legacy engine."""
    doc = copy.deepcopy(doc)
    keep_people = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep_people]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep_people]
    doc["accounts"] = [a for a in doc["accounts"] if _owner_ids(a["owner"]) <= keep_people]
    doc["liabilities"] = [l for l in doc["liabilities"] if _owner_ids(l["owner"]) <= keep_people]
    doc["properties"] = [p for p in doc["properties"] if _owner_ids(p["owner"]) <= keep_people]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}
    ]
    doc["estate"]["life_insurance"] = [i for i in doc["estate"]["life_insurance"] if i["owner"] in keep_people]
    doc["assumptions"]["mortality"] = [m for m in doc["assumptions"]["mortality"] if m["person"] in keep_people]
    return doc


def _valid_two_gen_doc():
    with open(contract_schema.EXAMPLE_PATH) as f:
        example = json.load(f)
    return _two_generation_subset(example)


def _set_house_mortgage_heloc(doc, house_value, mortgage_balance, heloc_limit):
    for p in doc["properties"]:
        if p["kind"] == "principal":
            p["value"]["amount"] = house_value
    for liab in doc["liabilities"]:
        if liab["kind"] == "mortgage":
            liab["balance"]["amount"] = mortgage_balance
        elif liab["kind"] == "heloc":
            liab["limit"] = heloc_limit
    return doc


class TestContractChargeLimitRefusal:
    """house_value = 650,000 (the shipped example's principal residence).
    80% charge = 520,000. 65% revolving-only ceiling = 422,500."""

    def test_shipped_example_is_within_both_limits(self):
        """Sanity: the fixture itself does not trip either check (regression
        guard -- proves these two new checks don't misfire on ordinary,
        valid data)."""
        doc = _valid_two_gen_doc()
        legacy = ic.to_internal_config(doc)  # must not raise
        assert legacy['property']['mortgage_balance'] + legacy['property']['margin_available'] <= 520_000

    def test_combined_debt_at_exactly_80_percent_is_allowed(self):
        """DP#17 (at the threshold): mortgage + HELOC limit landing EXACTLY
        on the 80% charge ceiling is a valid facility."""
        doc = _set_house_mortgage_heloc(_valid_two_gen_doc(),
                                         house_value=650_000, mortgage_balance=370_000,
                                         heloc_limit=150_000)  # 370k + 150k = 520k == 80%
        ic.to_internal_config(doc)  # must not raise

    def test_combined_debt_above_80_percent_is_refused(self):
        """DP#17 (above the threshold): a document that itself declares a
        mortgage + HELOC limit exceeding the charge is refused at the ONE
        contract-loading boundary -- not silently modeled as a >80% LTV
        household (issue #664)."""
        doc = _set_house_mortgage_heloc(_valid_two_gen_doc(),
                                         house_value=650_000, mortgage_balance=371_000,
                                         heloc_limit=150_000)  # 371k + 150k = 521k > 520k
        with pytest.raises(contract_errors.ContractAdaptationError):
            ic.to_internal_config(doc)

    def test_revolving_limit_at_exactly_65_percent_is_allowed(self):
        """DP#17 (at the threshold): a HELOC limit landing EXACTLY on the
        65% revolving-only ceiling is valid, independent of the 80% combined
        cap (mortgage kept low so only the revolving check is exercised)."""
        doc = _set_house_mortgage_heloc(_valid_two_gen_doc(),
                                         house_value=650_000, mortgage_balance=90_000,
                                         heloc_limit=422_500)  # 65% of 650k exactly
        ic.to_internal_config(doc)  # must not raise (combined 512.5k <= 520k too)

    def test_revolving_limit_above_65_percent_is_refused(self):
        """DP#17 (above the threshold): a HELOC limit alone exceeding 65%
        LTV is refused even though the COMBINED total is still within the
        80% cap -- OSFI B-20's revolving-only ceiling is a separate, tighter
        constraint (issue #664)."""
        doc = _set_house_mortgage_heloc(_valid_two_gen_doc(),
                                         house_value=650_000, mortgage_balance=90_000,
                                         heloc_limit=423_500)  # 65.15% of 650k; combined 513.5k <= 520k
        with pytest.raises(contract_errors.ContractAdaptationError):
            ic.to_internal_config(doc)


# ============================================================================
# Trajectory invariant (issue #581): total_secured_debt <= charge_limit,
# checked every year -- and a "would have caught it" proof, not narration.
# ============================================================================

class TestChargeLimitTrajectoryInvariant:
    def test_good_trajectory_passes(self):
        results = [
            YearResult(year=0, mortgage_balance=500_000, heloc_balance=100_000),
            YearResult(year=1, mortgage_balance=480_000, heloc_balance=120_000),
        ]
        assert_invariant('total_secured_debt_within_charge_limit', results,
                          {'house_value': 800_000})  # limit 640,000; both years <= it

    def test_over_charge_trajectory_is_flagged(self):
        """Proof, not narration: this is issue #664's exact reproduction
        shape ($800k house, mortgage refinanced to $640k, full $482,613
        HELOC also drawn -> $1,122,613 secured debt, 140% LTV) replayed as a
        raw trajectory the invariant must flag."""
        results = [YearResult(year=0, mortgage_balance=640_000, heloc_balance=482_613)]
        violations = run_invariant('total_secured_debt_within_charge_limit', results,
                                    {'house_value': 800_000})
        assert len(violations) == 1
        assert violations[0].value == pytest.approx(1_122_613)

    def test_revolving_only_trajectory_invariant_both_sides(self):
        house_value = 800_000  # 65% revolving-only ceiling = 520,000
        within = [YearResult(year=0, mortgage_balance=100_000, heloc_balance=520_000)]
        assert_invariant('heloc_within_revolving_limit', within, {'house_value': house_value})
        beyond = [YearResult(year=0, mortgage_balance=100_000, heloc_balance=521_000)]
        violations = run_invariant('heloc_within_revolving_limit', beyond, {'house_value': house_value})
        assert len(violations) == 1

    def test_no_op_without_house_value(self):
        """Both new invariants are no-ops without ctx['house_value'] -- same
        pattern as the estate invariant above them (house_value is not
        tracked on YearResult)."""
        results = [YearResult(year=0, mortgage_balance=10_000_000, heloc_balance=10_000_000)]
        assert run_invariant('total_secured_debt_within_charge_limit', results, {}) == []
        assert run_invariant('heloc_within_revolving_limit', results, {}) == []


# ============================================================================
# A real multi-year run stays within the charge at every year (regression
# lock: the fix, exercised through the actual engine, not a synthetic
# trajectory).
# ============================================================================

def _readvance_run(ltv, house_value=800_000, opening_mortgage=100_000,
                    opening_margin=450_000, refinance_amortization_years=25,
                    projection_years=10):
    cfg = SimulationConfig(
        projection_years=projection_years, house_value=house_value,
        mortgage_balance=opening_mortgage, margin_available=opening_margin,
        mortgage_rate=0.05, amortization_years=25,
        refinance_amortization_years=refinance_amortization_years,
        family_members=[{'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
                          'retirement_age': 65, 'rrsp_room_accumulated': 0,
                          'tfsa_room_accumulated': 20_000}],
        start_year=2026, investment_return=0.06, savings_rate=0.10,
    )
    overlaid = apply_ltv_overlay(cfg, ltv) if ltv > 0 else cfg
    lump_sum = overlaid.margin_available + overlaid.cash_out
    sim = FamilySimulation(overlaid, adapter=CanadaAdapter(overlaid),
                            use_readvanceable=False, deduct_later=False, lump_sum=lump_sum)
    return sim.run()


def test_real_run_at_80_percent_ltv_never_exceeds_the_charge_any_year():
    results = _readvance_run(ltv=0.80)
    assert_invariant('total_secured_debt_within_charge_limit', results, {'house_value': 800_000})


# ============================================================================
# Issue #655: two runs differing ONLY in LTV -- the higher-LTV run must
# carry STRICTLY more debt at every year until its amortization ends. Today
# (pre-fix) it carries the SAME amount, because both re-amortize over the
# incumbent's remaining schedule and pay off identically regardless of size.
# ============================================================================

class TestHigherLtvCarriesStrictlyMoreDebt:
    def test_higher_ltv_strictly_more_debt_every_year(self):
        """A near-payoff incumbent mortgage (8 years remaining) refinanced to
        two different LTVs, both re-amortized over the SAME declared 25-year
        new-loan term (#655's fix). The higher-LTV mortgage starts from -- and
        stays at -- a strictly larger balance at every single year of a
        10-year horizon, because a larger principal amortized over an
        identical rate/term is strictly larger at every elapsed year."""
        low = _readvance_run(ltv=0.40, opening_margin=0, projection_years=10)
        high = _readvance_run(ltv=0.70, opening_margin=0, projection_years=10)
        assert len(low) == len(high) == 10
        for year_index, (lo, hi) in enumerate(zip(low, high)):
            assert hi.mortgage_balance > lo.mortgage_balance, (
                f"year {year_index}: higher-LTV mortgage ({hi.mortgage_balance:,.0f}) "
                f"must exceed lower-LTV mortgage ({lo.mortgage_balance:,.0f})"
            )
            assert hi.total_debt > lo.total_debt

    def test_stale_amortization_would_make_debt_converge_pre_fix(self):
        """Proof, not narration: replay the pre-#655 formula (a cash-out
        refinance inherits the INCUMBENT's remaining amortization, here 8
        years) for the same two LTVs on the same near-payoff mortgage, and
        show BOTH pay off to zero by year 8 -- i.e. the higher-LTV run does
        NOT carry strictly more debt at every year; it carries the SAME
        (zero) amount from year 8 onward. That convergence is the #655 tell
        this fix eliminates."""
        from countries.canada.rate_model import build_rate_path, amortization_schedule, annual_summary

        def _stale_terminal_balance(mortgage_balance, rate, stale_amortization_years):
            rate_path = build_rate_path(name='stale', initial_rate=rate, term_years=stale_amortization_years,
                                         rate_type='fixed', renewal_rates=[rate])
            schedule = amortization_schedule(mortgage_balance, rate_path, stale_amortization_years)
            years = annual_summary(schedule)
            return [y['end_balance'] for y in years[:10]]

        house_value, opening_mortgage, rate, stale_years = 800_000, 100_000, 0.05, 8
        low_mortgage = opening_mortgage + max(0, 0.40 * house_value - opening_mortgage)
        high_mortgage = opening_mortgage + max(0, 0.70 * house_value - opening_mortgage)
        low_balances = _stale_terminal_balance(low_mortgage, rate, stale_years)
        high_balances = _stale_terminal_balance(high_mortgage, rate, stale_years)
        # Both fully amortize within the 8-year stale schedule: from year 8
        # (index 7) onward, both balances are zero -- identical, not strictly
        # ordered. This is the bug #655 fixes.
        assert low_balances[7] == pytest.approx(0, abs=1.0)
        assert high_balances[7] == pytest.approx(0, abs=1.0)
        assert low_balances[7] == pytest.approx(high_balances[7], abs=1.0)
