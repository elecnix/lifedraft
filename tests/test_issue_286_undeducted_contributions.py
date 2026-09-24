#!/usr/bin/env python3
"""Issue #286 -- RRSP contributions already made but not yet deducted.

The Notice of Assessment's "unused RRSP contributions available to deduct"
had nowhere to go: a household that contributed last year and has not yet
deducted started the projection with an EMPTY deduction ledger. The contract
now carries ``people[].room.rrsp.undeducted_contributions`` (the planner's
correction: room is a PERSON-level grant in the Canada overlay, there is no
``accounts[].room``), and ``SimState.initial`` seeds the ledger from it.

What is pinned here:
  * the leaf reaches the ENGINE, not just the adapter (a contract through
    ``load_and_map`` -> ``FamilySimulation.run`` claims it in year 0);
  * absence stays absence (no key, a disclosed caveat), a declared 0 is a
    value (no entry, no caveat);
  * the leaf is RRSP-scoped in the schema, and a person the ledger cannot
    hold (a child, an additional adult) is refused, never silently dropped.

The example contract is the shipped synthetic one; figures are fabricated
round numbers and ids are role-based (DP#4/DP#15).
"""

import copy
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import countries.canada  # noqa: F401  (register the jurisdiction adapter)
import contract_schema
import input_contract
from contract_errors import ContractAdaptationError, ContractValidationError
from model_fidelity import FidelityContext, active_approximations
from simulation_config import SimulationConfig
from simulation_state import SimState
from tax_calculator import deduction_value
from tax_data import default_tax_provider

B = default_tax_provider().get_combined_brackets(2026, province="quebec")
CAVEAT = 'rrsp_undeducted_contributions_undeclared'


def _doc():
    """The example trimmed to the primary couple + children. Its private
    loans are removed so the primary's taxable income is exactly their
    employment income (the taxable-base cap with an interest deduction is
    pinned in test_issue_286_rrsp_deduction_cap)."""
    from test_input_contract import _two_generation_subset
    with open(contract_schema.EXAMPLE_PATH) as f:
        doc = _two_generation_subset(json.load(f))
    doc["private_loans"] = []
    return doc


def _with_undeducted(doc, person_id, amount):
    doc = copy.deepcopy(doc)
    for p in doc["people"]:
        if p["id"] == person_id:
            p["room"]["rrsp"]["undeducted_contributions"] = amount
    return doc


def _map(doc, tmp_path):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(doc))
    return input_contract.load_and_map(str(path))


def _member(cfg, role):
    return next(m for m in cfg["family"]["members"] if m["role"] == role)


def _year0_without_new_contributions(cfg_dict):
    """Run the real fold, deduct-now (the example also declares a deduct-
    later bracket target), with the strategy contributing NOTHING, so any
    year-0 RRSP refund can only come from the seeded ledger."""
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation
    from strategy import AllocationResult

    cfg = SimulationConfig.from_dict(cfg_dict)
    with mock.patch('strategy.StrategyEngine.allocate',
                    return_value=AllocationResult()):
        results = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                                   deduct_later=False).run()
    return results[0]


def test_declared_undeducted_reaches_engine(tmp_path):
    cfg = _map(_with_undeducted(_doc(), "p1", 30_000), tmp_path)
    assert _member(cfg, "primary")["rrsp_undeducted_contributions"] == 30_000
    r0 = _year0_without_new_contributions(cfg)
    assert r0.rrsp_tax_savings > 0
    # The seeded $30k is claimed against the primary's own year-0 income
    # (no rental/loan left in the trimmed example, so taxable == employment
    # income), bracket-fill.
    assert abs(r0.rrsp_tax_savings
               - deduction_value(r0.primary_income, 30_000, B)) < 1e-6
    assert r0.rrsp_deduction_carried_forward == 0.0


def test_absent_is_disclosed_not_zeroed(tmp_path):
    cfg = _map(_doc(), tmp_path)
    primary = _member(cfg, "primary")
    assert "rrsp_undeducted_contributions" not in primary
    assert SimState.initial(SimulationConfig.from_dict(cfg)) \
        .jurisdiction_state['canada']['rrsp_ledger'] == []
    assert _year0_without_new_contributions(cfg).rrsp_tax_savings == 0.0
    by_id = {a.id: a for a in active_approximations(cfg)}
    assert CAVEAT in by_id
    findings = ' '.join(by_id[CAVEAT].findings(FidelityContext(cfg=cfg)))
    assert 'primary' in findings and 'Notice of Assessment' in findings


def test_declared_zero_is_a_value(tmp_path):
    doc = _with_undeducted(_with_undeducted(_doc(), "p1", 0), "p2", 0)
    cfg = _map(doc, tmp_path)
    assert _member(cfg, "primary")["rrsp_undeducted_contributions"] == 0
    assert SimState.initial(SimulationConfig.from_dict(cfg)) \
        .jurisdiction_state['canada']['rrsp_ledger'] == []
    assert CAVEAT not in {a.id for a in active_approximations(cfg)}


def test_declared_for_one_person_still_names_the_other(tmp_path):
    cfg = _map(_with_undeducted(_doc(), "p1", 0), tmp_path)
    by_id = {a.id: a for a in active_approximations(cfg)}
    findings = ' '.join(by_id[CAVEAT].findings(FidelityContext(cfg=cfg)))
    assert findings.startswith('spouse:') and 'primary:' not in findings


def test_spouse_seed_is_the_spouses_own_deduction(tmp_path):
    cfg = _map(_with_undeducted(_doc(), "p2", 10_000), tmp_path)
    ledger = SimState.initial(SimulationConfig.from_dict(cfg)) \
        .jurisdiction_state['canada']['rrsp_ledger']
    assert [(e['role'], e['amount'], e['year'], e['deducted'])
            for e in ledger] == [('spouse', 10_000, -1, False)]
    r0 = _year0_without_new_contributions(cfg)
    assert abs(r0.rrsp_tax_savings
               - deduction_value(r0.spouse_income, 10_000, B)) < 1e-6


def test_tfsa_room_rejects_leaf():
    """The leaf is RRSP-scoped: accepted on the rrsp grant, refused on the
    tfsa grant (the shared ``room`` def stays closed)."""
    contract_schema.validate_contract(_with_undeducted(_doc(), "p1", 1_000))
    doc = _doc()
    doc["people"][0]["room"]["tfsa"]["undeducted_contributions"] = 1_000
    with pytest.raises(ContractValidationError, match="undeducted_contributions"):
        contract_schema.validate_contract(doc)


def test_rrsp_grant_still_requires_room_and_date():
    doc = _with_undeducted(_doc(), "p1", 1_000)
    contract_schema.validate_contract(doc)
    del doc["people"][0]["room"]["rrsp"]["contribution_room"]
    with pytest.raises(ContractValidationError, match="people/0/room/rrsp"):
        contract_schema.validate_contract(doc)


def test_child_leaf_refused(tmp_path):
    doc = _doc()
    for p in doc["people"]:
        if p["id"] == "ca":
            p["room"]["rrsp"] = {"contribution_room": 0, "as_of": "2026-01-01",
                                 "undeducted_contributions": 1_000}
    with pytest.raises(ContractAdaptationError, match="undeducted_contributions"):
        _map(doc, tmp_path)


def test_extra_adult_leaf_refused_by_the_engine():
    """An internal config reaching SimState.initial with an additional
    adult's undeducted contributions (no ledger slot) raises, never drops."""
    cfg = SimulationConfig(
        projection_years=1, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=0.0, start_year=2026, province='quebec',
        investment_return=0.0, salary_growth=0.0, children=[],
        family_members=[
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 100_000},
            {'role': 'spouse', 'birth_year': 1982, 'gross_income': 50_000},
            {'role': 'x1', 'id': 'x1', 'birth_year': 2000,
             'gross_income': 40_000, 'rrsp_undeducted_contributions': 5_000},
        ])
    with pytest.raises(ValueError, match="rrsp_undeducted_contributions"):
        SimState.initial(cfg)


def _full_run(cfg_dict):
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation

    cfg = SimulationConfig.from_dict(cfg_dict)
    return FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                            deduct_later=False).run()


def test_seeding_moves_no_money(tmp_path):
    """INV-14: undeducted contributions are ALREADY inside the declared RRSP
    balance (and already netted out of the CRA room figure). Seeding the
    ledger may only add deduction entries -- it must not add to any balance,
    reduce any room, or otherwise move money. Declared vs absent, everything
    else equal: every year's balances and debt are identical; only the
    reported refund differs."""
    (tmp_path / "absent").mkdir()
    (tmp_path / "declared").mkdir()
    absent_cfg = _map(_doc(), tmp_path / "absent")
    declared_cfg = _map(_with_undeducted(_doc(), "p1", 30_000),
                        tmp_path / "declared")

    open_absent = SimState.initial(SimulationConfig.from_dict(absent_cfg))
    open_declared = SimState.initial(SimulationConfig.from_dict(declared_cfg))
    ca_absent = open_absent.jurisdiction_state['canada']
    ca_declared = open_declared.jurisdiction_state['canada']
    assert ca_declared['adult_rrsp'] == ca_absent['adult_rrsp']
    assert [e['amount'] for e in ca_declared['rrsp_ledger']] == [30_000]

    absent = _full_run(absent_cfg)
    declared = _full_run(declared_cfg)
    assert len(absent) == len(declared) > 1
    for a, d in zip(absent, declared):
        assert d.total_assets == a.total_assets, a.year
        assert d.total_rrsp == a.total_rrsp, a.year
        assert d.primary_rrsp == a.primary_rrsp, a.year
        assert d.spouse_rrsp == a.spouse_rrsp, a.year
        assert d.total_debt == a.total_debt, a.year
    # ... while the seed was genuinely claimed (otherwise the equality above
    # would be vacuous).
    assert (sum(r.rrsp_tax_savings for r in declared)
            > sum(r.rrsp_tax_savings for r in absent))
