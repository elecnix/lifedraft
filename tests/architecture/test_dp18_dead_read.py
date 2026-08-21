"""DP#18, one level down: the dead-*READ* class (issue #847).

DP#18's existing guard (``test_dp18_dead_write.py``) proves an *overlay* that
mutates a config lands on a key the engine reads -- a dead **write**. #847 is
the mirror-image hole the same principle leaves open one seam downstream: a
declared contract leaf that IS read, IS computed into a result, and then has
that **result silently discarded** by the consumer -- a dead **read**.

## Why the existing guards are all structurally blind to it (from #847)

#846 is the live example that slipped through every one of them:

- ``test_schema_coverage.py``'s CONSUMED map cites a line that *reads* the
  leaf (``scenario_discovery._convert_refinance_scenarios`` really does read
  ``cash_out``). The citation is TRUE -> green. But it certifies a READ, not a
  USE.
- ``test_contract_reachability.py`` proves the leaf reaches
  ``to_internal_config``. It does. The discard happens further downstream,
  past that detector's horizon.
- The coverage ratchet measures EXECUTED lines. The reading code executes ->
  the file is covered -> green. Coverage measures execution, never usefulness.

So: read OK, reaches OK, executed OK, **used NO** -- and until #847 nothing
looked at the last column. #846's concrete shape: a household's declared
``decisions.mortgage.refinance_options`` was faithfully converted into
``discover_anchors(cfg)['refinance']`` and then ``optimize.py`` read only
``['income']`` and substituted a hardcoded LTV ladder -- adding a second
declared refinance option produced byte-identical output.

## What this file does instead: the mutation-style guard (#847's approach 2)

The behavioural standard DP#18 already sets ("not verified by a test that
asserts the merged config changed; verified by a test that runs the ENGINE ...
and asserts the OUTPUT changed"), applied to declared *decision leaves* rather
than overlay functions: for each household-declarable dimension routed through
``scenario_discovery.discover_anchors``, build two configs that differ ONLY in
that declared leaf, run the REAL optimizer pipeline, and assert the ranked
output moved. If it did not, the declaration is inert -- its computed result is
being discarded downstream, exactly #846.

This is a *generic* guard, not a bespoke per-block regression test (which
``test_issue_846`` and ``test_dp_income_scenario_reaches_engine`` already are):
the gap #847 names is that the CLASS had no guard, so the *next* declarable
dimension wired to ``discover_anchors`` could be discarded the same way with
every check still green. ``test_every_declarable_dimension_is_probed_or_tracked``
closes that -- a new ``scenarios.<dim>`` override branch fails the build until
it is either probed here or explicitly triaged in ``_NOT_YET_PROBED``.

Scope, stated (not hidden): ``refinance`` (#846) and ``income`` (#665) probe
through their dedicated single-dimension explorations (``run_ltv_exploration`` /
``run_income_scenario_exploration``). #883 extends the SAME guard to the other
three declarable dimensions -- ``mortgage``, ``strategy``, ``resp_action`` --
which have no dedicated exploration and are instead consumed by the shipped
``compare-scenarios`` CLI (``simulate.py``: ``discover_anchors`` ->
``build_all_overlays`` -> ``evaluate_overlay``). ``_grid_fingerprint`` is the
isolation helper #883 calls for: it pins every other dimension to a single
declared candidate (``_grid_cfg``) so the ranked ``net_benefit`` fingerprint
moves only with the one leaf a probe varies.

Two of those three MOVE the output (``mortgage`` -> the rate path;
``strategy`` -> the contribution split, once the household has real savings
capacity to allocate). The third, ``resp_action``, is a PROVEN dead READ: the
EAP-vs-collapse proceeds are computed, differ, and are booked as
``property.free_cash`` by ``apply_overlay`` -- but ``simulation.py`` stores
``self.free_cash`` and never reads it, so the two actions rank identically. Its
probe is ``xfail(strict)`` against the engine issue that will make it live
(#914); the day ``free_cash`` is consumed the probe xpasses and fails loudly,
forcing the marker off. ``_NOT_YET_PROBED`` is now empty -- every declarable
dimension is probed, the guard fires on the one that is dead.
"""
from __future__ import annotations

import ast
import contextlib
import io
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ═══════════════════════════════════════════════════════════════════════════
# Enumerate, from the source itself, which dimensions a household can DECLARE
# ═══════════════════════════════════════════════════════════════════════════
#
# discover_anchors resolves each declarable dimension as `scenarios.get('X',
# [])` -> "if declared use it, else auto-discover". Reading that set off the
# AST (rather than hardcoding it) means a newly-added override dimension is
# discovered here automatically and must then be probed or triaged below.

def _declarable_dimensions() -> set[str]:
    src = (REPO_ROOT / "scenario_discovery.py").read_text()
    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "discover_anchors"),
        None,
    )
    assert fn is not None, "discover_anchors not found -- has it been renamed?"
    dims: set[str] = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "scenarios"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            dims.add(node.args[0].value)
    assert dims, "no `scenarios.get('X')` override branches found -- the AST "
    "shape discover_anchors uses to declare a dimension has changed."
    return dims


# ═══════════════════════════════════════════════════════════════════════════
# Behavioural probes: two configs differing ONLY in one declared leaf
# ═══════════════════════════════════════════════════════════════════════════

def _base_cfg() -> dict:
    """A fabricated household, round numbers, role labels (DP#4/DP#15)."""
    return {
        "family": {
            "members": [
                {"role": "primary", "gross_income": 150000,
                 "rrsp_room_accumulated": 30000, "tfsa_room_accumulated": 40000,
                 "fhsa_first_time_buyer_since": None, "fhsa_room_accumulated": 0},
                {"role": "spouse", "gross_income": 70000,
                 "rrsp_room_accumulated": 20000, "tfsa_room_accumulated": 40000,
                 "fhsa_first_time_buyer_since": None, "fhsa_room_accumulated": 0},
            ],
            "children": [],
        },
        "property": {
            "house_value": 800000, "mortgage_balance": 300000,
            "mortgage_rate": 0.05, "margin_available": 50000,
            "ltv_max": 0.80, "heloc_readvance": True,
            "refinance_amortization_years": 25,
        },
        "accounts": {"resp_current_balance": 0},
        "assumptions": {"investment_return": 0.07},
        "scenarios": {},
    }


def _quiet(fn, *args, **kwargs):
    """Run a chatty optimizer entry point without its DP#13 placeholder
    warnings drowning the test log."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _probe_refinance() -> tuple:
    """#846: declared ``decisions.mortgage.refinance_options`` -> the surplus
    taken as a mortgage ADVANCE. The dollar taken out is what ``apply_overlay``
    books as debt and invests, so two cash-out amounts must rank differently.
    Before #846 these were byte-identical (the hardcoded ladder decided all)."""
    import optimize

    def cfg(cash_out):
        c = _base_cfg()
        c["scenarios"]["refinance"] = [
            {"id": "line_draw", "label": "From the line", "cash_out": 0},
            {"id": "advance", "label": "As a mortgage advance", "cash_out": cash_out},
        ]
        return c

    def fp(cash_out):
        rows = _quiet(optimize.run_ltv_exploration, cfg(cash_out))
        advance = [r for r in rows if r["refinance_id"] == "advance"]
        return tuple(sorted(round(r.get("net_benefit", 0), 4) for r in advance))

    return fp(100000), fp(200000)


def _probe_income() -> tuple:
    """#665: declared ``decisions.income`` -> a dated earnings override. The
    primary's gross income drives tax, RRSP room and savings, so two override
    amounts must rank differently. Before #665 the block was parsed, mapped to
    ``cfg['scenarios']['income']`` and then never read by the engine."""
    import optimize

    def cfg(primary_income):
        c = _base_cfg()
        c["scenarios"]["income"] = [
            {"id": "plan", "label": "Plan", "members": [
                {"role": "primary", "kind": "employment",
                 "gross_income": primary_income, "from": "2026-01-01", "to": None},
            ]},
        ]
        return c

    def fp(primary_income):
        rows = _quiet(optimize.run_income_scenario_exploration, cfg(primary_income))
        plan = [r for r in rows if r["income_scenario_id"] == "plan"]
        return tuple(sorted(round(r.get("net_benefit", 0), 4) for r in plan))

    return fp(120000), fp(250000)


# ── #883: the three dimensions with no dedicated single-dimension exploration ─
# mortgage / strategy / resp_action are consumed by the shipped
# ``compare-scenarios`` CLI (simulate.py), not by a run_*_exploration entry
# point. ``_grid_fingerprint`` runs that pipeline and reads the ranked output;
# ``_grid_cfg`` pins every OTHER dimension to one declared candidate so the
# fingerprint isolates the leaf a probe varies (the guard's two-configs-differ-
# in-one-leaf standard, applied without a bespoke exploration to lean on).

def _grid_cfg() -> dict:
    """A fabricated household (round numbers, role labels -- DP#4/DP#15) with
    real annual savings and every ``scenarios.<dim>`` pinned to a single
    candidate, so ``build_all_overlays`` yields one combination per value of
    the ONE dimension a probe varies. margin_available=0 / heloc_readvance=
    False keep the HELOC dimension (and its warnings) out of the picture."""
    return {
        "family": {"members": [
            {"role": "primary", "gross_income": 200000,
             "rrsp_room_accumulated": 100000, "tfsa_room_accumulated": 90000,
             "fhsa_first_time_buyer_since": None, "fhsa_room_accumulated": 0},
            {"role": "spouse", "gross_income": 90000,
             "rrsp_room_accumulated": 60000, "tfsa_room_accumulated": 90000,
             "fhsa_first_time_buyer_since": None, "fhsa_room_accumulated": 0},
        ], "children": []},
        "property": {
            "house_value": 800000, "mortgage_balance": 200000,
            "mortgage_rate": 0.05, "margin_available": 0, "ltv_max": 0.80,
            "heloc_readvance": False, "refinance_amortization_years": 25,
        },
        "accounts": {"resp_current_balance": 0},
        "assumptions": {"investment_return": 0.07},
        # A real savings rate: without investable surplus the allocation split
        # is legitimately inert (nothing to allocate), and the strategy probe
        # would prove nothing.
        "savings": {"rate": 0.30},
        "scenarios": {
            "income": [{"id": "plan", "label": "Plan", "members": [
                {"role": "primary", "kind": "employment", "gross_income": 200000,
                 "from": "2026-01-01", "to": None}]}],
            "mortgage": [{"id": "m", "label": "M", "rate": 0.05,
                          "type": "variable", "term_years": 5}],
            "refinance": [{"id": "none", "label": "No refinance", "cash_out": 0}],
            "strategy": [{"id": "s", "label": "S", "rrsp_pct": 0.4,
                          "tfsa_pct": 0.4, "non_reg_pct": 0.2}],
            "resp_action": [{"id": "keep", "label": "keep"}],
        },
    }


def _grid_fingerprint(cfg: dict) -> tuple:
    """Run the ``compare-scenarios`` pipeline the way ``simulate.main`` does --
    ``discover_anchors`` -> ``build_all_overlays`` -> ``evaluate_overlay`` --
    and return the sorted ranked ``net_benefit`` tuple. This is the isolation
    helper #883 names: mortgage/strategy/resp_action have no dedicated
    single-dimension exploration, so the probe reads the grid's ranked output
    directly, with the other dimensions pinned by ``_grid_cfg``."""
    from scenario_discovery import discover_anchors
    import simulate

    anchors = _quiet(discover_anchors, cfg)
    combos = simulate.build_all_overlays(cfg, anchors)
    scores = [
        round(_quiet(simulate.evaluate_overlay, cfg,
                     combo["overlay"], combo["strategy_alloc"]).get("net_benefit", 0), 4)
        for combo in combos
    ]
    return tuple(sorted(scores))


def _probe_mortgage() -> tuple:
    """Declared ``scenarios.mortgage`` -> each candidate's ``rate`` drives the
    rate path (``evaluate_overlay`` -> ``build_rate_path``), so two mortgage
    rates must rank differently."""
    def cfg(rate):
        c = _grid_cfg()
        c["scenarios"]["mortgage"] = [
            {"id": "m", "label": "M", "rate": rate, "type": "variable", "term_years": 5}]
        return c

    return _grid_fingerprint(cfg(0.03)), _grid_fingerprint(cfg(0.09))


def _probe_strategy() -> tuple:
    """Declared ``scenarios.strategy`` -> the contribution allocation split
    (``build_all_overlays`` -> ``strategy_alloc`` -> ``AllocationStrategy``).
    With real annual savings (``_grid_cfg`` sets ``savings.rate``) the
    RRSP-vs-TFSA split changes tax and terminal balances, so two allocations
    must rank differently."""
    def cfg(rrsp_pct):
        c = _grid_cfg()
        c["scenarios"]["strategy"] = [
            {"id": "s", "label": "S", "rrsp_pct": rrsp_pct,
             "tfsa_pct": round(0.8 - rrsp_pct, 4), "non_reg_pct": 0.2}]
        return c

    return _grid_fingerprint(cfg(0.1)), _grid_fingerprint(cfg(0.7))


def _probe_resp_action() -> tuple:
    """Declared ``scenarios.resp_action`` -> the RESP EAP-vs-collapse proceeds.
    #914 (FIXED): the proceeds ARE computed (they differ -- e.g. 55,500 vs
    36,833 on a 60k RESP), booked as ``property.free_cash`` by ``apply_overlay``,
    and NOW invested as a year-0 non-registered lump in ``simulation.py`` (the
    engine reads ``self.free_cash`` instead of discarding it), so the two
    actions rank DIFFERENTLY -- the READ is live.

    #890 note: declaring a single resp_action no longer ISOLATES the grid -- a
    declaration now ANNOTATES the auto-discovered set (keep/eap/collapse) rather
    than replacing it, so when the RESP has a balance the sweep always contains
    all three actions regardless of what is declared. So this probe reads ONE
    unioned grid (the whole sweep) and partitions its ranked ``net_benefit`` by
    the action each overlay carries: the EAP combos vs the COLLAPSE combos. If
    the action moves the engine they differ (#914 live); a dead READ would make
    them identical (#846) -- the same two-distinct-values-must-differ standard,
    read off the actions the sweep already explores instead of off a
    declaration that no longer narrows."""
    from scenario_discovery import discover_anchors
    import simulate

    c = _grid_cfg()
    c["accounts"]["resp_current_balance"] = 60000
    anchors = _quiet(discover_anchors, c)
    combos = simulate.build_all_overlays(c, anchors)

    def fp(action: str) -> tuple:
        return tuple(sorted(
            round(_quiet(simulate.evaluate_overlay, c,
                         combo["overlay"], combo["strategy_alloc"]).get("net_benefit", 0), 4)
            for combo in combos
            if f"RESP {action}" in combo["overlay"].label
        ))

    return fp("eap"), fp("collapse")


# Registry: dimension -> (contract path for the failure message, probe fn).
_PROBES = {
    "refinance": ("decisions.mortgage.refinance_options", _probe_refinance),
    "income": ("decisions.income", _probe_income),
    "mortgage": ("scenarios.mortgage (rate scenarios)", _probe_mortgage),
    "strategy": ("scenarios.strategy (contribution allocation)", _probe_strategy),
    "resp_action": ("decisions.resp_action", _probe_resp_action),
}

# Dimensions whose probe currently FIRES: a proven dead READ, tracked to the
# engine issue that will make it live. xfail(strict) rather than an allowlist
# entry -- the dimension IS probed (its result is genuinely discarded today),
# and the strict marker means the day the engine consumes that result the probe
# xpasses and FAILS, forcing the marker off and the dimension live. Same
# non-silent discipline as DP#18/DP#32's allowlists, one seam sharper.
#
# Empty now: #914 wired ``resp_action``'s free-cash proceeds into the engine
# (invested as a year-0 non-registered lump), so its probe is LIVE -- it runs
# unmarked under ``test_declared_leaf_moves_the_ranked_output`` and asserts the
# two actions rank differently. Kept as the mechanism: a newly-discovered dead
# READ registers here with its tracking issue until the engine consumes it.
_KNOWN_DEAD_READS: dict = {}

# Declarable dimensions with no probe AND not a known dead read -- triaged to a
# tracking issue. Empty now (#883 probed the last three); kept as the mechanism
# so a NEWLY-added ``scenarios.<dim>`` override still fails the build until it is
# probed or triaged.
_NOT_YET_PROBED: dict = {}


def _dimension_param(dimension: str):
    """Attach an xfail(strict) marker to a dimension that is a known dead read
    (#914), so its firing probe keeps CI honest without a red build and
    auto-escalates the moment the engine is fixed."""
    if dimension in _KNOWN_DEAD_READS:
        return pytest.param(dimension, marks=pytest.mark.xfail(
            strict=True,
            reason=(
                f"{dimension} is a PROVEN dead READ (declared, computed, "
                f"discarded) tracked by {_KNOWN_DEAD_READS[dimension]}; the "
                f"guard fires. An xpass here means the engine now consumes the "
                f"result -- drop this marker and let the probe assert live."
            ),
        ))
    return dimension


@pytest.mark.parametrize(
    "dimension", [_dimension_param(d) for d in sorted(_PROBES)])
def test_declared_leaf_moves_the_ranked_output(dimension):
    """The mutation-style dead-READ guard (#847). A declared decision leaf
    whose two distinct values produce IDENTICAL ranked output is a dead read:
    it is parsed, converted into a discover_anchors dimension, and then
    discarded downstream -- reachability and coverage stay green throughout
    (#846)."""
    contract_path, probe = _PROBES[dimension]
    a, b = probe()
    assert a != b, (
        f"declared {contract_path!r} ({dimension}) produced IDENTICAL ranked "
        f"optimizer output for two distinct values -- the leaf is READ and "
        f"converted into discover_anchors['{dimension}'], but its result is "
        f"DISCARDED downstream (a dead READ, the #846 class). Every existing "
        f"guard -- schema-coverage citation, contract reachability, the "
        f"coverage ratchet -- stays green on this, which is exactly why #847 "
        f"needs this behavioural check."
    )


def test_every_declarable_dimension_is_probed_or_tracked():
    """The generic half of #847: the gap was that the CLASS had no guard, so a
    NEW declarable dimension could be discarded with every check green. Every
    ``scenarios.<dim>`` override branch discover_anchors resolves must be
    either probed above (live, or xfail against a tracked dead read) or
    explicitly triaged in ``_NOT_YET_PROBED`` -- no silent truncation."""
    declarable = _declarable_dimensions()
    accounted = set(_PROBES) | set(_NOT_YET_PROBED)

    untracked = declarable - accounted
    assert not untracked, (
        f"discover_anchors resolves declarable dimension(s) {sorted(untracked)} "
        f"that are neither probed by test_declared_leaf_moves_the_ranked_output "
        f"nor triaged in _NOT_YET_PROBED. A household can declare them, so their "
        f"computed result could be silently discarded (#846/#847). Add a probe, "
        f"or triage them to a tracking issue."
    )

    stale = accounted - declarable
    assert not stale, (
        f"{sorted(stale)} are registered as declarable dimensions but "
        f"discover_anchors no longer resolves a `scenarios.<dim>` override for "
        f"them -- remove the stale probe/allowlist entry (DP#9)."
    )


def test_not_yet_probed_entries_cite_a_tracking_issue():
    for dim, issue in _NOT_YET_PROBED.items():
        assert issue.startswith("#") and issue[1:].isdigit(), (
            f"{dim}: _NOT_YET_PROBED entry must cite a '#NNN' issue, got {issue!r}"
        )


def test_known_dead_read_entries_are_probed_and_cite_an_issue():
    """A known dead read must attach to a REAL probe (so the xfail marks a
    firing behavioural check, not a phantom) and cite the engine issue that
    will make it live (same non-silent discipline as _NOT_YET_PROBED)."""
    for dim, issue in _KNOWN_DEAD_READS.items():
        assert dim in _PROBES, (
            f"{dim} is flagged a known dead read but has no probe in _PROBES -- "
            f"the xfail must attach to a real behavioural probe."
        )
        assert issue.startswith("#") and issue[1:].isdigit(), (
            f"{dim}: _KNOWN_DEAD_READS entry must cite a '#NNN' issue, got {issue!r}"
        )
