#!/usr/bin/env python3
"""Issue #862: optimizer objectives are runtime-selectable (contract + CLI).

Every objective in ``objective.OBJECTIVES`` was registered and unit-tested,
but NONE was reachable from a real run -- ``optimize.py`` hardcoded
``MAX_NET_BENEFIT`` and there was no CLI flag or contract field. DP#22 ("the
optimizer ranks, it doesn't choose") was half-built: the objectives were
pluggable data, but the plug had no socket. This file proves the socket
exists, both ways:

  1. a declared ``decisions.objective`` reaches the internal config and
     provably CHANGES which objective a run is scored under (``objective_name``
     in the ranked results differs), and
  2. an unknown name -- from either the contract or the CLI ``--objective``
     flag -- is refused LOUDLY (DP#32), naming the bad value and listing the
     valid names, never silently scored under the default.

The default is a NO-OP: a contract that declares no objective still ranks on
``max_net_benefit`` (the golden invariant depends on this).

All test data is the shipped example, trimmed to one couple + their children
(DP#15: synthetic ids p1/p2/ca/cb, round numbers -- no personal data).
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
import optimize
from objective import MAX_NET_BENEFIT, OBJECTIVES
import contract_errors
import contract_schema


def _owner_ids(owner):
    return {j["person"] for j in owner["joint"]} if isinstance(owner, dict) else {owner}


def _two_generation_subset(doc):
    """Trim the shipped 4-generation example to p1/p2 + their direct children
    -- the sub-family Phase 1's adapter can honestly map (see input_contract's
    module docstring)."""
    doc = copy.deepcopy(doc)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
    doc["accounts"] = [a for a in doc["accounts"] if _owner_ids(a["owner"]) <= keep]
    doc["liabilities"] = [l for l in doc["liabilities"] if _owner_ids(l["owner"]) <= keep]
    doc["properties"] = [p for p in doc["properties"] if _owner_ids(p["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep]
    return doc


def _base_doc():
    with open(contract_schema.EXAMPLE_PATH) as f:
        return _two_generation_subset(json.load(f))


class TestObjectiveResolution(unittest.TestCase):
    """optimize.resolve_objective: CLI > contract > default (DP#22)."""

    def test_default_is_max_net_benefit_when_nothing_declared(self):
        """Nothing declared -> the historical default. The golden invariant
        rides on this: an undeclared objective must be a pure no-op."""
        self.assertIs(optimize.resolve_objective(None, {}), MAX_NET_BENEFIT)

    def test_contract_objective_resolves(self):
        cfg = {"objective": "max_after_tax_estate"}
        self.assertEqual(optimize.resolve_objective(None, cfg).name,
                         "max_after_tax_estate")

    def test_cli_overrides_contract(self):
        """--objective wins over decisions.objective (ad-hoc comparison)."""
        cfg = {"objective": "max_after_tax_estate"}
        self.assertEqual(
            optimize.resolve_objective("max_family_after_tax_networth", cfg).name,
            "max_family_after_tax_networth")

    def test_unknown_cli_name_refused_loudly(self):
        """DP#32: an unknown --objective names the bad value + the valid names,
        never a silent fallback to the default."""
        with self.assertRaises(optimize.ObjectiveSelectionError) as ctx:
            optimize.resolve_objective("max_bogus", {})
        msg = str(ctx.exception)
        self.assertIn("max_bogus", msg)
        self.assertIn("max_net_benefit", msg)


class TestContractField(unittest.TestCase):
    """decisions.objective threads through input_contract -> internal config."""

    def test_declared_objective_reaches_internal_config(self):
        doc = _base_doc()
        doc["decisions"]["objective"] = "max_family_after_tax_networth"
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["objective"], "max_family_after_tax_networth")

    def test_absent_objective_is_a_no_op(self):
        """No decisions.objective -> no 'objective' key on the internal config,
        so resolution falls to the default (DP#32: absence is a no-op)."""
        cfg = ic.to_internal_config(_base_doc())
        self.assertNotIn("objective", cfg)
        self.assertIs(optimize.resolve_objective(None, cfg), MAX_NET_BENEFIT)

    def test_unknown_contract_objective_refused_at_load(self):
        """DP#32: a typo'd decisions.objective is refused at the ingestion
        boundary, naming the bad value + the valid names -- not three layers
        deep, and never silently scored under the wrong objective."""
        doc = _base_doc()
        doc["decisions"]["objective"] = "max_bogus"
        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("max_bogus", msg)
        self.assertIn("max_net_benefit", msg)


class TestObjectiveChangesTheRun(unittest.TestCase):
    """The reach the issue is about: a declared objective provably changes
    which objective a real run scores under (objective_name differs)."""

    def test_declared_objective_changes_reported_objective_name(self):
        base = _base_doc()

        cfg_default = ic.to_internal_config(base)
        res_default = optimize.run_optimization(
            cfg_default, "x",
            objective=optimize.resolve_objective(None, cfg_default))

        doc2 = copy.deepcopy(base)
        doc2["decisions"]["objective"] = "max_family_after_tax_networth"
        cfg2 = ic.to_internal_config(doc2)
        res2 = optimize.run_optimization(
            cfg2, "x", objective=optimize.resolve_objective(None, cfg2))

        # Every ranked row is scored under the resolved objective, and the two
        # runs disagree about which one -- the reach that did not exist before.
        self.assertTrue(res_default and res2)
        self.assertTrue(all(r["objective_name"] == "max_net_benefit"
                            for r in res_default))
        self.assertTrue(all(r["objective_name"] == "max_family_after_tax_networth"
                            for r in res2))
        self.assertNotEqual(res_default[0]["objective_name"],
                            res2[0]["objective_name"])

    def test_cli_flag_changes_reported_objective_name(self):
        """The CLI override path scores the run under the flagged objective
        even when the contract declared none."""
        cfg = ic.to_internal_config(_base_doc())
        res = optimize.run_optimization(
            cfg, "x",
            objective=optimize.resolve_objective("max_terminal_wealth", cfg))
        self.assertTrue(all(r["objective_name"] == "max_terminal_wealth"
                            for r in res))


class TestListObjectives(unittest.TestCase):
    """--list-objectives surfaces the registry so the plug's sockets are
    discoverable."""

    def test_lists_every_registered_objective(self):
        text = "\n".join(optimize._list_objectives_text())
        for name in OBJECTIVES:
            self.assertIn(name, text)


# ── CLI end-to-end (drives optimize.main in-process, DP#22 through the real
#    entry point) ─────────────────────────────────────────────────────────

def _write_contract(tmp_path, objective=None):
    """Write the trimmed example contract to disk, optionally declaring a
    decisions.objective, and return the path."""
    doc = _base_doc()
    if objective is not None:
        doc["decisions"]["objective"] = objective
    path = tmp_path / "contract.json"
    with open(path, "w") as f:
        json.dump(doc, f)
    return path


def test_cli_list_objectives_prints_the_menu(monkeypatch, capsys):
    """`optimize.py --list-objectives` prints every registered objective and
    exits before touching any input document."""
    monkeypatch.setattr(sys, "argv", ["optimize.py", "--list-objectives"])
    optimize.main()
    out = capsys.readouterr().out
    for name in OBJECTIVES:
        assert name in out


def test_cli_unknown_objective_refused_loudly(tmp_path, monkeypatch, capsys):
    """`--objective <typo>` refuses loudly (DP#32) -- names the bad value and
    lists the valid names -- instead of silently scoring under the default."""
    path = _write_contract(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["optimize.py", "--input", str(path),
                         "--objective", "max_bogus"])
    optimize.main()
    out = capsys.readouterr().out
    assert "Unknown objective" in out
    assert "max_bogus" in out
    assert "max_net_benefit" in out


def test_cli_objective_flag_changes_reported_objective_name(tmp_path, monkeypatch):
    """The acceptance the issue is about: `--objective
    max_family_after_tax_networth` makes the REAL CLI run score under -- and
    report -- that objective, not the hardcoded max_net_benefit default."""
    path = _write_contract(tmp_path)
    out_json = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv",
                        ["optimize.py", "--input", str(path),
                         "--objective", "max_family_after_tax_networth",
                         "--json", str(out_json)])
    optimize.main()
    text = out_json.read_text()
    assert '"objective_name": "max_family_after_tax_networth"' in text
    assert '"objective_name": "max_net_benefit"' not in text


def test_cli_contract_objective_changes_reported_objective_name(tmp_path, monkeypatch):
    """The contract path: a declared decisions.objective (no CLI flag) drives
    the same reported objective through the real CLI."""
    path = _write_contract(tmp_path, objective="max_family_after_tax_networth")
    out_json = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv",
                        ["optimize.py", "--input", str(path),
                         "--json", str(out_json)])
    optimize.main()
    text = out_json.read_text()
    assert '"objective_name": "max_family_after_tax_networth"' in text


if __name__ == "__main__":
    unittest.main()
