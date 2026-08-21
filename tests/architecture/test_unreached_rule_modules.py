"""A rule that no production code path calls must not be able to look finished.

Issues #710 (AMT), #711 (CPP/QPP sharing), #712 (pension splitting), #702
(attribution). One defect, four instances:

    A tax rule is fully implemented, unit-tested in isolation, and called by
    NOTHING in production. The module looks done. Its tests are green. It
    contributes exactly zero to every real run, and the household's number is
    confidently wrong by the entire value of the rule.

AMT is the instance that shows why a green suite is no defence — twice over.

`amt.py` was complete and had 38 passing unit tests. `compute_total_tax` — the
one function that consumed it — had zero non-test callers, so no run ever
computed a minimum tax. That is the defect this guard detects.

But the second lesson is sharper, and it is why AMT is on the allowlist below
rather than wired: **when we went to wire it, the module's tax BASE turned out to
be fabricated.** It added the RRSP deduction back to adjusted taxable income;
ITA s.127.52(1) does not (its add-back list is closed, and RRSP is not on it).
Wiring it as it stood would have invented a ~$33k minimum tax for a household
making a large RRSP contribution and cut a real projected refund by ~21% for a
tax that does not exist.

So: 38 green unit tests proved the arithmetic of a rule nobody called, computed
on a base that was wrong. "Implemented and tested" told us nothing about either
question. A guard that had forced AMT to be *reached* without anyone re-reading
the statute would merely have shipped the wrong number faster — which is why the
allowlist row for AMT is a considered "not yet", not a TODO. See #754.

Nothing in the repo could have caught the reachability half. `test_schema_coverage` proves a leaf
is *read*; the DP#18 guard proves an overlay *lands*; neither says a rule is
*reached*. Unit tests cannot: a module's own tests import it directly, so they
are green exactly when the module works and say nothing about who calls it. That
is the hole. Test-only callers are the whole hole, and this guard does not count
them: it walks the call graph from the PRODUCTION entry points only
(`call_graph.ENTRY_MODULES`), so a module kept alive purely by its own test file
reads as dead — which it is.

WHAT COUNTS AS "REACHED"
------------------------
At least one public entry point (top-level function or class) of the module is
reachable, by a chain of CALLS, from `optimize.py` / `simulate.py` /
`simulation.py` / `simulation_rules.py`. An IMPORT is not a call:
`countries/canada/__init__.py` re-exports every rule module in the package, and
if a bare re-export conferred reachability this guard would certify the whole
dead surface as live. (#711's own evidence line: "only re-exported; never
invoked on the optimize path.")

Reachability is transitive from production, not "has any caller anywhere" —
otherwise dead-calling-dead passes. `pension_split_optimizer`'s only caller is
`cpp_sharing`, which nothing calls (#712: "dead-on-dead").

See `call_graph.py` for what the scan deliberately over-approximates. It errs
toward calling things REACHED, so a finding here is strong evidence of dead
code, and a clean result is weaker evidence of live code. For a build gate that
is the right asymmetry: this guard does not cry wolf.

THE ALLOWLIST IS NOT A PLACE TO PUT YOUR MODULE
-----------------------------------------------
Every entry needs an open issue. The allowlist is a registry of *known,
triaged, filed* debt — not a way to make the build green. Per AGENTS.md: "When a
guard fires, fix the code — do not add an allowlist entry."

It also cannot rot silently: an entry that becomes reachable (someone wired the
module up) FAILS this test as `stale`, forcing the entry to be deleted. The
allowlist can only shrink without someone noticing.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from call_graph import CallGraph, module_name  # noqa: E402
from repo_scan import ROOT, iter_source_files  # noqa: E402


# Modules that implement no rule of their own and so cannot be "unreached":
# package re-export barrels (`__init__.py`). Everything else under `countries/`
# is a rule module and must earn its place in a real run.
def _rule_modules() -> list[str]:
    mods = []
    for relpath in sorted(iter_source_files(ROOT)):
        if not relpath.startswith("countries" + os.sep):
            continue
        if os.path.basename(relpath) == "__init__.py":
            continue
        mods.append(module_name(relpath))
    return mods


# ---------------------------------------------------------------------------
# The allowlist: rule modules known to be unreached, each with the open issue
# that tracks wiring it. Fix the code; do not add rows here.
# ---------------------------------------------------------------------------
KNOWN_UNREACHED: dict[str, str] = {
    # --- Tax/benefit rules that are built and never run -------------------
    # #710 (countries.canada.amt) was REMOVED from this allowlist when AMT was
    # WIRED into the live fold: simulation_rules.apply_amt (the 'amt' rule, dead
    # last in RULE_ORDER — a year-end assessment after every gain is realized)
    # calls countries.canada.amt.total_tax_with_amt on the year's realized income
    # (regular federal tax assembled from tax_calc's federal helpers). It charges
    # the household max(regular, AMT) and books the surcharge. #754 unblocked it
    # by threading the year's realized capital gain (the only add-back that can
    # make AMT bite here) onto YearResult; the carry-forward / 50%-of-credits /
    # QC IMR remainder is #747.
    # #711 (cpp_sharing) and #712 (pension_split_optimizer) were REMOVED from
    # this allowlist when they were wired into the live retirement fold:
    # simulation_rules.apply_retirement_income now calls
    # cpp_sharing.share_cpp_amounts and pension_split_optimizer.split_pension_amounts
    # to redistribute CPP/eligible-pension between two retired spouses per the
    # decisions.cpp_share / decisions.pension_split_pct elections the optimizer
    # sweeps (DP#22/#30). The per-spouse drawdown (#363 PR 4) prices the tax.
    # #702 (countries.canada.attribution) was REMOVED from this allowlist when
    # the s.74.2 minor-lender attribution decision was WIRED into the live fold:
    # countries.canada.private_loan_interest.classify_private_loan_interest (called
    # each year from simulation._private_loan_interest_adjustments over the declared
    # private_loans) now delegates the minor-vs-adult decision to
    # attribution.check_attribution(TransferType.MINOR_CHILD, ...) instead of
    # re-spelling the < 18 threshold itself (DP#9/DP#10). A minor lender's interest
    # is thus attributed back to the borrower via the rule module, so attribution.py
    # is reached from production. The TOSI / spousal-property / prescribed-rate-loan
    # entry points still await a contract leaf that can express an inter-spousal or
    # capital transfer (#703/#726) before they can bite.
    # #473 (asset_location) was REMOVED from this allowlist when it was wired
    # into the live optimize flow: asset_location_optimize.recommend_asset_location
    # (called by optimize.main on every run) reuses asset_location.light_vs_ludicrous
    # to price the chosen tax-efficient placement's per-asset-class tax drag, now
    # that #641 makes per-account composition reach the return engine.
    # #704 (countries.canada.hbp_rules) was REMOVED from this allowlist when the
    # Home Buyers' Plan was WIRED into the live fold: simulation_state.
    # apply_child_first_home_purchases (called from simulate_year_pure's child
    # step) builds an HBPAccount for a child who becomes a first-time home buyer,
    # withdrawing up to HBP_MAX_WITHDRAWAL from the child's RRSP non-taxably and
    # tracking its 15-year repayment schedule.
    "countries.canada.ird_penalty": (
        "#724 — IRD / breakage penalty on discharging a fixed-rate mortgage. "
        "Priced nowhere, so every refinance in the optimizer is penalty-free."
    ),
    "countries.canada.provinces.ontario_credits": (
        "#745 — NOT a dead clone: the production tax path computes no Ontario "
        "surtax, health premium, trillium, LIFT, or sales-tax credit, so an "
        "Ontario household in a real run pays none of its surtax/health premium "
        "and receives none of its refundable credits — a live omission. Wiring "
        "is blocked on the #710-AMT shape: the refundable credits "
        "(ontario_sales_tax_credit, ontario_trillium_benefit) need "
        "adjusted_family_net_income + num_adults/num_children, and trillium "
        "additionally needs oeptc/noec (energy + property-tax components) the "
        "fold does not carry; ontario_lift_credit needs individual_net_income + "
        "family_net_income the fold does not carry — the same 'thread a new "
        "input through the fold' blocker as AMT's per-member realized gains. "
        "ontario_surtax and ontario_health_premium are wireable in principle "
        "(basic Ontario tax + taxable income), but surtax's statutory base is "
        "basic Ontario tax AFTER non-refundable credits, which the bracket-only "
        "model does not compute — wiring it on the pre-credit base would "
        "over-state the surtax without a statute-interaction review. Considered "
        "NOT YET, not a TODO."
    ),
    "countries.canada.provinces.quebec.quebec_lif": (
        "#745 — Quebec LIF max/temporary-income rules; the engine uses the "
        "federal LIF path in `locked_in_account` for Quebec residents too."
    ),
    "countries.canada.claiming_age_optimizer": (
        "#745 — CPP/OAS claiming-age optimizer (#291's subject). The engine takes "
        "the claiming age as an input instead of optimizing it."
    ),
    "countries.canada.cpp_estimator": (
        "#745 — CPP benefit estimation from an earnings history. The engine reads "
        "`cpp_monthly_estimated` from the contract instead."
    ),
    "countries.canada.renewal_model": (
        "#745 — mortgage renewal-path modelling; superseded in practice by "
        "`rate_model`'s rate paths, but never deleted (DP#9)."
    ),
    "countries.canada.sim_state": (
        "#745 — a second, unused SimState shape; production threads "
        "`simulation_state.SimState`. Dead clone (DP#9)."
    ),

    # --- Not rules: data providers / infrastructure -----------------------
    # #746: these rows are a DIFFERENT KIND of entry from the rule debt above.
    # The rule rows are "implemented, tested, not yet wired" — triaged debt with
    # a wiring path (delete the row when someone wires it). The rows below are
    # the DATA layer (DP#25 layer 1) and reporting side (DP#7): they are
    # unreachable from the fold BY DESIGN and are meant to STAY that way. DP#12
    # says real data is fetched and cached out-of-band; the fold reads that
    # cached output, it never calls the fetcher — a provider the simulation
    # called directly would be the bug. So "unreached" is the correct state
    # here, not debt, and there is no issue to close by wiring them.
    #
    # Decision for #746 (why they sit in this allowlist rather than being
    # moved or specially scoped): the reach guard deliberately keeps its broad
    # "every non-__init__ module under countries/ is a candidate" scope. A
    # narrower, name-based "rule module" heuristic (skip *_data / *_registry /
    # *_provider) would be fragile — it would silently excuse a genuinely dead
    # rule that happened to be misnamed, which is exactly the failure this
    # guard exists to catch. An explicit, issue-cited allowlist row per data
    # provider is auditable; a fuzzy structural skip is not. They stay under
    # countries/ because the data layer is colocated with the jurisdiction it
    # serves (DP#25: importing a jurisdiction package is a deliberate act).
    "countries.canada.boc_data": (
        "#746 — Bank of Canada rate FETCHER (DP#12: real data is fetched and "
        "cached out-of-band, not called from the simulation fold). Unreached from "
        "`optimize`/`simulate` by design; it feeds the cached rate data those "
        "then read."
    ),
    "countries.canada.market_rates": (
        "#746 — mortgage-rate quote provider; same out-of-band DP#12 shape as "
        "boc_data."
    ),
    # #917: countries.canada.product_registry is NO LONGER unreached -- the
    # contract adapter (input_contract._registered_composition_accounts) now
    # resolves declared registered-account product holdings into a composition
    # via ProductRegistry, so it is reached from a real `--input` run. Its
    # allowlist row is deleted rather than kept (the guard's own instruction).
    "countries.canada.tax_bracket_fallbacks": (
        "#635 — the bracket fallback table; reached only via a data-absence path "
        "the provider takes before the fold runs."
    ),
    "countries.canada.federal_tax_data": (
        "#635 — the federal bracket table, extracted to a province-independent "
        "module so the fallback builder (above) can derive from it during "
        "countries.canada's re-entrant import. Reached via tax_bracket_fallbacks "
        "and re-exported by countries.canada, not by the fold itself."
    ),
    "countries.canada.provinces.ontario": (
        "#746 — `OntarioTaxData` container; the provider loads Ontario brackets "
        "from the data files, not through this class."
    ),
}


@pytest.fixture(scope="module")
def graph() -> CallGraph:
    return CallGraph()


def test_every_rule_module_is_reached_from_production(graph: CallGraph):
    """Every rule module under countries/ is called by a real run, or is a
    filed, issue-linked exception.

    This is the guard for the whole 'implemented, tested, never called' family.
    """
    unreached = [
        mod for mod in _rule_modules()
        if graph.public_entry_points(mod) and not graph.reached_entry_points(mod)
    ]
    unlisted = sorted(set(unreached) - set(KNOWN_UNREACHED))

    assert not unlisted, (
        "These countries/ rule modules implement a tax/benefit computation that "
        "NO production code path calls. They contribute zero to every run, so "
        "every number they would have changed is wrong by the full value of the "
        "rule — and their own unit tests stay green, because a module's tests "
        "call it directly.\n\n"
        + "\n".join(
            f"  {mod}\n      public entry points: "
            f"{sorted(graph.public_entry_points(mod))}"
            for mod in unlisted
        )
        + "\n\nWire it into the fold (see the `amt` rule in simulation_rules.py "
        "for the shape), or file an issue and add it to KNOWN_UNREACHED with the "
        "issue number. Do not add a row here to make the build green."
    )


def test_allowlist_has_no_stale_entries(graph: CallGraph):
    """An allowlisted module that someone has since WIRED UP must be removed
    from the allowlist.

    Without this, the allowlist could silently keep claiming a module is dead
    long after it was fixed — and the next reader would trust it. The list is
    only allowed to shrink unnoticed, never to lie.
    """
    stale = sorted(
        mod for mod in KNOWN_UNREACHED
        if mod in set(_rule_modules()) and graph.reached_entry_points(mod)
    )
    assert not stale, (
        "These modules are on KNOWN_UNREACHED but ARE now reached from "
        "production — someone wired them up. Delete their rows (and close the "
        "issue):\n"
        + "\n".join(f"  {mod}" for mod in stale)
    )


def test_allowlist_names_only_real_modules():
    """A typo'd or deleted module name in the allowlist silently excuses
    nothing while looking like it excuses something."""
    known = set(_rule_modules())
    ghosts = sorted(set(KNOWN_UNREACHED) - known)
    assert not ghosts, (
        "KNOWN_UNREACHED names modules that do not exist (renamed? deleted? "
        "typo?):\n" + "\n".join(f"  {mod}" for mod in ghosts)
    )


def test_every_allowlist_entry_cites_an_issue():
    """An exception without a filed issue is not triaged debt, it is a hiding
    place."""
    missing = sorted(
        mod for mod, why in KNOWN_UNREACHED.items() if "#" not in why
    )
    assert not missing, (
        "KNOWN_UNREACHED entries with no issue reference:\n"
        + "\n".join(f"  {mod}" for mod in missing)
    )


# ---------------------------------------------------------------------------
# The regression pins: the three rules this PR's issues are about.
# ---------------------------------------------------------------------------

def test_amt_is_reached_from_production(graph: CallGraph):
    """#710. AMT IS now computed on every run — the wiring landed.

    This test used to assert the opposite (AMT deliberately unreached). That was
    a considered "not yet": `amt.py`'s tax BASE had been fabricated (it added the
    RRSP deduction back to adjusted taxable income, which ITA s.127.52(1) does
    not — its add-back list is closed and RRSP is not on it), and even once the
    base was fixed, AMT could not fire because the only add-back big enough to
    clear the exemption is 100% capital-gains inclusion (s.127.52(1)(d)) and the
    fold surfaced no realized capital gain to feed it.

    #754 threaded the year's realized capital gain onto YearResult, and #710
    wired the assessment: simulation_rules.apply_amt (the 'amt' rule, dead last
    in RULE_ORDER) calls countries.canada.amt.total_tax_with_amt on the year's
    realized income, charges the household max(regular, AMT), and books the
    surcharge. So the module is now reached from production, and its
    KNOWN_UNREACHED row is gone.
    """
    assert graph.reached_entry_points("countries.canada.amt"), (
        "countries.canada.amt is NOT reached from production — the #710 AMT "
        "wiring regressed. apply_amt (the 'amt' rule) must call total_tax_with_amt "
        "on the year's realized income so the module is reached; if you removed "
        "that call, either restore it or re-add the KNOWN_UNREACHED row with an "
        "issue (see #710/#754)."
    )


def test_attribution_is_reached_from_production(graph: CallGraph):
    """#702. attribution.py IS now called on every run — the wiring landed.

    The module (spousal & minor-child attribution, TOSI, prescribed-rate loans)
    was fully built, unit-tested in isolation, and called by nothing — the exact
    'implemented, tested, never reached' defect this guard exists for. #702 wired
    the s.74.2 minor-lender arm: `private_loan_interest.classify_private_loan_interest`
    (run each year from `simulation._private_loan_interest_adjustments` over the
    declared `private_loans`) delegates the minor-vs-adult decision to
    `attribution.check_attribution(TransferType.MINOR_CHILD, ...)` rather than
    re-spelling the `< 18` threshold itself (DP#9/DP#10). So a minor lender's
    interest is attributed back to the borrower THROUGH the rule module, and
    `check_attribution` is now reached from production.
    """
    assert graph.reached_entry_points("countries.canada.attribution"), (
        "countries.canada.attribution is NOT reached from production — the #702 "
        "attribution wiring regressed. classify_private_loan_interest must call "
        "check_attribution to decide s.74.2 minor-lender attribution so the module "
        "is reached; if you removed that call, either restore it or re-add the "
        "KNOWN_UNREACHED row with an issue (see #702)."
    )
