#!/usr/bin/env python3
"""Integration tests for the ``zev_purchases[]`` block.

DP#11: the unit tests in ``countries/canada/tests/test_zev_incentive.py`` and
``countries/canada/provinces/quebec/tests/test_roulez_vert.py`` verify each
module's contract. This file verifies COMPOSITION: that a declared acquisition
survives validation, the adapter, ``SimulationConfig``, a round-trip, and
actually reaches the objective.

That last step is the point. AGENTS.md lists "parsed, mapped, then never passed"
as a trap this codebase has already fallen into: ``decisions.income[]`` reached
the config and stopped there, and the schema-coverage guard passed anyway
because the ADAPTER read the leaf. So the load-bearing assertion here is
``test_incentive_changes_the_objective`` -- a measured delta in
``compute_net_benefit``, not the mere presence of a key on the config.

All data is fabricated round numbers (DP#4/DP#15).
"""

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The adapter was split per contract namespace and the config per concern, so
# each name is imported from the module that OWNS it -- no re-export shim (DP#9).
import contract_schema
import contract_transfers
from contract_errors import ContractAdaptationError
from simulation_config import SimulationConfig
from year_result import YearResult

# An acquisition inside BOTH programs' open windows, under every cap.
OPEN_WINDOW_BEV = {
    "id": "ev_a",
    "acquisition_date": "2024-06-01",
    "base_msrp": 45000.0,
    "trim_msrp": 50000.0,
    "vehicle_class": "car",
    "propulsion": "battery_electric",
    "is_lease": False,
}


def _example_with(zev_entries):
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = json.load(f)
    doc = copy.deepcopy(doc)
    doc["zev_purchases"] = copy.deepcopy(zev_entries)
    return doc


# --------------------------------------------------------------------------
# The block validates and survives the adapter.
# --------------------------------------------------------------------------

def test_example_without_the_block_still_validates():
    """DP#16: absent trigger data disables the module. The shipped example (and
    the golden household) declare no vehicle and must be unaffected."""
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = json.load(f)
    contract_schema.validate_contract(doc)
    # Assert on the MAPPER, not on to_internal_config: the shipped example is a
    # four-generation household the adapter refuses outright (a documented,
    # pre-existing limit -- see contract_people's #698/#643/#901 refusal), so
    # driving the whole adapter here would test that refusal, not this block.
    # The previous spelling guarded the assertion behind `hasattr(ic,
    # "map_to_internal")`, a name that never existed, so it never ran at all.
    assert contract_transfers._map_zev_purchases(doc) == []


def test_declared_acquisition_validates():
    contract_schema.validate_contract(_example_with([OPEN_WINDOW_BEV]))


def test_schema_rejects_a_lease_without_a_term():
    """The schema's conditional required, independent of the adapter's."""
    bad = dict(OPEN_WINDOW_BEV, is_lease=True)
    with pytest.raises(Exception):
        contract_schema.validate_contract(_example_with([bad]))


def test_schema_rejects_a_phev_without_a_range():
    bad = dict(OPEN_WINDOW_BEV, propulsion="phev")
    with pytest.raises(Exception):
        contract_schema.validate_contract(_example_with([bad]))


def test_schema_rejects_an_unknown_propulsion():
    bad = dict(OPEN_WINDOW_BEV, propulsion="diesel")
    with pytest.raises(Exception):
        contract_schema.validate_contract(_example_with([bad]))


# --------------------------------------------------------------------------
# The adapter's own loud refusals (DP#32). These must fire for a config
# assembled in CODE, which never passes through schema validation.
# --------------------------------------------------------------------------

def test_adapter_refuses_a_duplicate_id():
    doc = {"zev_purchases": [OPEN_WINDOW_BEV, dict(OPEN_WINDOW_BEV)]}
    with pytest.raises(ContractAdaptationError, match="more than once"):
        contract_transfers._map_zev_purchases(doc)


def test_adapter_refuses_a_lease_without_a_term():
    doc = {"zev_purchases": [dict(OPEN_WINDOW_BEV, is_lease=True)]}
    with pytest.raises(ContractAdaptationError, match="lease_term_months"):
        contract_transfers._map_zev_purchases(doc)


def test_adapter_refuses_a_phev_without_a_range():
    doc = {"zev_purchases": [dict(OPEN_WINDOW_BEV, propulsion="phev")]}
    with pytest.raises(ContractAdaptationError, match="electric_range_km"):
        contract_transfers._map_zev_purchases(doc)


def test_adapter_maps_every_field():
    entry = dict(OPEN_WINDOW_BEV, propulsion="phev", electric_range_km=60,
                 is_lease=True, lease_term_months=36)
    mapped = contract_transfers._map_zev_purchases({"zev_purchases": [entry]})
    assert len(mapped) == 1
    assert mapped[0]["acquisition_date"] == "2024-06-01"
    assert mapped[0]["electric_range_km"] == 60.0
    assert mapped[0]["lease_term_months"] == 36
    assert mapped[0]["vehicle_class"] == "car"


def test_adapter_returns_empty_when_absent():
    assert contract_transfers._map_zev_purchases({}) == []


# --------------------------------------------------------------------------
# SimulationConfig carries it, and it round-trips (DP#24).
# --------------------------------------------------------------------------

def test_config_round_trips_the_block():
    cfg_dict = {"zev_purchases": [OPEN_WINDOW_BEV]}
    cfg = SimulationConfig.from_dict(cfg_dict)
    assert cfg.zev_purchases == [OPEN_WINDOW_BEV]
    again = SimulationConfig.from_dict(cfg.to_dict())
    assert again.zev_purchases == cfg.zev_purchases


def test_config_defaults_to_empty():
    assert SimulationConfig.from_dict({}).zev_purchases == []


# --------------------------------------------------------------------------
# The load-bearing one: the incentive REACHES the objective.
# --------------------------------------------------------------------------

def _minimal_results():
    return [YearResult(
        year=2036, total_assets=500000, total_debt=0,
        total_rrsp=0, total_tfsa=0,
        non_reg_balance=0, non_reg_acb=0, resp_balance=0,
        lif_withdrawal=0, lif_balance=0, lira_balance=0,
    )]


def _base_cfg(**over):
    cfg = {
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 100000,
             'cpp_monthly_estimated': 0, 'oas_start_age': 65,
             'pension_income_annual': 0},
        ]},
        'assumptions': {'oas_annual': 0, 'capital_gains_inclusion': 0.50},
        'tax': {'country': 'canada', 'province': 'quebec'},
    }
    cfg.update(over)
    return cfg


def test_incentive_changes_the_objective():
    """A declared acquisition must MOVE the number, by exactly the two programs'
    amounts. Asserting the key exists on the config would not have caught
    ``decisions.income[]``, which reached the config and stopped there."""
    from optimize import compute_net_benefit

    results = _minimal_results()
    without = compute_net_benefit(results, _base_cfg())
    with_ev = compute_net_benefit(
        results, _base_cfg(zev_purchases=[OPEN_WINDOW_BEV]))

    # 2024-06-01 in Quebec: $5,000 federal iZEV + $7,000 Roulez vert.
    assert with_ev - without == pytest.approx(12000.0, abs=0.01)


def test_non_quebec_household_gets_only_the_federal_incentive():
    """The two programs are independent and the provincial one is gated on
    jurisdiction, not assumed."""
    from optimize import compute_net_benefit

    results = _minimal_results()
    cfg = _base_cfg(zev_purchases=[OPEN_WINDOW_BEV])
    cfg['tax'] = {'country': 'canada', 'province': 'ontario'}
    without = compute_net_benefit(results, _base_cfg())
    cfg_on = compute_net_benefit(results, cfg)
    assert cfg_on - without == pytest.approx(5000.0, abs=0.01)


def test_closed_program_year_moves_nothing():
    """DP#28/DP#32: an acquisition after both programs' priced windows adds
    exactly zero -- and the household is told why by the modules' reasons."""
    from optimize import compute_net_benefit

    results = _minimal_results()
    late = dict(OPEN_WINDOW_BEV, acquisition_date="2027-06-01")
    without = compute_net_benefit(results, _base_cfg())
    with_late = compute_net_benefit(results, _base_cfg(zev_purchases=[late]))
    assert with_late == pytest.approx(without, abs=0.01)


def test_absent_block_is_byte_identical():
    """The golden household declares no vehicle: its number must not move."""
    from optimize import compute_net_benefit

    results = _minimal_results()
    assert compute_net_benefit(results, _base_cfg()) == compute_net_benefit(
        results, _base_cfg(zev_purchases=[]))


def test_two_acquisitions_both_count():
    """Two vehicles are two acquisitions, each priced on its own date. The
    'returning the first match' trap would silently drop the second."""
    from optimize import compute_net_benefit

    results = _minimal_results()
    second = dict(OPEN_WINDOW_BEV, id="ev_b")
    without = compute_net_benefit(results, _base_cfg())
    with_two = compute_net_benefit(
        results, _base_cfg(zev_purchases=[OPEN_WINDOW_BEV, second]))
    assert with_two - without == pytest.approx(24000.0, abs=0.01)
