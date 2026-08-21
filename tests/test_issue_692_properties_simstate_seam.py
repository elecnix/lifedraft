#!/usr/bin/env python3
"""Issue #692 (epic #690, bite 1): a declared non-principal property reaches
SimState and its net equity (value - mortgage) counts in annual net worth.

Before this seam, ``to_internal_config`` read exactly ONE ``kind="principal"``
residence for the annual balance sheet (``input_contract`` ``prop_cfg`` ->
``house_value``); every other declared property the couple owned -- a cottage,
a rental -- was absent from ``SimState`` and therefore from every annual metric
(net worth / ``total_assets``), materialising only at the terminal estate. This
is the "properties[] beyond the principal are silently dropped" defect.

Scope of THIS bite (the SimState seam only): the couple's non-principal
properties are carried into the internal config + ``SimState`` and their net
equity is added to ``total_assets``. Rental income (#693), CCA (#694), the PRE
allocation (#695), a mid-horizon purchase (#696) and STR (#697) are LATER bites
and deliberately out of scope here -- the property's equity is a static
balance-sheet figure, not yet an income/appreciation model.

All fixtures use fabricated ids and round numbers (DP#15).
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation_state import SimState
from simulation import FamilySimulation

from test_input_contract import _load_example, _two_generation_subset


def _add_owned_cottage(doc, value, mortgage_balance):
    """A recreational property the PRIMARY COUPLE (p1/p2) jointly owns, with a
    mortgage of ``mortgage_balance`` secured against it. Net equity to the
    couple is ``value - mortgage_balance`` (both fully couple-owned)."""
    doc = copy.deepcopy(doc)
    doc["properties"].append({
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": value, "as_of": "2026-06-30"},
        "acb": 150000,
        "designated_principal_residence_years": [],
    })
    if mortgage_balance:
        doc["liabilities"].append({
            "id": "cottage_mortgage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "mortgage",
            "balance": {"amount": mortgage_balance, "as_of": "2026-06-30"},
            "rate": 0.05,
            "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 1500},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
            "collateral": "couple_cottage",
        })
    return doc


def _terminal_total_assets(doc):
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()[-1].total_assets


class PropertyEquityReachesNetWorth(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())  # principal only
        ic.validate_contract(self.base)

    def test_net_equity_is_added_to_terminal_total_assets(self):
        """A cottage worth 400k with a 250k mortgage lifts the household's
        annual net worth by exactly its 150k net equity."""
        with_cottage = _add_owned_cottage(self.base, value=400000,
                                           mortgage_balance=250000)
        ic.validate_contract(with_cottage)
        delta = _terminal_total_assets(with_cottage) - _terminal_total_assets(self.base)
        self.assertAlmostEqual(delta, 150000.0, places=6)

    def test_mortgage_free_property_contributes_its_full_value(self):
        """With no mortgage, the whole value is equity."""
        with_cottage = _add_owned_cottage(self.base, value=300000,
                                          mortgage_balance=0)
        ic.validate_contract(with_cottage)
        delta = _terminal_total_assets(with_cottage) - _terminal_total_assets(self.base)
        self.assertAlmostEqual(delta, 300000.0, places=6)

    def test_property_reaches_the_internal_config_and_simstate(self):
        with_cottage = _add_owned_cottage(self.base, value=400000,
                                          mortgage_balance=250000)
        legacy = ic.to_internal_config(with_cottage)
        self.assertIn("properties", legacy)
        equities = [p["net_equity"] for p in legacy["properties"]]
        self.assertEqual(equities, [150000.0])
        cfg = SimulationConfig.from_dict(legacy)
        state = SimState.initial(cfg)
        self.assertEqual(state.property_equities, [150000.0])


class TestAbsenceIsNoOp(unittest.TestCase):
    """DP#32: a household that declares no couple-owned non-principal property
    (the golden path) must be byte-identical to today -- no `properties` block,
    no SimState list, no movement in total_assets."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())  # principal only
        ic.validate_contract(self.base)

    def test_no_properties_block_emitted(self):
        legacy = ic.to_internal_config(self.base)
        self.assertNotIn("properties", legacy)

    def test_simstate_property_equities_defaults_empty(self):
        legacy = ic.to_internal_config(self.base)
        cfg = SimulationConfig.from_dict(legacy)
        state = SimState.initial(cfg)
        self.assertEqual(state.property_equities, [])

    def test_someone_elses_property_is_not_counted(self):
        """The shipped example's cottage is owned by the grandparents, not the
        primary couple -- it must NOT enter the couple's balance sheet."""
        full = _load_example()
        # The full 4-generation doc is refused for other reasons (#901); assert
        # the OWNERSHIP filter directly on the mapper instead.
        primary_id, spouse_id = ic._find_primary_and_spouse(
            _two_generation_subset(full))
        base = _two_generation_subset(full)
        base["properties"].append({
            "id": "grandparents_cottage",
            "owner": {"joint": [{"person": "gf", "pct": 0.5},
                                {"person": "gm", "pct": 0.5}]},
            "kind": "recreational",
            "value": {"amount": 310000, "as_of": "2026-06-30"},
            "acb": 180000,
            "designated_principal_residence_years": [],
        })
        owned = ic._map_owned_properties(base, primary_id, spouse_id)
        self.assertEqual(owned, [])


if __name__ == "__main__":
    unittest.main()
