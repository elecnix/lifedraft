#!/usr/bin/env python3
"""Issue #917: registered-account composition must reach the engine from a real
``--input`` contract, not only from an internally-constructed config.

#641 (per-account WHT drag) and #473/#859-B (asset-location placement) are both
fully wired and tested against a hand-built ``SimulationConfig``. But the single
contract->engine boundary, ``input_contract.to_internal_config``, mapped only
``non_reg`` composition into ``portfolio.accounts``. The rrsp/tfsa ``holdings``
declared on a real contract never populated ``portfolio.accounts.{rrsp,tfsa}``,
so from a ``--input`` document #641's drag and #473's recommendation were a
no-op: a household that declares "US equity in my RRSP" got the flat rate and no
placement advice.

These tests assert the boundary now threads that composition through:
- declared rrsp/tfsa product holdings populate ``portfolio.accounts.{rrsp,tfsa}``
  with a derived composition + yield (the shape both #641 and #473 read);
- the per-registered-pot WHT drag is derivable from the contract-declared
  holdings (rrsp < tfsa -- the US-treaty asset-location lever, #641);
- an asset-location placement decision is discoverable from the contract path
  (#473) once the two registered pots hold genuinely different profiles;
- absence is a strict no-op: registered accounts that declare no holdings add no
  ``portfolio.accounts`` entry, no drag, and no placement decision, and the
  legacy golden invariant (which never crosses this boundary) is untouched.
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import input_contract as ic
from countries.canada.portfolio import PortfolioConfig
import contract_schema


FOREIGN_PRODUCT = "synthetic_global_equity_index"   # US+intl equity, foreign_income
DOMESTIC_PRODUCT = "synthetic_fixed_income_index"    # bonds, no foreign leak


def _example_couple() -> dict:
    """The shipped contract example, trimmed to the p1/p2 couple + their two
    children + their own accounts (the same trimming tests/test_issue_691_mer.py
    uses)."""
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = copy.deepcopy(json.load(f))
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]

    def _owner_id(acc):
        o = acc.get("owner")
        if isinstance(o, str):
            return o
        if isinstance(o, dict):
            return o.get("person")
        return None

    doc["accounts"] = [a for a in doc["accounts"] if _owner_id(a) in keep]
    for section in ("gifts", "loans"):
        if section in doc:
            doc[section] = [
                g for g in doc[section]
                if g.get("from") in keep and g.get("to") in keep
            ]
    return doc


def _set_holdings(doc: dict, kind: str, product: str) -> None:
    """Point every account of ``kind`` at a single product (weight 1.0)."""
    for acc in doc["accounts"]:
        if acc["kind"] == kind:
            acc["holdings"] = [{"product": product, "weight": 1.0}]


def _drop_holdings(doc: dict, kind: str) -> None:
    for acc in doc["accounts"]:
        if acc["kind"] == kind:
            acc["holdings"] = []


class TestRegisteredCompositionReachesEngine:
    """Declared rrsp/tfsa holdings populate portfolio.accounts and drive the
    #641 WHT drag from the contract path."""

    def test_holdings_populate_portfolio_accounts(self):
        doc = _example_couple()
        _set_holdings(doc, "rrsp", FOREIGN_PRODUCT)
        _set_holdings(doc, "tfsa", FOREIGN_PRODUCT)
        contract_schema.validate_contract(doc)
        cfg = ic.to_internal_config(doc)

        accounts = cfg["portfolio"]["accounts"]
        assert "rrsp" in accounts
        assert "tfsa" in accounts
        # Foreign equity: the derived composition carries US/intl equity and the
        # yield block carries foreign income (the WHT physics' input).
        rrsp = accounts["rrsp"]
        assert rrsp["composition"]["us_equity_pct"] > 0
        assert rrsp["composition"]["intl_equity_pct"] > 0
        assert rrsp["yield"]["foreign_income"] > 0

    def test_wht_drag_derived_from_contract_holdings(self):
        doc = _example_couple()
        _set_holdings(doc, "rrsp", FOREIGN_PRODUCT)
        _set_holdings(doc, "tfsa", FOREIGN_PRODUCT)
        contract_schema.validate_contract(doc)
        cfg = ic.to_internal_config(doc)

        drag = PortfolioConfig.from_dict(cfg["portfolio"]).registered_wht_drag()
        assert drag.get("rrsp", 0) > 0
        assert drag.get("tfsa", 0) > 0
        # The US-treaty advantage: the same foreign holding leaks less in an
        # RRSP than a TFSA. This differential IS the asset-location lever #641
        # exists to make expressible from a real contract.
        assert drag["rrsp"] < drag["tfsa"]


class TestAssetLocationReachableFromContract:
    """A placement decision (#473) is discoverable from the contract path once
    the two registered pots hold genuinely different profiles."""

    def test_placement_decision_discoverable(self):
        from asset_location_optimize import discover_placements

        doc = _example_couple()
        _set_holdings(doc, "rrsp", FOREIGN_PRODUCT)
        _set_holdings(doc, "tfsa", DOMESTIC_PRODUCT)
        contract_schema.validate_contract(doc)
        cfg = ic.to_internal_config(doc)

        placements = discover_placements(cfg)
        # More than the single declared arrangement means the household has a
        # real "which pot holds the foreign sleeve" choice to optimize.
        assert len(placements) >= 2
        assert any(p["foreign_kind"] is not None for p in placements)


class TestAbsenceIsNoOp:
    """A contract that declares no registered composition is byte-identical to
    today: no portfolio entry, no drag, no placement decision."""

    def test_no_registered_holdings_no_portfolio_entry(self):
        doc = _example_couple()
        _drop_holdings(doc, "rrsp")
        _drop_holdings(doc, "tfsa")
        contract_schema.validate_contract(doc)
        cfg = ic.to_internal_config(doc)

        accounts = cfg["portfolio"].get("accounts", {})
        assert "rrsp" not in accounts
        assert "tfsa" not in accounts

    def test_domestic_holdings_emit_no_drag(self):
        doc = _example_couple()
        _set_holdings(doc, "rrsp", DOMESTIC_PRODUCT)
        _set_holdings(doc, "tfsa", DOMESTIC_PRODUCT)
        contract_schema.validate_contract(doc)
        cfg = ic.to_internal_config(doc)

        drag = PortfolioConfig.from_dict(cfg["portfolio"]).registered_wht_drag()
        assert "rrsp" not in drag
        assert "tfsa" not in drag

    def test_no_registered_holdings_no_placement(self):
        from asset_location_optimize import discover_placements

        doc = _example_couple()
        _drop_holdings(doc, "rrsp")
        _drop_holdings(doc, "tfsa")
        contract_schema.validate_contract(doc)
        cfg = ic.to_internal_config(doc)

        assert len(discover_placements(cfg)) == 1

    def test_legacy_golden_invariant_untouched(self):
        # This change touches only the contract adapter; the golden invariant is
        # computed from an internally-constructed config that never crosses that
        # boundary, so it cannot move by construction.
        from test_golden_trajectory_581 import golden_household_config, _run
        assert _run(golden_household_config())[-1].total_assets == 9709753.139463063
