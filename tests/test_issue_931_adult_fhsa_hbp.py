#!/usr/bin/env python3
"""Issue #931: an ADULT can become a first-time home buyer (follow-on to #704).

#704 wired the first-home instruments -- FHSA qualifying withdrawal + HBP RRSP
withdrawal (15-year repayment) -- via ``first_home_purchases[]`` but scoped the
buyer to a declared CHILD. #931 lifts that restriction for an ADULT
(primary/spouse), reusing the existing per-adult FHSA store (#893) and RRSP pots:

  * **FHSA** qualifying withdrawal: the household FHSA (slot 0) comes out
    TAX-FREE for the first home and the account closes.
  * **HBP**: up to $60,000 comes out of the buyer's OWN RRSP NON-TAXABLY and is
    repaid over a 15-year schedule, restoring the RRSP.

Both proceeds land in the household non-registered cash as the down payment (net
worth conserved). The child path (``apply_child_first_home_purchases``) is
unchanged; both share the same per-account step (``_apply_first_home_to_account``,
DP#9). DP#32: a household that declares NO first-home purchase (the golden
household) is byte-identical to today -- ``TestAbsenceIsNoOp`` + the golden
invariant guard that.
"""

import unittest

import countries.canada  # noqa: F401


def _run(first_home_purchases, *, member_over=None, years=3, savings_rate=0.0,
         investment_return=0.0, primary_income=0):
    from simulation_config import SimulationConfig
    from countries.canada.adapter import CanadaAdapter
    from simulation import FamilySimulation
    # A primary born 1980 is a mid-career adult across the projection: eligible
    # to make a first-home FHSA/HBP withdrawal from their own pots. savings_rate 0
    # + investment_return 0 keep contribution/growth noise out so the withdrawal
    # is the only thing that moves the balances.
    member = {'role': 'primary', 'birth_year': 1980, 'gross_income': primary_income,
              'id': 'p1', 'rrsp_room_accumulated': 50_000,
              'tfsa_room_accumulated': 50_000}
    if member_over:
        member.update(member_over)
    cfg = SimulationConfig(
        projection_years=years, house_value=0, mortgage_balance=0,
        mortgage_rate=0.0, amortization_years=25, margin_available=0,
        savings_rate=savings_rate, living_costs=0.0, start_year=2026,
        province='quebec', investment_return=investment_return, salary_growth=0.0,
        family_members=[member],
        first_home_purchases=first_home_purchases)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg), strategy=None)
    sim.run()
    return sim


def _canada(sim):
    return sim._state.jurisdiction_state['canada']


def _fhsa0(sim):
    return list(_canada(sim)['adult_fhsa'].values())[0]


def _rrsp0(sim):
    return list(_canada(sim)['adult_rrsp'].values())[0]


class TestAdultFhsaQualifyingWithdrawal(unittest.TestCase):

    def test_fhsa_withdrawn_tax_free_and_account_closes(self):
        # $40k FHSA, buys in the run's final year. The qualifying withdrawal is
        # TAX-FREE: the full $40k lands in the household cash and the FHSA closes
        # to $0. (Observed in the purchase year so the household's later-year cash
        # consumption -- a pre-existing zero-income draw -- does not obscure it.)
        sim = _run([{'buyer': 'p1', 'year': 2027}],
                   member_over={'fhsa_balance': 40_000,
                                'fhsa_room_accumulated': 0}, years=2)
        self.assertEqual(_fhsa0(sim)['balance'], 0.0,
                         msg="FHSA must be emptied by the qualifying withdrawal")
        self.assertEqual(sim._state.non_reg_balance, 40_000.0,
                         msg="the full FHSA balance funds the down payment "
                             "tax-free (no withholding)")


class TestAdultHbpWithdrawalAndRepayment(unittest.TestCase):

    def test_hbp_withdrawal_non_taxable_and_capped_at_60k(self):
        # $100k RRSP, buys in the run's final year. HBP withdraws the $60k maximum
        # NON-TAXABLY: the buyer's own RRSP drops to $40k and $60k funds the down
        # payment.
        sim = _run([{'buyer': 'p1', 'year': 2027}],
                   member_over={'rrsp_balance': 100_000}, years=2)
        self.assertEqual(_rrsp0(sim)['own'], 40_000.0,
                         msg="HBP withdraws min(RRSP, $60k) from the adult's RRSP")
        self.assertEqual(sim._state.non_reg_balance, 60_000.0,
                         msg="the HBP withdrawal is non-taxable -- the full "
                             "amount funds the down payment")

    def test_hbp_tracks_a_15_year_schedule_on_the_buyers_slot(self):
        sim = _run([{'buyer': 'p1', 'year': 2027}],
                   member_over={'rrsp_balance': 100_000}, years=3)
        hbp = _canada(sim)['adult_hbp']['p1']
        self.assertEqual(hbp['slot'], 0, msg="routed to the primary's RRSP slot")
        self.assertEqual(hbp['withdrawal'], 60_000.0)
        self.assertEqual(len(hbp['repayment_schedule']), 15,
                         msg="the HBP repays over 15 years")
        # Repayment starts the 3rd year after a 2027 withdrawal (2030); a 3-year
        # run ends 2028, so nothing is repaid yet -- outstanding is still full.
        self.assertEqual(hbp['outstanding'], 60_000.0)

    def test_hbp_repays_over_time_restoring_the_rrsp(self):
        # Run past the 2030 repayment start: the outstanding balance falls below
        # the withdrawal and the buyer's own RRSP receives the repayments back.
        sim = _run([{'buyer': 'p1', 'year': 2027}],
                   member_over={'rrsp_balance': 100_000}, years=7)
        hbp = _canada(sim)['adult_hbp']['p1']
        self.assertGreater(hbp['repaid'], 0.0,
                           msg="repayments must have started by 2030")
        self.assertLess(hbp['outstanding'], 60_000.0)
        self.assertAlmostEqual(hbp['repaid'] + hbp['outstanding'], 60_000.0,
                               places=2)
        self.assertAlmostEqual(_rrsp0(sim)['own'], 40_000.0 + hbp['repaid'],
                               places=2,
                               msg="repaid dollars move cash -> the RRSP")


class TestAdultFhsaAndHbpSameHome(unittest.TestCase):

    def test_both_instruments_fund_one_purchase_without_double_count(self):
        # FHSA ($30k) and HBP ($60k of a $100k RRSP) for the SAME home: the down
        # payment is exactly their sum ($90k), each instrument counted once.
        sim = _run([{'buyer': 'p1', 'year': 2027}],
                   member_over={'fhsa_balance': 30_000, 'fhsa_room_accumulated': 0,
                                'rrsp_balance': 100_000}, years=2)
        self.assertEqual(_fhsa0(sim)['balance'], 0.0)
        self.assertEqual(_rrsp0(sim)['own'], 40_000.0)
        self.assertEqual(sim._state.non_reg_balance, 90_000.0,
                         msg="FHSA + HBP fund one down payment, no double-count")


class TestAbsenceIsNoOp(unittest.TestCase):

    def test_no_purchase_declared_leaves_the_adult_pots_untouched(self):
        # The SAME adult with the SAME opening accounts, run WITHOUT a declared
        # purchase: FHSA/RRSP are untouched and no HBP record is created.
        sim = _run([], member_over={'fhsa_balance': 40_000,
                                    'fhsa_room_accumulated': 0,
                                    'rrsp_balance': 100_000}, years=5)
        self.assertEqual(_fhsa0(sim)['balance'], 40_000.0)
        self.assertEqual(_rrsp0(sim)['own'], 100_000.0)
        self.assertEqual(sim._state.non_reg_balance, 0.0)
        self.assertEqual(_canada(sim)['adult_hbp'], {},
                         msg="no HBP record without a declared purchase")


if __name__ == '__main__':
    unittest.main()
