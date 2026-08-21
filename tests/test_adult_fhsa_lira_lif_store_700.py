"""Issue #700/#643/#704 (Step 4): FHSA / CRI-LIRA / LIF are per-adult stores,
not singleton household pots.

`jurisdiction_state['canada']` used to carry ONE `fhsa_*` / one `lira_*` / one
`lif_*` set of flat scalars -- a shape that structurally cannot hold a second
adult's FHSA (input_contract even refused a document where both spouses owned
one). They are now::

    adult_fhsa = {adult_id: {'balance','room','lifetime_used','lifetime_limit'}}
    adult_lira = {adult_id: {'balance','birth_year','jurisdiction',
                             'reference_rate','conversion_year'}}
    adult_lif  = {adult_id: {'balance','birth_year','jurisdiction',
                             'reference_rate'}}

keyed by the stable entity id from #699, canonical adult order (primary first).
Same seam pattern as the RRSP (Step 2) / TFSA (Step 3) stores. Slot 0 drives the
still-single-slot compute; a SECOND adult's FHSA (slot 1) COMPOUNDS via the
writeback growth pass (the acceptance point of #704). Per-adult FHSA
contribution/room allocation and LIRA/LIF 2-owner conversion mechanics are the
tracked Step 4 follow-up -- opening balances and growth are proven here.

For a household with <=1 owner of each kind (the golden path) each store reduces
to a single entry, so the golden invariant is byte-identical (guarded
separately by test_golden_trajectory_581).
"""
from countries.canada.adapter import CanadaAdapter
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import (
    adult_fhsa_slot, adult_fhsa_total, adult_fhsa_active,
    adult_lira_total, adult_lif_total, adult_lira_slot, adult_lif_slot,
    rebuild_adult_lira, rebuild_adult_lif,
    _default_canada_state, _host_account_balance, _carve_from_canada,
)


def _base(members, *, investment_return=0.0, projection_years=1, lira=None):
    cfg = {
        'family': {'members': members},
        'assumptions': {'start_year': 2026, 'projection_years': projection_years,
                        'investment_return': investment_return, 'salary_growth': 0.0},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'margin_available': 0, 'heloc_readvance': False},
        'savings': {'rate': 0.0},
        'tax': {'province': 'qc', 'year': 2026},
    }
    if lira is not None:
        cfg['lira'] = lira
    return cfg


def _sim(cfg):
    sim_cfg = SimulationConfig.from_dict(cfg)
    return FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg),
                            use_readvanceable=False, deduct_later=False)


def _canada_state(cfg):
    return _sim(cfg)._state.jurisdiction_state['canada']


def _dual_fhsa():
    return _base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'fhsa_balance': 20_000, 'fhsa_room_accumulated': 8_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
         'fhsa_balance': 30_000, 'fhsa_room_accumulated': 8_000},
    ], investment_return=0.10)


# ── storage shape ──────────────────────────────────────────────────────────

def test_fhsa_store_is_keyed_by_entity_id_in_adult_order():
    assert list(_canada_state(_dual_fhsa())['adult_fhsa'].keys()) == ['primary', 'spouse']


def test_fhsa_total_equals_the_two_owners_sum():
    assert adult_fhsa_total(_canada_state(_dual_fhsa())) == 20_000 + 30_000


def test_fhsa_slot_zero_and_one_carry_each_owner_balance_and_room():
    st = _canada_state(_dual_fhsa())
    assert adult_fhsa_slot(st, 0)['balance'] == 20_000
    assert adult_fhsa_slot(st, 1)['balance'] == 30_000


def test_absent_fhsa_slot_reads_as_an_empty_account():
    st = _canada_state(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'fhsa_balance': 25_000, 'fhsa_room_accumulated': 8_000}]))
    # A single-FHSA household keeps a single slot; slot 1 reads as zeros.
    assert adult_fhsa_slot(st, 1)['balance'] == 0.0
    assert adult_fhsa_active(st) is True


# ── the acceptance point: two FHSAs compound INDEPENDENTLY (#704) ────────────

def test_two_adult_fhsas_compound_independently_over_one_year():
    """A previously-refused two-FHSA household now loads, and each adult's FHSA
    grows in its OWN slot at the investment return -- slot 1 (spouse) via the
    writeback growth pass, slot 0 (primary) via the compute. Different opening
    balances -> different post-year balances (no blending into one pot)."""
    sim = _sim(_dual_fhsa())
    sim.run()
    st = sim._state.jurisdiction_state['canada']
    assert list(st['adult_fhsa'].keys()) == ['primary', 'spouse']
    assert st['adult_fhsa']['primary']['balance'] == 20_000 * 1.10
    assert st['adult_fhsa']['spouse']['balance'] == 30_000 * 1.10


def test_two_adult_fhsas_compound_independently_over_multiple_years():
    sim = _sim(_base([
        {'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
         'fhsa_balance': 20_000, 'fhsa_room_accumulated': 8_000},
        {'role': 'spouse', 'birth_year': 1980, 'gross_income': 0,
         'fhsa_balance': 30_000, 'fhsa_room_accumulated': 8_000},
    ], investment_return=0.10, projection_years=3))
    sim.run()
    st = sim._state.jurisdiction_state['canada']
    # Slot 1 gets growth only (no per-adult contribution this step): a clean
    # 3-year compound. Slot 0 also has no contribution here (savings rate 0),
    # so both are pure geometric growth off their own opening balance.
    assert round(st['adult_fhsa']['spouse']['balance'], 2) == round(30_000 * 1.10 ** 3, 2)
    assert round(st['adult_fhsa']['primary']['balance'], 2) == round(20_000 * 1.10 ** 3, 2)


# ── LIRA / LIF: single primary-keyed slot, isomorphic to the old singleton ──

def test_lira_and_lif_seed_a_single_primary_keyed_slot():
    st = _canada_state(_base(
        [{'role': 'primary', 'birth_year': 1960, 'gross_income': 0},
         {'role': 'spouse', 'birth_year': 1960, 'gross_income': 0}],
        lira={'balance': 100_000, 'birth_year': 1960, 'jurisdiction': 'federal'}))
    assert list(st['adult_lira'].keys()) == ['primary']
    assert adult_lira_total(st) == 100_000
    assert adult_lira_slot(st, 0)['birth_year'] == 1960
    # LIF is empty until the age-71 conversion; its slot still exists.
    assert list(st['adult_lif'].keys()) == ['primary']
    assert adult_lif_total(st) == 0.0
    assert adult_lif_slot(st, 0)['jurisdiction'] == 'federal'


# ── emergency reserve carves the FHSA store (#688 regression guard) ─────────

def test_emergency_reserve_held_in_fhsa_reads_and_carves_the_store():
    canada = _default_canada_state()
    canada['adult_fhsa'] = {
        'primary': {'balance': 20_000, 'room': 0.0,
                    'lifetime_used': 0.0, 'lifetime_limit': 40_000},
    }
    assert _host_account_balance('fhsa', canada, non_reg_balance=0) == 20_000
    _carve_from_canada('fhsa', canada, 5_000)
    assert canada['adult_fhsa']['primary']['balance'] == 15_000


# ── rebuild carries a further adult forward unchanged (2-owner conversion is
# the Step 4 follow-up; slot 0 alone takes the single-slot compute scalars) ──

def test_rebuild_lira_carries_a_third_adult_forward_as_a_fresh_copy():
    """The single-slot conversion compute only drives slot 0; any further adult
    in the LIRA store (admitted only at Step 8/#698) must be carried forward
    UNCHANGED by ``rebuild_adult_lira`` -- and as a FRESH dict, never aliased to
    the prior entry (covers the else branch)."""
    third = {'balance': 88_000.0, 'birth_year': 1965, 'jurisdiction': 'quebec',
             'reference_rate': 0.05, 'conversion_year': 0}
    prior = {
        'primary': {'balance': 100_000.0, 'birth_year': 1960,
                    'jurisdiction': 'federal', 'reference_rate': 0.06,
                    'conversion_year': 0},
        'child_a': third,
    }
    new_store = rebuild_adult_lira(
        prior, balance=104_000.0, birth_year=1960, jurisdiction='federal',
        reference_rate=0.06, conversion_year=2031)
    # slot 0 takes the fresh conversion scalars ...
    assert new_store['primary']['balance'] == 104_000.0
    assert new_store['primary']['conversion_year'] == 2031
    # ... the further adult is carried forward with the same money, fresh object.
    assert new_store['child_a'] == third
    assert new_store['child_a'] is not third


def test_rebuild_lif_carries_a_third_adult_forward_as_a_fresh_copy():
    """LIF analogue of the LIRA carry-forward guard (covers the else branch)."""
    third = {'balance': 70_000.0, 'birth_year': 1965, 'jurisdiction': 'quebec',
             'reference_rate': 0.05}
    prior = {
        'primary': {'balance': 50_000.0, 'birth_year': 1960,
                    'jurisdiction': 'federal', 'reference_rate': 0.06},
        'child_a': third,
    }
    new_store = rebuild_adult_lif(
        prior, balance=52_000.0, birth_year=1960, jurisdiction='federal',
        reference_rate=0.06)
    assert new_store['primary']['balance'] == 52_000.0
    assert new_store['child_a'] == third
    assert new_store['child_a'] is not third
