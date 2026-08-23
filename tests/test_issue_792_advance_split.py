"""Issue #792: declare the deductible-vs-registered advance split (DP#2/DP#15).

A household refinancing can CHOOSE how much of the advance to route into a
DEDUCTIBLE non-registered investment account (ITA s.20(1)(c) interest tracing
is established only when the borrowed money is deployed into income-producing
non-reg at the refinance) versus into registered accounts (RRSP/TFSA, where
borrowed-money interest is NOT deductible, s.18(11)). This split is an
irreversible decision made at the refinance -- the household, not the engine,
makes the call. Before #792 the engine optimized the split internally (fill
registered first, non-reg gets the remainder) and exposed no lever to declare
a chosen split.

These tests pin the contract:

  * `StrategyEngine.fill_room(deductible_non_reg_first=X)` fronts X into non-reg
    BEFORE the registered waterfall, so registered is backfilled from the
    remainder (and from ongoing income in later years).
  * `None` (no declared split) is byte-for-byte today's behaviour (registered
    first). A declared 0 is a real choice, distinct from absence.
  * The declared amount is capped at the lump sum (cannot route more to non-reg
    than the advance provides) and never negative.
  * End to end: a contract declaring `advance_split.deductible_non_reg: X`
    produces a run where ~$X of the year-0 advance lands in deductible non-reg.
  * The lever round-trips through SimulationConfig.to_dict/from_dict (DP#24).

Fabricated round numbers, role-based names (DP#4/DP#15). No real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from countries.canada.adapter import CanadaAdapter  # noqa: E402
from countries.canada.strategies import STRATEGIES  # noqa: E402
from simulation import FamilySimulation  # noqa: E402
from scenario_overlay import ScenarioOverlay, apply_overlay
from simulation_config import SimulationConfig
from strategy import FamilyState, StrategyEngine  # noqa: E402


# ── a fabricated household state with enough registered room that the split
#    actually displaces registered fill (the case where the lever bites).
#    RRSP room is 0 so the RRSP waterfall is inert and the fixture isolates
#    the TFSA-vs-non-reg decision the lever governs (same isolation trick as
#    test_golden_trajectory_581.cashout_base_config).
def _state(primary_tfsa_room=200_000.0, spouse_tfsa_room=100_000.0) -> FamilyState:
    return FamilyState(
        primary_income=150_000, spouse_income=60_000,
        primary_marginal_rate=0.40, spouse_marginal_rate=0.30,
        primary_rrsp_room=0, spouse_rrsp_room=0,
        primary_tfsa_room=primary_tfsa_room, spouse_tfsa_room=spouse_tfsa_room,
        fhsa_room=0, fhsa_lifetime_remaining=0,
        resp_eligible_children=0, annual_savings=0, bracket_gap=0.10,
    )


_ENGINE = StrategyEngine(STRATEGIES["readvance_priority"])
_LUMP = 200_000.0  # fabricated advance + margin to invest at year 0


# ============================================================ fill_room unit
def test_absent_split_fills_registered_first_non_reg_gets_remainder():
    """None = today's internal optimization. With 300k TFSA room and a 200k
    lump, the whole lump fills TFSA and non-reg gets nothing."""
    r = _ENGINE.fill_room(_LUMP, _state(), deductible_non_reg_first=None)
    assert r.non_reg == 0.0
    assert r.primary_tfsa + r.spouse_tfsa == _LUMP


def test_declared_split_front_loads_non_reg_before_registered():
    """A declared 120k to deductible non-reg is routed there FIRST; the
    remaining 80k of the advance fills TFSA. Registered is backfilled from
    income in later years (allocate), not from the advance."""
    r = _ENGINE.fill_room(_LUMP, _state(), deductible_non_reg_first=120_000.0)
    assert r.non_reg == 120_000.0
    assert r.primary_tfsa + r.spouse_tfsa == 80_000.0


def test_declared_split_of_zero_is_a_real_choice_distinct_from_absence():
    """A declared 0 routes nothing to non-reg first -- numerically equal to
    None here, but it is the household's declared choice (route nothing to
    deductible), not the engine's default. Both are honoured."""
    r_zero = _ENGINE.fill_room(_LUMP, _state(), deductible_non_reg_first=0.0)
    r_none = _ENGINE.fill_room(_LUMP, _state(), deductible_non_reg_first=None)
    assert r_zero.non_reg == 0.0
    assert r_zero.primary_tfsa + r_zero.spouse_tfsa == _LUMP
    # and the two are numerically identical (the distinction is semantic --
    # a declared 0 vs an absent lever -- not a numeric one in this case)
    assert r_zero.non_reg == r_none.non_reg
    assert (r_zero.primary_tfsa + r_zero.spouse_tfsa) == \
           (r_none.primary_tfsa + r_none.spouse_tfsa)


def test_declared_split_is_capped_at_the_lump_sum():
    """Cannot route more to non-reg than the advance provides. A declared 300k
    against a 200k lump caps at 200k -- the whole advance is deductible non-reg
    and registered is entirely backfilled from income."""
    r = _ENGINE.fill_room(_LUMP, _state(), deductible_non_reg_first=300_000.0)
    assert r.non_reg == _LUMP
    assert r.primary_tfsa + r.spouse_tfsa == 0.0


def test_declared_split_never_routes_a_negative_amount():
    """A negative declared amount is clamped to 0 (defensive; the schema's
    `money` minimum is 0, so this cannot arrive from a valid contract, but the
    pure function must not invent a negative non-reg position)."""
    r = _ENGINE.fill_room(_LUMP, _state(), deductible_non_reg_first=-50_000.0)
    assert r.non_reg == 0.0
    assert r.primary_tfsa + r.spouse_tfsa == _LUMP


def test_declared_split_preserves_total_invested():
    """The split does not create or destroy money -- the full lump sum is still
    allocated across accounts (DP#3: pure function over the lump)."""
    for declared in (None, 0.0, 50_000.0, 120_000.0, 300_000.0):
        r = _ENGINE.fill_room(_LUMP, _state(), deductible_non_reg_first=declared)
        assert r.total_allocated == _LUMP
        assert r.unused == 0.0


def test_declared_split_fills_registered_from_remainder_not_beyond_room():
    """When the declared non-reg amount leaves more registered room than the
    remainder can fill, the remainder fills what it can and the rest goes to
    non-reg (the residual sink) -- registered is never over-filled."""
    # 1M TFSA room, 200k lump, 120k declared to non-reg -> 80k remainder fills
    # 80k TFSA; non-reg = 120k (front-load) + 0 (no spill, room absorbed it).
    big_state = _state(primary_tfsa_room=1_000_000.0, spouse_tfsa_room=0.0)
    r = _ENGINE.fill_room(_LUMP, big_state, deductible_non_reg_first=120_000.0)
    assert r.primary_tfsa == 80_000.0
    assert r.non_reg == 120_000.0


def test_declared_split_with_no_registered_room_routes_all_to_non_reg():
    """A household with no registered room: the split is moot -- everything is
    non-reg either way -- but the declared amount is still honoured (it just
    equals the lump)."""
    no_room = _state(primary_tfsa_room=0.0, spouse_tfsa_room=0.0)
    r_none = _ENGINE.fill_room(_LUMP, no_room, deductible_non_reg_first=None)
    r_split = _ENGINE.fill_room(_LUMP, no_room, deductible_non_reg_first=120_000.0)
    assert r_none.non_reg == _LUMP
    assert r_split.non_reg == _LUMP


# ============================================================ SimulationConfig round-trip
def test_the_declared_split_round_trips_through_to_dict_from_dict():
    """DP#24: a declared split survives a load->modify->save cycle. None must
    round-trip to 'absent' (not a literal null), and a declared 0 is preserved
    as 0 (a real choice, distinct from absence)."""
    base = {
        "property": {"house_value": 600_000, "mortgage_balance": 200_000,
                     "mortgage_rate": 0.05, "amortization_years": 20,
                     "ltv_max": 0.80, "refinance_advance_deductible_non_reg": 250_000.0},
        "family": {"members": [{"role": "primary", "birth_year": 1980, "gross_income": 150_000,
                                "retirement_age": 65, "rrsp_room_accumulated": 0,
                                "tfsa_room_accumulated": 20_000}],
                   "children": []},
        "accounts": {"rrsp_annual_max": 31_000},
        "assumptions": {"start_year": 2026, "projection_years": 10,
                        "investment_return": 0.06, "frozen_brackets": True},
        "savings": {"rate": 0.10},
        "tax": {"province": "qc"},
    }
    cfg = SimulationConfig.from_dict(base)
    assert cfg.refinance_advance_deductible_non_reg == 250_000.0
    out = cfg.to_dict()
    assert out["property"]["refinance_advance_deductible_non_reg"] == 250_000.0
    # and reloads
    cfg2 = SimulationConfig.from_dict(out)
    assert cfg2.refinance_advance_deductible_non_reg == 250_000.0


def test_an_absent_split_round_trips_to_absent_not_null():
    """DP#32: absence must not become a literal null that a naive re-read
    treats as 'declared but empty'. The key is omitted entirely on save."""
    base = {
        "property": {"house_value": 600_000, "mortgage_balance": 200_000,
                     "mortgage_rate": 0.05, "amortization_years": 20, "ltv_max": 0.80},
        "family": {"members": [{"role": "primary", "birth_year": 1980, "gross_income": 150_000,
                                "retirement_age": 65, "rrsp_room_accumulated": 0,
                                "tfsa_room_accumulated": 20_000}], "children": []},
        "accounts": {"rrsp_annual_max": 31_000},
        "assumptions": {"start_year": 2026, "projection_years": 10,
                        "investment_return": 0.06, "frozen_brackets": True},
        "savings": {"rate": 0.10},
        "tax": {"province": "qc"},
    }
    cfg = SimulationConfig.from_dict(base)
    assert cfg.refinance_advance_deductible_non_reg is None
    out = cfg.to_dict()
    assert "refinance_advance_deductible_non_reg" not in out["property"]
    cfg2 = SimulationConfig.from_dict(out)
    assert cfg2.refinance_advance_deductible_non_reg is None


# ============================================================ end to end
# Reuse the golden-trajectory fixture's cash-out household (fabricated round
# numbers, DP#15): a 220k cash-out refinance against a 600k house with 150k
# undrawn margin. TFSA room is 20k each (40k total), RRSP room 0 -- so with a
# declared split that exceeds 40k, the lever visibly displaces registered fill.
from test_golden_trajectory_581 import (  # noqa: E402
    CASHOUT_CASH_OUT,
    cashout_base_config,
)


def _run_cashout(declared_non_reg: float | None):
    base = cashout_base_config()
    if declared_non_reg is not None:
        base["property"]["refinance_advance_deductible_non_reg"] = declared_non_reg
    # Zero the savings rate so the ONLY year-0 money flow is the borrowed
    # lump sum (annual savings would otherwise pile into non_reg too via
    # `allocate`, confounding the assertion that the declared SPLIT of the
    # ADVANCE is what landed in non-reg). Fabricated, DP#15.
    base["savings"]["rate"] = 0.0
    overlay = ScenarioOverlay(
        label="split_e2e", cash_out=CASHOUT_CASH_OUT,
        mortgage_rate=base["property"]["mortgage_rate"],
        refinance_amortization_years=25,
    )
    overlaid = apply_overlay(base, overlay)
    sim_cfg = SimulationConfig.from_dict(overlaid)
    lump_sum = sim_cfg.margin_available + sim_cfg.cash_out
    sim = FamilySimulation(
        sim_cfg, adapter=CanadaAdapter(sim_cfg),
        use_readvanceable=False, deduct_later=False, lump_sum=lump_sum,
    )
    return sim.run(), sim_cfg


def _year0_non_reg_contribution(results) -> float:
    """The dollars of the year-0 advance invested into non-reg, read off the
    year-0 result's `contributions` dict (the allocation, before growth)."""
    return results[0].contributions.get("non_reg", 0.0)


def _year0_tfsa_contribution(results) -> float:
    return results[0].contributions.get("primary_tfsa", 0.0) + \
        results[0].contributions.get("spouse_tfsa", 0.0)


def test_e2e_a_declared_split_lands_in_deductible_non_reg_at_year_0():
    """Acceptance from the issue: a contract declaring deductible_non_reg: X
    produces a run where ~$X of the advance is in deductible non-reg from
    year 1. Here X=150000, which exceeds the 40k TFSA room, so the lever
    fronts 150k into non-reg and only 40k fills TFSA (remainder 30k also
    non-reg) -> year-0 non_reg contribution = 180000."""
    results, cfg = _run_cashout(declared_non_reg=150_000.0)
    assert cfg.refinance_advance_deductible_non_reg == 150_000.0
    non_reg0 = _year0_non_reg_contribution(results)
    # 150k front-loaded + (220k lump - 150k - 40k TFSA room) = 180k to non-reg
    assert non_reg0 == pytest.approx(180_000.0, abs=1.0)


def test_e2e_absent_split_is_today_registered_first_behaviour():
    """No declared split -> the advance fills the 40k TFSA room first and the
    remainder (180k) goes to non-reg. (The total non_reg is high here because
    registered room is small; the point is the engine chose it, not the
    household's declared split.)"""
    results, cfg = _run_cashout(declared_non_reg=None)
    assert cfg.refinance_advance_deductible_non_reg is None
    non_reg0 = _year0_non_reg_contribution(results)
    assert non_reg0 == pytest.approx(180_000.0, abs=1.0)


def test_e2e_declared_split_displaces_registered_fill_when_it_exceeds_room():
    """The lever's whole point: a declared amount LARGER than the natural
    non-reg remainder routes more to deductible non-reg and LESS to registered
    than today's optimization. With a tiny TFSA room (40k) this household's
    natural non-reg is already 180k, so a declared 50k does not displace --
    but a declared 200k fronts 200k into non-reg and leaves only 20k for TFSA
    (room caps it), reducing registered fill from 40k to 20k."""
    results, cfg = _run_cashout(declared_non_reg=200_000.0)
    non_reg0 = _year0_non_reg_contribution(results)
    tfsa0 = _year0_tfsa_contribution(results)
    # 200k front-loaded (capped at the 220k lump), 20k remainder fills 20k of
    # the 40k TFSA room -> non_reg = 200k, TFSA = 20k (displaced from 40k).
    assert non_reg0 == pytest.approx(200_000.0, abs=1.0)
    assert tfsa0 == pytest.approx(20_000.0, abs=1.0)

    # and the absent-split baseline fills the full 40k TFSA room, proving the
    # lever -- not something else -- caused the displacement.
    base_results, _ = _run_cashout(declared_non_reg=None)
    assert _year0_tfsa_contribution(base_results) == pytest.approx(40_000.0, abs=1.0)


# ============================================================ contract mapping (input_contract)
import copy  # noqa: E402

from test_input_contract import _load_example, _two_generation_subset  # noqa: E402

import input_contract as ic  # noqa: E402


def _example_doc_with_first_refinance_declaring_split(amount: float) -> dict:
    """Load the shipped example, trim to the two-generation sub-family
    the Phase 1 adapter can map (see test_input_contract.
    _two_generation_subset), and put `advance_split.deductible_non_reg`
    on the FIRST refinance option (the one input_contract reads -- it
    takes refinance_options[0]). The shipped first option is `no_refi`
    (no split); this adds one. Fabricated amount, DP#15."""
    doc = _two_generation_subset(_load_example())
    refi = copy.deepcopy(doc["decisions"]["mortgage"]["refinance_options"])
    refi[0]["advance_split"] = {"deductible_non_reg": amount}
    doc["decisions"]["mortgage"]["refinance_options"] = refi
    return doc


def test_input_contract_maps_a_declared_split_into_the_internal_property_key():
    """The contract lever `decisions.mortgage.refinance_options[0].advance_
    split.deductible_non_reg` reaches the internal config as `property.
    refinance_advance_deductible_non_reg` (the key SimulationConfig.from_dict
    reads onto the config field). This is the schema-coverage-relevant hop:
    the leaf is consumed here, not merely parsed."""
    doc = _example_doc_with_first_refinance_declaring_split(250_000.0)
    ic.validate_contract(doc)  # the contract is schema-valid with advance_split
    legacy = ic.to_internal_config(doc)
    assert legacy["property"]["refinance_advance_deductible_non_reg"] == 250_000.0
    # and it loads onto a SimulationConfig that carries the field
    cfg = SimulationConfig.from_dict(legacy)
    assert cfg.refinance_advance_deductible_non_reg == 250_000.0


def test_input_contract_omits_the_key_when_no_split_is_declared():
    """DP#32: absence is absence. When NO refinance option declares an
    advance_split, the internal key is simply not written -- a downstream
    `.get('refinance_advance_deductible_non_reg')` returns None (today's
    behaviour), never a fabricated 0. (The split is read from whichever
    option declares one, not just the first -- so removing it from every
    option is what makes the key absent.)"""
    doc = _two_generation_subset(_load_example())
    # strip advance_split from every option (the shipped example has one on
    # refi_50k; removing all of them is the true 'absent' state)
    for opt in doc["decisions"]["mortgage"]["refinance_options"]:
        opt.pop("advance_split", None)
    legacy = ic.to_internal_config(doc)
    assert "refinance_advance_deductible_non_reg" not in legacy["property"]
    cfg = SimulationConfig.from_dict(legacy)
    assert cfg.refinance_advance_deductible_non_reg is None


# ============================================================ contract -> engine (the wiring the reachability detector proves)
def _contract_to_runnable_cfg(doc):
    """Contract doc -> to_internal_config -> SimulationConfig, with the first
    cash-out refinance option's advance booked via apply_overlay (a refinance
    OPTION is a candidate scenario, not the base property's cash_out -- the
    base stays cash_out 0 until an overlay books the advance). Zero savings so
    the only year-0 money flow is the borrowed lump (DP#15 fabrication)."""
    legacy = ic.to_internal_config(doc)
    legacy["savings"]["rate"] = 0.0
    cash_out = doc["decisions"]["mortgage"]["refinance_options"][0]["cash_out"]
    overlay = ScenarioOverlay(
        label="contract_split_e2e", cash_out=cash_out,
        mortgage_rate=legacy["property"]["mortgage_rate"],
        refinance_amortization_years=25,
    )
    overlaid = apply_overlay(legacy, overlay)
    sim_cfg = SimulationConfig.from_dict(overlaid)
    return sim_cfg


def test_contract_advance_split_provably_changes_the_invested_non_reg_lump():
    """The reachability detector (test_contract_reachability) proves mutating
    the contract leaf moves the internal config; this proves it moves the
    ENGINE. A contract whose refinance option declares
    advance_split.deductible_non_reg = X flows through to_internal_config ->
    SimulationConfig.from_dict -> FamilySimulation, and the year-0 invested
    non-reg lump reflects the declared split (X front-loaded, registered
    backfilled from the remainder). This is the full contract -> engine path
    the issue's acceptance demands -- not a property-key shortcut."""
    # A contract with a cash-out refinance option declaring a 250k deductible
    # split (fabricated, DP#15).
    doc = _example_doc_with_first_refinance_declaring_split(250_000.0)
    doc["decisions"]["mortgage"]["refinance_options"][0]["cash_out"] = 150_000
    doc["decisions"]["mortgage"]["refinance_options"][0]["ltv"] = 0.75
    doc["decisions"]["mortgage"]["refinance_options"][0]["amortization_years"] = 25
    ic.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    # HOP 1: the leaf reached the internal config key the optimizer reads
    assert legacy["property"]["refinance_advance_deductible_non_reg"] == 250_000.0
    # HOP 2 + 3: from_dict reads it, and the engine honors it. Book the option's
    # cash-out via the overlay, run the sim, and read the year-0 non-reg lump.
    sim_cfg = _contract_to_runnable_cfg(doc)
    assert sim_cfg.refinance_advance_deductible_non_reg == 250_000.0
    lump = sim_cfg.margin_available + sim_cfg.cash_out
    sim = FamilySimulation(
        sim_cfg, adapter=CanadaAdapter(sim_cfg),
        use_readvanceable=False, deduct_later=False, lump_sum=lump,
    )
    non_reg0 = sim.run()[0].contributions.get("non_reg", 0.0)
    # The declared 250k is capped at the 150k advance -> 150k front-loaded into
    # non-reg (registered room fills from the remainder, which is 0 here).
    assert non_reg0 == pytest.approx(150_000.0, abs=1.0)

    # and the SAME contract with NO advance_split produces a DIFFERENT
    # (registered-first) year-0 non-reg lump, proving the lever -- not
    # something else -- moved it.
    doc_none = _two_generation_subset(_load_example())
    for opt in doc_none["decisions"]["mortgage"]["refinance_options"]:
        opt.pop("advance_split", None)
    doc_none["decisions"]["mortgage"]["refinance_options"][0]["cash_out"] = 150_000
    doc_none["decisions"]["mortgage"]["refinance_options"][0]["ltv"] = 0.75
    doc_none["decisions"]["mortgage"]["refinance_options"][0]["amortization_years"] = 25
    legacy_none = ic.to_internal_config(doc_none)
    assert "refinance_advance_deductible_non_reg" not in legacy_none["property"]
    sim_cfg_none = _contract_to_runnable_cfg(doc_none)
    assert sim_cfg_none.refinance_advance_deductible_non_reg is None
    lump_none = sim_cfg_none.margin_available + sim_cfg_none.cash_out
    sim_none = FamilySimulation(
        sim_cfg_none, adapter=CanadaAdapter(sim_cfg_none),
        use_readvanceable=False, deduct_later=False, lump_sum=lump_none,
    )
    non_reg0_none = sim_none.run()[0].contributions.get("non_reg", 0.0)
    # With no declared split, registered fills first; the non-reg lump is the
    # remainder AFTER registered room -- strictly less than the 150k the
    # declared-split path front-loaded (the lever moved the non-reg lump).
    assert non_reg0 > non_reg0_none
