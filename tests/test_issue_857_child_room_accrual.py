#!/usr/bin/env python3
"""Issue #857: a child's registered contribution room must ACCRUE year over
year, exactly as an adult's does.

Bite 2 (#856) grew a child's OWN accounts and DECREMENTED declared room as it
was used, but never GREW room over the projection: a 15-year-old opened no TFSA
room at 18, understating a young saver's long-horizon capacity. This is the
accrual half:

  * TFSA room begins accruing the annual TFSA limit the year the child turns 18
    (age-computed from birth_year, DP#1) and each year thereafter.
  * RRSP room accrues the statutory 18% of the child's OWN earned income each
    year (ITA s.146(1)), capped at the year's RRSP dollar limit -- the SAME
    formula the adult path uses (apply_contribution_room /
    step_extra_adult_accounts), DP#9.

DP#20: year-versioned limits from the tax provider. DP#32: a pre-18 child with
no income accrues NOTHING (a modelled zero, the no-op the golden household
relies on).
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


class TestChildTfsaRoomAccruesAt18(unittest.TestCase):

    def test_no_tfsa_room_accrues_before_18(self):
        # Child born 2010 -> ages 16, 17 across cal 2026-2027. Not yet 18, no
        # income: TFSA room stays a modelled 0.0 (nothing opens before 18).
        sim = _run([{'name': 'child_a', 'birth_year': 2010, 'id': 'c1',
                     'gross_income': 0}], years=2)
        self.assertEqual(_child_accounts(sim)[0]['tfsa_room'], 0.0,
                         msg="TFSA room must NOT open before the child turns 18")

    def test_tfsa_room_opens_the_year_the_child_turns_18(self):
        # Same child, run one year longer: cal 2026-2028, ages 16, 17, 18. The
        # ONLY accruing year is 2028 (age 18) -> exactly one year's TFSA limit
        # (DP#20: the provider's year-versioned 2028 figure, $7,354.37), no
        # income so no contribution decrements it.
        sim = _run([{'name': 'child_a', 'birth_year': 2010, 'id': 'c1',
                     'gross_income': 0}], years=3)
        self.assertAlmostEqual(_child_accounts(sim)[0]['tfsa_room'], 7354.37,
                               places=2,
                               msg="TFSA room must open (one annual limit) the "
                                   "year the child turns 18")

    def test_tfsa_room_accrues_each_year_after_18(self):
        # cal 2026-2030, ages 16..20. Accruing years 2028/2029/2030 (18/19/20):
        # 7354.37 + 7538.23 + 7726.69 = 22619.29 (year-versioned limits, DP#20).
        sim = _run([{'name': 'child_a', 'birth_year': 2010, 'id': 'c1',
                     'gross_income': 0}], years=5)
        self.assertAlmostEqual(_child_accounts(sim)[0]['tfsa_room'], 22619.29,
                               places=2,
                               msg="TFSA room must keep accruing every year "
                                   "after 18")


class TestChildRrspRoomAccruesFromEarnedIncome(unittest.TestCase):

    def test_rrsp_room_accrues_18pct_of_earned_income(self):
        # An adult-age child ($20,000 earned income) with ample TFSA room: the
        # room-priority waterfall lands all savings in TFSA, so RRSP receives no
        # contribution and its room is PURE accrual -- 18% * 20,000 = 3,600/yr,
        # 3 years -> 10,800. The RRSP BALANCE stays 0 (this is room, not a
        # contribution). salary_growth 0 keeps earned income flat.
        sim = _run([{'name': 'child_b', 'birth_year': 2005, 'id': 'c1',
                     'gross_income': 20_000,
                     'tfsa_room_accumulated': 100_000,
                     'rrsp_room_accumulated': 0}], years=3)
        acct = _child_accounts(sim)[0]
        self.assertAlmostEqual(acct['rrsp_room'], 10_800.0, places=2,
                               msg="RRSP room must accrue 18% of the child's own "
                                   "earned income each year")
        self.assertEqual(acct['rrsp_balance'], 0.0,
                         msg="room accrual is not a contribution -- the RRSP "
                             "balance stays 0 when savings route elsewhere")

    def test_no_rrsp_room_accrues_without_earned_income(self):
        # DP#32: a child with no earned income accrues no RRSP room (a modelled
        # zero), even at an adult age.
        sim = _run([{'name': 'child_c', 'birth_year': 2005, 'id': 'c1',
                     'gross_income': 0}], years=3)
        self.assertEqual(_child_accounts(sim)[0]['rrsp_room'], 0.0)


if __name__ == '__main__':
    unittest.main()
