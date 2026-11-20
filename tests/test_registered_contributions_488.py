"""Issue #488: a dated one-off registered contribution, its deduction election,
and the best deduction schedule.

Engine behaviour is asserted by driving ``FamilySimulation.run()`` on a
synthetic two-adult contract (the in-repo ``single-run-two-adult`` example,
fabricated round numbers) with and without the declared contribution -- never
by building engine state by hand (DP#11). The schedule solver is a pure
function and is unit-tested directly on hand-checkable brackets.
"""

import copy
import json
import os

import pytest

from contract_errors import ContractAdaptationError, ContractValidationError
from contract_schema import validate_contract
from input_contract import to_internal_config
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter

from declared_contributions import (
    check_declared_contributions_runnable, declared_for_year)
from deduction_schedule import (
    best_deduction_schedule, claim_as_available, render_deduction_schedule,
    summarize_deduction_schedule)
from rrsp_ledger import RRSPListLedger

_BASE = os.path.join(os.path.dirname(__file__), '..', 'examples', 'lifedraft',
                     'single-run-two-adult', 'input.json')

# Two-bracket toy schedule: 0% to 10k, 20% to 50k, 40% above.
BR = [{'min': 0, 'max': 10000, 'rate': 0.0},
      {'min': 10000, 'max': 50000, 'rate': 0.20},
      {'min': 50000, 'max': 0, 'rate': 0.40}]


def _base_doc():
    with open(_BASE) as f:
        return json.load(f)


def _doc(*contributions, mutate=None):
    d = _base_doc()
    if contributions:
        d['registered_contributions'] = [dict(c) for c in contributions]
    if mutate:
        mutate(d)
    return d


def _contrib(**over):
    c = {'id': 'lump', 'contributor': 'p1', 'account': 'p1_rrsp',
         'date': '2027-03-01', 'amount': 10000,
         'deduction_timing': 'contribution_year'}
    c.update(over)
    return {k: v for k, v in c.items() if v is not None}  # None = omit the key


def _map(doc):
    validate_contract(doc)
    return to_internal_config(doc)


def _config(doc):
    return SimulationConfig.from_dict(_map(doc))


def _run(doc):
    cfg = _config(doc)
    return cfg, FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()


@pytest.fixture(scope='module')
def baseline():
    return _run(_base_doc())[1]


# ── Contract adapter ────────────────────────────────────────────────────────

class TestAdapter:
    def test_absent_block_adds_no_key_and_roundtrips(self):
        legacy = _map(_base_doc())
        assert 'registered_contributions' not in legacy
        cfg = SimulationConfig.from_dict(legacy)
        assert cfg.registered_contributions == []
        assert 'registered_contributions' not in cfg.to_dict()

    def test_maps_each_bookable_kind_to_its_slot_and_role(self):
        legacy = _map(_doc(
            _contrib(id='own'),
            _contrib(id='spousal', account='spousal_rrsp_p2',
                     deduction_timing='deferred', deduct_from='2031-06-01'),
            _contrib(id='sp_own', contributor='p2', account='p2_rrsp'),
            _contrib(id='tfsa', account='p1_tfsa', amount=5000,
                     deduction_timing=None)))
        got = {e['id']: e for e in legacy['registered_contributions']}
        assert got['own'] == {'id': 'own', 'year': 2027, 'plan': 'rrsp',
                              'role': 'primary', 'amount': 10000.0,
                              'deduct_from_year': 2027}
        assert (got['spousal']['role'], got['spousal']['deduct_from_year']) == ('spousal', 2031)
        assert got['sp_own']['role'] == 'spouse'
        assert (got['tfsa']['plan'], got['tfsa']['deduct_from_year']) == ('tfsa', None)
        cfg = SimulationConfig.from_dict(legacy)
        assert SimulationConfig.from_dict(cfg.to_dict()).registered_contributions == \
            cfg.registered_contributions

    @pytest.mark.parametrize('over, match', [
        ({'account': 'p2_fhsa', 'contributor': 'p2'}, 'no FHSA deduction'),
        ({'account': 'family_resp'}, 'RESP'),
        ({'account': 'joint_nonreg'}, "only rrsp, spousal_rrsp and tfsa"),
        ({'account': 'nope'}, 'not a declared account'),
        ({'contributor': 'p2'}, 'paid by its owner'),
        ({'account': 'spousal_rrsp_p2', 'contributor': 'p2'}, 'paid by the primary'),
        ({'date': '2026-01-01'}, 'before as_of'),
        ({'date': '2090-01-01'}, 'after the horizon'),
        ({'deduction_timing': None}, 'must declare deduction_timing'),
        ({'account': 'p1_tfsa'}, 'not deductible'),
        ({'deduction_timing': 'deferred', 'deduct_from': '2027-12-31'}, 'later tax year'),
    ])
    def test_refusals(self, over, match):
        c = _contrib(**over)
        with pytest.raises(ContractAdaptationError, match=match):
            _map(_doc(c))

    def test_duplicate_id_refused(self):
        with pytest.raises(ContractAdaptationError, match='more than once'):
            _map(_doc(_contrib(), _contrib()))

    def test_deferred_without_deduct_from_is_a_schema_error(self):
        with pytest.raises(ContractValidationError):
            _map(_doc(_contrib(deduction_timing='deferred')))

    def test_deduct_from_without_deferral_is_a_schema_error(self):
        with pytest.raises(ContractValidationError):
            _map(_doc(_contrib(deduct_from='2030-01-01')))


# ── The fold ────────────────────────────────────────────────────────────────

class TestEngine:
    def test_contribution_lands_and_is_deducted_in_its_year(self, baseline):
        _, r = _run(_doc(_contrib()))
        # 2027 is projection year 1.
        assert r[0].rrsp_tax_savings == pytest.approx(baseline[0].rrsp_tax_savings)
        assert r[1].rrsp_tax_savings > baseline[1].rrsp_tax_savings + 1000
        assert r[1].primary_rrsp > baseline[1].primary_rrsp + 9000
        assert r[1].contributions['primary_rrsp'] == pytest.approx(
            baseline[1].contributions['primary_rrsp'] + 10000)

    def test_deferred_deduction_is_carried_then_applied(self, baseline):
        """The contribution is booked in 2027, its deduction is NOT claimed in
        2027-2029, and it IS claimed in 2030, the declared deduct_from year."""
        _, r = _run(_doc(_contrib(deduction_timing='deferred',
                                  deduct_from='2030-01-01')))
        assert r[1].primary_rrsp > baseline[1].primary_rrsp + 9000
        for y in (1, 2, 3):
            assert r[y].rrsp_tax_savings == pytest.approx(baseline[y].rrsp_tax_savings)
            # A chosen deferral is not "carried forward by the cap".
            assert r[y].rrsp_deduction_carried_forward == pytest.approx(
                baseline[y].rrsp_deduction_carried_forward)
        assert r[4].rrsp_tax_savings > baseline[4].rrsp_tax_savings + 1000
        claimed_2030 = sum(c['amount'] for c in r[4].deduction_claims if c['year'] == 1)
        assert claimed_2030 == pytest.approx(10000)

    def test_spouse_own_and_spousal_contributions_land_in_their_pots(self, baseline):
        _, r = _run(_doc(_contrib(id='a', contributor='p2', account='p2_rrsp'),
                         _contrib(id='b', account='spousal_rrsp_p2', amount=5000)))
        assert r[1].contributions['spouse_rrsp'] == pytest.approx(
            baseline[1].contributions['spouse_rrsp'] + 10000)
        assert r[1].contributions['spousal_rrsp'] == pytest.approx(
            baseline[1].contributions['spousal_rrsp'] + 5000)

    def test_tfsa_contribution_lands_without_deduction(self, baseline):
        c = _contrib(account='p1_tfsa', amount=5000, deduction_timing=None)
        _, r = _run(_doc(c))
        assert r[1].contributions['primary_tfsa'] == pytest.approx(
            baseline[1].contributions['primary_tfsa'] + 5000)
        assert r[1].rrsp_tax_savings == pytest.approx(baseline[1].rrsp_tax_savings)

    @pytest.mark.parametrize('over', [
        {'amount': 500000},
        {'account': 'p1_tfsa', 'amount': 500000, 'deduction_timing': None},
    ])
    def test_over_room_is_refused_not_clamped(self, over):
        c = _contrib(**over)
        with pytest.raises(ValueError, match='exceeds the'):
            _run(_doc(c))

    def test_monthly_time_step_refuses(self):
        cfg = _config(_doc(_contrib()))
        cfg.time_step = 'monthly'
        with pytest.raises(ValueError, match='monthly'):
            FamilySimulation(cfg, adapter=CanadaAdapter(cfg)).run()

    def test_outside_projection_refuses(self):
        entries = [{'id': 'x', 'year': 2040, 'plan': 'rrsp', 'role': 'primary',
                    'amount': 1.0, 'deduct_from_year': 2040}]
        with pytest.raises(ValueError, match='outside the projection'):
            check_declared_contributions_runnable(entries, 2026, 5, 'annual')
        check_declared_contributions_runnable(entries, 2026, 20, 'annual')
        check_declared_contributions_runnable([], 2026, 1, 'monthly')

    def test_income_path_recorded_only_when_declared(self, baseline):
        assert all(r.rrsp_deduction_base is None for r in baseline)
        _, r = _run(_doc(_contrib()))
        assert all(set(y.rrsp_deduction_base) == {'primary', 'spouse', 'brackets'}
                   for y in r)
        assert r[0].rrsp_deduction_base['primary'] > 0


def test_declared_for_year_splits_slots_and_deferrals():
    entries = [
        {'id': 'a', 'year': 2027, 'plan': 'rrsp', 'role': 'spousal',
         'amount': 100.0, 'deduct_from_year': 2029},
        {'id': 'b', 'year': 2027, 'plan': 'tfsa', 'role': 'spouse',
         'amount': 50.0, 'deduct_from_year': None},
        {'id': 'c', 'year': 2028, 'plan': 'rrsp', 'role': 'primary',
         'amount': 7.0, 'deduct_from_year': 2028},
    ]
    amounts, deferred = declared_for_year(entries, 2027, 2026)
    assert amounts['spousal_rrsp'] == 100.0 and amounts['spouse_tfsa'] == 50.0
    assert amounts['primary_rrsp'] == 0.0
    assert deferred == [{'role': 'spousal', 'amount': 100.0, 'claimable_from': 3}]
    amounts, deferred = declared_for_year(entries, 2028, 2026)
    assert amounts['primary_rrsp'] == 7.0 and deferred == []


# ── The ledger honours the deferral ─────────────────────────────────────────

def test_ledger_skips_entry_until_claimable_and_split_keeps_the_date():
    ledger = RRSPListLedger()
    ledger.add_contribution(year=0, amount=1000, role='primary', claimable_from=2)
    out = ledger.claim_useful_deductions(year=1, income=60000, brackets=BR,
                                         roles=('primary',))
    assert out['amount'] == 0 and out['carried_forward'] == 0
    assert ledger.undeducted_total() == 1000
    # Year 2: only 400 of headroom above the 10k floor -> partial claim.
    out = ledger.claim_useful_deductions(year=2, income=10400, brackets=BR,
                                         roles=('primary',))
    assert out['amount'] == pytest.approx(400)
    assert out['carried_forward'] == pytest.approx(600)
    rest = [e for e in ledger if not e['deducted']]
    assert rest[0]['claimable_from'] == 2


# ── The schedule solver ─────────────────────────────────────────────────────

def _path(*incomes):
    return [{'income': i, 'brackets': BR} for i in incomes]


class TestSolver:
    def test_puts_the_deduction_in_the_highest_bracket_years(self):
        # Year 0 sits at 30k (20%), year 2 at 60k (10k of 40% band).
        best = best_deduction_schedule([{'amount': 15000, 'available_from': 0}],
                                       _path(30000, 30000, 60000), 0)
        assert [(c['index'], round(c['amount'])) for c in best['claims']] == \
            [(0, 5000), (2, 10000)]
        assert best['tax_saved'] == pytest.approx(10000 * 0.4 + 5000 * 0.2)
        asap = claim_as_available([{'amount': 15000, 'available_from': 0}],
                                  _path(30000, 30000, 60000), 0)
        assert asap['tax_saved'] == pytest.approx(15000 * 0.2)

    def test_never_claims_before_the_contribution_exists(self):
        best = best_deduction_schedule([{'amount': 5000, 'available_from': 1}],
                                       _path(90000, 30000), 0)
        assert [c['index'] for c in best['claims']] == [1]

    def test_bracket_target_is_a_floor_and_the_rest_stays_carried(self):
        best = best_deduction_schedule([{'amount': 20000, 'available_from': 0}],
                                       _path(60000), 50000)
        assert best['claimed'] == pytest.approx(10000)
        assert best['unclaimed'] == pytest.approx(10000)

    def test_rejects_an_empty_path_and_an_out_of_range_release(self):
        with pytest.raises(ValueError):
            best_deduction_schedule([], [], 0)
        with pytest.raises(ValueError):
            best_deduction_schedule([{'amount': 1, 'available_from': 3}],
                                    _path(1), 0)


class TestScheduleReport:
    def test_absent_when_nothing_declared(self, baseline):
        cfg = _config(_base_doc())
        assert summarize_deduction_schedule(cfg, baseline) is None
        assert render_deduction_schedule(None) == []

    def test_names_years_and_amounts_and_includes_opening_position(self):
        def opening(d):
            for p in d['people']:
                if p['id'] == 'p1':
                    p['room']['rrsp']['undeducted_contributions'] = 2000
        cfg, r = _run(_doc(_contrib(deduction_timing='deferred',
                                    deduct_from='2030-01-01'), mutate=opening))
        s = summarize_deduction_schedule(cfg, r)
        assert set(s) == {'primary'}
        p = s['primary']
        assert p['opening_undeducted'] == 2000
        best = p['best']
        assert best['claimed'] + best['unclaimed'] == pytest.approx(12000)
        assert best['tax_saved'] >= p['as_declared']['tax_saved'] - 1e-6
        assert best['tax_saved'] >= p['claim_as_available']['tax_saved'] - 1e-6
        assert all(c['year'] >= 2026 for c in best['claims'])
        text = '\n'.join(render_deduction_schedule(s))
        first = best['claims'][0]
        assert f"{first['year']}: deduct ${first['amount']:,.0f}" in text
        assert 'deduction deferred to 2030' in text
        assert 'opening undeducted position: $2,000' in text

    def test_markdown_and_cli_surfaces_print_the_schedule(self, capsys):
        from output_plugins import (
            MarkdownReport, _print_deduction_schedule_report,
            _deduction_schedule_lines)
        cfg, r = _run(_doc(_contrib()))
        row = {'label': 'toy', 'net_benefit': 1.0,
               'deduction_schedule': summarize_deduction_schedule(cfg, r)}
        lines = _deduction_schedule_lines([row])
        assert lines[1] == '  (#1 scenario: toy)'
        assert _deduction_schedule_lines([{'label': 'x', 'net_benefit': 1.0}]) == []
        _print_deduction_schedule_report([row])
        assert 'BEST RRSP DEDUCTION SCHEDULE' in capsys.readouterr().out
        md = MarkdownReport([row], _map(_doc(_contrib())), title='t').render()
        assert '## Best RRSP Deduction Schedule' in md


def test_optimizer_row_carries_the_schedule_only_when_declared():
    """The ranking row (what optimize.py prints and reports) carries
    ``deduction_schedule`` for a declared contribution and no key otherwise."""
    from optimize import evaluate_strategy_with_simulation
    from countries.canada.rate_model import build_rate_path
    from strategy import AllocationStrategy
    strategy = AllocationStrategy(name='t', rrsp_pct=0.5, tfsa_pct=0.5)
    rate = build_rate_path(name='t', initial_rate=0.05, term_years=5,
                           rate_type='variable', renewal_rates=[0.05])
    row = evaluate_strategy_with_simulation(
        name='t', strategy=strategy, config=_config(_doc(_contrib())),
        rate_path=rate, include_year_by_year=False)
    assert set(row['deduction_schedule']) == {'primary'}
    row = evaluate_strategy_with_simulation(
        name='t', strategy=strategy, config=_config(_base_doc()),
        rate_path=rate, include_year_by_year=False)
    assert 'deduction_schedule' not in row


class TestAdapterDefences:
    """The adapter is what the engine trusts, so it re-checks what the schema
    also enforces -- a config assembled in code skips the schema (DP#32).
    Called directly on a minimal document, bypassing validation."""

    def _doc(self, **over):
        c = {'id': 'x', 'contributor': 'p1', 'account': 'a',
             'date': '2027-01-01', 'amount': 1,
             'deduction_timing': 'contribution_year'}
        c.update(over)
        return {'as_of': '2026-07-01', 'registered_contributions': [c],
                'accounts': [{'id': 'a', 'kind': 'rrsp', 'owner': c['contributor']}]}

    @pytest.mark.parametrize('over, match', [
        ({'contributor': 'gx'}, 'neither the primary nor the spouse'),
        ({'deduct_from': '2030-01-01'}, 'only meaningful with'),
        ({'deduction_timing': 'deferred'}, 'requires deduct_from'),
    ])
    def test_refusals(self, over, match):
        from contract_contributions import map_registered_contributions
        with pytest.raises(ContractAdaptationError, match=match):
            map_registered_contributions(self._doc(**over), 'p1', 'p2', None)


def test_render_names_what_stays_carried_at_the_horizon():
    s = {'primary': {
        'contributions': [{'id': 'x', 'year': 2027, 'amount': 20000.0,
                           'deduct_from_year': 2027}],
        'opening_undeducted': 0.0, 'floor': 'deduct_later_bracket_target',
        'bracket_target': 50000.0,
        'best': {'claims': [{'year': 2027, 'amount': 10000.0, 'tax_saved': 4000.0}],
                 'tax_saved': 4000.0, 'claimed': 10000.0, 'unclaimed': 10000.0},
        'as_declared': {'tax_saved': 4000.0},
        'claim_as_available': {'tax_saved': 4000.0},
        'savings_flow_rrsp': 0.0}}
    text = '\n'.join(render_deduction_schedule(s))
    assert 'still carried at the horizon: $10,000' in text
    assert 'never deduct below $50,000 (deduct_later_bracket_target)' in text
    assert 'savings flow' not in text
