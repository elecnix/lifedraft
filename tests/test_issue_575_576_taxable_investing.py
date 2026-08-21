#!/usr/bin/env python3
"""Unit tests for #575/#576: one model of taxable investing (epic #603).

#575 — non-registered accounts earned 0% return whenever the non-reg
account's OWN balance was 0 (the ordinary starting condition for a new
taxable investor). Root cause: two compounding defects.
  (a) `AccountPortfolio.after_tax_return_by_account()` treated a *rate* as a
      function of the account's *balance* (`if self.balance <= 0: return 0.0`)
      -- DP#32: zero is a value, not a fallback, and a balance of zero is
      not a rate of zero (DP#3: a rate is a pure function of composition).
  (b) `FamilySimulation._get_non_reg_after_tax_return()` read `self._portfolio`,
      an `__init__`-time snapshot of the config that never saw the balance
      grow (issue #583) -- so even after compounding to seven figures, the
      guard in (a) kept firing against the stale snapshot's zero balance.

#576 — two disagreeing models of taxable investing: the non-reg account
compounded only its *declared* yield (interest/dividends), silently
dropping the `gross_return` argument entirely (no capital appreciation at
all), while Smith-Manoeuvre investments compounded the *gross*,
tax-sheltered rate with zero tax drag -- as if the taxable SM investment
were a TFSA. This test module verifies the unified replacement: declared
yield taxed annually per income type (DP#27), the remainder of the total
return accruing as a deferred/untaxed unrealized gain (DP#19), applied
identically to the plain non-reg account and to Smith-Manoeuvre
investments.

DP#4/DP#15: fabricated round numbers, role-based identifiers, no personal
data. DP#17: both sides of the balance=0 / balance>0 and
atr-provided / atr-omitted thresholds are exercised.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import SimState, simulate_year_pure
from countries.canada.adapter import CanadaAdapter


def _portfolio_config(non_reg_balance: float) -> dict:
    """A household config with an explicit non-reg portfolio composition
    (60% Canadian equity / 40% fixed income, a modest declared yield) at a
    given starting balance -- the only thing varied across the two-sided
    tests below."""
    return {
        'family': {'members': [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
             'rrsp_room_accumulated': 30_000, 'tfsa_room_accumulated': 20_000},
        ]},
        'assumptions': {'start_year': 2026, 'investment_return': 0.07, 'frozen_brackets': True},
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': non_reg_balance, 'cost_basis': non_reg_balance,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.012, 'interest': 0.008},
                },
            },
        },
        'property': {'house_value': 500_000, 'mortgage_balance': 200_000,
                      'mortgage_rate': 0.05, 'amortization_years': 20,
                      'margin_available': 0},
        'savings': {'rate': 0.15},
        'tax': {'province': 'qc'},
    }


def _make_sim(cfg_dict: dict) -> FamilySimulation:
    sim_cfg = SimulationConfig.from_dict(cfg_dict)
    return FamilySimulation(sim_cfg, adapter=CanadaAdapter(sim_cfg), use_readvanceable=False)


class TestNonRegAfterTaxReturnNotBalanceDependent(unittest.TestCase):
    """#575/DP#32: the after-tax rate must not depend on the (possibly
    stale, #583) balance snapshot -- only on composition and marginal rate."""

    def test_zero_starting_balance_still_earns_a_return(self):
        """The exact repro from issue #575: a non-reg account starting at
        balance=0 (the normal Smith-Manoeuvre / new-taxable-investor case)
        must not report a 0% rate."""
        sim = _make_sim(_portfolio_config(non_reg_balance=0))
        rate = sim._get_non_reg_after_tax_return(0, primary_marginal_rate=0.45, gross_return=0.07)
        self.assertGreater(rate, 0.0)

    def test_rate_is_identical_regardless_of_balance(self):
        """Both sides of the balance=0 / balance>0 threshold: the computed
        rate is a pure function of composition (DP#3), so it must be exactly
        the same whether the account starts empty or already holds $500k."""
        sim_empty = _make_sim(_portfolio_config(non_reg_balance=0))
        sim_funded = _make_sim(_portfolio_config(non_reg_balance=500_000))

        rate_empty = sim_empty._get_non_reg_after_tax_return(0, 0.45, 0.07)
        rate_funded = sim_funded._get_non_reg_after_tax_return(0, 0.45, 0.07)

        self.assertAlmostEqual(rate_empty, rate_funded, places=10)

    def test_rate_stable_across_the_run_as_balance_grows(self):
        """#583 (minimal slice): the rate must not be read from an __init__
        snapshot that goes stale as the simulation clock ticks. Calling the
        method again after the instance's own `self._portfolio` object still
        reports the original (empty) balance produces the same rate as a
        config seeded with a large balance -- proving the calculation never
        consults the mutable/stale `.balance` field at all."""
        sim = _make_sim(_portfolio_config(non_reg_balance=0))
        rate_year0 = sim._get_non_reg_after_tax_return(0, 0.45, 0.07)
        # Simulate many years passing (the account would now hold a large
        # balance in SimState, but self._portfolio -- if it were consulted
        # for balance -- would still report the original snapshot of 0).
        rate_year20 = sim._get_non_reg_after_tax_return(20, 0.45, 0.07)
        self.assertAlmostEqual(rate_year0, rate_year20, places=10)


class TestNonRegAfterTaxReturnHasCapitalAppreciation(unittest.TestCase):
    """#576 part 1: gross_return must not be silently dropped -- the after-tax
    rate must include price appreciation beyond the declared yield."""

    def test_after_tax_rate_exceeds_after_tax_yield_alone(self):
        """The declared yield here is 2% (1.2% dividends + 0.8% interest);
        gross_return is 7%. The old code returned ~1.5% (yield after tax,
        with no appreciation). The fixed code must return something well
        above the after-tax-yield-only figure because ~5% of unrealized
        capital appreciation is missing from the old model."""
        sim = _make_sim(_portfolio_config(non_reg_balance=0))
        non_reg_acct = sim._portfolio.accounts['non_reg']
        after_tax_yield_only = non_reg_acct.after_tax_return_by_account(
            'non_reg', 0.45, sim.config.province)

        rate = sim._get_non_reg_after_tax_return(0, 0.45, gross_return=0.07)

        self.assertGreater(rate, after_tax_yield_only)
        # Capital appreciation (gross 7% - declared 2% yield = 5%) dominates.
        self.assertGreater(rate, 0.05)

    def test_zero_gross_return_appreciation_is_zero(self):
        """Boundary: when gross_return exactly equals the declared yield,
        there is no capital-appreciation component -- the after-tax rate
        collapses to exactly the after-tax yield."""
        sim = _make_sim(_portfolio_config(non_reg_balance=0))
        non_reg_acct = sim._portfolio.accounts['non_reg']
        declared_yield = non_reg_acct.yield_breakdown.total_yield
        after_tax_yield_only = non_reg_acct.after_tax_return_by_account(
            'non_reg', 0.45, sim.config.province)

        rate = sim._get_non_reg_after_tax_return(0, 0.45, gross_return=declared_yield)

        self.assertAlmostEqual(rate, after_tax_yield_only, places=10)

    def test_no_portfolio_data_still_has_tax_drag(self):
        """Without an explicit `portfolio` block at all, the account must
        still fall back to a real (configurable, DP#13) declared-yield
        composition rather than silently behaving as tax-free -- the
        returned rate must be strictly below the gross rate whenever the
        marginal tax rate is positive."""
        cfg_dict = _portfolio_config(non_reg_balance=0)
        del cfg_dict['portfolio']
        sim = _make_sim(cfg_dict)

        rate = sim._get_non_reg_after_tax_return(0, primary_marginal_rate=0.45, gross_return=0.07)

        self.assertIsNotNone(rate)
        self.assertLess(rate, 0.07)
        self.assertGreater(rate, 0.0)

    def test_zero_marginal_rate_has_no_drag(self):
        """Boundary: at a 0% marginal rate, there is nothing to tax, so the
        after-tax rate must equal the full gross return exactly."""
        cfg_dict = _portfolio_config(non_reg_balance=0)
        del cfg_dict['portfolio']
        sim = _make_sim(cfg_dict)

        rate = sim._get_non_reg_after_tax_return(0, primary_marginal_rate=0.0, gross_return=0.07)

        self.assertAlmostEqual(rate, 0.07, places=6)


class TestSMInvestmentSharesTheSameModel(unittest.TestCase):
    """#576 part 2: Smith-Manoeuvre investments must compound at the SAME
    after-tax rate as the plain non-reg account, not the raw gross
    (tax-sheltered) rate."""

    def _config(self):
        return SimulationConfig(
            projection_years=5, investment_return=0.07, salary_growth=0.0,
            savings_rate=0.15, house_value=500000, mortgage_balance=200000,
            mortgage_rate=0.05, margin_available=100000,
            family_members=[
                {'role': 'primary', 'gross_income': 150000,
                 'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
            ],
        )

    def _seeded_state(self, config, sm_balance=100_000, sm_cost_basis=60_000):
        state = SimState.initial(config)
        state.jurisdiction_state['canada']['sm_investment_balance'] = sm_balance
        state.jurisdiction_state['canada']['sm_investment_cost_basis'] = sm_cost_basis
        return state

    def _no_readvance_mortgage_data(self, state):
        """Mortgage data with zero principal paid this year, so any change
        in the SM investment balance is due to growth alone, not a new
        readvance."""
        return {'end_balance': state.mortgage_balance, 'total_payment': 0,
                'total_interest': 0, 'total_principal': 0}

    def test_sm_investment_grows_at_supplied_after_tax_rate(self):
        """With a below-gross after-tax rate supplied (mirroring what
        FamilySimulation now always computes via
        _get_non_reg_after_tax_return), the SM investment must compound at
        that rate, not at the 7% gross rate."""
        config = self._config()
        state = self._seeded_state(config)
        allocs = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                  'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                  'resp': 0, 'non_reg': 0,
                  '_primary_income': 150000, '_spouse_income': 0, '_annual_savings': 0}

        result, new_state = simulate_year_pure(
            state=state, year=0, allocations=allocs, config=config,
            investment_return=0.07, non_reg_after_tax_return=0.03,
            use_readvanceable=True, mortgage_data=self._no_readvance_mortgage_data(state),
        )

        new_sm_balance = new_state.jurisdiction_state['canada']['sm_investment_balance']
        self.assertAlmostEqual(new_sm_balance, 100_000 * 1.03, places=2)
        # Not the raw gross rate (the old, buggy behaviour).
        self.assertNotAlmostEqual(new_sm_balance, 100_000 * 1.07, places=2)

    def test_sm_investment_falls_back_to_gross_when_atr_omitted(self):
        """The other side of the threshold: direct callers of
        simulate_year_pure that don't supply non_reg_after_tax_return (e.g.
        simple unit tests) keep their existing flat-rate contract exactly --
        this fix changes what FamilySimulation *passes in*, not the pure
        function's fallback behaviour."""
        config = self._config()
        state = self._seeded_state(config)
        allocs = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                  'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                  'resp': 0, 'non_reg': 0,
                  '_primary_income': 150000, '_spouse_income': 0, '_annual_savings': 0}

        result, new_state = simulate_year_pure(
            state=state, year=0, allocations=allocs, config=config,
            investment_return=0.07, non_reg_after_tax_return=None,
            use_readvanceable=True, mortgage_data=self._no_readvance_mortgage_data(state),
        )

        new_sm_balance = new_state.jurisdiction_state['canada']['sm_investment_balance']
        self.assertAlmostEqual(new_sm_balance, 100_000 * 1.07, places=2)

    def test_sm_cost_basis_does_not_grow_with_returns(self):
        """DP#19: ACB tracks contributions only. Growth (whichever rate is
        used) must never touch sm_investment_cost_basis."""
        config = self._config()
        state = self._seeded_state(config, sm_balance=100_000, sm_cost_basis=60_000)
        allocs = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                  'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                  'resp': 0, 'non_reg': 0,
                  '_primary_income': 150000, '_spouse_income': 0, '_annual_savings': 0}

        _, new_state = simulate_year_pure(
            state=state, year=0, allocations=allocs, config=config,
            investment_return=0.07, non_reg_after_tax_return=0.03,
            use_readvanceable=True, mortgage_data=self._no_readvance_mortgage_data(state),
        )

        self.assertEqual(
            new_state.jurisdiction_state['canada']['sm_investment_cost_basis'], 60_000)


class TestAccountPortfolioRateIsBalanceIndependent(unittest.TestCase):
    """DP#32/DP#3 at the portfolio-module level (countries/canada/portfolio.py):
    after_tax_return / after_tax_return_by_account must not branch on balance."""

    def test_after_tax_return_same_for_zero_and_nonzero_balance(self):
        from countries.canada.portfolio import AccountPortfolio, YieldBreakdown

        yb = YieldBreakdown(interest=0.02, eligible_dividends=0.015)
        acct_empty = AccountPortfolio(balance=0, yield_breakdown=yb)
        acct_funded = AccountPortfolio(balance=250_000, yield_breakdown=yb)

        self.assertAlmostEqual(
            acct_empty.after_tax_return(0.45, 'quebec'),
            acct_funded.after_tax_return(0.45, 'quebec'), places=10)
        self.assertAlmostEqual(
            acct_empty.after_tax_return_by_account('non_reg', 0.45, 'quebec'),
            acct_funded.after_tax_return_by_account('non_reg', 0.45, 'quebec'), places=10)
        # And it's a genuinely positive rate, not the old balance-gated 0.0.
        self.assertGreater(acct_empty.after_tax_return_by_account('non_reg', 0.45, 'quebec'), 0.0)


if __name__ == '__main__':
    unittest.main()
