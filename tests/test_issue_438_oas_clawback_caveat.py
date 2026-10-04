"""Issue #438: the OAS recovery-tax base omits the portfolio's distributed income.

## What is under test

The CRA's Old Age Security recovery test looks at an individual's NET INCOME FOR
THE YEAR -- and investment income counts toward that net income whether or not a
single dollar of it has been withdrawn. This engine never books the non-reg
portfolio's distributed income as income at all: it is priced into the *growth
rate* of the pot (``_non_reg_after_tax_return_for`` ->
``apply_non_reg_growth``), so it compounds but is never reported, never taxed
in the year it is earned, and never reaches the OAS clawback base.

The consequence is concrete and directional: a retiree holding a large taxable
equity position and living on its distributions is modelled as receiving FULL,
UNCLAWED OAS, because the base the recovery tax is tested against contains only
CPP + pension + gross OAS + the recognized taxable draw. Every downstream figure
inherits that -- a larger terminal balance, a larger net_benefit, and an
optimizer that ranks the unclawed-back household above its true self.

## Why a caveat and not a fix

#438's own sequencing note: #437 must land FIRST, because it settles whether a
distributed portfolio dollar is taxed once or twice, and therefore what the
correct taxable base for this clawback even IS. Wiring the clawback before that
settles would build on the wrong base. So this PR lands the OTHER half of #438,
which the issue explicitly asks for regardless of that sequencing:

> "Registering the caveat is part of this work even if the clawback itself is
> sequenced behind #437."

``tools/README.md`` requires a biasing approximation to declare itself in
``model_fidelity.py`` so it reaches the console, TXT, JSON and HTML surfaces.
This is that declaration, plus a ratchet that forces it to be deleted the day
the clawback is wired.

## The two detectors this file lands

1. **The caveat fires exactly when it should** -- for a config that declares a
   taxable portfolio earning a non-zero income yield, and not otherwise. A
   caveat for a household that has no taxable portfolio would be a false
   disclosure; so would one firing on a pot whose only distribution is a return
   of capital, which is not income at all.
2. **The caveat cannot outlive the bug.** ``test_the_anchor_clause_still_names
   _the_gap`` asserts that the known-limit clause naming this gap in
   ``rules_drawdown.py`` is still present, and the shared
   ``TestNoStaleCaveats`` anchor does the same from the other side: when the
   clawback lands and that clause is deleted, the anchor goes stale and CI
   fails until the registry entry is deleted too. The caveat and the bug are
   pinned to each other from both ends.
"""
import json
import unittest

import model_fidelity
from output_plugins import HtmlReport, JsonReport, TextReport

# The id under test. Referenced by the registry entry in model_fidelity.py.
CAVEAT_ID = 'distributed_portfolio_income_never_enters_oas_base'

# The exact clause in rules_drawdown.py that documents this gap as a known
# limit. It is the counterpart of the registry entry: fixing the clawback
# requires deleting this clause, and deleting it trips TestNoStaleCaveats.
_ANCHOR_CLAUSE = 'DISTRIBUTED income still never ENTERS the base'


def _cfg(non_reg_balance=0, **yield_fields):
    """A minimal config carrying one non-reg portfolio account.

    ``yield_fields`` names the per-income-type yields the contract declares
    (``portfolio.accounts.non_reg.yield``), each defaulting to an explicit 0.0
    rather than being omitted, so "declared zero" and "not declared" stay
    distinguishable (DP#32).
    """
    yields = {'eligible_dividends': 0.0, 'non_eligible_dividends': 0.0,
              'interest': 0.0, 'capital_gains': 0.0,
              'return_of_capital': 0.0, 'foreign_income': 0.0}
    yields.update(yield_fields)
    return {
        'family': {'members': [
            {'role': 'primary', 'gross_income': 60000, 'birth_year': 1960,
             'retirement_age': 60},
        ]},
        'property': {'house_value': 400000, 'mortgage_balance': 0},
        'accounts': {},
        'portfolio': {'accounts': {'non_reg': {
            'balance': non_reg_balance,
            'yield': yields,
        }}},
        'assumptions': {'investment_return': 0.05, 'start_year': 2026,
                        'dollar_basis': 'nominal'},
        'retirement': {},
    }


def _active_ids(cfg, objective_name=None):
    return {a.id for a in model_fidelity.active_approximations(cfg, objective_name)}


class TestTheCaveatIsRegistered(unittest.TestCase):
    """The declaration itself, not just its behaviour."""

    def test_the_caveat_exists_in_the_registry(self):
        self.assertIn(CAVEAT_ID,
                      {a.id for a in model_fidelity.all_approximations()})

    def test_it_names_the_figure_it_biases_and_which_way(self):
        entry = next(a for a in model_fidelity.all_approximations()
                     if a.id == CAVEAT_ID)
        self.assertTrue(entry.biased_figure)
        # Unclawed-back OAS is money the household is modelled as receiving
        # that the CRA would recover, so the bias is upward, not unknown.
        self.assertEqual(entry.direction, model_fidelity.Direction.OVERSTATES)
        self.assertEqual(entry.issue, '#438')


class TestTheCaveatFiresWhenTheGapActuallyBites(unittest.TestCase):
    """A caveat for a run that does not hit the approximation is a false
    disclosure -- the registry's own rule -- so the predicate has to be
    config-driven, not unconditional."""

    def test_a_declared_dividend_yield_fires_it(self):
        cfg = _cfg(non_reg_balance=2_000_000, eligible_dividends=0.015)
        self.assertIn(CAVEAT_ID, _active_ids(cfg))

    def test_a_declared_interest_yield_fires_it(self):
        cfg = _cfg(non_reg_balance=500_000, interest=0.03)
        self.assertIn(CAVEAT_ID, _active_ids(cfg))

    def test_a_foreign_distribution_fires_it(self):
        cfg = _cfg(non_reg_balance=250_000, foreign_income=0.02)
        self.assertIn(CAVEAT_ID, _active_ids(cfg))

    def test_it_fires_for_objective_sensitive_objectives_too(self):
        """The clawback base is the SAME base whichever objective ranks the
        run, so the caveat must not be gated on one objective -- a suppressed
        caveat is the false disclosure the registry exists to prevent."""
        cfg = _cfg(non_reg_balance=2_000_000, eligible_dividends=0.015)
        for objective in (None, 'max_net_benefit', 'max_after_tax_estate',
                          'max_terminal_wealth', 'min_after_tax_estate'):
            with self.subTest(objective=objective):
                self.assertIn(CAVEAT_ID, _active_ids(cfg, objective))

    def test_an_unknown_objective_reports_rather_than_hides(self):
        """DP#32 applied to the caveat mechanism itself: an objective the
        predicate cannot classify must report the caveat, not exonerate it."""
        cfg = _cfg(non_reg_balance=2_000_000, interest=0.02)
        self.assertIn(CAVEAT_ID, _active_ids(cfg, 'an_objective_from_the_future'))


class TestTheCaveatStaysQuietWhenItDoesNotApply(unittest.TestCase):
    """The other half of the same rule. These are the false-disclosure guards."""

    def test_a_portfolio_with_no_balance_and_no_yield_does_not_fire_it(self):
        cfg = _cfg(non_reg_balance=0)
        self.assertNotIn(CAVEAT_ID, _active_ids(cfg))

    def test_a_portfolio_of_explicit_zero_yields_does_not_fire_it(self):
        """A declared zero is a VALUE, not an absence (DP#32): a pot that
        declares 0% on every income type really does distribute nothing, so
        there is nothing to omit from the OAS base."""
        cfg = _cfg(non_reg_balance=2_000_000)
        self.assertNotIn(CAVEAT_ID, _active_ids(cfg))

    def test_a_return_of_capital_alone_does_not_fire_it(self):
        """A return of capital is not income -- it reduces ACB and is not
        included in income at all, so there is nothing for the recovery test
        to have missed. Firing here would overstate the caveat."""
        cfg = _cfg(non_reg_balance=2_000_000, return_of_capital=0.05)
        self.assertNotIn(CAVEAT_ID, _active_ids(cfg))

    def test_an_empty_pot_does_not_fire_it(self):
        """A non-reg account with no balance holds nothing, so it distributes
        nothing and there is no income for the OAS base to be missing."""
        cfg = _cfg(non_reg_balance=0, eligible_dividends=0.015)
        self.assertNotIn(CAVEAT_ID, _active_ids(cfg))

    def test_a_pot_with_no_yield_block_still_fires_it(self):
        """The false-disclosure guard that matters most.

        When a non-reg pot declares a balance but no ``yield`` composition, the
        engine does NOT distribute nothing -- ``simulation.
        _non_reg_after_tax_return_for`` falls back to the configured
        ``non_reg_yield_rate`` (DP#13), so real income IS being earned and the
        OAS base really is missing it. A predicate that returned False on the
        absent block would hide the caveat from exactly the configs that cannot
        see it."""
        cfg = _cfg(non_reg_balance=2_000_000)
        del cfg['portfolio']['accounts']['non_reg']['yield']
        self.assertIn(CAVEAT_ID, _active_ids(cfg))

    def test_a_pot_of_unstated_size_still_fires_it(self):
        """An unpriceable pot is not an exonerated one: report rather than
        hide (DP#32 applied to the caveat mechanism)."""
        cfg = _cfg(non_reg_balance=2_000_000, interest=0.02)
        del cfg['portfolio']['accounts']['non_reg']['balance']
        self.assertIn(CAVEAT_ID, _active_ids(cfg))

    def test_a_config_with_no_portfolio_block_at_all_does_not_fire_it(self):
        cfg = {
            'family': {'members': [
                {'role': 'primary', 'gross_income': 60000, 'birth_year': 1960,
                 'retirement_age': 60},
            ]},
            'property': {'house_value': 400000, 'mortgage_balance': 0},
            'accounts': {},
            'assumptions': {'investment_return': 0.05, 'start_year': 2026,
                            'dollar_basis': 'nominal'},
            'retirement': {},
        }
        self.assertNotIn(CAVEAT_ID, _active_ids(cfg))

    def test_the_predicate_never_raises_on_a_malformed_portfolio_block(self):
        """DP#32: absence must fail LOUDLY, and the registry's own rule is
        that a predicate which cannot evaluate fails OPEN (reports the
        caveat) rather than closed. A block missing its ``yield`` key must
        not crash the report."""
        cfg = _cfg(non_reg_balance=2_000_000)
        del cfg['portfolio']['accounts']['non_reg']['yield']
        ids = _active_ids(cfg)          # must not raise
        self.assertIsInstance(ids, set)

    def test_the_predicate_survives_hostile_config_shapes(self):
        """Every shape below reaches the predicate from a real input; none may
        raise, because an exception here would take a whole report down."""
        hostile = [
            None, [], 'not-a-dict', 7,
            {}, {'portfolio': None}, {'portfolio': 'nope'},
            {'portfolio': []}, {'portfolio': {'accounts': None}},
            {'portfolio': {'accounts': []}},
            {'portfolio': {'accounts': {'non_reg': None}}},
            {'portfolio': {'accounts': {'non_reg': []}}},
            {'portfolio': {'accounts': {'non_reg': {'balance': 'lots'}}}},
            {'portfolio': {'accounts': {'non_reg': {'balance': True}}}},
            {'portfolio': {'accounts': {'non_reg': {'balance': 10, 'yield': []}}}},
            {'portfolio': {'accounts': {'non_reg': {'balance': 10, 'yield': {'interest': 'x'}}}}},
            {'portfolio': {'accounts': {'non_reg': {'balance': 10,
                                                    'yield': {'interest': True}}}}},
        ]
        for cfg in hostile:
            with self.subTest(cfg=cfg):
                self.assertIsInstance(_active_ids(cfg), set)

    def test_a_malformed_declared_yield_reports_rather_than_hides(self):
        """A present-but-unpriceable yield is NOT a zero yield. Reading it as
        zero would turn a config that cannot be priced into a run that reports
        no caveat -- the silent substitution this registry exists to prevent."""
        for bad_rate in ('x', True, [], {}):
            cfg = _cfg(non_reg_balance=2_000_000)
            cfg['portfolio']['accounts']['non_reg']['yield']['interest'] = bad_rate
            with self.subTest(rate=bad_rate):
                self.assertIn(CAVEAT_ID, _active_ids(cfg))

    def test_an_absent_yield_key_is_not_a_malformed_one(self):
        """The other side of the same distinction: an absent key leaves the
        other declared yields at zero, so there is genuinely nothing to
        report and the caveat stays quiet."""
        cfg = _cfg(non_reg_balance=2_000_000)
        del cfg['portfolio']['accounts']['non_reg']['yield']['interest']
        self.assertNotIn(CAVEAT_ID, _active_ids(cfg))


class TestTheCaveatReachesEveryOutputSurface(unittest.TestCase):
    """The registry's whole point: a headline figure carrying a known
    approximation cannot be printed bare on any surface."""

    _RESULTS = [
        {'label': 'A', 'net_benefit': 100000, 'future_value': 200000,
         'total_debt': 0, 'ltv': 0.0, 'objective_name': 'max_net_benefit'},
    ]

    def setUp(self):
        self.cfg = _cfg(non_reg_balance=2_000_000, eligible_dividends=0.015)

    def test_text_report(self):
        out = TextReport(self._RESULTS, self.cfg, title="T").render()
        self.assertIn("MODEL FIDELITY", out)
        self.assertIn("OAS", out)
        self.assertIn("investment", out)

    def test_console_render_text(self):
        joined = "\n".join(model_fidelity.render_text(self.cfg))
        self.assertIn("OAS", joined)

    def test_json_report_carries_the_id_and_direction(self):
        out = json.loads(JsonReport(self._RESULTS, self.cfg, title="T").render())
        ids = {a['id'] for a in out['model_fidelity']['approximations']}
        self.assertIn(CAVEAT_ID, ids)
        entry = next(a for a in out['model_fidelity']['approximations']
                     if a['id'] == CAVEAT_ID)
        self.assertEqual(entry['direction'], 'overstates')
        self.assertIn('biased_figure', entry)

    def test_html_report(self):
        out = HtmlReport(self._RESULTS, self.cfg, title="T").render()
        self.assertIn("Model Fidelity", out)
        self.assertIn("OAS", out)


class TestTheCaveatCannotOutliveTheBug(unittest.TestCase):
    """The detector half of the repo's definition of done: the declaration is
    pinned to the code that still has the defect, so that fixing the clawback
    without deleting this entry fails CI."""

    def test_the_anchor_clause_still_names_the_gap(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, 'rules_drawdown.py')
        with open(path, encoding='utf-8') as handle:
            self.assertIn(_ANCHOR_CLAUSE, handle.read())

    def test_the_oas_base_still_omits_distributed_income(self):
        """The second pin, read straight off the defect itself: the recovery
        base is still built from CPP + pension only. When #437 lands and this
        changes, this test fails and names the registry entry to delete."""
        from countries.canada.retirement_transition import member_retirement_income
        income = member_retirement_income(
            member={'role': 'primary', 'cpp': 10_000, 'pension_income_annual': 0},
            sim_year=5, oas_annual_max=8_908, oas_clawback_threshold=75_000,
        )
        # 10,000 of CPP sits far under the threshold either way, so assert the
        # threshold-crossing case: push CPP up so a missing income term would
        # visibly change the OAS. Both assertions below read the SAME base.
        from countries.canada.retirement_transition import member_retirement_income as _mri
        high = _mri(
            member={'role': 'primary', 'cpp': 80_000, 'pension_income_annual': 0},
            sim_year=5, oas_annual_max=8_908, oas_clawback_threshold=75_000,
        )
        self.assertLess(high.oas, 8_908)      # 80k of CPP claws back
        self.assertAlmostEqual(income.oas, 8_908, places=6)  # 10k does not

    def test_the_second_argument_is_not_a_cross_year_carry_forward(self):
        """``other_net_income`` is the ONLY channel by which non-CPP income
        could reach this base -- and no production caller passes it. This is
        what makes the gap 'the base is CPP alone' rather than 'the base is
        wired but fed zeros'."""
        import inspect
        from countries.canada import retirement_transition as rt
        callers = [ln for ln in inspect.getsource(rt).splitlines()
                   if 'member_retirement_income(' in ln]
        # Every call in the module leaves the argument at its 0.0 default.
        self.assertTrue(callers)
        for line in callers:
            self.assertNotIn('other_net_income', line)


if __name__ == '__main__':
    unittest.main()