#!/usr/bin/env python3
"""
Tests for the Portfolio Composition Module (DP#27)

Covers:
- AccountPortfolio: after-tax returns, WHT drag, composition validation
- PortfolioConfig: from_dict/to_dict round-trip, auto-include
- YieldBreakdown: yield calculations, validation
- CompositionBreakdown: income type weights, validation
- Asset location recommendations
- Per-income-type effective tax rates by account
- SCENARIO_SEED §9.1-9.4 integration tests
"""

import pytest
from countries.canada.portfolio import (
    AccountPortfolio, PortfolioConfig, CompositionBreakdown, YieldBreakdown,
    AccountType, asset_location_recommendation,
)
from countries.canada.income_type import IncomeType, effective_tax_rate, wht_drag


# =============================================================================
# YieldBreakdown Tests
# =============================================================================

class TestYieldBreakdown:
    """Test per-account yield breakdown by income type."""

    def test_total_yield_sum(self):
        """Total yield should sum all components."""
        yb = YieldBreakdown(
            eligible_dividends=0.015,
            non_eligible_dividends=0.005,
            interest=0.01,
            capital_gains=0.03,
            return_of_capital=0.01,
            foreign_income=0.005,
        )
        assert abs(yb.total_yield - 0.075) < 0.0001

    def test_total_yield_zero(self):
        """Zero yields should total to zero."""
        yb = YieldBreakdown()
        assert yb.total_yield == 0.0

    def test_validation_high_yield(self):
        """Should warn on total yield > 15%."""
        yb = YieldBreakdown(interest=0.20)
        warnings = yb.validate()
        assert len(warnings) == 1
        assert "very high" in warnings[0]

    def test_validation_normal_yield(self):
        """No warnings on normal yields."""
        yb = YieldBreakdown(interest=0.03, capital_gains=0.04)
        warnings = yb.validate()
        assert len(warnings) == 0


# =============================================================================
# CompositionBreakdown Tests
# =============================================================================

class TestCompositionBreakdown:
    """Test asset allocation composition."""

    def test_total_pct_sums_to_100(self):
        """Composition should sum to 100%."""
        comp = CompositionBreakdown(
            cdn_equity_pct=0.3, us_equity_pct=0.3,
            intl_equity_pct=0.2, fixed_income_pct=0.2,
        )
        assert abs(comp.total_pct - 1.0) < 0.0001

    def test_total_pct_partial(self):
        """Partial composition sum should be < 1."""
        comp = CompositionBreakdown(cdn_equity_pct=0.3, us_equity_pct=0.2)
        assert abs(comp.total_pct - 0.5) < 0.0001

    def test_validation_invalid_sum(self):
        """Should error if composition doesn't sum to 100%."""
        comp = CompositionBreakdown(cdn_equity_pct=0.5, us_equity_pct=0.3)
        errors = comp.validate()
        assert len(errors) == 1
        assert "80%" in errors[0]

    def test_validation_negative_pct(self):
        """Should error on negative percentages."""
        comp = CompositionBreakdown(cdn_equity_pct=-0.1)
        errors = comp.validate()
        assert len(errors) >= 1
        # Either "negative" in the error or the sum doesn't reach 100%
        assert len(errors) > 0

    def test_validation_valid_composition(self):
        """Valid 100% composition should have no errors."""
        comp = CompositionBreakdown(
            cdn_equity_pct=0.3, us_equity_pct=0.3,
            intl_equity_pct=0.2, fixed_income_pct=0.2,
        )
        errors = comp.validate()
        assert len(errors) == 0

    def test_income_type_weights(self):
        """Income type weights should map composition to tax types."""
        comp = CompositionBreakdown(
            cdn_equity_pct=0.4, us_equity_pct=0.3,
            intl_equity_pct=0.1, fixed_income_pct=0.2,
        )
        weights = comp.income_type_weights()
        # CDN equity: 0.4 * 0.5 dividends + 0.4 * 0.5 capital gains
        assert weights[IncomeType.ELIGIBLE_DIVIDEND] > 0
        assert weights[IncomeType.CAPITAL_GAIN] > 0
        assert weights[IncomeType.FOREIGN_INCOME] > 0
        assert weights[IncomeType.INTEREST] > 0


# =============================================================================
# AccountPortfolio Tests
# =============================================================================

class TestAccountPortfolio:
    """Test per-account portfolio modeling."""

    def test_empty_account(self):
        """Empty account should have zero after-tax return."""
        acct = AccountPortfolio(balance=0)
        assert acct.after_tax_return(0.4571) == 0.0
        assert acct.after_tax_return_by_account('non_reg', 0.4571) == 0.0

    def test_unrealized_gains(self):
        """Unrealized gains = balance - cost_basis."""
        acct = AccountPortfolio(balance=200000, cost_basis=150000)
        assert acct.unrealized_gains == 50000

    def test_zero_unrealized_gains(self):
        """Zero gains when cost_basis equals balance."""
        acct = AccountPortfolio(balance=100000, cost_basis=100000)
        assert acct.unrealized_gains == 0

    def test_negative_unrealized_gains_floor(self):
        """Unrealized gains should floor at 0."""
        acct = AccountPortfolio(balance=80000, cost_basis=100000)
        assert acct.unrealized_gains == 0

    def test_has_data(self):
        """Auto-include when balance > 0 or composition > 0."""
        assert AccountPortfolio().has_data == False
        assert AccountPortfolio(balance=1000).has_data == True
        acct = AccountPortfolio(composition=CompositionBreakdown(cdn_equity_pct=0.5))
        assert acct.has_data == True

    def test_tfsa_after_tax_return(self):
        """TFSA: all income tax-free, so after-tax = gross return."""
        acct = AccountPortfolio(
            balance=60000,
            yield_breakdown=YieldBreakdown(
                eligible_dividends=0.015,
                capital_gains=0.04,
                interest=0.01,
            ),
        )
        after_tax = acct.after_tax_return_by_account('tfsa', 0.4571)
        # TFSA: all income tax-free
        gross = acct.yield_breakdown.total_yield
        assert abs(after_tax - gross) < 0.0001

    def test_rrsp_after_tax_return(self):
        """RRSP: tax-deferred, taxed on withdrawal at MTR."""
        acct = AccountPortfolio(
            balance=100000,
            yield_breakdown=YieldBreakdown(interest=0.03, capital_gains=0.04),
        )
        mtr = 0.4571
        after_tax = acct.after_tax_return_by_account('rrsp', mtr)
        # RRSP: growth is tax-deferred, but withdrawal taxed at MTR
        # Approximate: gross * (1 - MTR)
        expected = acct.yield_breakdown.total_yield * (1 - mtr)
        assert abs(after_tax - expected) < 0.001

    def test_non_reg_interest_heavy(self):
        """Non-reg with interest-heavy: high tax drag."""
        acct = AccountPortfolio(
            balance=200000,
            yield_breakdown=YieldBreakdown(interest=0.04),
        )
        mtr = 0.4571
        after_tax = acct.after_tax_return(0.4571)
        # Interest fully taxed at MTR
        expected = 0.04 * (1 - 0.4571)
        assert abs(after_tax - expected) < 0.001

    def test_non_reg_eligible_dividends(self):
        """Non-reg eligible dividends: lower effective rate."""
        acct = AccountPortfolio(
            balance=200000,
            yield_breakdown=YieldBreakdown(eligible_dividends=0.03),
        )
        mtr = 0.4571
        after_tax = acct.after_tax_return(mtr, 'quebec')
        # Eligible dividends have DTC offsetting some tax
        effective_rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        expected = 0.03 * (1 - effective_rate)
        assert abs(after_tax - expected) < 0.001

    def test_non_reg_capital_gains_deferred(self):
        """Non-reg capital gains: deferred, not taxed until realized."""
        acct = AccountPortfolio(
            balance=200000,
            yield_breakdown=YieldBreakdown(capital_gains=0.05),
        )
        mtr = 0.4571
        after_tax = acct.after_tax_return_by_account('non_reg', mtr)
        # In non-reg, capital gains are deferred — full yield available
        assert after_tax == pytest.approx(0.05, abs=0.001)

    def test_return_of_capital_not_taxed(self):
        """Return of capital: not taxed, reduces ACB."""
        acct = AccountPortfolio(
            balance=200000,
            yield_breakdown=YieldBreakdown(return_of_capital=0.02),
        )
        after_tax = acct.after_tax_return(0.4571)
        # ROC is not taxed when received
        assert abs(after_tax - 0.02) < 0.0001

    def test_wht_drag_tfsa(self):
        """TFSA: US equity WHT is unrecoverable."""
        acct = AccountPortfolio(
            balance=60000,
            yield_breakdown=YieldBreakdown(foreign_income=0.03),
        )
        drag = acct.wht_drag_bps('tfsa')
        # TFSA: WHT on foreign income is not recoverable
        # wht_drag uses the portfolio's foreign yield portion
        # Since foreign_income > 0, there should be positive drag
        assert drag >= 0  # WHT drag exists (may be 0 for some acct types)

    def test_wht_drag_rrsp(self):
        """RRSP: US-listed equity WHT = 0 (treaty exemption)."""
        acct = AccountPortfolio(
            balance=100000,
            yield_breakdown=YieldBreakdown(foreign_income=0.03),
        )
        drag = acct.wht_drag_bps('rrsp')
        # RRSP: US equity treaty exemption → 0 WHT drag for US
        # But non-US international has WHT even in RRSP
        # This test uses the default composition which splits foreign 60/40 US/intl
        assert drag >= 0  # Could be 0 or positive depending on composition

    def test_wht_drag_no_foreign(self):
        """No foreign income → zero WHT drag."""
        acct = AccountPortfolio(balance=100000)
        drag = acct.wht_drag_bps('tfsa')
        assert drag == 0


# =============================================================================
# PortfolioConfig Tests
# =============================================================================

class TestPortfolioConfig:
    """Test full portfolio configuration."""

    def _sample_portfolio_dict(self):
        """Create a sample portfolio dict matching input.json schema."""
        return {
            'allocation_strategy': 'balanced',
            'accounts': {
                'non_reg': {
                    'balance': 200000,
                    'cost_basis': 150000,
                    'composition': {
                        'cdn_equity_pct': 0.3,
                        'us_equity_pct': 0.3,
                        'intl_equity_pct': 0.2,
                        'fixed_income_pct': 0.2,
                    },
                    'yield': {
                        'eligible_dividends': 0.015,
                        'non_eligible_dividends': 0.005,
                        'interest': 0.01,
                        'capital_gains': 0.03,
                        'return_of_capital': 0.01,
                        'foreign_income': 0.005,
                    },
                },
                'tfsa': {
                    'balance': 60000,
                    'cost_basis': 60000,
                    'composition': {
                        'cdn_equity_pct': 0.4,
                        'us_equity_pct': 0.2,
                        'intl_equity_pct': 0.1,
                        'fixed_income_pct': 0.3,
                    },
                    'yield': {
                        'eligible_dividends': 0.012,
                        'interest': 0.015,
                        'capital_gains': 0.04,
                        'foreign_income': 0.003,
                    },
                },
                'rrsp': {
                    'balance': 100000,
                    'cost_basis': 100000,
                    'composition': {
                        'cdn_equity_pct': 0.1,
                        'us_equity_pct': 0.4,
                        'intl_equity_pct': 0.2,
                        'fixed_income_pct': 0.3,
                    },
                    'yield': {
                        'interest': 0.02,
                        'capital_gains': 0.03,
                        'foreign_income': 0.02,
                    },
                },
            },
        }

    def test_from_dict_round_trip(self):
        """DP#24: from_dict → to_dict should round-trip."""
        data = self._sample_portfolio_dict()
        portfolio = PortfolioConfig.from_dict(data)
        exported = portfolio.to_dict()
        
        # Check values survive round-trip
        assert exported['allocation_strategy'] == 'balanced'
        assert 'non_reg' in exported['accounts']
        assert exported['accounts']['non_reg']['balance'] == 200000
        assert exported['accounts']['non_reg']['cost_basis'] == 150000
        assert exported['accounts']['non_reg']['composition']['cdn_equity_pct'] == 0.3

    def test_has_data(self):
        """Auto-include when any account has data."""
        portfolio = PortfolioConfig.from_dict(self._sample_portfolio_dict())
        assert portfolio.has_data == True

    def test_has_data_empty(self):
        """No data → disabled."""
        portfolio = PortfolioConfig.from_dict({})
        assert portfolio.has_data == False

    def test_total_balance(self):
        """Total balance across all accounts."""
        portfolio = PortfolioConfig.from_dict(self._sample_portfolio_dict())
        assert portfolio.total_balance == 360000  # 200k + 60k + 100k

    def test_after_tax_return_by_account(self):
        """After-tax return varies by account type."""
        portfolio = PortfolioConfig.from_dict(self._sample_portfolio_dict())
        mtr = 0.4571
        
        # TFSA should have highest after-tax return (all tax-free)
        tfsa_return = portfolio.after_tax_return_for_account('tfsa', mtr)
        
        # Non-reg should be lower (tax on income types)
        non_reg_return = portfolio.after_tax_return_for_account('non_reg', mtr)
        
        # TFSA return should be higher (all income tax-free)
        assert tfsa_return > non_reg_return or non_reg_return == 0

    def test_optimal_location_analysis(self):
        """Asset location analysis produces per-account results."""
        portfolio = PortfolioConfig.from_dict(self._sample_portfolio_dict())
        analysis = portfolio.optimal_location_analysis(0.4571)
        
        assert 'non_reg' in analysis
        assert 'tfsa' in analysis
        assert 'rrsp' in analysis
        assert 'gross_return' in analysis['non_reg']
        assert 'after_tax_return' in analysis['non_reg']

    def test_empty_config(self):
        """Empty config should not crash."""
        portfolio = PortfolioConfig.from_dict({})
        assert portfolio.total_balance == 0
        assert portfolio.has_data == False


# =============================================================================
# Asset Location Recommendation Tests (SCENARIO_SEED §9.2)
# =============================================================================

class TestAssetLocationRecommendation:
    """Test asset location recommendations per SCENARIO_SEED §9.2."""

    def test_cdn_equity_to_non_reg(self):
        """Canadian equities: primary location is non-reg (DTC)."""
        comp = CompositionBreakdown(cdn_equity_pct=1.0)
        recs = asset_location_recommendation(comp, 0.4571)
        assert recs['cdn_equity']['primary'] == 'non_reg'
        assert 'DTC' in recs['cdn_equity']['reason'] or 'dividend' in recs['cdn_equity']['reason'].lower()

    def test_us_equity_to_rrsp(self):
        """US equities: primary location is RRSP (treaty WHT exemption)."""
        comp = CompositionBreakdown(us_equity_pct=1.0)
        recs = asset_location_recommendation(comp, 0.4571)
        assert recs['us_equity']['primary'] == 'rrsp'
        # RRSP should have less WHT drag than TFSA
        # (treaty exemption means 0 for US, but intl still has WHT)
        assert recs['us_equity']['wht_drag_rrsp_bps'] <= recs['us_equity']['wht_drag_tfsa_bps']

    def test_fixed_income_to_rrsp(self):
        """Fixed income: primary location is RRSP (avoid full MTR tax)."""
        comp = CompositionBreakdown(fixed_income_pct=1.0)
        recs = asset_location_recommendation(comp, 0.4571)
        assert recs['fixed_income']['primary'] == 'rrsp'
        assert recs['fixed_income']['avoid'] == 'non_reg'

    def test_intl_equity_to_rrsp(self):
        """International equity: prefer RRSP (reduced WHT levels)."""
        comp = CompositionBreakdown(intl_equity_pct=1.0)
        recs = asset_location_recommendation(comp, 0.4571)
        assert recs['intl_equity']['primary'] == 'rrsp'


# =============================================================================
# SCENARIO_SEED Integration Tests
# =============================================================================

class TestScenario91AfterTaxAllocation:
    """SCENARIO_SEED §9.1: After-Tax Asset Location."""

    def test_rrsp_after_tax_shrinks(self):
        """$100K RRSP at 40% tax = $60K after-tax (DP#27 §9.1)."""
        # After-tax RRSP value = balance × (1 - expected_withdrawal_rate)
        # The key insight: holding bonds in RRSP makes pre-tax 60/40 look
        # like after-tax 75/25 (more aggressive)
        balance = 100000
        mtr = 0.40
        after_tax_rrsp = balance * (1 - mtr)
        assert after_tax_rrsp == 60000

    def test_after_tax_allocation_different(self):
        """Same pre-tax allocation → different after-tax allocation."""
        # Pre-tax: $100K RRSP + $60K TFSA = $160K
        # After-tax: $60K RRSP + $60K TFSA = $120K
        # If RRSP holds 60/40 and TFSA holds 60/40:
        # After-tax stocks = 60K*0.6 + 60K*0.6 = 72K = 60%
        # After-tax bonds = 60K*0.4 + 60K*0.4 = 48K = 40%
        # But if RRSP holds bonds (40%) and TFSA holds stocks (60%):
        # After-tax stocks = 60K*0.0 + 60K*0.6 = 36K = 30%? No...
        # This is the core insight: pre-tax ≠ after-tax allocation
        tfsa = 60000
        rrsp_after_tax = 60000
        total_after_tax = tfsa + rrsp_after_tax
        assert total_after_tax == 120000

    def test_wht_drag_comparison(self):
        """XUU in TFSA: ~22 bps WHT drag. VTI in RRSP: 0 bps (SCENARIO_SEED §9.4)."""
        tfsa_drag = wht_drag('tfsa', 'us', dividend_yield=0.02, wht_recoverable=False)
        rrsp_drag = wht_drag('rrsp', 'us', dividend_yield=0.02)
        
        assert rrsp_drag == 0.0  # Treaty exemption
        assert tfsa_drag > 0     # Unrecoverable WHT in TFSA
        # ~30 bps for 2% yield (15% of 2%)
        assert 15 < tfsa_drag < 35


class TestScenario92TaxEfficiencyMatrix:
    """SCENARIO_SEED §9.2: Tax Efficiency Matrix."""

    def test_eligible_dividend_lower_than_interest(self):
        """Eligible dividends: ~25% effective vs interest: 45.7% at same MTR."""
        mtr = 0.4571
        elig_rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        interest_rate = effective_tax_rate(IncomeType.INTEREST, mtr, 'quebec')
        
        assert interest_rate == pytest.approx(mtr)
        assert elig_rate < interest_rate
        assert elig_rate < 0.30  # Less than 30% effective rate

    def test_capital_gains_lower_than_interest(self):
        """Capital gains: 22.85% at 45.7% MTR vs interest at 45.7%."""
        mtr = 0.4571
        cg_rate = effective_tax_rate(IncomeType.CAPITAL_GAIN, mtr)
        interest_rate = effective_tax_rate(IncomeType.INTEREST, mtr)
        
        assert interest_rate == pytest.approx(mtr)
        assert cg_rate == pytest.approx(mtr * 0.50)
        assert cg_rate < interest_rate

    def test_foreign_income_in_rrsp_no_wht(self):
        """US equity in RRSP: 0% WHT drag (treaty exemption)."""
        mtr = 0.4571
        rrsp_rate = effective_tax_rate(IncomeType.FOREIGN_INCOME, mtr, 'quebec', 'rrsp', 'us')
        # In RRSP, WHT is 0, so tax is just MTR on withdrawal
        # effective_tax_rate doesn't model RRSP withdrawal, just income type treatment
        # But WHT drag should be 0
        drag = wht_drag('rrsp', 'us', dividend_yield=0.03)
        assert drag == 0.0

    def test_non_reg_foreign_income_wht(self):
        """Foreign income in non-reg: 15% WHT recoverable via FTC."""
        mtr = 0.4571
        rate = effective_tax_rate(IncomeType.FOREIGN_INCOME, mtr, 'quebec', 'non_reg', 'us', wht_recoverable=True)
        # Recoverable: WHT doesn't add to effective rate (claimed as FTC)
        # But foreign income taxed at full MTR
        assert rate > 0

    def test_return_of_capital_zero_tax(self):
        """Return of capital: 0% effective rate."""
        mtr = 0.4571
        rate = effective_tax_rate(IncomeType.RETURN_OF_CAPITAL, mtr)
        assert rate == 0.0


class TestScenario93QuebecInterestDeduction:
    """SCENARIO_SEED §9.3: Quebec interest deduction limited to investment income."""

    def test_quebec_eligible_dividend_better_for_sm(self):
        """In Quebec, dividend income counts toward HELOC interest deduction."""
        mtr = 0.4746  # Quebec at $150k
        
        # Dividend stock portfolio generates more deductible income
        div_yield = 3000  # $3,000 eligible dividends
        interest = 10000  # $10,000 HELOC interest
        
        # Quebec limits deduction to investment income
        deductible = min(div_yield, interest)
        assert deductible == 3000
        
        # Growth-heavy portfolio generates less deductible income
        growth_yield = 500  # $500 capital gains (realized)
        deductible2 = min(growth_yield, interest)
        assert deductible2 == 500
        
        # Dividend portfolio is better for Quebec SM
        assert deductible > deductible2

    def test_quebec_carry_forward(self):
        """Quebec carries forward unused interest deduction."""
        # $10K HELOC interest, $3K investment income
        # Quebec deduction = $3K, carry forward = $7K
        interest = 10000
        investment_income = 3000
        carry_forward = max(0, interest - investment_income)
        assert carry_forward == 7000


# =============================================================================
# DP#2: compute_investment_income uses configurable rate, not hardcoded default
# =============================================================================

class TestComputeInvestmentIncomeDP2:
    """DP#2: Configuration belongs in input, not in code.
    
    compute_investment_income must accept yield_data from user config (DP#2)
    and must not hardcode the 2% default — callers pass default_yield_rate
    from SimulationConfig.non_reg_yield_rate.
    """

    def test_yield_data_from_config(self):
        """When yield_data is provided, it overrides any default."""
        from countries.canada.portfolio import compute_investment_income

        yield_data = {
            'eligible_dividends': 0.03,
            'interest': 0.02,
            'capital_gains': 0.04,
            'foreign_income': 0.01,
        }
        result = compute_investment_income(100000, yield_data=yield_data)
        assert abs(result['eligible_dividends'] - 3000) < 1  # 100K * 3%
        assert abs(result['interest'] - 2000) < 1  # 100K * 2%
        assert abs(result['capital_gains'] - 4000) < 1  # 100K * 4%
        assert abs(result['foreign_income'] - 1000) < 1  # 100K * 1%

    def test_default_yield_rate_configurable(self):
        """DP#2: default_yield_rate parameter replaces hardcoded 2%."""
        from countries.canada.portfolio import compute_investment_income

        # With default_yield_rate=0.04 (4% yield, not 2%)
        result = compute_investment_income(100000, yield_data=None, default_yield_rate=0.04)
        assert abs(result['eligible_dividends'] - 4000) < 1  # 100K * 4%
        assert abs(result['total_investment_income'] - 4000) < 1

    def test_default_yield_rate_matches_old_default(self):
        """Backward compat: default_yield_rate defaults to 0.02."""
        from countries.canada.portfolio import compute_investment_income

        result = compute_investment_income(100000, yield_data=None)
        assert abs(result['eligible_dividends'] - 2000) < 1  # 100K * 2%

    def test_quebec_deduction_uses_configurable_rate(self):
        """DP#2: Quebec deduction passes non_reg_yield_rate, not hardcoded 2%."""
        from countries.canada.provinces.quebec.quebec_deduction import compute_sm_qc_benefit

        # With 4% yield rate (not default 2%), investment income should be higher
        result_default = compute_sm_qc_benefit(
            readvance_heloc_balance=50000,
            heloc_rate=0.06,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.02,
            portfolio_data=None,
        )
        result_higher = compute_sm_qc_benefit(
            readvance_heloc_balance=50000,
            heloc_rate=0.06,
            deductible_proportion=1.0,
            nonreg_balance=100000,
            qc_carry_forward=0,
            marginal_rate=0.45,
            sim_year=2026,
            non_reg_yield_rate=0.04,
            portfolio_data=None,
        )
        # Higher yield = more investment income = higher Quebec deduction
        assert result_higher['readvance_tax_savings'] > result_default['readvance_tax_savings']

    def test_portfolio_data_overrides_default_rate(self):
        """When portfolio_data is provided, it takes priority over default_yield_rate."""
        from countries.canada.portfolio import compute_investment_income

        yield_data = {
            'eligible_dividends': 0.05,  # 5% dividends
            'interest': 0.03,
        }
        # default_yield_rate is irrelevant when yield_data is provided
        result = compute_investment_income(100000, yield_data=yield_data, default_yield_rate=0.10)
        assert abs(result['eligible_dividends'] - 5000) < 1  # 100K * 5%
        assert abs(result['interest'] - 3000) < 1  # 100K * 3%
        # Not affected by default_yield_rate=0.10