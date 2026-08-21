"""Tests for issue #1000: declared FHSA room must reach the optimizer's SEARCH.

The bug: a declared ``fhsa_room_accumulated`` activates the FHSA store
(``SimState.initial`` builds it from ``fhsa_room_accumulated`` directly), but
the allocation gate is ``s.fhsa_pct > 0`` and every built-in strategy carried
``fhsa_pct=0.0`` -- so the optimizer's strategy search never tried a nonzero
FHSA split, and declared room moved nothing for the whole horizon (DP#14:
data that should trigger behaviour was inert). The engine knew about the room
(``has_fhsa`` True, ``adult_fhsa_total_room`` 8000) and then routed nothing
there -- exactly the "parsed, mapped, then never passed" class this repo
exists to kill.

The fix is invariant-preserving: the DEFAULT strategy's ``fhsa_pct`` is
untouched (the golden fixture runs ``adapter.get_default_strategy()``, NOT the
optimizer), and the optimizer's search now emits FHSA-enabled variants of each
discovered strategy -- gated on the household actually declaring FHSA room
(``state.fhsa_room > 0`` and ``state.fhsa_lifetime_remaining > 0``, DP#32:
absent room => FHSA is not swept, never silently defaulted). Each variant lifts
``fhsa_pct`` off zero and rebalances by drawing the same share from
``non_reg_pct`` so the allocation percentages still sum to ~1.0.

These tests reproduce the issue's motivating evidence mechanically rather than
asserting a brittle hardcoded snapshot (this repo's own convention -- see
test_golden_trajectory_581.py's docstring). Fabricated, round-numbered data
only (DP#4/DP#15) -- no personal data.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from optimize import run_optimization
from objective import MAX_AFTER_TAX_ESTATE

from test_golden_trajectory_581 import golden_household_config


def _fhsa_contrib_total(result: dict) -> float:
    """Sum the per-year FHSA contributions the optimizer's ranked row reports."""
    return sum(y.get("contributions", {}).get("fhsa", 0.0)
               for y in result["year_by_year"])


def _terminal(result: dict) -> float:
    return result["year_by_year"][-1]["total_assets"]


def _with_fhsa_room(room: float):
    """Golden household that declares FHSA room AND first-time-buyer eligibility.

    ``fhsa_first_time_buyer_since`` is what lets the optimizer's gate see the
    declared room (mirrors scenario_discovery._build_family_state): without it
    the household is never FHSA-eligible and room is zeroed, so no FHSA variants
    are generated. The golden fixture itself omits this field, which is why the
    golden invariant is byte-exact unaffected by the search change.
    """
    cfg = golden_household_config()
    cfg["family"]["members"][0]["fhsa_room_accumulated"] = room
    cfg["family"]["members"][0]["fhsa_first_time_buyer_since"] = "2024-01-01"
    return cfg


def test_declared_room_produces_nonzero_fhsa_contributions():
    """A household with declared FHSA room, run through the optimizer, must see
    NONZERO FHSA contributions in at least one ranked plan (the issue's core
    complaint: contributions were 0.0 for the whole horizon)."""
    cfg = _with_fhsa_room(8_000)
    results = run_optimization(cfg, objective=MAX_AFTER_TAX_ESTATE)
    fhsa_variants = [r for r in results if "_fhsa_" in r.get("strategy", "")]
    assert fhsa_variants, (
        "optimizer produced no FHSA-enabled strategy variants for a household "
        "with declared FHSA room; declared room is still inert (issue #1000)"
    )
    best = max(fhsa_variants, key=_fhsa_contrib_total)
    assert _fhsa_contrib_total(best) > 0.0, (
        f"FHSA variant {best['strategy']!r} routed zero FHSA contributions "
        f"despite declared room -- the gate is still inert"
    )


def test_with_room_terminal_differs_from_zero_room_control():
    """The money question the issue asks: a household with declared FHSA room
    must have a terminal that DIFFERS from the same household with zero FHSA
    room. The issue showed Δ=0.0000 byte-identical; the fix must move it."""
    with_room = run_optimization(_with_fhsa_room(8_000),
                                 objective=MAX_AFTER_TAX_ESTATE)
    # Zero-room control: same household, first-time-buyer eligible but no room.
    zero_cfg = golden_household_config()
    zero_cfg["family"]["members"][0]["fhsa_room_accumulated"] = 0
    zero_cfg["family"]["members"][0]["fhsa_first_time_buyer_since"] = "2024-01-01"
    zero_room = run_optimization(zero_cfg, objective=MAX_AFTER_TAX_ESTATE)

    # The BEST FHSA-routing plan's terminal must differ from the zero-room
    # household's best: the with-room household can route savings to FHSA, the
    # zero-room one cannot, so their optima diverge. (We compare the best FHSA
    # variant, not with_room[0], because the overall winner may be a non-FHSA
    # drawdown-order variant that pass 2 promoted -- the issue's question is
    # whether an FHSA-routing plan EXISTS and moves the terminal, not whether
    # it is the global optimum.)
    fhsa_variants = [r for r in with_room if "_fhsa_" in r.get("strategy", "")]
    assert fhsa_variants, "no FHSA variants generated for a with-room household"
    best_fhsa = max(fhsa_variants, key=_terminal)
    assert _terminal(best_fhsa) != _terminal(zero_room[0]), (
        f"best FHSA variant terminal {_terminal(best_fhsa)!r} equals zero-room "
        f"terminal {_terminal(zero_room[0])!r}: declared FHSA room still "
        f"moves nothing (issue #1000)"
    )
    # And specifically an FHSA variant's terminal must differ from its
    # fhsa_pct=0 parent's terminal under the SAME household (with room).
    parent = next((r for r in with_room if r["strategy"] == "balanced"), None)
    variant = next((r for r in with_room
                    if r["strategy"] == "balanced_fhsa_5"), None)
    assert parent is not None and variant is not None
    assert _terminal(variant) != _terminal(parent), (
        "balanced_fhsa_5 terminal equals balanced terminal: the FHSA share "
        "the variant declares does not actually move money (issue #1000)"
    )
    assert _fhsa_contrib_total(variant) > 0.0
    assert _fhsa_contrib_total(parent) == 0.0


def test_no_fhsa_room_household_is_unaffected():
    """DP#32: a household that declares NO FHSA room must not get FHSA variants
    -- absent room is not silently defaulted to a sweep. The optimizer's search
    for such a household is byte-for-byte the pre-fix search."""
    cfg = golden_household_config()
    # Golden household declares fhsa_room_accumulated but NO
    # fhsa_first_time_buyer_since => never eligible => no FHSA variants.
    results = run_optimization(cfg, objective=MAX_AFTER_TAX_ESTATE)
    fhsa_variants = [r for r in results if "_fhsa_" in r.get("strategy", "")]
    assert fhsa_variants == [], (
        f"household with no FHSA eligibility got FHSA variants "
        f"{[r['strategy'] for r in fhsa_variants]} -- absent room was "
        f"silently swept (DP#32 violation)"
    )
    # And every ranked row routes zero FHSA contributions.
    for r in results:
        assert _fhsa_contrib_total(r) == 0.0


def test_golden_invariant_byte_exact():
    """HARD CONSTRAINT (GLM_TASK.md): the golden invariant 9709753.139463063
    MUST stay byte-exact. The fix adds FHSA to the optimizer SEARCH only; the
    golden fixture runs adapter.get_default_strategy() (fhsa_pct=0), NOT the
    optimizer, so it is structurally immune to the FHSA change. The value was
    moved from 9766299.424395865 to 9709753.139463063 by the sanctioned #1001
    drawdown-nets-RRIF change (a first-time-buyer-ineligible golden household
    is unaffected by FHSA, so this is just the new golden anchor). Verified
    here by running the fixture directly -- if this moves, STOP and report
    (do not re-pin)."""
    from test_golden_trajectory_581 import _run
    assert _run(golden_household_config())[-1].total_assets == 9709753.139463063


def test_variant_skipped_when_non_reg_residual_is_zero():
    """DP#32: the FHSA share is capped at the strategy's non-reg residual so the
    allocation total never goes negative and no other account's target share is
    silently cut. A strategy with non_reg_pct=0 must produce NO FHSA variant
    (there is nowhere to rebalance from) rather than a variant with a negative
    non_reg_pct -- and a sibling strategy with room to rebalance still gets one.
    This also covers the `share <= 0: continue` guard in discover_strategies."""
    from countries.canada.strategies import discover_strategies
    from strategy import FamilyState, AllocationStrategy

    state = FamilyState(primary_marginal_rate=0.4571,
                        fhsa_room=8_000, fhsa_lifetime_remaining=40_000)
    custom = {
        # No non-reg residual to rebalance from -> must yield no FHSA variant.
        "zero_nonreg": AllocationStrategy(name="zero_nonreg",
                                          rrsp_pct=0.5, tfsa_pct=0.5,
                                          spousal_rrsp_pct=0.0, resp_pct=0.0,
                                          non_reg_pct=0.0),
        # Has a non-reg residual -> must yield FHSA variants.
        "with_room": AllocationStrategy(name="with_room",
                                         rrsp_pct=0.3, tfsa_pct=0.3,
                                         spousal_rrsp_pct=0.1, resp_pct=0.07,
                                         non_reg_pct=0.23),
    }
    discovered = discover_strategies(state, investment_return=0.07,
                                     heloc_rate=0.05,
                                     custom_strategies=custom)
    # zero_nonreg must NOT have an FHSA variant (no residual to draw from).
    assert not any(k.startswith("zero_nonreg_fhsa_") for k in discovered), (
        "zero_nonreg produced an FHSA variant with no non-reg residual to "
        "rebalance from: "
        + str([k for k in discovered if k.startswith("zero_nonreg_fhsa_")])
    )
    # with_room MUST have FHSA variants, and they rebalance exactly: fhsa_pct
    # lifted, non_reg_pct reduced by the same share, total unchanged.
    variants = [k for k in discovered if k.startswith("with_room_fhsa_")]
    assert variants, "with_room produced no FHSA variant despite a non-reg residual"
    parent = custom["with_room"]
    for k in variants:
        v = discovered[k]
        assert v.fhsa_pct > 0.0
        assert v.non_reg_pct == parent.non_reg_pct - v.fhsa_pct
        assert abs(v.total_pct - parent.total_pct) < 1e-9