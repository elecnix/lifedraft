#!/usr/bin/env python3
"""Issue #859 (epic #841 follow-up, Part A): a cross-member LOAN that funds a
child's registered room is booked on a FAMILY BALANCE SHEET -- the lender holds
a RECEIVABLE (asset) and the child holds the funded contribution plus an
offsetting LIABILITY.

Bite 3 (#858) shipped the parent->child GIFT and DEFERRED the loan variant with
a precise reason: a funding loan differs from a gift only in that the parent
holds a receivable rather than giving the money away, and with no family-wide
net-worth view yet a loan was INDISTINGUISHABLE from a gift -- it would misstate
DP#18 (a loan must NOT reduce the lender's net worth -- the receivable offsets
it). Bite 4 (#861) built the family net-worth view. This lands the loan on it.

A LOAN is expressed as a REPAYABLE gift (``repayable: true``): the funding flow
is identical to a gift (it fills the child's OWN registered room, capped to the
child's remaining ACCRUED room #857, carved from the adult base), but the
principal that lands is booked as a receivable/liability so the two members'
net-worth pictures are honest.

Key facts asserted (against the code, not assumed):
- The lender's household net worth is NOT reduced by lending (receivable offsets
  the carve) -- unlike a GIFT of the same amount, which DOES reduce it (DP#18).
- The family TOTAL is identical for a loan and an equal gift: an intra-family
  loan is a wash for the family (receivable == liability), so it neither creates
  nor destroys family wealth -- the balance-sheet total ties out to the family
  objective exactly (DP#9).
- The booked receivable/liability respects the child's accrued registered room
  (#857): a loan larger than the room books only the room-capped principal.
DP#32: absence is a modelled zero (no repayable transfer -> zero receivable, and
the child accounts are bit-identical to the plain-gift/self-funded case).
DP#15: fabricated names / round numbers only.
"""

import unittest

import countries.canada  # noqa: F401
from simulation_state import (
    _initial_child_accounts, child_savings_for_year,
    child_gift_funding_for_year, child_loan_funded_for_year,
    _step_child_accounts,
)


def _teen_saver(**over):
    # 18yo, $9,000 income she cannot use to fill her $16k FHSA + $7k TFSA room.
    ch = {'name': 'kid', 'birth_year': 2008, 'id': 'ca',
          'gross_income': 9_000,
          'fhsa_room_accumulated': 16_000, 'fhsa_lifetime_limit': 40_000,
          'tfsa_room_accumulated': 7_000}
    ch.update(over)
    return ch


def _run(children, gifts=(), *, savings_rate=0.10, investment_return=0.05,
         salary_growth=0.0, years=3, primary_income=200_000):
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
        children=children, gifts=list(gifts))
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
    sim.results = sim.run()
    # Attach a mapped-config dict shaped the way objective.py reads it.
    sim._cfg_dict = {'tax': {'province': 'quebec', 'start_year': 2026}}
    return sim


def _child(sim):
    return sim._state.jurisdiction_state['canada']['child_accounts'][0]


class TestLoanFundedRoomBalanceSheet(unittest.TestCase):

    def test_a_loan_books_a_lender_receivable_and_a_child_liability(self):
        from objective import family_balance_sheet
        loan = [{'id': 'l1', 'from': 'p1', 'to': 'ca',
                 'amount': 20_000, 'repayable': True}]
        sim = _run([_teen_saver()], loan)

        # The child's registered room filled from the loan (as a gift would),
        # AND the principal that landed shows as a receivable/liability.
        child = _child(sim)
        principal = child['loan_funded_principal']
        self.assertGreater(principal, 0.0,
                           "the loan funded the child's room -> a principal owed")

        bs = family_balance_sheet(sim.results, sim._cfg_dict)
        self.assertAlmostEqual(bs['loans_receivable'], principal, places=6,
                               msg="lender holds a receivable == principal lent")
        self.assertAlmostEqual(bs['children'][0]['loan_liability'], principal,
                               places=6,
                               msg="the child owes the principal (liability)")

    def test_a_funding_loan_does_not_reduce_the_lenders_net_worth(self):
        # DP#18: the whole point deferred from bite 3. A LOAN and a GIFT of the
        # same amount move the SAME cash into the child's room (identical carve,
        # identical child growth, identical household estate) -- but the LENDER's
        # net worth is preserved for a LOAN (the receivable offsets the carve)
        # and reduced for a GIFT. The delta is exactly the principal.
        from objective import family_balance_sheet
        amount = 20_000
        gift = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': amount}]
        loan = [{'id': 'l1', 'from': 'p1', 'to': 'ca',
                 'amount': amount, 'repayable': True}]
        gs = family_balance_sheet(_run([_teen_saver()], gift).results,
                                  {'tax': {'province': 'quebec', 'start_year': 2026}})
        ls = family_balance_sheet(_run([_teen_saver()], loan).results,
                                  {'tax': {'province': 'quebec', 'start_year': 2026}})
        principal = ls['loans_receivable']
        self.assertGreater(principal, 0.0)
        # The household estate itself is identical (same carve, same fill).
        self.assertAlmostEqual(gs['household_after_tax_estate'],
                               ls['household_after_tax_estate'], places=4)
        # But the lender's NET WORTH is higher under the loan by the receivable.
        self.assertAlmostEqual(ls['household_net_worth'] - gs['household_net_worth'],
                               principal, places=4,
                               msg="a loan preserves the lender's net worth; a "
                                   "gift of the same amount does not (DP#18)")

    def test_family_total_is_a_wash_and_ties_to_the_objective(self):
        # An intra-family loan neither creates nor destroys FAMILY wealth
        # (receivable == liability): the family total equals that of an equal
        # gift, and equals the family objective exactly (DP#9 -- one number).
        from objective import family_balance_sheet, _family_after_tax_networth
        cfg = {'tax': {'province': 'quebec', 'start_year': 2026}}
        gift = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 20_000}]
        loan = [{'id': 'l1', 'from': 'p1', 'to': 'ca',
                 'amount': 20_000, 'repayable': True}]
        g = _run([_teen_saver()], gift).results
        l = _run([_teen_saver()], loan).results
        self.assertAlmostEqual(family_balance_sheet(g, cfg)['family_total'],
                               family_balance_sheet(l, cfg)['family_total'],
                               places=4)
        self.assertAlmostEqual(family_balance_sheet(l, cfg)['family_total'],
                               _family_after_tax_networth(l, cfg), places=6,
                               msg="the balance sheet total IS the family "
                                   "objective (one spelling, DP#9)")

    def test_the_receivable_respects_the_childs_accrued_room(self):
        # #857: a loan larger than the room books only the room-capped principal.
        # Room = $16k FHSA + $7k TFSA = $23k; own after-tax savings take a sliver
        # first, so the loan-funded principal never exceeds the remaining room.
        children = [_teen_saver()]
        gifts = [{'id': 'l1', 'from': 'p1', 'to': 'ca',
                  'amount': 100_000, 'repayable': True}]
        prior = _initial_child_accounts(children)
        own = child_savings_for_year(children, 0.10, 0.0, 0)
        loan = child_loan_funded_for_year(children, gifts, own, prior)
        self.assertLessEqual(loan[0], 23_000.0 + 1e-6,
                             "the loan-funded principal cannot exceed the room")
        self.assertAlmostEqual(loan[0], 23_000.0 - own[0], places=6,
                               msg="capped at the child's remaining registered "
                                   "room after their own savings (#857)")

    def test_loan_first_split_sums_to_the_total_funding(self):
        # When a child receives BOTH a loan and a gift, the loan is funded FIRST
        # within the room; the two portions must sum to the SAME total the carve
        # uses (child_gift_funding_for_year), so no dollar is double-counted or
        # lost (DP#18/DP#9).
        children = [_teen_saver()]
        transfers = [
            {'id': 'l1', 'from': 'p1', 'to': 'ca', 'amount': 10_000,
             'repayable': True},
            {'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 30_000},
        ]
        prior = _initial_child_accounts(children)
        own = child_savings_for_year(children, 0.10, 0.0, 0)
        total = child_gift_funding_for_year(children, transfers, own, prior)
        loan = child_loan_funded_for_year(children, transfers, own, prior)
        self.assertLessEqual(loan[0], total[0] + 1e-9)
        self.assertAlmostEqual(loan[0], 10_000.0, places=6,
                               msg="the $10k loan fits inside the room -> funded "
                                   "in full, ahead of the gift")


class TestAbsenceIsNoOp(unittest.TestCase):
    """DP#32: no repayable transfer -> a modelled zero everywhere."""

    def test_no_repayable_transfer_books_no_receivable(self):
        from objective import family_balance_sheet
        cfg = {'tax': {'province': 'quebec', 'start_year': 2026}}
        # A plain GIFT (not repayable) books nothing on the loan balance sheet.
        gift = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 20_000}]
        bs = family_balance_sheet(_run([_teen_saver()], gift).results, cfg)
        self.assertEqual(bs['loans_receivable'], 0.0)
        self.assertEqual(bs['children'][0]['loan_liability'], 0.0)
        # And household net worth == the bare estate (no receivable to add).
        self.assertEqual(bs['household_net_worth'],
                         bs['household_after_tax_estate'])

    def test_a_plain_gift_and_a_loan_grow_the_child_identically(self):
        # The loan variant changes only the BALANCE SHEET, never the child's
        # account growth: a gift and a repayable gift of the same amount leave
        # the child's balances bit-identical (the liability is separate).
        gift = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 20_000}]
        loan = [{'id': 'l1', 'from': 'p1', 'to': 'ca',
                 'amount': 20_000, 'repayable': True}]
        g, l = _child(_run([_teen_saver()], gift)), _child(_run([_teen_saver()], loan))
        for k in ('rrsp_balance', 'tfsa_balance', 'fhsa_balance',
                  'non_reg_balance'):
            self.assertEqual(g[k], l[k],
                             f"{k}: the loan variant must not change growth")

    def test_step_without_loan_amounts_leaves_principal_zero(self):
        children = [_teen_saver()]
        prior = _initial_child_accounts(children)
        own = child_savings_for_year(children, 0.10, 0.0, 0)
        pcts = {'tfsa': 0.0, 'fhsa': 0.0, 'rrsp': 0.0, 'non_reg': 0.0}
        stepped = _step_child_accounts(prior, own, pcts, 0.0)[0]
        self.assertEqual(stepped['loan_funded_principal'], 0.0)


class TestRepayableReachesInternalConfig(unittest.TestCase):
    """The ``repayable`` leaf reaches the internal config through the real
    adapter (input_contract), defaulting False when undeclared."""

    def test_repayable_defaults_false(self):
        from input_contract import to_internal_config
        from test_issue_841_child_gift_bite3 import _two_generation_doc
        doc = _two_generation_doc(
            {"id": "g1", "from": "p1", "to": "ca", "amount": 20000})
        cfg = to_internal_config(doc)
        self.assertEqual(cfg["family"]["gifts"][0]["repayable"], False)

    def test_repayable_true_is_carried(self):
        from input_contract import to_internal_config
        from test_issue_841_child_gift_bite3 import _two_generation_doc
        doc = _two_generation_doc(
            {"id": "l1", "from": "p1", "to": "ca", "amount": 20000,
             "repayable": True})
        cfg = to_internal_config(doc)
        self.assertEqual(cfg["family"]["gifts"][0]["repayable"], True)


if __name__ == '__main__':
    unittest.main()
