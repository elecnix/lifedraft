"""Issue #700/#643 (Step 3): TFSA is a per-adult store, not two hardcoded pots.

`jurisdiction_state['canada']` used to carry `tfsa_primary_balance` /
`tfsa_spouse_balance` (and their room keys) as flat scalars -- a shape that
cannot hold a third adult's TFSA. It is now
`adult_tfsa = {adult_id: {'balance', 'room'}}`, keyed by the stable entity id
from #699, in canonical adult order (primary first). Same seam pattern as the
RRSP store (Step 2). For two adults it holds exactly the same money, so this is
a pure representation change (the golden invariant guards behaviour).
"""
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import (
    adult_tfsa_slot, adult_tfsa_total, rebuild_adult_tfsa,
    _default_canada_state, _host_account_balance, _carve_from_canada,
)


def _base(members, *, investment_return=0.0, projection_years=1):
    return {
        'family': {'members': members},
        'assumptions': {'start_year': 2026, 'projection_years': projection_years,
                        'investment_return': investment_return, 'salary_growth': 0.0},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'margin_available': 0, 'heloc_readvance': False},
        'savings': {'rate': 0.0},
        'tax': {'province': 'qc', 'year': 2026},
    }


def _canada_state(cfg):
    sim_cfg = SimulationConfig.from_dict(cfg)
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    return sim._state.jurisdiction_state['canada']


def _two_adults():
    return _canada_state(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'tfsa_balance': 40_000, 'tfsa_room_accumulated': 20_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
         'tfsa_balance': 60_000, 'tfsa_room_accumulated': 30_000},
    ]))


def test_store_is_keyed_by_entity_id_in_adult_order():
    assert list(_two_adults()['adult_tfsa'].keys()) == ['primary', 'spouse']


def test_total_equals_the_legacy_two_pot_sum():
    assert adult_tfsa_total(_two_adults()) == 40_000 + 60_000


def test_slot_zero_and_one_carry_the_two_adults_balance_and_room():
    st = _two_adults()
    assert adult_tfsa_slot(st, 0) == (40_000, 20_000)
    assert adult_tfsa_slot(st, 1) == (60_000, 30_000)


def test_absent_slot_reads_as_zeros_for_a_single_adult_household():
    st = _canada_state(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'tfsa_balance': 25_000}]))
    assert list(st['adult_tfsa'].keys()) == ['primary']
    assert adult_tfsa_slot(st, 1) == (0.0, 0.0)


def test_fold_preserves_the_store_shape_and_compounds_each_entry():
    sim_cfg = SimulationConfig.from_dict(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0, 'tfsa_balance': 40_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0, 'tfsa_balance': 60_000},
    ], investment_return=0.10))
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    sim.run()
    st = sim._state.jurisdiction_state['canada']
    assert list(st['adult_tfsa'].keys()) == ['primary', 'spouse']
    assert st['adult_tfsa']['primary']['balance'] == 40_000 * 1.10
    assert st['adult_tfsa']['spouse']['balance'] == 60_000 * 1.10


def test_emergency_reserve_held_in_tfsa_reads_and_carves_the_store():
    """A reserve held in a TFSA is carved from the per-adult store's slot 0
    (primary) / slot 1 (spouse), not the removed flat keys (#688 regression
    guard, mirroring the RRSP fix)."""
    canada = _default_canada_state()
    canada['adult_tfsa'] = {
        'primary': {'balance': 40_000, 'room': 0.0},
        'spouse': {'balance': 60_000, 'room': 0.0},
    }
    assert _host_account_balance('tfsa', canada, non_reg_balance=0) == 40_000
    assert _host_account_balance('tfsa_spouse', canada, non_reg_balance=0) == 60_000
    _carve_from_canada('tfsa', canada, 15_000)
    assert canada['adult_tfsa']['primary']['balance'] == 25_000
    _carve_from_canada('tfsa_spouse', canada, 10_000)
    assert canada['adult_tfsa']['spouse']['balance'] == 50_000


def test_rebuild_carries_a_third_adult_forward_as_a_fresh_copy():
    """The two-slot WorkingState compute only drives adults 0 and 1; any further
    adult in the store (admitted only at Step 8/#698) must be carried forward
    unchanged by ``rebuild_adult_tfsa`` -- and as a FRESH dict, never the prior
    entry mutated in place (the writeback contract). Exercises the else branch
    that no two-adult household reaches."""
    third = {'balance': 42_000.0, 'room': 7_000.0}
    prior = {
        'primary': {'balance': 40_000.0, 'room': 20_000.0},
        'spouse': {'balance': 60_000.0, 'room': 30_000.0},
        'child_a': third,
    }
    new_store = rebuild_adult_tfsa(
        prior, primary_balance=44_000.0, primary_room=21_000.0,
        spouse_balance=66_000.0, spouse_room=31_000.0)
    # slots 0/1 take the fresh WorkingState scalars; the third adult is carried
    # forward with the same money but as a new object (not aliased to `third`).
    assert new_store['primary'] == {'balance': 44_000.0, 'room': 21_000.0}
    assert new_store['spouse'] == {'balance': 66_000.0, 'room': 31_000.0}
    assert new_store['child_a'] == third
    assert new_store['child_a'] is not third


# NOTE (#700 Step 4): the 'fhsa' reserve-carve moved from the removed flat
# `fhsa_balance` key to the per-adult FHSA store's slot 0. Its regression guard
# now lives in test_adult_fhsa_lira_lif_store_700.py
# (test_emergency_reserve_held_in_fhsa_reads_and_carves_the_store); the old
# flat-key test was deleted with the key it exercised (DP#9, no back-compat).
