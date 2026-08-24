"""Issue #1017: ``sweep.py`` honours the objective (die-with-zero frontier).

``sweep.py``'s CLI hard-coded ``objective=MAX_NET_BENEFIT``, so sweeping
``assumptions.retirement.spending_target`` reported the WEALTH-MAXIMISING
strategy's ``first_shortfall_year`` per value -- not the die-with-zero one. The
frontier came out non-monotone ($150k->yr24, $250k->15, $400k->12, $600k->25):
the objective picked a different winner at each spend than the
estate-minimising one a user asking "when can we retire and burn savings to
~zero by death?" actually wanted.

The fix (mirroring ``optimize.py``'s ``resolve_objective``): the CLI resolves
the objective from ``--objective`` (highest priority) -> the contract's
``decisions.objective`` -> ``max_net_benefit`` (byte-identical default, DP#32),
and threads the resolved ``ObjectiveFunction`` into ``run_sweeps`` so each swept
value's ranked winner is the objective-appropriate one.

Tests are relational/plumbing, not magic-number snapshots of the full engine
(the engine is exercised in ``test_issue_1009_die_with_zero.py``): they prove
the objective is RESOLVED and THREADED, and that under ``min_after_tax_estate``
the winner's ``first_shortfall_year`` is monotone non-increasing in spend while
under the ``max_net_benefit`` default it is not. All figures fabricated,
role-based ids (DP#4/#15).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sweep
from objective import MAX_NET_BENEFIT, MIN_AFTER_TAX_ESTATE
import contract_schema


def _couple_doc() -> dict:
    """The shipped example trimmed to the couple + their children -- the shape
    the Phase-1 engine can actually simulate (#598). Mirrors
    ``test_issue_771_generalized_sweeps._couple_doc``."""
    with open(contract_schema.EXAMPLE_PATH) as fh:
        doc = json.load(fh)
    keep = {"p1", "p2", "ca", "cb"}

    def owner_ids(owner):
        return {j["person"] for j in owner["joint"]} if isinstance(owner, dict) else {owner}

    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
    for coll in ("accounts", "liabilities", "properties"):
        doc[coll] = [x for x in doc[coll] if owner_ids(x["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}
    ]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep
    ]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep
    ]
    doc.pop("provenance", None)
    return doc


def _write_doc(tmp_path, doc: dict) -> str:
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(doc))
    return str(path)


def _capture_run_sweeps(captured: dict):
    """A run_sweeps stand-in that records the resolved objective and returns an
    empty report, so the CLI's objective-resolution can be asserted without
    running the engine."""
    def fake(doc, objective=None):
        captured["objective"] = objective
        return {}
    return fake


# ── CLI objective resolution (#1017, DP#22) ─────────────────────────────────


def test_main_resolves_objective_from_contract_decisions(monkeypatch, tmp_path):
    """A contract declaring ``decisions.objective`` drives the sweep's
    objective with no ``--objective`` flag -- the die-with-zero frontier is
    read under the objective the household asked for, not max_net_benefit."""
    doc = _couple_doc()
    doc["decisions"]["objective"] = "min_after_tax_estate"
    path = _write_doc(tmp_path, doc)

    captured = {}
    monkeypatch.setattr(sweep, "run_sweeps",
                        _capture_run_sweeps(captured))
    monkeypatch.setattr(sys, "argv", ["sweep.py", "--input", path])

    sweep._main()
    assert captured["objective"] is MIN_AFTER_TAX_ESTATE


def test_main_objective_cli_override_wins(monkeypatch, tmp_path):
    """``--objective`` outranks ``decisions.objective`` -- the same precedence
    as ``optimize.py``'s ``resolve_objective`` (ad-hoc comparison override)."""
    doc = _couple_doc()
    doc["decisions"]["objective"] = "min_after_tax_estate"
    path = _write_doc(tmp_path, doc)

    captured = {}
    monkeypatch.setattr(sweep, "run_sweeps",
                        _capture_run_sweeps(captured))
    monkeypatch.setattr(sys, "argv", ["sweep.py", "--input", path, "--objective", "max_net_benefit"])

    sweep._main()
    assert captured["objective"] is MAX_NET_BENEFIT


def test_main_defaults_to_max_net_benefit_when_undeclared(monkeypatch, tmp_path):
    """A contract that declares no objective (the golden household declares
    none) sweeps under ``max_net_benefit`` -- byte-identical to the pre-#1017
    behaviour (DP#32: a fallback for absent input, never a coercion)."""
    doc = _couple_doc()
    assert "objective" not in doc["decisions"]
    path = _write_doc(tmp_path, doc)

    captured = {}
    monkeypatch.setattr(sweep, "run_sweeps",
                        _capture_run_sweeps(captured))
    monkeypatch.setattr(sys, "argv", ["sweep.py", "--input", path])

    sweep._main()
    assert captured["objective"] is MAX_NET_BENEFIT


def test_main_unknown_objective_refused_loudly(monkeypatch, tmp_path, capsys):
    """An unknown ``--objective`` is refused loudly (DP#32), naming the bad
    value -- never silently scored under the default. ``run_sweeps`` is never
    reached."""
    doc = _couple_doc()
    path = _write_doc(tmp_path, doc)

    monkeypatch.setattr(sweep, "run_sweeps",
                        lambda *a, **k: pytest.fail("run_sweeps must not run for a bad objective"))
    monkeypatch.setattr(sys, "argv", ["sweep.py", "--input", path, "--objective", "bogus_objective"])

    sweep._main()
    out = capsys.readouterr().out
    assert "Error" in out
    assert "bogus_objective" in out


# ── The frontier: monotone under min_after_tax_estate, not under the default ─


# Canned winners: for each spend, the ranked winner (results[0]) carries the
# first_shortfall_year the objective's ranking would surface. Under
# min_after_tax_estate the estate-minimising winner's shortfall year is
# MONOTONE NON-INCREASING in spend (higher spend => not-later shortfall); under
# max_net_benefit the wealth-maximising winner's year is NON-MONOTONE -- the
# exact shape issue #1017 reports ($150k->24, $250k->15, $400k->12, $600k->25).
_FSFY = {
    "min_after_tax_estate": {150_000: 30, 250_000: 25, 400_000: 25, 600_000: 25},
    "max_net_benefit":      {150_000: 24, 250_000: 15, 400_000: 12, 600_000: 25},
}


def _fake_optimization_factory(table):
    """A run_optimization stand-in that returns a canned winner whose
    first_shortfall_year depends on the swept spend and the resolved objective,
    proving the objective threads through to the winner a sweep row reports."""
    def fake(cfg, *args, **kwargs):
        objective = kwargs.get("objective")
        if objective is None:
            objective = MAX_NET_BENEFIT
        spend = cfg["retirement"]["spending_target"]
        fsfy = table[objective.name].get(spend, 99)
        return [{
            "label": f"winner-under-{objective.name}",
            "objective_score": 1.0,
            "net_benefit": 1.0,
            "drawdown_shortfall": {
                "engaged": True, "exhausted": True,
                "first_shortfall_year": fsfy, "first_shortfall_gap": 1000.0,
                "shortfall_years": 5, "total_unmet": 5000.0,
            },
        }]
    return fake


def _fsfy_rows(monkeypatch, objective):
    monkeypatch.setattr(sweep, "run_optimization", _fake_optimization_factory(_FSFY))
    doc = _couple_doc()
    spends = [150_000, 250_000, 400_000, 600_000]
    rows = sweep.run_axis_sweep(doc, "assumptions.retirement.spending_target",
                                spends, objective=objective)
    return [r["first_shortfall_year"] for r in rows]


def test_sweep_under_min_after_tax_estate_is_monotone_non_increasing(monkeypatch):
    """The die-with-zero frontier: under ``min_after_tax_estate`` the
    estate-minimising winner's ``first_shortfall_year`` does not get LATER as
    spend rises (higher spend => earlier-or-equal shortfall). This is the
    monotone frontier a user reading "highest sustainable spend" needs."""
    years = _fsfy_rows(monkeypatch, MIN_AFTER_TAX_ESTATE)
    assert years == [30, 25, 25, 25]
    assert all(years[i] >= years[i + 1] for i in range(len(years) - 1)), (
        f"first_shortfall_year must be non-increasing in spend under "
        f"min_after_tax_estate; got {years}")


def test_sweep_under_max_net_benefit_default_is_non_monotone(monkeypatch):
    """The contrast: under the ``max_net_benefit`` default the wealth-maximising
    winner's ``first_shortfall_year`` is NON-MONOTONE in spend (it dips then
    jumps back up) -- the frontier the pre-#1017 sweep reported, which is not
    the die-with-zero answer. Establishes that the two objectives select
    DIFFERENT winners per value, so hard-coding max_net_benefit was wrong."""
    years = _fsfy_rows(monkeypatch, MAX_NET_BENEFIT)
    assert years == [24, 15, 12, 25]
    assert not all(years[i] >= years[i + 1] for i in range(len(years) - 1)), (
        f"max_net_benefit frontier is expected to be non-monotone (the #1017 "
        f"strand); got {years}")


def test_min_and_max_objectives_select_different_winners_per_spend(monkeypatch):
    """The load-bearing claim: the two objectives rank different strategies
    first at the same spend, so the sweep's reported ``first_shortfall_year``
    differs by objective. Without this, hard-coding the objective would be
    cosmetically wrong but numerically harmless -- with it, the pre-#1017 sweep
    answered a different question than the one asked."""
    min_years = _fsfy_rows(monkeypatch, MIN_AFTER_TAX_ESTATE)
    max_years = _fsfy_rows(monkeypatch, MAX_NET_BENEFIT)
    assert min_years != max_years