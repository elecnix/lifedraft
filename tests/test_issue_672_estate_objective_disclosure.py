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

## Issue #290: the registered rollover now reaches max_net_benefit

#290 routed net_benefit's terminal REGISTERED tax through the same estate path
(``compute_estate``) max_after_tax_estate uses, so the registered-rollover
levers (``default_spousal_rollover`` and the ``p1_rrsp`` override) are now
PRICED under max_net_benefit (measured on this fixture: $76,796 and $7,163 of
VOI). The claims below were retargeted accordingly: the "$0 under
max_net_benefit" signal is gone for the registered levers -- which was the
fix -- and the inert-leaf disclosure text is now exercised under
``max_terminal_wealth`` (a pre-tax objective that prices no estate election at
all), where it is still true.

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
import contract_schema


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
    with open(contract_schema.EXAMPLE_PATH) as fh:
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
    contract_schema.validate_contract(doc)
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
    schema = copy.deepcopy(contract_schema.compose_schema())
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
def _estate_terminal_wealth_report():
    """The same scoped sweep under ``max_terminal_wealth`` -- a PRE-TAX
    objective that prices no estate election -- so the estate leaves are live
    in the engine but inert under the swept objective, and the report must name
    the objective that DOES price them (the #672 disclosure path)."""
    doc = _estate_live_contract()
    schema = _estate_scoped_schema()
    return voi.sweep(
        doc, objective=OBJECTIVES["max_terminal_wealth"], jobs=4,
        cross_objective=True, schema=schema,
    )


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


def test_p1_rrsp_override_is_priced_under_max_net_benefit(_estate_net_benefit_report):
    """Issue #290 RETARGETED the #672 measurement (was: "$0 under
    max_net_benefit, priced under max_after_tax_estate").

    The ``p1_rrsp`` override is THE registered-rollover lever of this fixture
    (the primary dies first; its RRSP rolling to the survivor vs. a deemed
    disposition at first death). Pre-#290 net_benefit re-projected the RRSP on
    hidden constants and priced it at $0, so this leaf was INERT under the
    default objective. Since #290 net_benefit's registered tax IS the estate
    path's, so the leaf is RANKED -- a strictly positive spread -- under
    max_net_benefit itself. This is the engine-driven VOI proof that the
    default objective now prices the registered rollover."""
    report = _estate_net_benefit_report

    ranked_by_pointer = {f.pointer: f for f in report.ranked}
    inert_pointers = {f.pointer for f in report.inert}
    pointer = "/estate/rollover_overrides/0/spousal_rollover"
    assert pointer not in inert_pointers, (
        "the p1_rrsp rollover override is INERT under max_net_benefit again -- "
        "net_benefit no longer prices the registered deemed disposition (#290)")
    assert pointer in ranked_by_pointer
    assert ranked_by_pointer[pointer].spread > 0.0


def test_registered_rollover_default_is_priced_under_max_net_benefit(_estate_net_benefit_report):
    """The global registered-rollover default is priced by the default
    objective too, and the report TEXT carries both leaves in the ranked
    section (a disclosure the reader sees, not a Python attribute)."""
    report = _estate_net_benefit_report
    ranked = {f.pointer: f for f in report.ranked}
    assert "/estate/default_spousal_rollover" in ranked
    assert ranked["/estate/default_spousal_rollover"].spread > 0.0
    text = voi.render_report(report)
    ranked_section = text.split("IRREDUCIBLE")[0]
    for pointer in ESTATE_LEAF_POINTERS:
        assert pointer in ranked_section


def test_p1_rrsp_override_is_inert_under_pretax_objective_and_names_the_estate(
        _estate_terminal_wealth_report):
    """Under a pre-tax objective the estate levers are inert at the optimum,
    and exactly the estate-inclusive objectives price them. Epic #841 bite 4:
    max_family_after_tax_networth EMBEDS the household after-tax estate;
    issue #1009's min_after_tax_estate is its mirror; issue #290 makes
    max_net_benefit price the registered rollover -- so all four move this
    leaf, and no other objective does."""
    report = _estate_terminal_wealth_report
    inert_by_pointer = {f.pointer: f for f in report.inert}
    finding = inert_by_pointer["/estate/rollover_overrides/0/spousal_rollover"]
    assert finding.spread == 0.0, "must be EXACTLY $0 under max_terminal_wealth"
    assert "max_after_tax_estate" in finding.moves_under, (
        "max_after_tax_estate must price this estate leaf (#672); got "
        f"{finding.moves_under!r}"
    )
    assert set(finding.moves_under) == {
        "max_after_tax_estate", "min_after_tax_estate",
        "max_family_after_tax_networth", "max_net_benefit",
    }, (
        "exactly the estate-inclusive objectives (and, since #290, "
        "max_net_benefit) must price this leaf; got "
        f"{finding.moves_under!r}"
    )


def test_report_text_names_the_pricing_objective_not_just_the_python_attribute(
        _estate_terminal_wealth_report):
    """DP#32: a fact that lives only in an unread Python attribute is not a
    disclosure. voi.render_report's TEXT must name max_after_tax_estate for
    the reader whenever the active sweep finds $0 on an estate lever."""
    report = _estate_terminal_wealth_report
    text = voi.render_report(report)
    finding = {f.pointer: f for f in report.inert}["/estate/default_spousal_rollover"]
    assert finding.moves_under

    assert "/estate/default_spousal_rollover" in text
    priced_lines = [ln for ln in text.splitlines() if "but it IS priced under:" in ln]
    assert priced_lines and all("max_after_tax_estate" in ln for ln in priced_lines)
    assert f"re-run with --objective {finding.moves_under[0]}" in text


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


def test_spousal_rollover_moves_both_reported_figures_differently():
    """The concrete, engine-level proof behind everything above: toggle ONE
    estate election on a real simulated household and watch the two reported
    figures respond -- differently.

    #1034 routed the SM sleeve's deemed disposition, and #290 the registered
    balances', through the SAME estate code path compute_after_tax_estate uses
    (DP#9), so the spousal-rollover election MOVES net_benefit. But the two
    figures still differ in what they price: max_after_tax_estate prices the
    FULL estate (the registered pots, the non-reg pot's deemed disposition
    stacked on the same terminal return, the SM sleeve, the property), while
    net_benefit takes only the estate's registered + SM components and prices
    the non-reg pot with its own marginal_rate.

    Pre-#290 this test asserted after_tax_estate moved by MORE than
    net_benefit for every strategy. That is no longer structural: the estate
    attributes a return's tax to the registered income FIRST (it runs the
    brackets from $0; the gains stack on top), so a rollover that shifts
    registered income between the two terminal returns can move the
    registered component while the estate's TOTAL tax does not move at all.
    Measured on this fixture after #290: the ``balanced`` strategy's
    after_tax_estate moved $0 while its net_benefit moved ~$2,041. That
    cross-pot basis mismatch is disclosed by model_fidelity
    (``net_benefit_registered_tax_at_horizon`` and
    ``net_benefit_sm_sleeve_cheaper_than_non_reg``). What remains a real,
    non-degenerate signal -- two figures, not two copies of one number -- is
    asserted below."""
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

    estate_diffs = {}
    nb_diffs = {}
    for name in common:
        estate_diffs[name] = results_roll[name]["after_tax_estate"] - results_no_roll[name]["after_tax_estate"]
        nb_diffs[name] = results_roll[name]["net_benefit"] - results_no_roll[name]["net_benefit"]
    assert max(abs(d) for d in estate_diffs.values()) > 0, (
        "after_tax_estate did not move for ANY strategy when the spousal-rollover "
        "election was toggled -- the side-by-side figure this PR adds would be dead weight"
    )
    # #1034/#290: the rollover must MOVE net_benefit for at least one strategy
    # (a full revert of the registered/SM estate pricing makes every nb diff 0).
    assert max(abs(d) for d in nb_diffs.values()) > 0.0, (
        "the spousal-rollover election did not move net_benefit for ANY "
        "strategy -- the registered/SM legs are no longer priced via the estate")
    # Two figures, not one: for some strategy the two responses differ.
    assert any(abs(estate_diffs[n] - nb_diffs[n]) > 1.0 for n in common), (
        "net_benefit and after_tax_estate responded identically to the rollover "
        "for every strategy -- the side-by-side table carries one number twice")
