#!/usr/bin/env python3
"""Epic #841 bite 4: a pluggable objective that ranks strategies by the WHOLE
FAMILY's after-tax net worth / estate -- ONE number across ALL members
(DP#22: the optimizer ranks, it does not choose).

Bites 1/2 (#844/#812) made each child a first-class savings subject with their
OWN registered/non-registered accounts, threaded through the per-year fold on
``jurisdiction_state['canada']['child_accounts']`` but DELIBERATELY kept OUT of
``total_assets()`` (the household view). This bite adds
``max_family_after_tax_networth``: the household's after-tax estate (both
adults, reusing ``max_after_tax_estate`` unchanged) PLUS each child's OWN
after-tax net worth -- the piece the family total must add back.

What these tests pin:
  - the objective is registered and selectable by name (DP#22);
  - a strategy that GROWS a child's wealth ranks ABOVE one that does not when
    the household totals are otherwise equal (child wealth is counted);
  - the household side is untouched -- ``max_after_tax_estate`` ignores the
    child accounts, and for a household with NO child-savers the family
    objective EQUALS the estate exactly (so the golden household -- RESP-only
    children -- ranks identically on either, and its invariant cannot move);
  - every member is valued on the SAME deemed-disposition rules: a child's
    TFSA/FHSA pass tax-free, a child's RRSP is taxed as ordinary income at
    death (no new tax invented -- the jurisdiction primitives are reused).

DP#15: fabricated round numbers and role-based names only (``_teen_saver``,
``child_a``) -- no personal data, ever. DP#4: round figures.
"""

import unittest

import countries.canada  # noqa: F401  (ensures the estate provider is registered)
from objective import (
    MAX_FAMILY_AFTER_TAX_NETWORTH, MAX_AFTER_TAX_ESTATE, get_objective,
    _family_after_tax_networth,
)
from year_result import YearResult


# A Quebec terminal return in a fixed tax year, so the deemed-disposition
# brackets are pinned and the arithmetic is deterministic (DP#4).
CFG = {'tax': {'province': 'quebec', 'year': 2026}}


def _yr(child_accounts=None, **kwargs) -> YearResult:
    """A single terminal YearResult with round, fabricated numbers (DP#4/#15)."""
    defaults = dict(
        primary_rrsp=0.0, spouse_rrsp=0.0, spousal_rrsp=0.0,
        total_tfsa=0.0, non_reg_balance=0.0, non_reg_acb=0.0,
        lif_balance=0.0, lira_balance=0.0,
        mortgage_balance=0.0, heloc_balance=0.0, total_debt=0.0,
        total_assets=0.0,
    )
    defaults.update(kwargs)
    yr = YearResult(**defaults)
    if child_accounts is not None:
        yr.child_accounts = child_accounts
    return yr


def _child(rrsp=0.0, tfsa=0.0, fhsa=0.0, non_reg=0.0, non_reg_acb=0.0) -> dict:
    """One child's OWN accounts, shaped like the bite-2 child_accounts dict."""
    return {
        'rrsp_balance': rrsp, 'tfsa_balance': tfsa, 'fhsa_balance': fhsa,
        'non_reg_balance': non_reg, 'non_reg_acb': non_reg_acb,
    }


class TestRegistration(unittest.TestCase):
    """DP#22: the objective is data, selectable by name."""

    def test_family_objective_is_registered(self):
        self.assertIs(get_objective('max_family_after_tax_networth'),
                      MAX_FAMILY_AFTER_TAX_NETWORTH)

    def test_description_states_it_spans_all_members(self):
        desc = MAX_FAMILY_AFTER_TAX_NETWORTH.description.lower()
        self.assertIn('family', desc)
        self.assertIn('child', desc)


class TestChildWealthMovesTheFamilyRanking(unittest.TestCase):
    """The headline: growing a child's wealth ranks a strategy higher on the
    FAMILY objective, when the two households are otherwise identical."""

    #: identical household balance sheet for both strategies (adults only)
    HOUSEHOLD = dict(total_tfsa=500_000, total_assets=500_000)

    def test_growing_a_childs_wealth_ranks_higher(self):
        no_growth = [_yr(child_accounts=[_child(tfsa=0)], **self.HOUSEHOLD)]
        grew_child = [_yr(child_accounts=[_child(tfsa=100_000)], **self.HOUSEHOLD)]

        fam = MAX_FAMILY_AFTER_TAX_NETWORTH
        self.assertGreater(fam.evaluate(grew_child, CFG),
                           fam.evaluate(no_growth, CFG))
        # A tax-free TFSA dollar moves the family total dollar-for-dollar.
        self.assertEqual(
            fam.evaluate(grew_child, CFG) - fam.evaluate(no_growth, CFG),
            100_000)

    def test_the_household_view_is_blind_to_the_childs_wealth(self):
        """Same two strategies score IDENTICALLY on the household estate --
        proving the divergence above is the CHILD term, not a household
        difference. total_assets()/max_after_tax_estate never see the child
        accounts (bite 2)."""
        no_growth = [_yr(child_accounts=[_child(tfsa=0)], **self.HOUSEHOLD)]
        grew_child = [_yr(child_accounts=[_child(tfsa=100_000)], **self.HOUSEHOLD)]
        est = MAX_AFTER_TAX_ESTATE
        self.assertEqual(est.evaluate(no_growth, CFG),
                         est.evaluate(grew_child, CFG))


class TestFamilyEqualsEstateWithoutChildSavers(unittest.TestCase):
    """Opt-in: for a household with no child-savers the family objective is the
    estate exactly -- so the golden household (RESP-only children) ranks
    identically on either and its invariant cannot move."""

    HOUSEHOLD = dict(primary_rrsp=400_000, total_tfsa=100_000,
                     total_assets=500_000)

    def test_no_child_accounts_at_all(self):
        results = [_yr(**self.HOUSEHOLD)]  # child_accounts defaults to []
        self.assertEqual(results[-1].child_accounts, [])
        self.assertEqual(MAX_FAMILY_AFTER_TAX_NETWORTH.evaluate(results, CFG),
                         MAX_AFTER_TAX_ESTATE.evaluate(results, CFG))

    def test_all_zero_child_savers_are_a_modelled_zero(self):
        """RESP-only children still get an all-zero child_accounts entry (a
        modelled zero saver, bite 1/2) -- it must add exactly 0.0, not fail."""
        results = [_yr(child_accounts=[_child(), _child()], **self.HOUSEHOLD)]
        self.assertEqual(MAX_FAMILY_AFTER_TAX_NETWORTH.evaluate(results, CFG),
                         MAX_AFTER_TAX_ESTATE.evaluate(results, CFG))


class TestEveryMemberIsValuedOnTheSameDeathTax(unittest.TestCase):
    """No new tax is invented: a child is valued on the SAME deemed-disposition
    rules the adults' estate uses."""

    HOUSEHOLD = dict(total_tfsa=500_000, total_assets=500_000)

    def test_child_tfsa_and_fhsa_pass_tax_free(self):
        tfsa_kid = [_yr(child_accounts=[_child(tfsa=80_000)], **self.HOUSEHOLD)]
        fhsa_kid = [_yr(child_accounts=[_child(fhsa=80_000)], **self.HOUSEHOLD)]
        fam = MAX_FAMILY_AFTER_TAX_NETWORTH
        base = fam.evaluate([_yr(child_accounts=[_child()], **self.HOUSEHOLD)], CFG)
        # both add their full face value -- no death tax on TFSA or FHSA
        self.assertEqual(fam.evaluate(tfsa_kid, CFG) - base, 80_000)
        self.assertEqual(fam.evaluate(fhsa_kid, CFG) - base, 80_000)

    def test_child_rrsp_is_deemed_disposed_as_ordinary_income(self):
        """$1 of a child's RRSP is worth strictly less than $1 of their TFSA --
        the RRSP is fully included on the child's terminal return (ITA
        s.146(8.8)), the TFSA passes tax-free."""
        rrsp_kid = [_yr(child_accounts=[_child(rrsp=100_000)], **self.HOUSEHOLD)]
        tfsa_kid = [_yr(child_accounts=[_child(tfsa=100_000)], **self.HOUSEHOLD)]
        fam = MAX_FAMILY_AFTER_TAX_NETWORTH
        self.assertLess(fam.evaluate(rrsp_kid, CFG), fam.evaluate(tfsa_kid, CFG))

    def test_child_non_reg_gain_is_taxed_but_capital_returns_are_not(self):
        """A child's non-reg account is taxed on its ACCRUED gain only (FMV -
        ACB), not on the returned capital -- exactly the adults' non-reg
        treatment. A fully-cost account (ACB == FMV) adds its full value."""
        fam = MAX_FAMILY_AFTER_TAX_NETWORTH
        base = fam.evaluate([_yr(child_accounts=[_child()], **self.HOUSEHOLD)], CFG)

        no_gain = [_yr(child_accounts=[_child(non_reg=50_000, non_reg_acb=50_000)],
                       **self.HOUSEHOLD)]
        self.assertEqual(fam.evaluate(no_gain, CFG) - base, 50_000)

        with_gain = [_yr(child_accounts=[_child(non_reg=50_000, non_reg_acb=10_000)],
                         **self.HOUSEHOLD)]
        # accrued gain -> some capital-gains tax -> adds strictly less than face
        self.assertLess(fam.evaluate(with_gain, CFG) - base, 50_000)
        self.assertGreater(fam.evaluate(with_gain, CFG) - base, 0.0)


class TestDegenerateInputs(unittest.TestCase):
    def test_empty_results_score_zero_via_evaluate(self):
        """ObjectiveFunction.evaluate short-circuits an empty projection to 0.0
        before ever calling the objective."""
        self.assertEqual(MAX_FAMILY_AFTER_TAX_NETWORTH.evaluate([], CFG), 0.0)

    def test_raw_objective_fn_is_empty_safe(self):
        """The objective's own fn is empty-safe INDEPENDENTLY of evaluate's
        guard: the children term short-circuits to a hard zero (no terminal
        year to read), mirroring compute_after_tax_estate's empty-results
        behaviour (DP#32: a modelled zero, not an IndexError)."""
        self.assertEqual(_family_after_tax_networth([], CFG), 0.0)


class TestMultipleChildrenAreSummed(unittest.TestCase):
    def test_two_child_savers_both_count(self):
        household = dict(total_tfsa=500_000, total_assets=500_000)
        fam = MAX_FAMILY_AFTER_TAX_NETWORTH
        one = [_yr(child_accounts=[_child(tfsa=30_000)], **household)]
        two = [_yr(child_accounts=[_child(tfsa=30_000), _child(tfsa=20_000)],
                   **household)]
        self.assertEqual(fam.evaluate(two, CFG) - fam.evaluate(one, CFG), 20_000)


class TestEndToEndChildSaverRun(unittest.TestCase):
    """A real per-year fold run: a child-saver household's family objective
    STRICTLY exceeds its household estate, because the fold actually grows the
    child's accounts and threads them onto the terminal YearResult."""

    def _run(self, children):
        from simulation_config import SimulationConfig
        from countries.canada.adapter import CanadaAdapter
        from simulation import FamilySimulation
        from strategy import AllocationStrategy
        cfg = SimulationConfig(
            projection_years=5, house_value=0, mortgage_balance=0,
            mortgage_rate=0.0, amortization_years=25, margin_available=0,
            savings_rate=0.10, living_costs=0.0, start_year=2026,
            province='quebec', investment_return=0.05, salary_growth=0.0,
            family_members=[{'role': 'primary', 'birth_year': 1980,
                             'gross_income': 120_000, 'id': 'p1',
                             'rrsp_room_accumulated': 50_000,
                             'tfsa_room_accumulated': 50_000}],
            children=children)
        # A declared child TFSA target so allocate_child routes the child's
        # savings into the child's TFSA (grows child wealth deterministically).
        strategy = AllocationStrategy(child_tfsa_pct=1.0)
        sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg), strategy=strategy)
        results = sim.run()
        return results, cfg.to_dict() if hasattr(cfg, 'to_dict') else {}

    def test_family_exceeds_household_estate_for_a_child_saver(self):
        # A teen saver with income and TFSA room -> the fold funds their TFSA.
        teen_saver = {'role': 'child', 'birth_year': 2008, 'id': 'c1',
                      'gross_income': 12_000, 'tfsa_room_accumulated': 20_000}
        results, run_cfg = self._run([teen_saver])
        run_cfg.setdefault('tax', {'province': 'quebec', 'year': 2026})

        final = results[-1]
        # bite 4 wiring: the child accounts reached the terminal YearResult...
        self.assertTrue(final.child_accounts)
        child_tfsa = final.child_accounts[0]['tfsa_balance']
        self.assertGreater(child_tfsa, 0.0)

        fam = MAX_FAMILY_AFTER_TAX_NETWORTH.evaluate(results, run_cfg)
        est = MAX_AFTER_TAX_ESTATE.evaluate(results, run_cfg)
        # ...and the family objective counts them, the estate does not.
        self.assertGreater(fam, est)
        # the child's TFSA is tax-free, so the whole gap IS the child's TFSA
        self.assertAlmostEqual(fam - est, child_tfsa, places=6)


if __name__ == '__main__':
    unittest.main()
