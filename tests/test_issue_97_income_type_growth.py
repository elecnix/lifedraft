#!/usr/bin/env python3
"""Tests for issue #97: Simulation step should apply income-type-specific growth
and tax treatment per portfolio composition (DP#27).

The simulation previously applied a flat investment_return rate to all accounts.
This test verifies that:
1. Non-reg investments use income-type-specific after-tax returns when portfolio
   data is available
2. Registered accounts (RRSP, TFSA, FHSA) still grow at the gross rate
3. Without portfolio data, the simulation falls back to the flat rate (backward compat)
4. The portfolio module correctly computes after-tax returns for different income types
"""

import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock

from countries.canada.portfolio import (
    PortfolioConfig, AccountPortfolio, YieldBreakdown, CompositionBreakdown,
)
from countries.canada.income_type import IncomeType, effective_tax_rate


class TestNonRegAfterTaxReturn:
    """Test that simulate_year_pure uses income-type-specific after-tax returns
    for non-reg investments (DP#27)."""

    def test_simulate_year_pure_with_non_reg_atr(self):
        """When non_reg_after_tax_return is provided, non-reg grows at that rate
        instead of the flat investment_return."""
        from simulation_state import SimState, simulate_year_pure
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 130000, 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 50000, 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
        )
        state = SimState.initial(config)
        state.non_reg_balance = 100000
        state.non_reg_acb = 80000

        allocations = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                       'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                       'non_reg': 0, 'resp': 0,
                       '_primary_income': 130000, '_spouse_income': 50000, '_annual_savings': 0}

        # Without non_reg_after_tax_return: flat rate
        result_flat, _ = simulate_year_pure(
            state=state, year=0, allocations=allocations, config=config,
            investment_return=0.07,
            use_readvanceable=False, mortgage_data={'end_balance': 0},
        )
        flat_non_reg = result_flat.non_reg_balance

        # With non_reg_after_tax_return=0.04 (lower than gross 0.07)
        # Non-reg should grow at 4% instead of 7%
        state2 = SimState.initial(config)
        state2.non_reg_balance = 100000
        state2.non_reg_acb = 80000

        result_atr, _ = simulate_year_pure(
            state=state2, year=0, allocations=allocations, config=config,
            investment_return=0.07,
            non_reg_after_tax_return=0.04,
            use_readvanceable=False, mortgage_data={'end_balance': 0},
        )
        atr_non_reg = result_atr.non_reg_balance

        # Non-reg should grow at 4% (after-tax) instead of 7% (gross)
        # 100000 * 1.04 = 104000
        assert atr_non_reg == pytest.approx(104000, abs=1)

        # Without portfolio data, non-reg grows at 7% (gross)
        # 100000 * 1.07 = 107000
        assert flat_non_reg == pytest.approx(107000, abs=1)

        # The after-tax version should be lower
        assert atr_non_reg < flat_non_reg

    def test_simulate_year_pure_atr_none_falls_back_to_gross(self):
        """When non_reg_after_tax_return is None, falls back to flat investment_return."""
        from simulation_state import SimState, simulate_year_pure
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 130000, 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 50000, 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
        )
        state = SimState.initial(config)
        state.non_reg_balance = 100000
        state.non_reg_acb = 80000

        allocations = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                       'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                       'non_reg': 0, 'resp': 0,
                       '_primary_income': 130000, '_spouse_income': 50000, '_annual_savings': 0}

        # None should fall back to investment_return
        result, _ = simulate_year_pure(
            state=state, year=0, allocations=allocations, config=config,
            investment_return=0.07,
            non_reg_after_tax_return=None,
            use_readvanceable=False, mortgage_data={'end_balance': 0},
        )

        assert result.non_reg_balance == pytest.approx(107000, abs=1)

    def test_registered_accounts_always_grow_at_gross_rate(self):
        """RRSP, TFSA, and FHSA grow at gross investment_return regardless
        of non_reg_after_tax_return (they're tax-sheltered)."""
        from simulation_state import SimState, simulate_year_pure
        from simulation_config import SimulationConfig

        config = SimulationConfig(
            investment_return=0.07,
            family_members=[
                {'role': 'primary', 'gross_income': 130000, 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                {'role': 'spouse', 'gross_income': 50000, 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
        )
        state = SimState.initial(config)
        # DP#25: Canada-specific fields are in jurisdiction_state['canada']
        state.jurisdiction_state['canada']['adult_rrsp']['primary']['own'] = 50000  # #700
        state.jurisdiction_state['canada']['adult_tfsa']['primary']['balance'] = 30000  # #700
        state.non_reg_balance = 20000

        allocations = {'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
                       'primary_tfsa': 0, 'spouse_tfsa': 0, 'fhsa': 0,
                       'non_reg': 0, 'resp': 0,
                       '_primary_income': 130000, '_spouse_income': 50000, '_annual_savings': 0}

        # With low non-reg after-tax return
        result, _ = simulate_year_pure(
            state=state, year=0, allocations=allocations, config=config,
            investment_return=0.07,
            non_reg_after_tax_return=0.03,
            use_readvanceable=False, mortgage_data={'end_balance': 0},
        )

        # RRSP and TFSA grow at 7% (gross, tax-sheltered)
        assert result.primary_rrsp == pytest.approx(50000 * 1.07, abs=1)
        assert result.primary_tfsa == pytest.approx(30000 * 1.07, abs=1)
        # Non-reg grows at 3% (after-tax)
        assert result.non_reg_balance == pytest.approx(20000 * 1.03, abs=1)


class TestPortfolioAfterTaxReturn:
    """Test PortfolioConfig and AccountPortfolio after-tax return computation."""

    def test_portfolio_config_from_dict(self):
        """PortfolioConfig loads from input.json portfolio section."""
        cfg_data = {
            'accounts': {
                'non_reg': {
                    'balance': 100000,
                    'cost_basis': 80000,
                    'composition': {
                        'cdn_equity_pct': 0.30,
                        'us_equity_pct': 0.30,
                        'intl_equity_pct': 0.10,
                        'fixed_income_pct': 0.30,
                    },
                    'yield': {
                        'eligible_dividends': 0.015,
                        'interest': 0.015,
                        'capital_gains': 0.025,
                        'foreign_income': 0.015,
                    }
                }
            }
        }
        portfolio = PortfolioConfig.from_dict(cfg_data)
        assert portfolio.has_data is True
        assert 'non_reg' in portfolio.accounts
        assert portfolio.accounts['non_reg'].balance == 100000

    def test_non_reg_after_tax_return_quebec(self):
        """Non-reg after-tax return in Quebec should account for eligible dividends DTC."""
        acct = AccountPortfolio(
            balance=100000,
            yield_breakdown=YieldBreakdown(
                eligible_dividends=0.02,
                interest=0.02,
                capital_gains=0.03,
                foreign_income=0.01,
            ),
        )
        mtr = 0.50  # 50% marginal rate in Quebec
        after_tax = acct.after_tax_return_by_account('non_reg', mtr, 'quebec')

        # Each income type should be taxed differently
        # Interest: 2% * (1 - 0.50) = 1.0%
        # Eligible dividends: ~2% * (1 - 0.30) = ~1.4% (lower due to DTC)
        # Capital gains: 3% * 1.0 = 3.0% (deferred, not taxed annually)
        # Foreign income: 1% * (1 - 0.65) ≈ 0.35%
        # Total should be around 5-6%
        assert after_tax > 0, f"After-tax return should be positive, got {after_tax}"
        assert after_tax < 0.08, f"After-tax return should be less than gross, got {after_tax}"

    def test_tfsa_grows_tax_free(self):
        """TFSA grows at the total yield (tax-free) regardless of income type."""
        acct = AccountPortfolio(
            balance=100000,
            yield_breakdown=YieldBreakdown(
                eligible_dividends=0.02,
                interest=0.03,
                capital_gains=0.02,
            ),
        )
        tfsa_return = acct.after_tax_return_by_account('tfsa', 0.50, 'quebec')
        # TFSA: all income is tax-free
        assert tfsa_return == pytest.approx(0.07, abs=0.001)  # Total yield = 7%

    def test_empty_portfolio_no_data(self):
        """Empty PortfolioConfig.has_data should be False."""
        portfolio = PortfolioConfig()
        assert portfolio.has_data is False

    def test_portfolio_round_trip(self):
        """DP#24: Portfolio config round-trips correctly."""
        cfg_data = {
            'accounts': {
                'non_reg': {
                    'balance': 50000,
                    'cost_basis': 40000,
                    'composition': {
                        'cdn_equity_pct': 0.40,
                        'us_equity_pct': 0.30,
                        'intl_equity_pct': 0.10,
                        'fixed_income_pct': 0.20,
                    },
                    'yield': {
                        'eligible_dividends': 0.015,
                        'interest': 0.01,
                        'capital_gains': 0.03,
                        'foreign_income': 0.01,
                    }
                }
            }
        }
        portfolio = PortfolioConfig.from_dict(cfg_data)
        exported = portfolio.to_dict()
        portfolio2 = PortfolioConfig.from_dict(exported)
        assert portfolio2.accounts['non_reg'].balance == 50000
        assert portfolio2.accounts['non_reg'].cost_basis == 40000
        assert portfolio2.accounts['non_reg'].yield_breakdown.eligible_dividends == pytest.approx(0.015)


class TestIncomeTypeEffectiveTaxRates:
    """Test that income-type-specific effective tax rates produce correct
    after-tax returns (DP#27 verification)."""

    def test_interest_taxed_at_mtr(self):
        """Interest income is fully taxable at marginal rate."""
        rate = effective_tax_rate(IncomeType.INTEREST, 0.50, 'quebec')
        assert rate == pytest.approx(0.50, abs=0.01)

    def test_eligible_dividends_lower_than_mtr(self):
        """Eligible dividends have lower effective rate due to DTC."""
        eligible_rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.50, 'quebec')
        interest_rate = effective_tax_rate(IncomeType.INTEREST, 0.50, 'quebec')
        assert eligible_rate < interest_rate, "Eligible dividends should be taxed less than interest"

    def test_capital_gains_lower_than_interest(self):
        """Capital gains at 50% inclusion should be half the MTR (approx)."""
        cg_rate = effective_tax_rate(IncomeType.CAPITAL_GAIN, 0.50, 'quebec')
        interest_rate = effective_tax_rate(IncomeType.INTEREST, 0.50, 'quebec')
        assert cg_rate < interest_rate, "Capital gains should be taxed less than interest"

    def test_readvance_investment_profitability_depends_on_income_type(self):
        """The after-tax return differential between non-reg (with income type)
        and deductible-interest Smith Manoeuvre depends on the income type mix.
        
        This is the key insight from issue #97: a flat 7% return makes the
        Smith Manoeuvre comparison wrong because the after-tax return varies
        significantly by income type.
        """
        mtr = 0.53  # ~53% combined marginal rate in Quebec

        # Non-reg with different compositions
        interest_only = YieldBreakdown(interest=0.07)
        dividends_only = YieldBreakdown(eligible_dividends=0.035)
        growth_only = YieldBreakdown(capital_gains=0.07)
        mixed = YieldBreakdown(eligible_dividends=0.015, interest=0.02, capital_gains=0.035, foreign_income=0.01)

        acct_int = AccountPortfolio(balance=100000, yield_breakdown=interest_only)
        acct_div = AccountPortfolio(balance=100000, yield_breakdown=dividends_only)
        acct_growth = AccountPortfolio(balance=100000, yield_breakdown=growth_only)
        acct_mixed = AccountPortfolio(balance=100000, yield_breakdown=mixed)

        int_atr = acct_int.after_tax_return_by_account('non_reg', mtr, 'quebec')
        div_atr = acct_div.after_tax_return_by_account('non_reg', mtr, 'quebec')
        growth_atr = acct_growth.after_tax_return_by_account('non_reg', mtr, 'quebec')
        mixed_atr = acct_mixed.after_tax_return_by_account('non_reg', mtr, 'quebec')

        # Interest-only: highest tax drag (fully taxed at MTR)
        # Growth-heavy: lowest tax drag (capital gains deferred)
        # Note: yields differ (interest 7% vs dividends 3.5% vs growth 7%),
        # so we compare tax rates, not absolute returns.
        int_eff = effective_tax_rate(IncomeType.INTEREST, mtr, 'quebec')
        div_eff = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        growth_eff = effective_tax_rate(IncomeType.CAPITAL_GAIN, mtr, 'quebec')
        assert int_eff > div_eff, "Interest should have higher effective tax rate than eligible dividends"
        assert div_eff > growth_eff, "Dividends should have higher effective tax rate than capital gains"
        assert mixed_atr > int_atr or mixed_atr > 0.04, "Mixed portfolio should have reasonable after-tax return"

        # The key point: at 53% MTR, HELOC interest after tax deduction costs:
        # 4.95% * (1 - 0.53) = 2.33%
        # Interest-only non-reg returns 7% * (1 - 0.53) = 3.29% after tax
        # Capital-gains-heavy returns 7% * (1 - 0.265) ≈ 5.15% after tax (deferred)
        # The profitability of Smith Manoeuvre DEPENDS on income type mix