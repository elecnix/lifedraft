"""A function-level unwired surface may not GROW — issue #417.

`test_unreached_rule_modules.py` walks the call graph from the production entry
points and fails when a whole MODULE has no reached public entry point. It
cannot see this class of debt, because the two modules #417 cleaned are both
*reached* modules:

  - `countries.canada.retirement` is reached (via `get_oas_annual_max`, read by
    `net_benefit_legs`, and `_get_rrif_rates`, read by `locked_in_account`), so
    the module guard passes — while `MemberRetirementData`, `cpp2_benefit` and
    `RetirementState.compute_cpp` sat inside it with zero production callers.
  - `countries.canada.cpp_sharing` is reached (via `share_cpp_amounts`, called
    from the live fold) — while `compute_cpp2_benefit` sat inside it computing
    a CPP2 maximum of 2190 where the year table says 800.

Deadness that is one level below what the module guard walks is invisible to
it, and it accumulated invisibly: the tests were green the whole time, which is
exactly what "implemented and unit-tested" is supposed to be worth and is not.

WHY A RATCHET AND NOT AN ALLOWLIST
----------------------------------
An allowlist of "known-dead symbols" is a registry of triaged debt, and the
temptation — the one AGENTS.md names — is to append a row to make a build
green. This baseline cannot be used that way: it only ever FAILS on an entry
that is not already here. Deleting a symbol removes a row; wiring one up
removes a row. Neither needs a decision, a citation, or an issue. Only
*adding* an unwired public symbol trips it, and the fix for that is to wire the
symbol or delete it — not to edit this file.

The scan is deliberately the same one the module guard uses, including its
documented over-approximation (every public top-level def of an entry module is
a root; instantiating a class reaches all its methods; an unresolvable receiver
produces no edge). So this guard under-reports like its sibling does, rather
than crying wolf into an ignore list.

SCOPE IS THE TWO MODULES #417 CLEANED, ON PURPOSE
------------------------------------------------
`countries/` currently holds ~150 unreached public symbols across ~40 modules.
Ratcheting all of them at once would redden the trunk for every PR that adds a
function the author has not wired *yet* — in other workstreams, about other
rules — which is how a guard acquires an allowlist. Scoping the ratchet to the
retirement surface means it can only ever fire on the debt this issue opened.
Widening `SCOPED_MODULES` is the natural follow-up, once the other modules'
dead surfaces have been triaged rather than merely counted.

Regenerate with:

    python tests/architecture/test_unreached_public_symbols.py --update
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from call_graph import CallGraph  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(HERE, "unreached_surface_baseline.json")

SCOPED_MODULES = ("countries.canada.cpp_sharing", "countries.canada.retirement")


def unreached_public_symbols() -> dict[str, list[str]]:
    """Public top-level defs in SCOPED_MODULES with no production call path."""
    graph = CallGraph()
    out: dict[str, list[str]] = {}
    for module in SCOPED_MODULES:
        facts = graph.facts.get(module)
        assert facts is not None, f"{module} is missing from the call graph"
        out[module] = sorted(
            name
            for name in facts.definitions
            if not name.startswith("_") and (module, name) not in graph.reached
        )
    return out


def _load_baseline() -> dict[str, list[str]]:
    with open(BASELINE_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {module: list(raw.get(module, [])) for module in SCOPED_MODULES}


def _update() -> None:
    current = unreached_public_symbols()
    payload = {"_README": json.load(open(BASELINE_PATH, encoding="utf-8"))["_README"]}
    payload.update(current)
    with open(BASELINE_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
    total = sum(len(v) for v in current.values())
    print(f"wrote {len(current)} modules / {total} symbols to {BASELINE_PATH}")


def test_no_unwired_public_symbol_added() -> None:
    """Fails when an unwired public symbol appears that the baseline lacks."""
    current = unreached_public_symbols()
    baseline = _load_baseline()
    added = {
        module: sorted(set(current[module]) - set(baseline[module]))
        for module in SCOPED_MODULES
        if set(current[module]) - set(baseline[module])
    }
    assert not added, (
        "New unwired public symbol(s) in a ratcheted module (#417). It has no "
        "production caller, so nothing it computes reaches a household — wire it "
        "or delete it. If it is genuinely wanted, wire it in this PR.\n"
        + json.dumps(added, indent=2)
    )


def test_baseline_has_no_stale_entries() -> None:
    """A baseline row whose symbol was deleted or wired up must be removed.

    Without this the baseline could silently accumulate rows for symbols that no
    longer exist, and `test_no_unwired_public_symbol_added` would stop meaning
    what it says. Shrinking is always safe; the two tests together make the
    baseline a record of the CURRENT known-dead set, not an append-only list.
    """
    current = unreached_public_symbols()
    baseline = _load_baseline()
    stale = {
        module: sorted(set(baseline[module]) - set(current[module]))
        for module in SCOPED_MODULES
        if set(baseline[module]) - set(current[module])
    }
    assert not stale, (
        "Stale baseline row(s): these symbols are no longer unwired (deleted or "
        "wired up). Delete the rows — a stale row is a claim about the code that "
        "is no longer true (#417).\n" + json.dumps(stale, indent=2)
    )


if __name__ == "__main__":
    if "--update" in sys.argv:
        _update()
    else:
        test_no_unwired_public_symbol_added()
        test_baseline_has_no_stale_entries()
        print("unreached public symbols: baseline matches the tree")