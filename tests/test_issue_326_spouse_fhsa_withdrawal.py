"""Issue #326: each first-time buyer drains their OWN FHSA, not the primary's.

ITA s.146.6(1) makes a qualifying withdrawal the HOLDER's -- an amount "the
holder receives from their own FHSA" -- and CRA states that a couple buying a
qualifying home together "can make a qualifying withdrawal from their own FHSAs
as long as you both meet all of the conditions". So a married couple has two
FHSA pots, and the CFFP's first-home scenario models exactly that (each spouse
with their own CELIAPP/FHSA, both withdrawing tax-free for the jointly purchased
home).

Before this, `apply_adult_first_home_purchases` built every buyer's synthetic
account with the slot-0 (household/primary) FHSA scalar, so: with only the SPOUSE
declared as buyer, the PRIMARY's FHSA was drained and closed on the spouse's
behalf while the spouse's own pot stayed invested, open and still accruing room
-- and it could then only leave as a taxable withdrawal or a transfer at
must_close, never as the tax-free down payment the law allows. With both declared,
only one pot funded the down payment and the household came up $16,000 short.

Every assertion here drives ``FamilySimulation.run`` and reads the engine's own
carried state. DP#15: fabricated, round-numbered fixtures.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import countries.canada  # noqa: F401

OPENING_FHSA = 16_000.0


def _run(buyers, *, years=2, purchase_year=2027, with_spouse=True):
    """A two-adult household, each partner holding their own $16,000 FHSA.

    Zero savings rate and zero investment return, so the qualifying withdrawal is
    the only thing that moves a balance: an unchanged FHSA stays at exactly
    $16,000 and a drained one goes to exactly $0.
    """
    from simulation_config import SimulationConfig
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation

    members = [{'role': 'primary', 'birth_year': 1980, 'gross_income': 0,
                'id': 'p1', 'rrsp_room_accumulated': 0,
                'tfsa_room_accumulated': 0,
                'fhsa_balance': OPENING_FHSA, 'fhsa_room_accumulated': 0}]
    if with_spouse:
        members.append({'role': 'spouse', 'birth_year': 1982, 'gross_income': 0,
                        'id': 's1', 'rrsp_room_accumulated': 0,
                        'tfsa_room_accumulated': 0,
                        'fhsa_balance': OPENING_FHSA, 'fhsa_room_accumulated': 0})
    purchases = [{'buyer': b, 'year': purchase_year} for b in buyers]
    cfg = SimulationConfig(
        projection_years=years, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=0.0, living_costs=0.0, start_year=2026,
        province='quebec', investment_return=0.0, salary_growth=0.0,
        family_members=members, first_home_purchases=purchases)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg), strategy=None)
    results = sim.run()
    return sim, results


def _fhsa(sim):
    return sim._state.jurisdiction_state['canada']['adult_fhsa']


class TestEachBuyerDrainsTheirOwnFhsa:
    def test_both_buyers_drain_both_pots_tax_free(self):
        """The acceptance case: each partner withdraws from their OWN FHSA, so
        the household's down payment is 2 x $16,000 and both accounts close."""
        sim, results = _run(['p1', 's1'])
        store = _fhsa(sim)
        assert len(store) == 2, "the household has two FHSA owners"
        for aid, entry in store.items():
            assert entry['balance'] == 0.0, f"{aid} was not drained"
            assert entry['room'] == 0.0, f"{aid} kept contribution room"
            assert entry['lifetime_used'] == entry['lifetime_limit'], (
                f"{aid}'s lifetime allowance was not exhausted -- the account "
                f"is not closed")
        # The withdrawal is TAX-FREE: it lands in non-registered cash and nothing
        # charges income tax on it.
        assert results[-1].non_reg_balance == pytest.approx(2 * OPENING_FHSA)

    def test_only_the_spouse_buying_drains_only_the_spouses_pot(self):
        """The wrong-person case the issue measured: before the fix the primary's
        FHSA was drained on the spouse's behalf."""
        sim, results = _run(['s1'])
        store = _fhsa(sim)
        assert len(store) == 2
        primary = next(v for k, v in store.items() if k == 'p1')
        spouse = next(v for k, v in store.items() if k == 's1')
        assert spouse['balance'] == 0.0 and spouse['room'] == 0.0
        assert spouse['lifetime_used'] == spouse['lifetime_limit']
        assert primary['balance'] == OPENING_FHSA, (
            "the primary did not buy: their FHSA must be untouched")
        assert primary['room'] != 0.0, "their room must stay open too"
        assert results[-1].non_reg_balance == pytest.approx(OPENING_FHSA)

    def test_only_the_primary_buying_is_unchanged(self):
        """The one case that already worked, pinned so the fix cannot break it."""
        sim, results = _run(['p1'])
        store = _fhsa(sim)
        primary = next(v for k, v in store.items() if k == 'p1')
        spouse = next(v for k, v in store.items() if k == 's1')
        assert primary['balance'] == 0.0 and primary['room'] == 0.0
        assert spouse['balance'] == OPENING_FHSA
        assert results[-1].non_reg_balance == pytest.approx(OPENING_FHSA)

    def test_no_purchase_leaves_both_pots_open_and_invested(self):
        sim, results = _run([])
        store = _fhsa(sim)
        assert [e['balance'] for e in store.values()] == \
            [OPENING_FHSA] * len(store)
        assert results[-1].non_reg_balance == 0.0


class TestAbsenceIsInert:
    def test_the_golden_invariant_is_unmoved(self):
        """A household with no first-home purchase must stay byte-identical."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'tests'))
        from test_golden_trajectory_581 import (golden_household_config, _run
                                                 as golden_run)
        assert golden_run(golden_household_config())[-1].total_assets == \
            pytest.approx(9_709_753.139463063)
