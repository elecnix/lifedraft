#!/usr/bin/env python3
"""Epic #841 bite 2 / issue #812: a child's OWN income funds a child's OWN
registered accounts, simulated in the per-year fold.

Bite 1 (#844) made a child-owned account + child income REACH the engine as
data. Bite 2 MODELS them: a child's own income * the household savings rate
funds contributions routed by StrategyEngine.allocate_child into the child's
OWN TFSA/FHSA/RRSP by ROOM (#701: a child is not taxed as an individual, so the
routing is by room, never by a deduction the child does not get). Those
accounts then compound year over year in the fold, entirely separate from the
household (primary/spouse) pot -- the family objective that sums across all
members is a later bite (#841 bite 4).

DP#15: fabricated round numbers only. DP#13: the routing honours the strategy's
declared child_*_pct (the contract's opinion); absent a declared target it
falls back to the room-priority waterfall -- a fallback, not a hardcoded
opinion. DP#32: a child with no income AND no room stays at a MODELLED zero (the
entry exists and is stepped to zero), not a skipped child. DP#18: the child's
dollars are REDIRECTED out of the adult pot, never created -- so the adult
household net worth is unchanged by giving the child income.
"""

import unittest

import countries.canada  # noqa: F401
from strategy import AllocationStrategy


def _run(children, *, savings_rate=0.10, investment_return=0.05,
         salary_growth=0.0, years=3, strategy=None, primary_income=120_000):
    from simulation_config import SimulationConfig
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation
    cfg = SimulationConfig(
        projection_years=years, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=savings_rate, living_costs=0.0, start_year=2026,
        province='quebec', investment_return=investment_return,
        salary_growth=salary_growth,
        family_members=[{'role': 'primary', 'birth_year': 1980,
                         'gross_income': primary_income, 'id': 'p1',
                         'rrsp_room_accumulated': 50_000,
                         'tfsa_room_accumulated': 50_000}],
        children=children)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg), strategy=strategy)
    sim.run()
    return sim


def _child_accounts(sim):
    return sim._state.jurisdiction_state['canada']['child_accounts']


class TestChildSaverIsModelledInTheFold(unittest.TestCase):

    def test_child_income_and_room_accrue_contributions_and_growth(self):
        # child with $10,000 income, $5,000 TFSA room. No declared child_*_pct
        # -> allocate_child's room-priority waterfall lands the savings in TFSA.
        # Issue #701 Step 6: the child is now taxed INDIVIDUALLY on their own
        # return -- $10,000 in Quebec's lowest combined bracket (25.69%) is
        # $2,569 tax, so the child funds their accounts from the $7,431 AFTER-TAX
        # income, not the gross:
        #   savings/yr = (10_000 - 2_569) * 0.10 = 743.10
        #   y1: (0 + 743.10)*1.05 = 780.255       room -> 4256.90
        #   y2: (780.255 + 743.10)*1.05 = 1599.52 room -> 3513.80
        #   y3: (1599.52 + 743.10)*1.05 = 2459.75 room -> 2770.70 (pre-accrual)
        sim = _run([{'name': 'kid', 'birth_year': 2008, 'id': 'c1',
                     'gross_income': 10_000, 'tfsa_room_accumulated': 5_000}])
        acct = _child_accounts(sim)[0]
        self.assertAlmostEqual(acct['tfsa_balance'], 2459.7538875, places=3,
                               msg="the child's OWN TFSA must accrue the child's "
                                   "OWN AFTER-TAX savings and compound each year")
        # Issue #857: this child (born 2008) is 18+ across all three years
        # (ages 18/19/20 in 2026/2027/2028), so TFSA room now ALSO ACCRUES the
        # year-versioned annual limit each year (7000 + 7175 + 7354.37 =
        # 21529.37) on top of the decrement-only 2770.70 -> 24300.07. Bite 2 was
        # decrement-only; #857 added the accrual half (see
        # test_issue_857_child_room_accrual.py).
        self.assertAlmostEqual(acct['tfsa_room'], 24300.07, places=2)
        # The balance exceeds the $2,229.30 contributed -> it GREW (was not
        # merely a running sum of contributions).
        self.assertGreater(acct['tfsa_balance'], 2229.30)
        # Nothing spilled to RRSP/FHSA/non-reg (room-priority filled TFSA first).
        self.assertEqual(acct['rrsp_balance'], 0.0)
        self.assertEqual(acct['fhsa_balance'], 0.0)
        self.assertEqual(acct['non_reg_balance'], 0.0)

    def test_child_with_no_income_and_no_room_is_a_modelled_zero(self):
        # DP#32: the entry EXISTS (the child was stepped every year) and is all
        # zero -- a modelled zero saver, not a silently skipped one.
        sim = _run([{'name': 'kid', 'birth_year': 2012, 'id': 'c1',
                     'gross_income': 0}])
        accts = _child_accounts(sim)
        self.assertEqual(len(accts), 1, "the zero child is still MODELLED "
                                        "(one entry), not dropped")
        acct = accts[0]
        for key in ('rrsp_balance', 'tfsa_balance', 'fhsa_balance',
                    'non_reg_balance'):
            self.assertEqual(acct[key], 0.0,
                             msg=f"{key} of a no-income/no-room child must be a "
                                 f"modelled 0.0")

    def test_child_income_does_not_inflate_the_adult_household_pot(self):
        # DP#18: the child's dollars are REDIRECTED to the child's OWN accounts,
        # not created and not double-counted into the adults' pot. So the ADULT
        # household net worth is identical whether the child earns $10,000 or
        # $0 (only the child's OWN, separately-threaded accounts differ).
        with_income = _run([{'name': 'kid', 'birth_year': 2008, 'id': 'c1',
                             'gross_income': 10_000,
                             'tfsa_room_accumulated': 5_000}])
        no_income = _run([{'name': 'kid', 'birth_year': 2008, 'id': 'c1',
                           'gross_income': 0, 'tfsa_room_accumulated': 5_000}])
        self.assertAlmostEqual(with_income._state.net_assets(),
                               no_income._state.net_assets(), places=6,
                               msg="a child's income must fund the CHILD's "
                                   "accounts, never inflate the adult pot")
        # ...and the child DID accrue in the with-income case (the redirection
        # is real, not a silent drop).
        self.assertGreater(_child_accounts(with_income)[0]['tfsa_balance'], 0.0)
        self.assertEqual(_child_accounts(no_income)[0]['tfsa_balance'], 0.0)

    def test_declared_child_fhsa_target_routes_to_fhsa_not_tfsa(self):
        # DP#13: the CONTRACT expresses the opinion. A strategy declaring
        # child_fhsa_pct=1.0 (a first-home goal) routes the child's savings to
        # the child's OWN FHSA, not the room-priority TFSA default. The child
        # has BOTH TFSA and FHSA room; the declared target picks FHSA.
        strat = AllocationStrategy(child_fhsa_pct=1.0)
        sim = _run([{'name': 'kid', 'birth_year': 2006, 'id': 'c1',
                     'gross_income': 10_000, 'tfsa_room_accumulated': 5_000,
                     'fhsa_room_accumulated': 8_000,
                     'fhsa_lifetime_limit': 40_000}], strategy=strat)
        acct = _child_accounts(sim)[0]
        self.assertGreater(acct['fhsa_balance'], 0.0,
                           msg="a declared child_fhsa_pct must route to the "
                               "child's OWN FHSA (the contract's opinion, DP#13)")
        self.assertEqual(acct['tfsa_balance'], 0.0,
                         msg="the declared FHSA target overrides the room-"
                             "priority TFSA-first default")

    def test_no_children_declared_leaves_child_accounts_empty_and_inert(self):
        # The golden-shaped case: no children at all -> an empty child_accounts
        # list, nothing to model (this is the no-op the golden invariant relies
        # on, exercised here as a unit).
        sim = _run([])
        self.assertEqual(_child_accounts(sim), [])


if __name__ == '__main__':
    unittest.main()
