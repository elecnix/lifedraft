#!/usr/bin/env python3
"""Issue #704: a child can become a first-time home buyer.

Canada gives a first-time home buyer two instruments, both correctly built in
``countries/canada/fhsa.py`` / ``countries/canada/hbp_rules.py`` but -- until
this issue -- wired to no first-home purchase for any member:

  * **FHSA** qualifying withdrawal: the whole FHSA balance comes out TAX-FREE for
    a first home and the account closes.
  * **HBP** (Home Buyers' Plan): up to $60,000 comes out of the RRSP NON-TAXABLY
    and is repaid over a 15-year schedule.

A child now becomes a full saver (#856/#857/#893), so a child's own FHSA/RRSP can
fund the child's own first home. ``first_home_purchases`` on the config declares
``{'buyer': <child id>, 'year': <int>}``; the fold
(``apply_child_first_home_purchases``) fires both withdrawals in that year,
landing the proceeds in the child's cash as the down payment (net worth
conserved) and tracking the HBP repayment.

DP#32: a household that declares NO first-home purchase (the golden household) is
byte-identical to today -- ``TestAbsenceIsNoOp`` and the golden invariant guard
that.
"""

import copy
import json
import unittest

import countries.canada  # noqa: F401


def _run(children, first_home_purchases, *, years=3, savings_rate=0.0,
         investment_return=0.0, salary_growth=0.0, primary_income=120_000):
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
        children=children,
        first_home_purchases=first_home_purchases)
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg), strategy=None)
    sim.run()
    return sim


def _child(sim, i=0):
    return sim._state.jurisdiction_state['canada']['child_accounts'][i]


# A child born 2005 is an adult (age 21+) across the projection: eligible to hold
# and withdraw from an FHSA/RRSP. gross_income 0 keeps room/tax noise out.
def _buyer_child(**over):
    base = {'name': 'child_a', 'birth_year': 2005, 'id': 'c1', 'gross_income': 0}
    base.update(over)
    return base


class TestFhsaQualifyingWithdrawal(unittest.TestCase):

    def test_fhsa_withdrawn_tax_free_and_account_closes(self):
        # $40k FHSA, buys in 2027. The qualifying withdrawal is TAX-FREE: the
        # FULL $40k lands in the child's cash (nothing lost to tax/withholding),
        # and the FHSA closes to $0.
        sim = _run([_buyer_child(fhsa_balance=40_000)],
                   [{'buyer': 'c1', 'year': 2027}], years=3)
        acc = _child(sim)
        self.assertEqual(acc['fhsa_balance'], 0.0,
                         msg="FHSA must be emptied by the qualifying withdrawal")
        self.assertEqual(acc['fhsa_lifetime_remaining'], 0.0,
                         msg="the FHSA closes after a qualifying withdrawal")
        self.assertEqual(acc['non_reg_balance'], 40_000.0,
                         msg="the full FHSA balance funds the down payment "
                             "tax-free (no withholding)")


class TestHbpWithdrawalAndRepayment(unittest.TestCase):

    def test_hbp_withdrawal_non_taxable_and_capped_at_60k(self):
        # $100k RRSP, buys in 2027. HBP withdraws the $60k maximum NON-TAXABLY:
        # RRSP drops to $40k and the full $60k funds the down payment.
        sim = _run([_buyer_child(rrsp_balance=100_000)],
                   [{'buyer': 'c1', 'year': 2027}], years=3)
        acc = _child(sim)
        self.assertEqual(acc['rrsp_balance'], 40_000.0,
                         msg="HBP withdraws min(RRSP, $60k) from the RRSP")
        self.assertEqual(acc['non_reg_balance'], 60_000.0,
                         msg="the HBP withdrawal is non-taxable -- the full "
                             "amount funds the down payment")

    def test_hbp_tracks_a_15_year_repayment_schedule(self):
        sim = _run([_buyer_child(rrsp_balance=100_000)],
                   [{'buyer': 'c1', 'year': 2027}], years=3)
        hbp = _child(sim)['hbp']
        self.assertEqual(hbp['withdrawal'], 60_000.0)
        self.assertEqual(len(hbp['repayment_schedule']), 15,
                         msg="the HBP repays over 15 years")
        self.assertAlmostEqual(
            sum(r['actual_payment'] for r in hbp['repayment_schedule']),
            60_000.0, places=2,
            msg="the schedule repays the whole withdrawal")
        # Repayment starts the 3rd year after a 2027 withdrawal (2030); a 3-year
        # run ends 2028, so nothing is repaid yet -- outstanding is still full.
        self.assertEqual(hbp['outstanding'], 60_000.0)

    def test_hbp_repays_over_time_reducing_outstanding(self):
        # Run long enough (through 2032) to cross the 2030 repayment start: the
        # outstanding balance must have fallen below the withdrawal, and the RRSP
        # must have received the repayments back (net worth conserved).
        sim = _run([_buyer_child(rrsp_balance=100_000)],
                   [{'buyer': 'c1', 'year': 2027}], years=7)
        acc = _child(sim)
        hbp = acc['hbp']
        self.assertGreater(hbp['repaid'], 0.0,
                           msg="repayments must have started by 2030")
        self.assertLess(hbp['outstanding'], 60_000.0)
        self.assertAlmostEqual(hbp['repaid'] + hbp['outstanding'], 60_000.0,
                               places=2)
        # The repaid dollars moved cash -> RRSP: RRSP rose above the $40k left
        # after the withdrawal by exactly what was repaid.
        self.assertAlmostEqual(acc['rrsp_balance'], 40_000.0 + hbp['repaid'],
                               places=2)


class TestFhsaAndHbpSameHome(unittest.TestCase):

    def test_both_instruments_fund_one_purchase_without_double_count(self):
        # FHSA ($30k) and HBP ($60k of a $100k RRSP) for the SAME home: the down
        # payment is exactly their sum ($90k), each instrument counted once.
        sim = _run([_buyer_child(fhsa_balance=30_000, rrsp_balance=100_000)],
                   [{'buyer': 'c1', 'year': 2027}], years=3)
        acc = _child(sim)
        self.assertEqual(acc['fhsa_balance'], 0.0)
        self.assertEqual(acc['rrsp_balance'], 40_000.0)
        self.assertEqual(acc['non_reg_balance'], 90_000.0,
                         msg="FHSA + HBP fund one down payment, no double-count")


class TestAbsenceIsNoOp(unittest.TestCase):

    def test_no_purchase_declared_is_byte_identical(self):
        # The SAME child with the SAME opening accounts, run with and without a
        # declared purchase, must match in every child-account field when no
        # purchase is declared -- a first-home purchase is purely additive.
        kids = lambda: [_buyer_child(fhsa_balance=40_000, rrsp_balance=100_000)]
        absent = _child(_run(kids(), [], years=5))
        # sanity: the no-purchase child keeps its FHSA and RRSP untouched by #704
        self.assertEqual(absent['fhsa_balance'], 40_000.0)
        self.assertEqual(absent['rrsp_balance'], 100_000.0)
        self.assertNotIn('hbp', absent,
                         msg="no HBP record exists without a declared purchase")

    def test_purchase_for_a_different_child_leaves_this_child_untouched(self):
        # Two children; only c2 buys. c1's accounts must be byte-identical to the
        # no-purchase case (the pass keys on the child's id).
        sim = _run([_buyer_child(id='c1', fhsa_balance=40_000),
                    _buyer_child(id='c2', fhsa_balance=25_000)],
                   [{'buyer': 'c2', 'year': 2027}], years=3)
        c1 = sim._state.jurisdiction_state['canada']['child_accounts'][0]
        self.assertEqual(c1['fhsa_balance'], 40_000.0)
        self.assertNotIn('hbp', c1)


def _owner_ids(owner):
    if isinstance(owner, dict):
        return {j["person"] for j in owner["joint"]}
    return {owner}


def _two_generation_doc(first_home_purchases):
    """The shipped example.json trimmed to the couple + their direct children --
    the one known-valid contract document -- with first_home_purchases injected,
    to exercise the real adapter (input_contract.to_internal_config)."""
    import input_contract as ic
    with open(ic.EXAMPLE_PATH) as fh:
        doc = copy.deepcopy(json.load(fh))
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
    doc["first_home_purchases"] = first_home_purchases
    return doc


class TestFirstHomePurchaseReachesConfigViaContract(unittest.TestCase):
    """The declared first_home_purchases[] block reaches the internal config
    through the real adapter, and a buyer who is not a declared child is refused
    loudly (DP#32), not silently dropped."""

    def test_a_valid_child_buyer_reaches_the_internal_config(self):
        from input_contract import to_internal_config
        cfg = to_internal_config(
            _two_generation_doc([{"buyer": "ca", "year": 2035}]))
        purchases = cfg["family"]["first_home_purchases"]
        self.assertEqual(purchases, [{"buyer": "ca", "year": 2035}])

    def test_an_adult_buyer_reaches_the_internal_config(self):
        # p1 is an adult member. Issue #931 lifts the child-only restriction: an
        # adult first-time buyer is now ACCEPTED (routed to the adult FHSA/RRSP
        # pots), not refused.
        from input_contract import to_internal_config
        cfg = to_internal_config(
            _two_generation_doc([{"buyer": "p1", "year": 2035}]))
        self.assertEqual(cfg["family"]["first_home_purchases"],
                         [{"buyer": "p1", "year": 2035}])

    def test_a_buyer_that_matches_no_member_is_refused(self):
        # An id that is neither a declared child nor a declared adult is a typo:
        # refused loudly (DP#32), not silently dropped.
        from input_contract import to_internal_config, ContractAdaptationError
        with self.assertRaises(ContractAdaptationError) as cm:
            to_internal_config(
                _two_generation_doc([{"buyer": "nobody", "year": 2035}]))
        self.assertIn("nobody", str(cm.exception))

    def test_absent_block_is_an_empty_list(self):
        from input_contract import to_internal_config
        cfg = to_internal_config(_two_generation_doc([]))
        self.assertEqual(cfg["family"]["first_home_purchases"], [])


if __name__ == '__main__':
    unittest.main()
