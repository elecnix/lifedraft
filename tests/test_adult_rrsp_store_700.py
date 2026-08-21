"""Issue #700/#643 (Step 2): RRSP is a per-adult store, not three hardcoded pots.

`jurisdiction_state['canada']` used to carry `rrsp_balance` (primary's own),
`spouse_rrsp_balance` (spouse's own) and `spousal_rrsp_balance` (the spousal RRSP
the primary contributes and the spouse annuits) as three flat scalars -- a shape
that cannot hold a third adult's RRSP without inventing a fourth key. It is now
`adult_rrsp = {adult_id: {'own', 'own_room', 'spousal_as_annuitant'}}`, keyed by
the stable entity id from #699, in canonical adult order (primary first).

For a two-adult household this holds exactly the same money, so the change is a
pure representation change (the golden invariant guards the behaviour half; these
tests guard the representation half): the store's total equals the old
three-pot sum, and slot 0 / slot 1 reproduce the old primary / spouse scalars.
"""
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import (
    adult_rrsp_slot, adult_rrsp_total, rebuild_adult_rrsp,
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
         'rrsp_balance': 500_000, 'rrsp_room_accumulated': 40_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
         'rrsp_balance': 100_000, 'rrsp_room_accumulated': 25_000,
         'spousal_rrsp_balance': 50_000},
    ]))


def test_store_is_keyed_by_entity_id_in_adult_order():
    st = _two_adults()
    assert list(st['adult_rrsp'].keys()) == ['primary', 'spouse']


def test_total_equals_the_legacy_three_pot_sum():
    st = _two_adults()
    # legacy: rrsp_balance + spouse_rrsp_balance + spousal_rrsp_balance
    assert adult_rrsp_total(st) == 500_000 + 100_000 + 50_000


def test_slot_zero_is_the_primaria_own_rrsp_and_room():
    own, room, spousal = adult_rrsp_slot(_two_adults(), 0)
    assert (own, room, spousal) == (500_000, 40_000, 0.0)


def test_slot_one_is_the_spouse_own_rrsp_room_and_annuited_spousal():
    own, room, spousal = adult_rrsp_slot(_two_adults(), 1)
    # the spousal RRSP (primary-contributed) is annuited by the SPOUSE
    assert (own, room, spousal) == (100_000, 25_000, 50_000)


def test_absent_slot_reads_as_zeros_for_a_single_adult_household():
    st = _canada_state(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'rrsp_balance': 300_000},
    ]))
    assert list(st['adult_rrsp'].keys()) == ['primary']
    assert adult_rrsp_slot(st, 1) == (0.0, 0.0, 0.0)


def test_emergency_reserve_held_in_rrsp_reads_the_per_adult_store():
    """Regression: moving RRSP into the per-adult store left the emergency
    reserve (#688) reading the removed flat 'rrsp_balance' key -> it saw a $0
    host and would silently carve nothing. The host balance for a reserve held
    in an RRSP is the primary adult's own RRSP."""
    canada = _default_canada_state()
    canada['adult_rrsp'] = {
        'primary': {'own': 500_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}
    assert _host_account_balance('rrsp', canada, non_reg_balance=0) == 500_000


def test_emergency_reserve_carve_reduces_the_primary_rrsp_in_place():
    """The carve must land on the per-adult store, not a dead flat key."""
    canada = _default_canada_state()
    canada['adult_rrsp'] = {
        'primary': {'own': 500_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}
    _carve_from_canada('rrsp', canada, 20_000)
    assert canada['adult_rrsp']['primary']['own'] == 480_000
    # and total_assets sees the carved-down balance, not a phantom flat key
    assert adult_rrsp_total(canada) == 480_000


def test_rebuild_carries_a_third_adult_forward_as_a_fresh_copy():
    """The two-slot WorkingState compute only drives adults 0 and 1; any further
    adult in the store (admitted only at Step 8/#698) must be carried forward
    unchanged by ``rebuild_adult_rrsp`` -- and as a FRESH dict, never the prior
    entry mutated in place (the writeback contract). Exercises the else branch
    that no two-adult household reaches."""
    third = {'own': 42_000.0, 'own_room': 7_000.0, 'spousal_as_annuitant': 0.0}
    prior = {
        'primary': {'own': 500_000.0, 'own_room': 40_000.0, 'spousal_as_annuitant': 0.0},
        'spouse': {'own': 100_000.0, 'own_room': 25_000.0, 'spousal_as_annuitant': 50_000.0},
        'child_a': third,
    }
    new_store = rebuild_adult_rrsp(
        prior, primary_own=550_000.0, primary_room=41_000.0,
        spouse_own=110_000.0, spouse_room=26_000.0, spousal_as_annuitant=55_000.0)
    # slots 0/1 take the fresh WorkingState scalars; the third adult is carried
    # forward with the same money but as a new object (not aliased to `third`).
    assert new_store['primary'] == {'own': 550_000.0, 'own_room': 41_000.0,
                                    'spousal_as_annuitant': 0.0}
    assert new_store['child_a'] == third
    assert new_store['child_a'] is not third


def test_fold_preserves_the_store_shape_and_compounds_each_entry():
    """After a year, the store still has one entry per adult and every pot --
    including the spouse's annuited spousal RRSP -- has grown by the return."""
    sim_cfg = SimulationConfig.from_dict(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'rrsp_balance': 500_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
         'rrsp_balance': 100_000, 'spousal_rrsp_balance': 50_000},
    ], investment_return=0.10, projection_years=1))
    sim = FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                           use_readvanceable=False, deduct_later=False)
    sim.run()
    st = sim._state.jurisdiction_state['canada']
    assert list(st['adult_rrsp'].keys()) == ['primary', 'spouse']
    # 10% growth on every pot; the spousal pot stays on the spouse entry.
    assert st['adult_rrsp']['primary']['own'] == 500_000 * 1.10
    assert st['adult_rrsp']['spouse']['own'] == 100_000 * 1.10
    assert st['adult_rrsp']['spouse']['spousal_as_annuitant'] == 50_000 * 1.10
