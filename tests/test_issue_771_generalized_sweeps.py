"""Issue #771: sensitivity.sweeps is a GENERAL contract-path -> values map.

The founding property under test is DP#32's: a sweep path that does not resolve
to a real contract leaf must FAIL LOUDLY, naming the path -- never silently
collapse into a single-point run that only looks like a sweep (the "engine
substitutes nothing" defect this repo exists to kill). Alongside it:

* a declared path (e.g. assumptions.retirement.spending_target) produces one
  optimizer result per value, carrying the objective AND the first-shortfall
  year (#707/#770);
* the three legacy axes are SUGAR over the same resolver (DP#9) -- sweeping the
  alias must be byte-for-byte the same as sweeping the canonical path it names,
  which is the strongest possible statement of "no second spelling."

Tests are invariant/relational, not magic-number snapshots: higher retirement
spending yields a lower objective; the alias run equals the literal-path run.
All figures fabricated, role-based ids (DP#4/DP#15).
"""
from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sweep
import contract_schema


def _couple_doc() -> dict:
    """The shipped example trimmed to the couple + their children -- the shape
    the Phase-1 engine can actually simulate (#598; the full four-generation
    example is correctly REFUSED). Mirrors tests/test_voi_661.py's fixture."""
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


# ── The DP#32 property: a bad path fails loudly, naming it ───────────────────

def test_a_path_that_does_not_resolve_fails_loudly_naming_the_path():
    doc = _couple_doc()
    with pytest.raises(sweep.SweepPathError) as exc:
        sweep.run_axis_sweep(doc, "assumptions.nonexistent.key", [1, 2])
    assert "assumptions.nonexistent.key" in str(exc.value)


def test_bad_path_fails_before_any_simulation_runs(monkeypatch):
    """The loud failure is validation, not a post-run crash: the optimizer must
    never be invoked for an unresolvable axis (DP#32 -- catch the typo, don't
    burn a pass on it)."""
    doc = _couple_doc()
    called = []
    monkeypatch.setattr(sweep, "run_optimization", lambda *a, **k: called.append(1) or [])
    with pytest.raises(sweep.SweepPathError):
        sweep.run_axis_sweep(doc, "assumptions.retirement.no_such_leaf", [1])
    assert called == []


def test_out_of_range_list_index_fails_loudly():
    doc = _couple_doc()
    with pytest.raises(sweep.SweepPathError):
        sweep.resolve_leaf(doc, f"liabilities.{len(doc['liabilities'])}.rate")


def test_resolve_leaf_returns_container_and_key_for_a_real_leaf():
    doc = _couple_doc()
    container, key = sweep.resolve_leaf(doc, "assumptions.retirement.spending_target")
    assert container is doc["assumptions"]["retirement"]
    assert key == "spending_target"


# ── A declared path: one row per value, objective + shortfall reported ───────

def test_one_result_row_per_swept_value():
    doc = _couple_doc()
    values = [70000, 100000, 125000]
    rows = sweep.run_axis_sweep(doc, "assumptions.retirement.spending_target", values)
    assert [r["value"] for r in rows] == values


def test_every_row_reports_objective_and_first_shortfall_year():
    doc = _couple_doc()
    rows = sweep.run_axis_sweep(doc, "assumptions.retirement.spending_target", [70000, 125000])
    for r in rows:
        assert r["objective_score"] is not None
        assert "first_shortfall_year" in r  # present even when None (never dropped)


def test_higher_retirement_spending_lowers_the_objective():
    """Relational invariant (not a snapshot): asking the plan to fund more
    net spending in retirement cannot RAISE terminal net benefit."""
    doc = _couple_doc()
    rows = sweep.run_axis_sweep(doc, "assumptions.retirement.spending_target", [70000, 125000])
    assert rows[0]["objective_score"] > rows[1]["objective_score"]


# ── DP#9: the legacy axes are sugar over the SAME resolver ───────────────────

def test_legacy_axis_expands_to_its_canonical_contract_path():
    doc = _couple_doc()
    assert sweep._expand_axis(doc, "investment_return") == ["assumptions.return_model.rate"]
    assert sweep._expand_axis(doc, "savings_rate") == ["assumptions.savings_rate"]


def test_mortgage_axis_broadcasts_to_every_mortgage_liability():
    doc = _couple_doc()
    expected = [f"liabilities.{i}.rate"
                for i, l in enumerate(doc["liabilities"]) if l["kind"] == "mortgage"]
    assert expected  # fixture has a mortgage
    assert sweep._expand_axis(doc, "mortgage_rate") == expected


def test_mortgage_axis_without_a_mortgage_fails_loudly():
    doc = _couple_doc()
    doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "mortgage"]
    with pytest.raises(sweep.SweepPathError) as exc:
        sweep._expand_axis(doc, "mortgage_rate")
    assert "mortgage_rate" in str(exc.value)


def test_legacy_alias_and_canonical_path_produce_identical_results():
    """The strongest DP#9 statement: sweeping ``investment_return`` and sweeping
    ``assumptions.return_model.rate`` are the SAME sweep, because the alias is
    nothing but sugar for that path -- not a parallel code path that happens to
    agree."""
    doc = _couple_doc()
    via_alias = sweep.run_axis_sweep(doc, "investment_return", [0.05])
    via_path = sweep.run_axis_sweep(doc, "assumptions.return_model.rate", [0.05])
    assert via_alias[0]["objective_score"] == via_path[0]["objective_score"]


# ── first_shortfall_year is surfaced verbatim from the winner's summary ──────

def test_first_shortfall_year_is_read_off_the_optimizer_winner(monkeypatch):
    """Prove the shortfall plumbing without depending on the engine actually
    exhausting: the ranked winner's drawdown_shortfall summary is what a row
    reports. results[0] is the winner because run_optimization sorts
    exhausted-below-solvent (#707)."""
    doc = _couple_doc()
    canned = [{
        "label": "bankrupt-but-top",
        "objective_score": 123.0,
        "net_benefit": 123.0,
        "drawdown_shortfall": {
            "engaged": True, "exhausted": True,
            "first_shortfall_year": 19, "first_shortfall_gap": 4000.0,
            "shortfall_years": 12, "total_unmet": 50000.0,
        },
    }]
    monkeypatch.setattr(sweep, "run_optimization", lambda *a, **k: canned)
    rows = sweep.run_axis_sweep(doc, "assumptions.retirement.spending_target", [90000])
    assert rows[0]["first_shortfall_year"] == 19
    assert rows[0]["exhausted"] is True
    assert rows[0]["objective_score"] == 123.0


# ── run_sweeps orchestration ─────────────────────────────────────────────────

def test_absent_sweeps_block_is_a_no_op_not_an_error():
    doc = _couple_doc()
    doc["sensitivity"]["sweeps"] = {}
    assert sweep.run_sweeps(doc) == {}


def test_run_sweeps_covers_every_declared_axis(monkeypatch):
    doc = _couple_doc()
    doc["sensitivity"]["sweeps"] = {
        "assumptions.retirement.spending_target": [80000],
        "savings_rate": [0.2],
    }
    monkeypatch.setattr(sweep, "run_optimization",
                        lambda *a, **k: [{"objective_score": 1.0, "net_benefit": 1.0, "label": "x"}])
    report = sweep.run_sweeps(doc)
    assert set(report) == {"assumptions.retirement.spending_target", "savings_rate"}


def test_empty_value_list_is_rejected():
    doc = _couple_doc()
    with pytest.raises(sweep.SweepPathError):
        sweep.run_axis_sweep(doc, "assumptions.retirement.spending_target", [])


# ── Path resolution: pointer form + the remaining loud-failure shapes ────────

def test_a_json_pointer_key_resolves_the_same_leaf_as_the_dotted_form():
    doc = _couple_doc()
    dotted = sweep.resolve_leaf(doc, "assumptions.retirement.spending_target")
    pointer = sweep.resolve_leaf(doc, "/assumptions/retirement/spending_target")
    assert dotted == pointer


def test_a_list_index_addresses_a_liability_rate():
    doc = _couple_doc()
    # `liabilities.0.rate` walks into the list at index 0 and lands on the rate
    # leaf of that liability -- container is the liability object, key "rate".
    container, key = sweep.resolve_leaf(doc, "liabilities.0.rate")
    assert container is doc["liabilities"][0]
    assert key == "rate"


def test_a_final_list_index_out_of_range_fails_loudly():
    """A path whose final segment indexes a list past its end (mortality is a
    list) is a bad path -- the loud failure covers the list-leaf branch."""
    doc = _couple_doc()
    n = len(doc["assumptions"]["mortality"])
    with pytest.raises(sweep.SweepPathError):
        sweep.resolve_leaf(doc, f"assumptions.mortality.{n}")


def test_a_valid_final_list_index_resolves_the_list_element():
    doc = _couple_doc()
    container, key = sweep.resolve_leaf(doc, "assumptions.mortality.0")
    assert container is doc["assumptions"]["mortality"]
    assert key == 0


def test_descending_through_a_scalar_mid_path_fails_loudly():
    """A scalar segment BEFORE the last (savings_rate is a number) cannot be
    descended into -- the loud failure covers the mid-path scalar branch."""
    doc = _couple_doc()
    with pytest.raises(sweep.SweepPathError):
        sweep.resolve_leaf(doc, "assumptions.savings_rate.child.deeper")


def test_a_non_integer_list_index_fails_loudly():
    doc = _couple_doc()
    with pytest.raises(sweep.SweepPathError):
        sweep.resolve_leaf(doc, "liabilities.notanint.rate")


def test_a_document_without_a_sensitivity_block_sweeps_nothing():
    doc = _couple_doc()
    doc.pop("sensitivity", None)
    assert sweep.run_sweeps(doc) == {}


def test_a_missing_intermediate_segment_fails_loudly():
    doc = _couple_doc()
    with pytest.raises(sweep.SweepPathError):
        sweep.resolve_leaf(doc, "assumptions.absent.deeper.leaf")


def test_descending_through_a_scalar_fails_loudly():
    """savings_rate is a number; asking for a key beneath it is a bad path."""
    doc = _couple_doc()
    with pytest.raises(sweep.SweepPathError):
        sweep.resolve_leaf(doc, "assumptions.savings_rate.child")


def test_no_optimizer_results_yields_a_row_with_no_objective(monkeypatch):
    doc = _couple_doc()
    monkeypatch.setattr(sweep, "run_optimization", lambda *a, **k: [])
    rows = sweep.run_axis_sweep(doc, "assumptions.retirement.spending_target", [90000])
    assert rows[0]["objective_score"] is None
    assert rows[0]["first_shortfall_year"] is None


# ── Readable output (acceptance criterion 4) ─────────────────────────────────

def _rows_for_format():
    return [
        {"axis": "x", "value": 70000, "objective_score": 10227699.0, "label": "a",
         "engaged": True, "exhausted": False, "first_shortfall_year": None, "shortfall_years": 0},
        {"axis": "x", "value": 125000, "objective_score": 3021459.0, "label": "b",
         "engaged": True, "exhausted": True, "first_shortfall_year": 19, "shortfall_years": 8},
        {"axis": "x", "value": 200000, "objective_score": None, "label": None,
         "engaged": False, "exhausted": False, "first_shortfall_year": None, "shortfall_years": 0},
    ]


def test_table_renders_value_objective_shortfall_and_exhausted():
    table = sweep.format_sweep_table("assumptions.retirement.spending_target", _rows_for_format())
    assert "assumptions.retirement.spending_target" in table
    assert "70,000" in table            # grouped dollar value
    assert "19" in table                # the first-shortfall year
    assert "YES" in table               # the exhausted row is flagged
    assert "n/a (no drawdown)" in table  # the un-engaged row is not a silent zero
    assert "n/a" in table               # the None objective is not printed as 0


def test_format_all_reports_when_no_sweeps_were_declared():
    assert "No sweeps declared" in sweep.format_all({})


def test_format_all_joins_every_axis():
    out = sweep.format_all({"axis_a": _rows_for_format(), "axis_b": _rows_for_format()})
    assert "axis_a" in out and "axis_b" in out


def test_table_renders_a_non_numeric_swept_value():
    rows = [{"axis": "x", "value": "high", "objective_score": 1.0, "label": "a",
             "engaged": True, "exhausted": False, "first_shortfall_year": None,
             "shortfall_years": 0}]
    assert "high" in sweep.format_sweep_table("x", rows)


def test_cli_prints_a_table_for_a_declared_sweep(monkeypatch, capsys):
    """The CLI entry loads a contract, runs its declared sweeps, and prints the
    curve -- the command a user actually runs (acceptance criterion 4)."""
    doc = _couple_doc()
    doc["sensitivity"]["sweeps"] = {"assumptions.retirement.spending_target": [80000]}
    monkeypatch.setattr(sys, "argv", ["sweep.py", "--input", "ignored.json"])
    monkeypatch.setattr(sweep.contract_schema, "load_contract_json", lambda p: doc)
    monkeypatch.setattr(sweep.contract_schema, "validate_contract", lambda d: None)
    monkeypatch.setattr(sweep, "run_optimization",
                        lambda *a, **k: [{"objective_score": 42.0, "net_benefit": 42.0, "label": "x"}])
    sweep._main()
    out = capsys.readouterr().out
    assert "assumptions.retirement.spending_target" in out
    assert "80,000" in out
