"""Enforcement for issue #672: the DEFAULT objective (max_net_benefit) must
not silently zero out the estate election levers.

## The measured bug (#661's VOI sweep, PR #668)

``max_net_benefit`` -- the default objective, and the one ``optimize.py``'s
console headline ranks on -- has EXACTLY ZERO sensitivity to the ``/estate``
election levers (``default_spousal_rollover``, per-account
``rollover_overrides``, ``tfsa_successor_holder``, ...). ``max_after_tax_estate``
prices them. A household that declared who dies first and what the rollover
election is has asked to be scored on it (DP#22: the optimizer ranks, it
doesn't choose -- the *user* picks; but the tool must not hide that the
default pick is blind to a lever worth six figures).

## What this file asserts (three, non-overlapping claims)

1. **``voi.py`` measures the claim directly** (not re-derived/guessed here):
   for every live ``/estate`` leaf this fixture exercises, ``max_net_benefit``
   prices it at exactly $0 while ``max_after_tax_estate`` does not -- and
   ``voi.render_report`` NAMES ``max_after_tax_estate`` in the text, not just
   in a Python attribute nobody reads.
2. **``model_fidelity`` discloses it in the console headline** even before any
   VOI sweep runs (``optimize.py`` prints ``model_fidelity.render_text``
   unconditionally on every run) -- the
   ``net_benefit_omits_estate_elections`` caveat (registered in this PR) fires
   for ``max_net_benefit`` and names ``max_after_tax_estate``.
3. **``optimize.py`` reports both objectives, and the numbers actually move
   differently** (issue #672, suggestion 3): toggling
   ``estate.default_spousal_rollover`` on a real simulated household changes
   ``after_tax_estate`` for a strategy but leaves that SAME strategy's
   ``net_benefit`` untouched -- proving the side-by-side table added in this
   PR carries a real, non-degenerate signal, not just two copies of one
   number.

DP#4/#15: fabricated round-number fixtures (the shipped
``schema/example.json``, trimmed to the couple + children, same pattern as
``tests/test_voi_661.py``/``tests/test_voi_671_schema_defaults.py``), no
personal data.
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
import model_fidelity
import voi
from objective import OBJECTIVES, estate_is_declared
from optimize import run_optimization


# ═══════════════════════════════════════════════════════════════════════════
# Fixture: the shipped couple-contract example, retargeting the shipped
# rollover_override onto a PRIMARY-owned account so BOTH /estate leaves this
# schema opts into VOI (default_spousal_rollover, and the per-account
# override) are actually LIVE for this document -- not just the first one.
# (The shipped override targets a spouse-owned account while the primary
# dies first, which voi.py's own module docstring documents as a legitimate,
# document-specific UNREAD case, not an engine defect. Retargeting it here
# is what makes this test exercise BOTH leaves rather than one.)
# ═══════════════════════════════════════════════════════════════════════════

def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _estate_live_contract() -> dict:
    with open(ic.EXAMPLE_PATH) as fh:
        doc = json.load(fh)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
    doc["accounts"] = [a for a in doc["accounts"] if _owner_ids(a["owner"]) <= keep]
    doc["liabilities"] = [l for l in doc["liabilities"] if _owner_ids(l["owner"]) <= keep]
    doc["properties"] = [p for p in doc["properties"] if _owner_ids(p["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [{"account": "p1_rrsp", "spousal_rollover": False}]
    doc["estate"]["life_insurance"] = [i for i in doc["estate"]["life_insurance"] if i["owner"] in keep]
    doc["assumptions"]["mortality"] = [m for m in doc["assumptions"]["mortality"] if m["person"] in keep]
    doc.pop("provenance", None)
    ic.validate_contract(doc)
    return doc


def _estate_scoped_schema() -> dict:
    """The full schema, with every ``x-uncertainty`` annotation stripped
    EXCEPT the two under ``/estate`` this test is about. Scopes the VOI
    sweep to a handful of simulation runs instead of every uncertain leaf
    in the whole contract (~20+ candidates, cross-objective, minutes) --
    same measurement, an order of magnitude cheaper. ``_deref`` resolves a
    ``$ref`` to the SAME dict object in ``schema['$defs']`` on every use, so
    stripping those two def-level nodes once clears every site that reaches
    them through a ``$ref`` too.
    """
    schema = copy.deepcopy(ic.compose_schema())
    keep_nodes = {
        id(schema["$defs"]["estate"]["properties"]["default_spousal_rollover"]),
        id(schema["$defs"]["rollover_override"]["properties"]["spousal_rollover"]),
    }

    def _strip(node):
        if isinstance(node, dict):
            if "x-uncertainty" in node and id(node) not in keep_nodes:
                del node["x-uncertainty"]
            for v in node.values():
                _strip(v)
        elif isinstance(node, list):
            for v in node:
                _strip(v)

    _strip(schema)
    return schema


ESTATE_LEAF_POINTERS = (
    "/estate/default_spousal_rollover",
    "/estate/rollover_overrides/0/spousal_rollover",
)


#: The three section-1 tests below all sweep the SAME estate-live contract under
#: the SAME arguments (objective=max_net_benefit, jobs=4, cross_objective=True,
#: schema=_estate_scoped_schema()). ``voi.sweep`` is a pure function of its
#: arguments (DP#3, verified in voi.py's docstring), so the result is identical
#: whether computed once or three times. Computing it once per module (~75s ->
#: ~25s) does not weaken any assertion: every test only READS ``report`` (no
#: mutation), and ``voi.render_report`` is pure. Module-scoped so the single
#: sweep is shared across the whole file.
@pytest.fixture(scope="module")
def _estate_net_benefit_report():
    doc = _estate_live_contract()
    schema = _estate_scoped_schema()
    return voi.sweep(
        doc, objective=OBJECTIVES["max_net_benefit"], jobs=4,
        cross_objective=True, schema=schema,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. voi.py measures the claim directly (behavioural, not static/guessed)
# ═══════════════════════════════════════════════════════════════════════════

def test_some_live_estate_leaf_is_priced_by_some_objective(_estate_net_benefit_report):
    """The #672 guard, at the level it can actually defend: the estate
    NAMESPACE must not go dark -- at least one live /estate leaf must be
    priced by some objective in OBJECTIVES. If NO live estate leaf moved any
    objective's optimum, the estate valuation is dead in the engine and the
    user is blind to every estate election -- that is the regression this
    guard exists to catch, and it still fails loudly here.

    ## Why this is per-NAMESPACE, not per-LEAF (#751 relaxed it; #782 tracks
    ## the residual gap)

    The original test required EVERY live estate leaf to move some objective's
    optimum. That premise is false in general, and #751 (which made
    StrategyEngine.allocate() honour the declared tfsa_pct/non_reg_pct) exposed
    it. A leaf can be genuinely estate-MATERIAL yet OPTIMUM-neutral:

      * This fixture PINS the dominant rollover account, ``p1_rrsp`` (the
        primary's RRSP; the primary dies first, so its rollover-vs-deemed-
        disposition is THE ~$85k lever), via ``rollover_overrides``. The global
        ``/estate/default_spousal_rollover`` therefore governs only the residual
        (survivor-owned / secondary) accounts.
      * VOI scores the BEST-achievable estate (voi._score returns ``max`` over
        strategies). Under #751's allocation the estate-optimal strategy is
        "Non-registered-first", which is indifferent to those residual accounts,
        so toggling the global default leaves the argmax unmoved -> $0 VOI.
      * BUT the global default still moves the estate for real, non-optimal
        strategies (measured: -$52,802 for Bracket-filling, -$71,457 for
        RRSP-meltdown). It is optimum-neutral, not immaterial. The max-based VOI
        under-reports it; disclosing optimum-neutral-but-material levers is the
        follow-up tracked in #782, not something this test can assert today.

    So requiring the GLOBAL default to move an optimum would be pinning a
    fixture coincidence (the old allocation happened to leave a $2,299.70
    residual at the optimum; #751 drove it to exactly $0). The leaf that DOES
    carry the optimum-moving signal is the ``p1_rrsp`` override -- asserted in
    ``test_p1_rrsp_override_reproduces_the_measured_672_numbers`` below."""
    report = _estate_net_benefit_report

    live = {f.pointer: f for f in (report.ranked + report.inert)}
    for pointer in ESTATE_LEAF_POINTERS:
        assert pointer in live, (
            f"{pointer} was UNREAD for this fixture (engine's mapped config "
            f"identical for both sampled values) -- the fixture doesn't "
            f"exercise it; this is a fixture bug, not a #672 finding"
        )

    def _priced(finding):
        # priced by the swept objective (ranked = non-zero net_benefit VOI),
        # or by some OTHER objective (moves_under populated by the cross pass).
        return finding in report.ranked or bool(finding.moves_under)

    priced_leaves = [p for p in ESTATE_LEAF_POINTERS if _priced(live[p])]
    assert priced_leaves, (
        "NO live /estate leaf is priced by ANY objective in OBJECTIVES -- the "
        "estate namespace is live in the engine but the whole of it is priced "
        "by nothing, so the user is blind to every estate election (#672). "
        f"Live leaves checked: {list(live)}"
    )


def test_p1_rrsp_override_reproduces_the_measured_672_numbers(_estate_net_benefit_report):
    """The exact case #672's issue text and voi.py's own module docstring
    cite: $0 under max_net_benefit, priced under max_after_tax_estate.

    #751 RETARGETED this assertion from ``/estate/default_spousal_rollover``
    to the ``p1_rrsp`` override leaf. This is a correction, not a rubber stamp:
    after #751 made allocate() honour the declared tfsa_pct/non_reg_pct, the
    GLOBAL default is optimum-neutral (its dominant account, ``p1_rrsp``, is
    pinned by this fixture's override -- see
    ``test_some_live_estate_leaf_is_priced_by_some_objective``), so it no
    longer moves the argmax. The leaf that now genuinely exhibits the #672
    pattern -- $0 under max_net_benefit, and the argmax moved ~$84,998 by
    max_after_tax_estate -- is the per-account override on ``p1_rrsp`` itself
    (the primary dies first; its RRSP rolling to the survivor vs. deemed
    disposition at first death IS the estate lever). We assert the leaf that
    actually carries the signal, measured, not the one that used to."""
    report = _estate_net_benefit_report

    inert_by_pointer = {f.pointer: f for f in report.inert}
    finding = inert_by_pointer["/estate/rollover_overrides/0/spousal_rollover"]
    assert finding.spread == 0.0, "must be EXACTLY $0 under max_net_benefit, not merely small"
    # Epic #841 bite 4: max_family_after_tax_networth EMBEDS the household
    # after-tax estate (family = estate + each child's own after-tax net
    # worth), so an estate lever that moves max_after_tax_estate moves the
    # family objective by exactly the same dollar too. Issue #1009 adds
    # min_after_tax_estate -- the mirror of max_after_tax_estate (the negated
    # estate) -- which prices the SAME estate leaf by the SAME deemed-
    # disposition math, so it joins the estate-inclusive set. All three
    # estate-pricing objectives now price this leaf, and none of the non-
    # estate objectives does. That is a true consequence of the new
    # objective, not a regression: the disclosure "this is priced under an
    # estate objective" is stronger, not weaker. We still assert
    # max_after_tax_estate is the CANONICAL one #672 names, and that ONLY the
    # estate-inclusive objectives move it.
    assert "max_after_tax_estate" in finding.moves_under, (
        "max_after_tax_estate must price this estate leaf (#672); got "
        f"{finding.moves_under!r}"
    )
    assert set(finding.moves_under) == {
        "max_after_tax_estate", "min_after_tax_estate",
        "max_family_after_tax_networth"
    }, (
        "exactly the three estate-inclusive objectives must price this leaf "
        "-- no non-estate objective should, and the family + min/max estate "
        "objectives should (the family embeds the estate; min/max are its "
        f"mirrors); got {finding.moves_under!r}"
    )


def test_report_text_names_the_pricing_objective_not_just_the_python_attribute(_estate_net_benefit_report):
    """DP#32: a fact that lives only in an unread Python attribute is not a
    disclosure. voi.render_report's TEXT must name max_after_tax_estate for
    the reader, whenever the active (net_benefit) sweep finds $0."""
    report = _estate_net_benefit_report
    text = voi.render_report(report)

    assert "/estate/default_spousal_rollover" in text
    assert "but it IS priced under: max_after_tax_estate" in text
    assert "re-run with --objective max_after_tax_estate" in text


# ═══════════════════════════════════════════════════════════════════════════
# 2. model_fidelity discloses it in the console headline (no VOI sweep needed)
# ═══════════════════════════════════════════════════════════════════════════

def test_model_fidelity_names_the_pricing_objective_when_net_benefit_is_blind():
    """optimize.py's main() prints model_fidelity.render_text(cfg,
    MAX_NET_BENEFIT.name) UNCONDITIONALLY, before any strategy is even
    simulated. That text must say max_net_benefit is (partially) blind to the
    estate levers -- #1034 closed the SM sleeve's deemed disposition (it now
    routes through the estate, so the spousal-rollover election moves it for a
    leveraged household), but the non-reg pot is still priced with net_benefit's
    own marginal_rate and TFSA / principal-residence / life-insurance are not
    priced at death at all, so those estate elections remain inert -- and must
    name max_after_tax_estate as the objective that prices the full estate --
    not merely say 'this figure is estimated' (that caveat already existed
    before #672 and is not strong enough, see model_fidelity.py's docstring
    for the distinction)."""
    cfg = {"assumptions": {"start_year": 2026}}
    text = "\n".join(model_fidelity.render_text(cfg, "max_net_benefit"))

    assert "net_benefit_omits_estate_elections" not in text  # ids aren't printed, summaries are
    assert "inert" in text.lower()
    assert "estate" in text.lower()
    assert "max_after_tax_estate" in text


def test_caveat_does_not_fire_for_the_objective_that_actually_prices_the_estate():
    cfg = {"assumptions": {"start_year": 2026}}
    ids = {a.id for a in model_fidelity.active_approximations(cfg, "max_after_tax_estate")}
    assert "net_benefit_omits_estate_elections" not in ids


# ═══════════════════════════════════════════════════════════════════════════
# 3. optimize.py reports both objectives, and they carry different signal
# ═══════════════════════════════════════════════════════════════════════════

def test_estate_is_declared_for_a_contract_sourced_run():
    """`estate` is a required schema key (DP#32-motivated, #600) -- every
    contract-sourced run declares it, so the side-by-side reporting this PR
    adds to optimize.py's main() is not a rare/opt-in code path."""
    doc = _estate_live_contract()
    cfg = ic.to_internal_config(doc)
    assert estate_is_declared(cfg)


def test_run_optimization_reports_after_tax_estate_alongside_net_benefit():
    doc = _estate_live_contract()
    cfg = ic.to_internal_config(doc)
    results = run_optimization(cfg)
    assert results
    for r in results:
        assert "net_benefit" in r
        assert "after_tax_estate" in r


def test_spousal_rollover_moves_after_tax_estate_more_than_net_benefit():
    """The concrete, engine-level proof behind everything above: toggle ONE
    estate election on a real simulated household and watch the two reported
    figures diverge exactly the way the VOI sweep says they must.

    #1034 closed the largest piece of #672's blindness: compute_net_benefit
    now prices the SM sleeve's terminal deemed disposition via the SAME estate
    code path compute_after_tax_estate uses (DP#9), so the spousal-rollover
    election -- which the SM sleeve mirrors, via the non-reg pot's rollover --
    now MOVES net_benefit for a leveraged household (pre-#1034 it moved it by
    exactly $0). But max_after_tax_estate still moves by MORE, because it
    prices the FULL estate's rollover (the non-reg pot's deemed disposition +
    the registered pots' rollover + the SM sleeve + the property), while
    net_benefit prices only the SM sleeve via the estate and still prices the
    non-reg pot with its own marginal_rate. The two objectives still DIVERGE on
    the rollover -- the side-by-side table this PR added still carries a real,
    non-degenerate signal, not two copies of one number."""
    doc = _estate_live_contract()
    doc_roll = copy.deepcopy(doc)
    doc_roll["estate"]["default_spousal_rollover"] = True
    doc_no_roll = copy.deepcopy(doc)
    doc_no_roll["estate"]["default_spousal_rollover"] = False

    cfg_roll = ic.to_internal_config(doc_roll)
    cfg_no_roll = ic.to_internal_config(doc_no_roll)

    results_roll = {r["strategy"]: r for r in run_optimization(cfg_roll)}
    results_no_roll = {r["strategy"]: r for r in run_optimization(cfg_no_roll)}

    common = set(results_roll) & set(results_no_roll)
    assert common, "the two runs discovered no common strategy -- fixture problem, not a #672 finding"

    estate_diffs = [
        abs(results_roll[name]["after_tax_estate"] - results_no_roll[name]["after_tax_estate"])
        for name in common
    ]
    assert max(estate_diffs) > 0, (
        "after_tax_estate did not move for ANY strategy when the spousal-rollover "
        "election was toggled -- the side-by-side figure this PR adds would be dead weight"
    )
    # #1034: net_benefit now moves too (via the SM sleeve's estate-priced deemed
    # disposition), but by LESS than after_tax_estate, which prices the whole
    # estate's rollover. The two objectives still diverge on the rollover -- the
    # disclosure's signal survives #1034's cross-objective alignment.
    nb_diffs = []
    for name in common:
        nb_diff = abs(results_roll[name]["net_benefit"] - results_no_roll[name]["net_benefit"])
        estate_diff = abs(results_roll[name]["after_tax_estate"] - results_no_roll[name]["after_tax_estate"])
        nb_diffs.append(nb_diff)
        assert estate_diff >= nb_diff, (
            f"after_tax_estate moved by {estate_diff:.0f} but net_benefit moved by "
            f"{nb_diff:.0f} for {name!r} -- net_benefit should move by LESS (it prices "
            f"only the SM sleeve via the estate, not the full estate's rollover)")
    # D7: the rollover must MOVE net_benefit for at least one leveraged strategy
    # (the cross-objective alignment #1034 wires). Without this assertion the
    # estate_diff >= nb_diff check above passes on a full revert of fix (a)
    # (every nb_diff becomes 0.0 and estate_diff >= 0 still holds), so the test
    # would not catch the regression it exists to guard.
    assert max(nb_diffs) > 0.0, (
        "the spousal-rollover election did not move net_benefit for ANY "
        "strategy -- #1034's cross-objective alignment (compute_net_benefit "
        "prices the SM sleeve via the estate) is not wired; reverting fix (a) "
        "would leave this test green")
