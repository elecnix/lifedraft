"""DP#18 enforcement: "An overlay must land on a key the engine reads, or it
has evaporated" -- the dead-write class of bug #591 is (see
DESIGN_PRINCIPLES.md #18 and #32's "override written to a key nothing reads
is not applied").

Two independent checks, because a dead write has two different shapes in this
codebase:

1. **Mutated-default writes** (``test_dp18_no_mutated_default_writes``): a
   syntactic AST pattern, ``expr.get(key, {})[...] = value``. When ``key`` is
   absent, ``.get`` returns a *fresh* throwaway container, and the assignment
   mutates that throwaway object, never ``expr`` itself. This is a stricter,
   fully-general version of #591's bug -- it doesn't even require a second
   competing read path to go dead silently, the write is dead on arrival.

2. **Overlay-output-differs** (``TestOverlayFunctionsReachTheEngine``): a
   behavioural check, because "does this write reach a key the engine reads"
   cannot be decided by reading the overlay function in isolation -- #591's
   actual bug was two *different* functions each individually well-formed,
   where only one wrote to the key ``simulation.py`` reads. The only way to
   prove an overlay isn't a no-op is to run the *engine* on the merged config
   and check the *output* moved (DP#18's own text: "verified ... by a test
   that runs the engine on the merged config and asserts the output
   changed"). This test is a generic, table-driven version of that check --
   every overlay-style function that mutates a config dict and is meant to
   change simulated output is registered once, and gets this assertion for
   free instead of a bespoke per-function regression test.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import repo_scan


# ═══════════════════════════════════════════════════════════════════════════
# 1. Mutated-default dead writes (AST)
# ═══════════════════════════════════════════════════════════════════════════

# Confirmed-live findings from repo_scan.find_mutated_default_writes(), each
# citing a tracking issue. Same allowlist discipline as DP#32: an unlisted
# finding fails the build; a stale entry (the code changed) also fails the
# build, so this can't silently grow OR silently go stale.
_CONFIRMED_MUTATED_DEFAULT_WRITES = {
    # countries/canada/rental.py's apply_life_events() province_change dead
    # write (#620) is FIXED: overlay.get('tax', {})['province'] = new_province
    # is now overlay.setdefault('tax', {})['province'] = new_province, which
    # mutates `overlay` itself in both the present- and absent-key cases.
}


def test_dp18_no_unlisted_mutated_default_writes():
    findings = repo_scan.find_mutated_default_writes()
    unlisted, _ = repo_scan.diff_against_allowlist(
        findings, {k: {} for k in _CONFIRMED_MUTATED_DEFAULT_WRITES}
    )
    if unlisted:
        lines = "\n".join(f"  {f.file}:{f.line}: {f.snippet}" for f in unlisted)
        raise AssertionError(
            "DP#18: found `expr.get(key, {})[...] = value` dead-write site(s) not "
            "triaged in tests/architecture/test_dp18_dead_write.py. This assigns into "
            "a container .get() only returns when `key` was already present -- when "
            "absent, the write lands on a throwaway object. Add each to "
            "_CONFIRMED_MUTATED_DEFAULT_WRITES (citing a tracking issue) once triaged:\n"
            f"{lines}"
        )


def test_dp18_mutated_default_allowlist_has_no_stale_entries():
    findings = repo_scan.find_mutated_default_writes()
    _, stale = repo_scan.diff_against_allowlist(
        findings, {k: {} for k in _CONFIRMED_MUTATED_DEFAULT_WRITES}
    )
    if stale:
        lines = "\n".join(f"  {file}: {snippet!r}" for file, snippet in stale)
        raise AssertionError(
            "DP#18: mutated-default-write allowlist entries no longer match any "
            f"source (fixed, or moved, without updating the allowlist):\n{lines}"
        )


def test_dp18_confirmed_mutated_writes_cite_a_tracking_issue():
    for key, meta in _CONFIRMED_MUTATED_DEFAULT_WRITES.items():
        issue = meta.get("issue", "")
        assert issue.startswith("#") and issue[1:].isdigit(), (
            f"{key}: entry must cite a '#NNN' issue, got {issue!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 2. Behavioural: every registered overlay function must move engine output
# ═══════════════════════════════════════════════════════════════════════════

def _fixture_cfg():
    """Minimal valid config carrying a *fixed* return_model block -- the
    presence of return_model is exactly what triggered #591 (SimulationConfig
    only re-materializes the deprecated assumptions.investment_return scalar
    when NO return_model block is present, and every schema-conformant config
    ships one). All data fabricated, round numbers, role-based names (DP#4,
    DP#15).
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
        "scenarios": {},
        "savings": {"rate": 0.2},
    }


def _best_fingerprint(cfg: dict) -> tuple:
    """Run the config through the real optimizer end-to-end and return a
    fingerprint of the top-ranked result. Two configs whose overlay actually
    reached the engine must produce different fingerprints; two configs whose
    overlay evaporated (#591's failure mode) produce identical ones.
    """
    from optimize import run_optimization
    results = run_optimization(deepcopy(cfg))
    assert results, "run_optimization returned no candidates -- fixture is broken"
    best = max(results, key=lambda r: r.get("net_benefit", 0))
    return (round(best["net_benefit"], 6), round(best["future_value"], 6))


class TestOverlayFunctionsReachTheEngine:
    """Table-driven version of DP#18's dead-write check: apply each
    registered overlay function twice, with genuinely different (non-trivial)
    arguments, and assert the ENGINE's output differs -- not just the merged
    config dict (DP#18: "not verified by a test that asserts the merged
    config changed; it is verified by a test that runs the engine ... and
    asserts the output changed, because a merge can succeed onto a dead
    key").
    """

    def test_apply_sensitivity_overlay(self):
        from optimize import apply_sensitivity_overlay
        base = _fixture_cfg()
        low = apply_sensitivity_overlay(base, "conservative")
        high = apply_sensitivity_overlay(base, "aggressive")
        assert _best_fingerprint(low) != _best_fingerprint(high), (
            "apply_sensitivity_overlay('conservative') and ('aggressive') produced "
            "IDENTICAL engine output -- the overlay is a dead write (#591-class bug)."
        )

    def test_apply_preset(self):
        from optimize import apply_preset
        base = _fixture_cfg()
        low = apply_preset(base, "conservative")
        high = apply_preset(base, "aggressive")
        assert _best_fingerprint(low) != _best_fingerprint(high), (
            "apply_preset('conservative') and ('aggressive') produced IDENTICAL "
            "engine output -- the overlay is a dead write (#591-class bug)."
        )

    def test_apply_anchor_preset(self):
        from optimize import apply_anchor_preset
        base = _fixture_cfg()
        low_rate = apply_anchor_preset(base, "renew_low")       # 3.5%
        high_rate = apply_anchor_preset(base, "renew_current")  # 5%
        assert _best_fingerprint(low_rate) != _best_fingerprint(high_rate), (
            "apply_anchor_preset('renew_low') and ('renew_current') produced "
            "IDENTICAL engine output -- the mortgage-rate anchor overlay is a dead write."
        )

    def test_compose_preset(self):
        from optimize import compose_preset
        base = _fixture_cfg()
        low = compose_preset(base, anchor_name="renew_low", overlay_name="conservative")
        high = compose_preset(base, anchor_name="refinance_fixed", overlay_name="aggressive")
        assert _best_fingerprint(low) != _best_fingerprint(high), (
            "compose_preset() with different anchor+overlay combinations produced "
            "IDENTICAL engine output -- one or both overlays are dead writes."
        )

    def test_run_stress_test(self):
        """run_stress_test is self-contained (calls run_optimization internally),
        so it's tested against its own returned net_benefit rather than
        _best_fingerprint."""
        from stress_scenarios import StressPath, run_stress_test
        base = _fixture_cfg()
        crash = StressPath("crash", [-0.20] * 10, [0.05] * 10)
        boom = StressPath("boom", [0.15] * 10, [0.03] * 10)
        r_crash = run_stress_test(base, crash)
        r_boom = run_stress_test(base, boom)
        assert r_crash["net_benefit"] != r_boom["net_benefit"], (
            "run_stress_test() under a crash path and a boom path produced "
            "IDENTICAL net_benefit -- the stress overlay is a dead write."
        )

    def test_apply_overlay_scenario_overlay_dataclass(self):
        """The ScenarioOverlay path (scenario_overlay.apply_overlay /
        simulate.evaluate_overlay) is a different overlay mechanism from the
        dict-returning functions above -- registered separately since it
        takes a dataclass, not overlay-name strings."""
        from scenario_overlay import ScenarioOverlay
        from simulate import evaluate_overlay
        base = _fixture_cfg()
        low = ScenarioOverlay(label="r=4%", mortgage_rate=0.05, investment_return=0.04, ltv=0.0)
        high = ScenarioOverlay(label="r=10%", mortgage_rate=0.05, investment_return=0.10, ltv=0.0)
        res_low = evaluate_overlay(base, low)
        res_high = evaluate_overlay(base, high)
        assert res_low["future_value"] != res_high["future_value"], (
            "evaluate_overlay() with investment_return=4% vs 10% produced IDENTICAL "
            "future_value -- the ScenarioOverlay return-rate write is a dead write."
        )
