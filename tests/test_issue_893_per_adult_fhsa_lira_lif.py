"""Issue #893: per-adult FHSA CONTRIBUTION/allocation + LIRA/LIF 2-owner
conversion -- the Step 4 (#700/#704) follow-up.

Step 4 landed the per-adult FHSA/LIRA/LIF STORES (keyed by entity id, primary
first) plus a growth pass, but left two behaviours single-slot:

  1. FHSA CONTRIBUTION: only slot 0 (the primary) was funded/room-allocated; a
     second adult's FHSA compounded but took no new contribution and accrued no
     room. This fixes that -- the household FHSA budget (now sized against the
     TOTAL household FHSA room) fills slot 0 first, then spills the remainder
     into each further owner's OWN FHSA, capped to that owner's OWN room and
     lifetime limit, with each owner's room re-accruing the annual limit.

  2. LIRA -> LIF CONVERSION: only slot 0 converted at the statutory age; a
     further owner's LIRA was carried flat (no growth, no conversion). This adds
     a pure per-owner conversion pass so EACH further owner's CRI/LIRA grows and
     converts to a LIF at ITS OWN birth-year-driven age (or elected year), using
     its own reference rate and jurisdiction.

Isomorphism note: both paths are no-ops for a household with <=1 owner of each
kind (the golden path) -- slot 0 keeps the byte-identical single-slot compute
and the further-owner passes have no slot to touch. Guarded by
test_golden_trajectory_581.
"""
from countries.canada.adapter import CanadaAdapter  # registers the LIF provider
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from strategy import AllocationStrategy
from simulation_state import (
    rebuild_adult_fhsa,
    convert_further_adult_locked_in,
    adult_fhsa_total_room,
    adult_fhsa_total_lifetime_remaining,
    _canada_fhsa_limits,
)

_LIFETIME = _canada_fhsa_limits()[2]


# ── per-owner FHSA contribution/allocation (rebuild_adult_fhsa) ──────────────

def _two_fhsa_prior():
    return {
        'primary': {'balance': 0.0, 'room': 8_000.0,
                    'lifetime_used': 0.0, 'lifetime_limit': _LIFETIME},
        'spouse': {'balance': 0.0, 'room': 8_000.0,
                   'lifetime_used': 0.0, 'lifetime_limit': _LIFETIME},
    }


def test_further_adult_fhsa_receives_a_contribution_against_its_own_room():
    """Slot 0 takes the compute scalars (primary contributed its 8k, room
    re-accrued); the leftover household budget (`overflow`) funds the spouse's
    OWN FHSA up to the spouse's OWN room, and the spouse's room re-accrues."""
    new = rebuild_adult_fhsa(
        _two_fhsa_prior(),
        balance=8_000.0, room=8_000.0, lifetime_used=8_000.0,
        lifetime_limit=_LIFETIME, growth=0.0,
        overflow=8_000.0, annual_limit=8_000.0)
    assert new['spouse']['balance'] == 8_000.0
    assert new['spouse']['lifetime_used'] == 8_000.0
    assert new['spouse']['room'] == 8_000.0  # 8000 - 8000 used, + 8000 annual


def test_further_adult_fhsa_contribution_is_capped_by_its_own_lifetime():
    prior = _two_fhsa_prior()
    prior['spouse']['lifetime_used'] = _LIFETIME - 2_000.0  # only 2k of room left
    new = rebuild_adult_fhsa(
        prior, balance=8_000.0, room=8_000.0, lifetime_used=8_000.0,
        lifetime_limit=_LIFETIME, growth=0.0,
        overflow=8_000.0, annual_limit=8_000.0)
    assert new['spouse']['balance'] == 2_000.0
    assert new['spouse']['lifetime_used'] == _LIFETIME


def test_further_adult_fhsa_with_no_overflow_only_grows():
    """Regression: with no leftover budget, a further owner's FHSA still just
    compounds (the Step 4 growth-only behaviour), never fabricating a room."""
    prior = _two_fhsa_prior()
    prior['spouse']['balance'] = 30_000.0
    new = rebuild_adult_fhsa(
        prior, balance=0.0, room=8_000.0, lifetime_used=0.0,
        lifetime_limit=_LIFETIME, growth=0.10,
        overflow=0.0, annual_limit=None)
    assert new['spouse']['balance'] == 30_000.0 * 1.10
    assert new['spouse']['lifetime_used'] == 0.0
    assert new['spouse']['room'] == 8_000.0  # annual_limit None => no re-accrual


def test_total_room_and_lifetime_helpers_sum_over_owners():
    canada = {'adult_fhsa': _two_fhsa_prior()}
    assert adult_fhsa_total_room(canada) == 16_000.0
    assert adult_fhsa_total_lifetime_remaining(canada) == 2 * _LIFETIME


def test_total_room_helpers_reduce_to_slot_zero_for_a_single_owner():
    """The golden path: one owner => the household totals ARE slot 0's values,
    so feeding them to the strategy is byte-identical to the old slot-0 read."""
    canada = {'adult_fhsa': {
        'primary': {'balance': 0.0, 'room': 8_000.0,
                    'lifetime_used': 0.0, 'lifetime_limit': _LIFETIME}}}
    assert adult_fhsa_total_room(canada) == 8_000.0
    assert adult_fhsa_total_lifetime_remaining(canada) == _LIFETIME


# ── per-owner LIRA -> LIF conversion (convert_further_adult_locked_in) ───────

def _locked_in_stores(spouse_birth, *, conversion_year=0):
    lira = {
        'primary': {'balance': 100_000.0, 'birth_year': 1980,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05,
                    'conversion_year': 0},
        'spouse': {'balance': 100_000.0, 'birth_year': spouse_birth,
                   'jurisdiction': 'quebec', 'reference_rate': 0.05,
                   'conversion_year': conversion_year},
    }
    lif = {
        'primary': {'balance': 0.0, 'birth_year': 0,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05},
        'spouse': {'balance': 0.0, 'birth_year': 0,
                   'jurisdiction': 'quebec', 'reference_rate': 0.05},
    }
    return lira, lif


def test_further_adult_lira_converts_to_lif_at_its_own_age():
    # Spouse born 1955 -> turns 71 in 2026: mandatory conversion this year.
    lira, lif = _locked_in_stores(1955)
    new_lira, new_lif = convert_further_adult_locked_in(
        lira, lif, calendar_year=2026, investment_return=0.0)
    assert new_lira['spouse']['balance'] == 0.0
    assert new_lif['spouse']['balance'] > 0.0
    assert new_lif['spouse']['birth_year'] == 1955
    # Slot 0 (the primary) is driven by the single-slot compute, NOT this pass.
    assert new_lira['primary'] == lira['primary']
    assert new_lif['primary'] == lif['primary']


def test_further_adult_lira_grows_until_its_conversion_age():
    # Spouse born 1980 -> far from 71 in 2026: the LIRA just compounds.
    lira, lif = _locked_in_stores(1980)
    new_lira, new_lif = convert_further_adult_locked_in(
        lira, lif, calendar_year=2026, investment_return=0.10)
    assert new_lira['spouse']['balance'] == 100_000.0 * 1.10
    assert new_lif['spouse']['balance'] == 0.0


def test_two_owners_convert_independently_each_at_its_own_age():
    """Primary (slot 0) is out of scope for this pass; two FURTHER owners with
    different birth years convert in different years -- proving per-owner, not
    household-wide, conversion."""
    lira = {
        'primary': {'balance': 100_000.0, 'birth_year': 1980,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05,
                    'conversion_year': 0},
        'older': {'balance': 100_000.0, 'birth_year': 1955,
                  'jurisdiction': 'quebec', 'reference_rate': 0.05,
                  'conversion_year': 0},
        'younger': {'balance': 100_000.0, 'birth_year': 1975,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05,
                    'conversion_year': 0},
    }
    lif = {k: {'balance': 0.0, 'birth_year': 0, 'jurisdiction': 'quebec',
               'reference_rate': 0.05} for k in lira}
    new_lira, new_lif = convert_further_adult_locked_in(
        lira, lif, calendar_year=2026, investment_return=0.0)
    # 1955 owner (age 71) converts; 1975 owner (age 51) does not.
    assert new_lira['older']['balance'] == 0.0
    assert new_lif['older']['balance'] > 0.0
    assert new_lira['younger']['balance'] == 100_000.0
    assert new_lif['younger']['balance'] == 0.0


def test_further_adult_already_converted_lif_compounds_in_a_later_year():
    """A further owner who converted in a PRIOR year (LIRA already 0, LIF holds
    the balance) simply compounds their LIF the next year -- the forced minimum
    withdrawal for a further owner is the deferred #698-706 decumulation work."""
    lira = {
        'primary': {'balance': 100_000.0, 'birth_year': 1980,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05,
                    'conversion_year': 0},
        'spouse': {'balance': 0.0, 'birth_year': 1955,
                   'jurisdiction': 'quebec', 'reference_rate': 0.05,
                   'conversion_year': 0},
    }
    lif = {
        'primary': {'balance': 0.0, 'birth_year': 0,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05},
        'spouse': {'balance': 80_000.0, 'birth_year': 1955,
                   'jurisdiction': 'quebec', 'reference_rate': 0.05},
    }
    new_lira, new_lif = convert_further_adult_locked_in(
        lira, lif, calendar_year=2027, investment_return=0.10)
    assert new_lira['spouse']['balance'] == 0.0
    assert new_lif['spouse']['balance'] == 80_000.0 * 1.10


def test_further_adult_lif_grows_defensively_while_its_lira_not_yet_converted():
    """Defensive path: a further owner holding BOTH an unconverted LIRA (too
    young to convert this year) and a pre-existing LIF -- the LIRA grows and the
    LIF grows alongside it (no conversion, no withdrawal)."""
    lira = {
        'primary': {'balance': 100_000.0, 'birth_year': 1980,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05,
                    'conversion_year': 0},
        'spouse': {'balance': 50_000.0, 'birth_year': 1985,
                   'jurisdiction': 'quebec', 'reference_rate': 0.05,
                   'conversion_year': 0},
    }
    lif = {
        'primary': {'balance': 0.0, 'birth_year': 0,
                    'jurisdiction': 'quebec', 'reference_rate': 0.05},
        'spouse': {'balance': 20_000.0, 'birth_year': 1985,
                   'jurisdiction': 'quebec', 'reference_rate': 0.05},
    }
    new_lira, new_lif = convert_further_adult_locked_in(
        lira, lif, calendar_year=2026, investment_return=0.10)
    assert new_lira['spouse']['balance'] == 50_000.0 * 1.10  # LIRA compounds
    assert new_lif['spouse']['balance'] == 20_000.0 * 1.10   # LIF grows defensively


def test_single_owner_locked_in_is_a_no_op_for_the_further_adult_pass():
    """Absence-is-no-op: with only slot 0 present the pass touches nothing, so
    the golden single-LIRA household cannot move."""
    lira = {'primary': {'balance': 100_000.0, 'birth_year': 1955,
                        'jurisdiction': 'quebec', 'reference_rate': 0.05,
                        'conversion_year': 0}}
    lif = {'primary': {'balance': 0.0, 'birth_year': 0,
                       'jurisdiction': 'quebec', 'reference_rate': 0.05}}
    new_lira, new_lif = convert_further_adult_locked_in(
        lira, lif, calendar_year=2026, investment_return=0.10)
    assert new_lira == lira
    assert new_lif == lif


# ── end-to-end: a dual-FHSA household funds BOTH owners' FHSAs ───────────────

def _dual_fhsa_config():
    """Two high-income adults, both owning an FHSA with room. A high savings
    rate plus an FHSA-directed strategy funds enough to fill BOTH owners' room
    (the household FHSA budget is sized against the TOTAL household room)."""
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1985, 'gross_income': 200_000,
             'fhsa_room_accumulated': 8_000},
            {'role': 'spouse', 'birth_year': 1985, 'gross_income': 200_000,
             'fhsa_room_accumulated': 8_000},
        ]},
        'assumptions': {'start_year': 2026, 'projection_years': 1,
                        'investment_return': 0.0, 'salary_growth': 0.0},
        'property': {'house_value': 0, 'mortgage_balance': 0,
                     'margin_available': 0, 'heloc_readvance': False},
        'savings': {'rate': 0.30},
        'tax': {'province': 'qc', 'year': 2026},
    }


def test_dual_fhsa_household_funds_both_owners_end_to_end():
    cfg = SimulationConfig.from_dict(_dual_fhsa_config())
    strategy = AllocationStrategy(name='FHSA-first', fhsa_pct=1.0)
    sim = FamilySimulation(cfg, strategy=strategy, adapter=CanadaAdapter(cfg),
                           use_readvanceable=False, deduct_later=False)
    sim.run()
    store = sim._state.jurisdiction_state['canada']['adult_fhsa']
    # Both owners received a real contribution (balance well above their $0
    # opening), not just growth -- the spouse's FHSA is funded, not carried.
    assert store['primary']['balance'] > 0.0
    assert store['spouse']['balance'] > 0.0
