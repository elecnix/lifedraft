#!/usr/bin/env python3
"""Epic #841 bite 3: a parent->child GIFT funds a child's OWN registered room
beyond what the child's own small income can.

Bite 2 (#812) capped a child at what their OWN income * the savings rate could
fund. The real family lever is a parent moving after-tax money to a child so the
child's registered room (FHSA/TFSA/RRSP) gets FILLED and the first-home /
tax-shelter value accrues in the CHILD's hands. This bite models that gift.

Tax facts modeled (verified against the code, not assumed):
- An inter-vivos cash gift is TAX-FREE to both -- NOT income to the child, NOT
  deductible to the donor. The gift never enters a taxable-income term; it only
  REDIRECTS after-tax savings. So the household's after-tax income is unchanged
  by making a gift (test_gift_does_not_change_household_after_tax_income).
- ITA s.74.1 minor-attribution has NO effect on a registered-room gift: FHSA/
  TFSA room requires age 18+ (a minor cannot hold it) AND registered growth is
  tax-sheltered (no income to attribute). It would matter only for a future
  NON-registered gift to a minor (bite 3 scope 3, asset location).

DP#18: money is CONSERVED -- the gift is carved out of the adult allocation base
(the parent's investable dollars), never created, and lands dollar-for-dollar in
the child's registered accounts (test_gift_is_money_conserved_not_created).
DP#32: a gift naming an undeclared donor, or a recipient who is not a child, is
refused loudly (test_gift_endpoints_are_validated_loudly). DP#15: fabricated
names / round numbers only.
"""

import json
import unittest

import countries.canada  # noqa: F401
from simulation_state import (
    _initial_child_accounts, child_savings_for_year,
    child_gift_funding_for_year, _step_child_accounts,
)
import contract_schema


# A teen-saver-shaped child (fabricated): 18, a $9,000 income she cannot use to fill
# her $16,000 FHSA + $7,000 TFSA room alone.
def _teen_saver(**over):
    ch = {'name': 'kid', 'birth_year': 2008, 'id': 'ca',
          'gross_income': 9_000,
          'fhsa_room_accumulated': 16_000, 'fhsa_lifetime_limit': 40_000,
          'tfsa_room_accumulated': 7_000}
    ch.update(over)
    return ch


def _run(children, gifts=(), *, savings_rate=0.10, investment_return=0.05,
         salary_growth=0.0, years=3, strategy=None, primary_income=200_000):
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
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg), strategy=strategy)
    sim.results = sim.run()
    return sim


def _child(sim):
    return sim._state.jurisdiction_state['canada']['child_accounts'][0]


def _registered(acct):
    return acct['tfsa_balance'] + acct['fhsa_balance'] + acct['rrsp_balance']


class TestParentGiftFundsChildRegisteredRoom(unittest.TestCase):

    def test_gift_fills_the_room_a_child_income_alone_cannot(self):
        # Concrete before/after: the SAME teen-saver-shaped child, self-funded vs a
        # $20,000/yr parent gift. Her $9,000 income * 0.10 = $900/yr barely
        # dents a $23,000 registered room; the gift fills it.
        gift = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 20_000}]
        self_funded = _run([_teen_saver()])
        with_gift = _run([_teen_saver()], gift)

        sf, wg = _child(self_funded), _child(with_gift)

        # Self-funded alone leaves the FHSA EMPTY after 3 years (her $900/yr all
        # went to the room-priority TFSA first and never reached the FHSA).
        self.assertEqual(sf['fhsa_balance'], 0.0)
        self.assertLess(_registered(sf), 3_100.0,
                        "her own $900/yr fills almost none of $23,000 room")

        # The gift fills BOTH the TFSA ($7,000 room) and most of the FHSA
        # ($16,000 room), and it grows -- the first-home value accrues in HER
        # hands. Concrete: registered ~ $26,500 vs ~ $3,000 self-funded.
        self.assertGreater(wg['fhsa_balance'], 18_000.0,
                           "the parent gift fills the child's FHSA she could "
                           "never self-fund")
        self.assertGreater(_registered(wg), 26_000.0)
        self.assertGreater(_registered(wg), _registered(sf) + 20_000.0,
                           "the gift moved the needle by an order of magnitude")

    def test_gift_is_money_conserved_not_created(self):
        # DP#18, at the fold: with growth switched OFF (return=0) so balances ARE
        # contributions, every gift dollar must land in the child's REGISTERED
        # accounts -- the amount the caller carves out of the adult base equals
        # the amount the child's accounts gain. Nothing is created; nothing is
        # lost to non-reg (that would be asset location, a later scope).
        children = [_teen_saver()]
        gifts = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 12_000}]
        prior = _initial_child_accounts(children)
        own = child_savings_for_year(children, 0.10, 0.0, 0)
        gift = child_gift_funding_for_year(children, gifts, own, prior)
        pcts = {'tfsa': 0.0, 'fhsa': 0.0, 'rrsp': 0.0, 'non_reg': 0.0}

        no_gift = _step_child_accounts(prior, own, pcts, 0.0)[0]
        with_gift = _step_child_accounts(prior, own, pcts, 0.0, gift)[0]

        gained = _registered(with_gift) - _registered(no_gift)
        self.assertAlmostEqual(gained, sum(gift), places=6,
                               msg="every gift dollar carved from the adult base "
                                   "must land in the child's registered accounts")
        self.assertGreater(sum(gift), 0.0)
        # The gift did NOT inflate non-reg (it stayed inside registered room).
        self.assertEqual(with_gift['non_reg_balance'],
                         no_gift['non_reg_balance'])

    def test_gift_self_limits_at_the_childs_registered_room(self):
        # A gift LARGER than the room funds only the room; the excess is not
        # transferred (the donor keeps it -- self-limiting). Room = $16k FHSA +
        # $7k TFSA = $23k; own savings = $900; so at most $23k - $900 = $22,100
        # of a $100,000 gift can flow, and none reaches non-reg.
        children = [_teen_saver()]
        gifts = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 100_000}]
        prior = _initial_child_accounts(children)
        own = child_savings_for_year(children, 0.10, 0.0, 0)
        gift = child_gift_funding_for_year(children, gifts, own, prior)
        self.assertAlmostEqual(gift[0], 23_000.0 - own[0], places=6,
                               msg="the gift is capped at the child's remaining "
                                   "registered room after their own savings")
        # And in a real run the gift never spills into the child's non-reg: the
        # first year fills the $23,000 room exactly ($22,100 gift + $900 own),
        # so non-reg stays 0 (any non-reg in LATER years is the child's OWN
        # savings once the room is full, never a gift dollar).
        sim = _run(children, gifts, years=1)
        self.assertEqual(_child(sim)['non_reg_balance'], 0.0,
                         "a self-limited gift never funds non-registered")

    def test_gift_does_not_change_household_after_tax_income(self):
        # Tax-free to both: a gift is NOT income to the child and NOT deductible
        # to the donor, so it enters no taxable-income term. The household's
        # after-tax income each year is therefore BIT-IDENTICAL with and without
        # the gift -- if the gift were wrongly taxed (or attributed under
        # s.74.1), this would move.
        gift = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 20_000}]
        no_gift = _run([_teen_saver()])
        with_gift = _run([_teen_saver()], gift)
        for i, (a, b) in enumerate(zip(no_gift.results, with_gift.results)):
            self.assertEqual(
                repr(a.after_tax_income), repr(b.after_tax_income),
                msg=f"year {i}: a gift must not change any tax -- it is tax-free "
                    f"to both the child (not income) and the donor (not "
                    f"deductible)")

    def test_no_gifts_is_a_noop_on_the_child_accounts(self):
        # The golden-shaped no-op: declaring no gifts leaves the child accounts
        # exactly where bite 2 put them (funded by the child's OWN income only).
        base = _child(_run([_teen_saver()]))
        empty = _child(_run([_teen_saver()], []))
        self.assertEqual(base, empty)

    def test_a_gift_targets_only_its_named_child(self):
        # Two children, one gift: the UN-gifted sibling is a modelled zero for
        # the gift (declared 0 -> no funding), while the named child's room fills.
        # The gift is per-recipient, never spread across every child.
        ca = _teen_saver(id='ca')
        cb = _teen_saver(id='cb')
        gift = [{'id': 'g1', 'from': 'p1', 'to': 'ca', 'amount': 20_000}]
        sim = _run([ca, cb], gift)
        accts = sim._state.jurisdiction_state['canada']['child_accounts']
        self.assertGreater(_registered(accts[0]), 26_000.0,
                           "the named child (ca) got the gift")
        # cb was NOT gifted: its registered balance is only its own $900/yr.
        self.assertLess(_registered(accts[1]), 3_100.0,
                        "the un-named sibling (cb) is funded by their own income "
                        "alone -- the gift is not spread across children")


def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _two_generation_doc(gift):
    """The shipped example.json trimmed to the couple + their direct children
    (the sub-family the adapter can map, #598) with a gift injected -- the one
    known-valid contract document to exercise the real adapter against."""
    import copy
    with open(contract_schema.EXAMPLE_PATH) as fh:
        doc = json.load(fh)
    doc = copy.deepcopy(doc)
    keep = {"p1", "p2", "ca", "cb"}
    doc["people"] = [p for p in doc["people"] if p["id"] in keep]
    for p in doc["people"]:
        p["relationships"] = [r for r in p["relationships"] if r["person"] in keep]
    doc["accounts"] = [a for a in doc["accounts"] if _owner_ids(a["owner"]) <= keep]
    doc["liabilities"] = [l for l in doc["liabilities"] if _owner_ids(l["owner"]) <= keep]
    doc["properties"] = [p for p in doc["properties"] if _owner_ids(p["owner"]) <= keep]
    doc["estate"]["rollover_overrides"] = [
        o for o in doc["estate"]["rollover_overrides"]
        if o["account"] in {a["id"] for a in doc["accounts"]}]
    doc["estate"]["life_insurance"] = [
        i for i in doc["estate"]["life_insurance"] if i["owner"] in keep]
    doc["assumptions"]["mortality"] = [
        m for m in doc["assumptions"]["mortality"] if m["person"] in keep]
    doc["gifts"] = [gift]
    return doc


class TestGiftEndpointsAreValidatedLoudly(unittest.TestCase):
    """DP#32: a gift naming an undeclared donor, or a recipient who is not a
    declared child, is a typo -- refused loudly, not a silently-dropped zero
    gift. Exercised through the real contract adapter (input_contract)."""

    def test_undeclared_donor_is_refused(self):
        from contract_errors import ContractAdaptationError
        from input_contract import to_internal_config
        doc = _two_generation_doc(
            {"id": "g1", "from": "nobody", "to": "ca", "amount": 20000})
        with self.assertRaises(ContractAdaptationError) as cm:
            to_internal_config(doc)
        self.assertIn("nobody", str(cm.exception))

    def test_recipient_that_is_not_a_child_is_refused(self):
        # p1 is an adult member, not a child -- a gift cannot fund an adult's
        # "child room" (there is none). Refused, not mapped to nothing.
        from contract_errors import ContractAdaptationError
        from input_contract import to_internal_config
        doc = _two_generation_doc(
            {"id": "g1", "from": "p2", "to": "p1", "amount": 20000})
        with self.assertRaises(ContractAdaptationError) as cm:
            to_internal_config(doc)
        self.assertIn("p1", str(cm.exception))

    def test_a_valid_gift_reaches_the_internal_config(self):
        from input_contract import to_internal_config
        doc = _two_generation_doc(
            {"id": "g1", "from": "p1", "to": "ca", "amount": 20000})
        cfg = to_internal_config(doc)
        gifts = cfg["family"]["gifts"]
        self.assertEqual(len(gifts), 1)
        self.assertEqual(gifts[0]["from"], "p1")
        self.assertEqual(gifts[0]["to"], "ca")
        self.assertEqual(gifts[0]["amount"], 20000.0)


if __name__ == '__main__':
    unittest.main()
