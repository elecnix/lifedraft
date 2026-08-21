#!/usr/bin/env python3
"""Issue #701 (Step 6 of #643): tax a CHILD's own income as the child's OWN
individual return.

Steps 1-5 made each ADULT taxed individually via a loop over ``config.adults()``
(no joint filing). Step 6 finishes #701 for the other members: a child's own
income was previously folded into the household total and NEVER run through
``tax_on_income`` as an individual -- it funded the child's own accounts tax-free
at the gross amount. This step taxes each child on their OWN income, in the
CHILD's own (lower) bracket, and funds the child's own accounts from the
AFTER-TAX remainder.

Why the golden invariant is unchanged (stated explicitly): the golden household's
two children earn ``gross_income == 0``; ``tax_on_income(0) == 0`` leaves the
child savings term at 0 and ``total_assets()`` (which excludes child_accounts) is
untouched -- so ``9709753.139463063`` is byte-identical. This is verified by the
existing golden-trajectory guard, not re-run here; below is the behaviour proof
plus a ``TestAbsenceIsNoOp`` locking the zero-income child to bite 2.

DP#15: fabricated round numbers only. DP#9: the child uses the SAME
``tax_on_income`` seam as every adult -- one tax spelling, not a child-specific
copy.
"""

import unittest

import countries.canada  # noqa: F401
from tax_data import default_tax_provider
from tax_calculator import tax_on_income, marginal_rate
from simulation_state import (
    child_savings_for_year, child_after_tax_savings_for_year,
)


def _run(children, *, savings_rate=0.10, investment_return=0.0,
         salary_growth=0.0, years=1, primary_income=200_000):
    """Growth OFF (return=0), one year: the child's TFSA balance equals exactly
    the one contribution, so the assertion reads the after-tax funding directly.
    A HIGH-earning parent ($200k, 45.71% top bracket) makes the "taxed in the
    child's own lower bracket, not the parent's marginal rate" claim concrete."""
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
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg))
    sim.run()
    return sim


def _child(sim):
    return sim._state.jurisdiction_state['canada']['child_accounts'][0]


BRACKETS = default_tax_provider().get_combined_brackets(2026, 'quebec')


class TestChildTaxedIndividually(unittest.TestCase):

    def test_child_account_funded_from_after_tax_not_gross(self):
        # A child earning $20,000 with $50,000 TFSA room, parent earning $200k.
        # The child is taxed on their OWN return: $20,000 sits in Quebec's lowest
        # combined bracket (25.69%) -> $5,138 tax -> $14,862 after tax. With
        # growth off and one year, the child's TFSA holds exactly the after-tax
        # contribution: 14_862 * 0.10 = 1_486.20 -- NOT the gross 2_000.
        child_income = 20_000
        tax = tax_on_income(child_income, BRACKETS)
        after_tax = child_income - tax
        expected = after_tax * 0.10
        sim = _run([{'name': 'kid', 'birth_year': 2008, 'id': 'c1',
                     'gross_income': child_income,
                     'tfsa_room_accumulated': 50_000}])
        self.assertAlmostEqual(_child(sim)['tfsa_balance'], expected, places=6,
                               msg="the child's account is funded from AFTER-TAX "
                                   "income, not the gross")
        # It is strictly less than the gross funding would have been -- the tax
        # was actually taken.
        self.assertLess(_child(sim)['tfsa_balance'], child_income * 0.10)

    def test_child_taxed_in_own_lower_bracket_not_parents_marginal_rate(self):
        # The whole point of #701: the child is a SEPARATE taxpayer. Their
        # $20,000 is taxed in the child's OWN bracket (25.69%), NOT joined onto
        # the $200,000 parent's return at the parent's 45.71% marginal rate.
        child_income = 20_000
        child_rate = marginal_rate(child_income, BRACKETS)
        parent_rate = marginal_rate(200_000, BRACKETS)
        self.assertLess(child_rate, parent_rate,
                        "the child's own bracket is lower than the parent's")
        # The after-tax funding reflects the CHILD's rate: if the child had been
        # taxed at the parent's marginal rate the account would be smaller still.
        after_tax_child = (child_income - tax_on_income(child_income, BRACKETS))
        as_if_parent_rate = child_income * (1 - parent_rate)
        sim = _run([{'name': 'kid', 'birth_year': 2008, 'id': 'c1',
                     'gross_income': child_income,
                     'tfsa_room_accumulated': 50_000}])
        self.assertAlmostEqual(_child(sim)['tfsa_balance'],
                               after_tax_child * 0.10, places=6)
        self.assertGreater(after_tax_child * 0.10, as_if_parent_rate * 0.10,
                           msg="taxing the child in their own lower bracket "
                               "leaves MORE after-tax than a parent's marginal "
                               "rate would")

    def test_two_children_taxed_separately_each_in_own_bracket(self):
        # Two children with different incomes are each taxed on their OWN return
        # (never pooled): each account reflects that child's own after-tax income.
        sim = _run([{'name': 'a', 'birth_year': 2008, 'id': 'c1',
                     'gross_income': 12_000, 'tfsa_room_accumulated': 50_000},
                    {'name': 'b', 'birth_year': 2009, 'id': 'c2',
                     'gross_income': 30_000, 'tfsa_room_accumulated': 50_000}])
        accts = sim._state.jurisdiction_state['canada']['child_accounts']
        e0 = (12_000 - tax_on_income(12_000, BRACKETS)) * 0.10
        e1 = (30_000 - tax_on_income(30_000, BRACKETS)) * 0.10
        self.assertAlmostEqual(accts[0]['tfsa_balance'], e0, places=6)
        self.assertAlmostEqual(accts[1]['tfsa_balance'], e1, places=6)


class TestAfterTaxHelperMatchesTheAdultTaxSeam(unittest.TestCase):
    """DP#9: the child's after-tax savings use the SAME ``tax_on_income`` seam
    as the adult loop -- one spelling. This pins the helper's arithmetic."""

    def test_after_tax_helper_equals_gross_minus_own_tax(self):
        children = [{'id': 'c1', 'gross_income': 25_000},
                    {'id': 'c2', 'gross_income': 0}]
        at = child_after_tax_savings_for_year(children, 0.10, 0.0, 0, BRACKETS)
        self.assertAlmostEqual(
            at[0], (25_000 - tax_on_income(25_000, BRACKETS)) * 0.10, places=9)
        self.assertEqual(at[1], 0.0)


class TestAbsenceIsNoOp(unittest.TestCase):
    """A child earning 0 is byte-identical to bite 2 (gross == after-tax when the
    tax is 0). This is the same property that keeps the golden household's
    ``9709753.139463063`` invariant unmoved."""

    def test_zero_income_child_after_tax_equals_gross(self):
        children = [{'id': 'c1', 'gross_income': 0},
                    {'id': 'c2', 'gross_income': 0}]
        gross = child_savings_for_year(children, 0.10, 0.0, 0)
        after_tax = child_after_tax_savings_for_year(
            children, 0.10, 0.0, 0, BRACKETS)
        self.assertEqual(after_tax, gross,
                         msg="a zero-income child pays tax_on_income(0)==0, so "
                             "after-tax funding is byte-identical to bite 2")
        self.assertEqual(after_tax, [0.0, 0.0])

    def test_zero_income_child_account_is_unchanged_modelled_zero(self):
        sim = _run([{'name': 'kid', 'birth_year': 2012, 'id': 'c1',
                     'gross_income': 0, 'tfsa_room_accumulated': 5_000}])
        for key in ('rrsp_balance', 'tfsa_balance', 'fhsa_balance',
                    'non_reg_balance'):
            self.assertEqual(_child(sim)[key], 0.0,
                             msg=f"{key} of a zero-income child stays a modelled "
                                 f"0.0 -- Step 6 changed nothing for them")


if __name__ == '__main__':
    unittest.main()
