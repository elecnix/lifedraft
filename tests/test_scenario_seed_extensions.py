#!/usr/bin/env python3
"""
Comprehensive Scenario Seed Integration Tests

Tests structural gaps against SCENARIO_SEED.md scenarios that the engine
actually wired up:
1. Investment income type composition (§9.1-9.3, §13.1-13.4)
2. Non-reg ACB tracking (§1.1, §8.1, §13.3, §14.2)
3. Smith Manoeuvre / debt swap / cash dam analysis (§1.1-1.6, §9.3) -- via
   countries.canada.debt, not the deleted countries.canada.rental
4. Retirement income + drawdown (§5.1-5.2, §12.1-12.3)
5. Return model / rate paths (§3.2, §8.1)

Also tests:
- Schema round-trips (DP#24)
- Auto-include triggers (DP#16)
- Design principles adherence (DP#1,4,8,14,19,27,28,30)

epic #603 Track C Phase 2 (DP#9): the HELOC-tracing (§1.1-1.6's HELOCConfig
half), rental property + cash damming (§1.5's RentalProperty/CashDam half),
rate-scenario-path (§8.2), and life events + employer benefits (§6.1-6.3's
LifeEvent/apply_life_events half) coverage that used to live here is gone,
along with the countries/canada/rental.py module and the SimulationConfig
*_data fields that fed it -- none had a production caller (#593's
DEAD_ALLOWLIST); a feature that never ran is not a feature. §1.1/§1.5/§1.6's
debt_swap_analysis/cash_dam_analysis and §6.1's MemberRetirementData.
employer_match are unaffected -- both live in countries/canada/debt.py and
countries/canada/retirement.py respectively, not rental.py.
"""

import pytest
import json
from copy import deepcopy

# Module imports
from countries.canada.portfolio import (
    PortfolioConfig, AccountPortfolio, CompositionBreakdown, YieldBreakdown,
    asset_location_recommendation,
)
from return_model import (
    StressedReturn,
    FixedReturn, VariableReturn,
    build_return_model, build_return_model_from_config,
)
from countries.canada.retirement import (
    MemberRetirementData, RetirementState, DrawdownOptimizer,
    oas_clawback, cpp_benefit, rrif_minimum_withdrawal,
    pension_splitting_available, OAS_ANNUAL_MAX,
)
from countries.canada.income_type import IncomeType, effective_tax_rate, wht_drag
from countries.canada.debt import (
    DebtInstrument, DebtPurpose, HELOCTracing, debt_swap_analysis,
    cash_dam_analysis, PrescribedRateLoan,
)
from simulation import SimulationConfig


# =============================================================================
# Schema Round-Trip Tests (DP#24)
# =============================================================================

class TestSchemaRoundTrip:
    """All new data sections must round-trip: load → modify → save."""

    def test_portfolio_round_trip(self):
        """Portfolio config from_dict → to_dict preserves data."""
        data = {
            'allocation_strategy': 'balanced',
            'accounts': {
                'non_reg': {
                    'balance': 200000, 'cost_basis': 150000,
                    'composition': {'cdn_equity_pct': 0.3, 'us_equity_pct': 0.3,
                                     'intl_equity_pct': 0.2, 'fixed_income_pct': 0.2},
                    'yield': {'eligible_dividends': 0.015, 'interest': 0.01,
                              'capital_gains': 0.03, 'return_of_capital': 0.01},
                },
                'rrsp': {
                    'balance': 100000, 'cost_basis': 100000,
                    'composition': {'us_equity_pct': 0.4, 'intl_equity_pct': 0.2,
                                     'fixed_income_pct': 0.3, 'cdn_equity_pct': 0.1},
                    'yield': {'interest': 0.02, 'foreign_income': 0.02},
                },
            },
        }
        portfolio = PortfolioConfig.from_dict(data)
        exported = portfolio.to_dict()
        assert exported['allocation_strategy'] == 'balanced'
        assert exported['accounts']['non_reg']['balance'] == 200000
        assert exported['accounts']['non_reg']['cost_basis'] == 150000

    def test_member_retirement_round_trip(self):
        """Member retirement data from_dict → to_dict round-trip."""
        data = {
            'role': 'primary', 'birth_year': 1979,
            'cpp_start_age': 65, 'cpp_monthly_estimated': 1250,
            'employer_rrsp_match_pct': 0.03, 'employer_rrsp_match_max': 3900,
        }
        member = MemberRetirementData.from_dict(data)
        exported = member.to_dict()
        assert exported['cpp_monthly_estimated'] == 1250
        assert exported['employer_rrsp_match_max'] == 3900


# =============================================================================
# Design Principles Tests
# =============================================================================

class TestDesignPrinciples:
    """Verify adherence to DESIGN_PRINCIPLES.md."""

    def test_dp1_store_dates_not_derived(self):
        """DP#1: Store birth_year, not age."""
        member = MemberRetirementData(birth_year=1979)
        assert member.birth_year == 1979
        # Age computed from date, not stored
        assert member.age_in(2026) == 47

    def test_dp4_role_based_names(self):
        """DP#4: Use 'primary'/'spouse', not person names."""
        member = MemberRetirementData(role="primary")
        assert member.role == "primary"

    def test_dp8_compose_through_data(self):
        """DP#8: Strategies and models are data objects, not subclasses."""
        # StressedReturn is a dataclass, not a Strategy subclass
        model = StressedReturn(crash_year=2, crash_pct=-0.40)
        assert hasattr(model, 'crash_year')
        assert hasattr(model, 'crash_pct')

    def test_dp14_scripts_read_common_schema(self):
        """DP#14: All scripts read input.json schema."""
        # PortfolioConfig reads from same schema
        portfolio = PortfolioConfig.from_dict({})
        assert portfolio.allocation_strategy == 'balanced'

    def test_dp19_track_cost_basis(self):
        """DP#19: Track cost basis from day one."""
        acct = AccountPortfolio(balance=200000, cost_basis=150000)
        assert acct.unrealized_gains == 50000
        assert acct.cost_basis > 0

    def test_dp27_investment_income_distinct_types(self):
        """DP#27: Different income types have different effective rates."""
        mtr = 0.4571
        interest_rate = effective_tax_rate(IncomeType.INTEREST, mtr)
        dividend_rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        cg_rate = effective_tax_rate(IncomeType.CAPITAL_GAIN, mtr)
        roc_rate = effective_tax_rate(IncomeType.RETURN_OF_CAPITAL, mtr)

        # Interest: full MTR
        assert interest_rate == pytest.approx(mtr)
        # Eligible dividends: lower than interest
        assert dividend_rate < interest_rate
        # Capital gains: 50% inclusion
        assert cg_rate == pytest.approx(mtr * 0.5)
        # ROC: zero
        assert roc_rate == 0

    def test_dp28_eligibility_date_computed(self):
        """DP#28: Eligibility computed from dates, not stored booleans."""
        member = MemberRetirementData(birth_year=1960)
        # CPP eligibility computed from birth_year
        assert member.is_cpp_eligible(2025) == True
        assert member.is_cpp_eligible(2024) == False

    def test_dp30_simulator_models_consequences_not_decisions(self):
        """DP#30: Asset location recommendations model consequences."""
        comp = CompositionBreakdown(cdn_equity_pct=0.4, fixed_income_pct=0.6)
        recs = asset_location_recommendation(comp, 0.4571)
        # Recommendations state what's optimal, not what to invest in
        assert 'primary' in recs['cdn_equity']
        assert 'reason' in recs['cdn_equity']


# =============================================================================
# SCENARIO_SEED Integration: Smith Manoeuvre (§1.1-1.6)
# =============================================================================

class TestScenario1xSmithManoeuvre:
    """Integration tests for Smith Manoeuvre scenarios."""

    def test_scenario_11_debt_swap(self):
        """§1.1: Debt swap net benefit (after capital gains tax on disposition)."""
        result = debt_swap_analysis(
            non_reg_balance=200000,
            adjusted_cost_base=150000,
            marginal_rate=0.48,
            mortgage_balance=500000,
            mortgage_rate=0.045,
            heloc_rate=0.0545,
        )
        # Should have positive net benefit over time
        assert result['capital_gain'] == 50000
        assert result['capital_gains_tax'] > 0
        assert result['one_time_tax_cost'] > 0
        # After tax cost comparison
        assert result['condition_met'] == True  # SM rate condition

    def test_scenario_14_rate_spread_sm_not_beneficial(self):
        """§1.4: When HELOC rate spread makes SM not beneficial."""
        result = debt_swap_analysis(
            non_reg_balance=0,  # No swap needed
            adjusted_cost_base=0,
            marginal_rate=0.43,
            mortgage_balance=300000,
            mortgage_rate=0.015,  # 1.5% mortgage
            heloc_rate=0.0395,    # 3.95% HELOC
        )
        # After-tax HELOC cost > mortgage cost
        heloc_after_tax = 0.0395 * (1 - 0.43)
        assert heloc_after_tax > 0.015  # SM costs more than mortgage

    def test_scenario_15_cash_dam_benefit(self):
        """§1.5: Cash dam annual benefit."""
        result = cash_dam_analysis(
            rental_income=24000,
            rental_expenses=18000,
            mortgage_balance=300000,
            mortgage_rate=0.045,
            heloc_rate=0.0545,
            marginal_rate=0.4571,
        )
        assert result['strategy'] == 'cash_dam'
        assert result['total_benefit'] > 0

    def test_scenario_16_dividend_diversion(self):
        """§1.6: Dividend diversion converts non-deductible to deductible."""
        annual_dividend = 4000
        heloc_rate = 0.0545
        mtr = 0.4571

        # Each year: $4K dividend pays down mortgage, $4K borrowed via HELOC
        annual_heloc_interest = annual_dividend * heloc_rate
        annual_tax_savings = annual_heloc_interest * mtr
        # After 10 years: $40K in deductible HELOC debt
        cumulative_deductible = annual_dividend * 10

        assert cumulative_deductible == 40000
        assert annual_tax_savings > 0


# =============================================================================
# SCENARIO_SEED Integration: Asset Location (§9.1-9.4)
# =============================================================================

class TestScenario9xAssetLocation:
    """Integration tests for asset location optimization."""

    def test_scenario_91_after_tax_rrsp(self):
        """§9.1: $100K RRSP at 40% tax = $60K after-tax."""
        rrsp_balance = 100000
        mtr = 0.40
        after_tax = rrsp_balance * (1 - mtr)
        assert after_tax == 60000

    def test_scenario_92_tax_efficiency_matrix(self):
        """§9.2: Tax drag by account type."""
        mtr = 0.4571  # Quebec at $130K

        # Interest in non-reg: fully taxed at MTR
        interest_rate = effective_tax_rate(IncomeType.INTEREST, mtr, 'quebec')
        assert interest_rate == pytest.approx(0.4571)

        # Eligible dividends in non-reg: ~25% effective
        div_rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        assert div_rate < 0.30  # Effective rate less than 30%

        # Capital gains in non-reg: 22.85% (50% inclusion at 45.71% MTR)
        cg_rate = effective_tax_rate(IncomeType.CAPITAL_GAIN, mtr, 'quebec')
        assert cg_rate == pytest.approx(0.22855, abs=0.01)

    def test_scenario_93_quebec_interest_deduction(self):
        """§9.3: Quebec limits HELOC interest deduction to investment income."""
        heloc_interest = 10000
        dividend_income = 3000
        capital_gains = 2000  # Only realized gains count

        # Quebec deduction limited to investment income
        qc_deductible = min(heloc_interest, dividend_income + capital_gains)
        qc_carryforward = heloc_interest - qc_deductible

        assert qc_deductible == 5000
        assert qc_carryforward == 5000

    def test_scenario_94_wht_by_account(self):
        """§9.4: Foreign withholding tax varies by account type."""
        # US equity in RRSP: 0% WHT (treaty) — recoverable via FTC
        rrsp_drag = wht_drag('rrsp', 'us', dividend_yield=0.02)
        assert rrsp_drag == 0

        # US equity in TFSA: unrecoverable ~30 bps WHT
        tfsa_drag = wht_drag('tfsa', 'us', dividend_yield=0.02, wht_recoverable=False)
        assert tfsa_drag > 0
        assert 15 < tfsa_drag < 35  # Approximately 30 bps at 2% yield

        # Canadian equity: 0% WHT everywhere
        # (Tested indirectly — CDN equities don't have foreign WHT)


# =============================================================================
# SCENARIO_SEED Integration: Stress Tests (§8.1)
# =============================================================================

class TestScenario8xStressTests:
    """Integration tests for stress test scenarios.

    §8.2 (rate-scenario-path stress) used to live here. Deleted in epic
    #603 Track C Phase 2 (DP#9) along with RateScenarioPath/
    RateScenarioConfig -- rate_scenarios.scenarios[] had zero production
    callers (#593's DEAD_ALLOWLIST).
    """

    def test_scenario_81_market_crash_while_leveraged(self):
        """§8.1: -40% crash, recovery over 5 years."""
        model = StressedReturn(crash_year=1, crash_pct=-0.40, recovery_years=5)

        # Pre-crash
        assert model.return_for_year(0) == 0.07
        # Crash
        assert model.return_for_year(1) == -0.40
        # Recovery years positive
        for yr in range(2, 7):
            assert model.return_for_year(yr) > 0
        # Post-recovery baseline
        assert model.return_for_year(10) == pytest.approx(0.07, abs=0.01)


# =============================================================================
# SCENARIO_SEED Integration: Retirement (§5.1-5.2, §12.1-12.3)
# =============================================================================

class TestScenario5x12xRetirement:
    """Integration tests for retirement scenarios."""

    def test_scenario_51_oas_clawback(self):
        """§5.1: OAS clawback at $106,148 income."""
        result = oas_clawback(106148)
        expected_clawback = (106148 - 95323) * 0.15
        assert result['clawback_amount'] == pytest.approx(expected_clawback, abs=1)

    def test_scenario_52_drawdown_order(self):
        """§5.2: TFSA → non-reg → RRSP drawdown minimizes OAS clawback."""
        state = RetirementState(
            age=72, rrif_balance=800000,
            tfsa_balance=200000, non_reg_balance=150000,
            non_reg_acb=100000, annual_expenses=60000,
        )
        # Drawdown order stored as data
        assert len(state.drawdown_order) == 3
        assert state.drawdown_order[0] == 'tfsa'  # Tax-free first

    def test_scenario_121_pension_splitting_calculation(self):
        """§12.1: Pension splitting reduces OAS clawback."""
        # Before splitting
        income_a = 106148
        clawback_before = oas_clawback(income_a)

        # After splitting $20K to spouse
        income_a_after = income_a - 20000
        clawback_after = oas_clawback(income_a_after)

        assert clawback_after['clawback_amount'] < clawback_before['clawback_amount']

    def test_scenario_123_oas_deferral_benefit(self):
        """§12.3: Defer OAS to 70 → 36% higher payment."""
        oas_at_65 = OAS_ANNUAL_MAX
        member = MemberRetirementData(oas_defer_months=60)
        oas_at_70 = member.oas_annual
        increase = (oas_at_70 - oas_at_65) / oas_at_65
        assert increase == pytest.approx(0.36, abs=0.01)


# =============================================================================
# SCENARIO_SEED Integration: Life Events (§6.1-6.3)
# =============================================================================

class TestScenario6xLifeEvents:
    """Integration tests for life event scenarios."""

    def test_scenario_61_full_family_optimization(self):
        """§6.1: Full family with employer match."""
        # Primary at $130K, spouse at $50K
        # Employer RRSP match: 3% on $130K = $3,900
        primary = MemberRetirementData(
            employer_rrsp_match_pct=0.03,
            employer_rrsp_match_max=3900,
        )
        match = primary.employer_match(130000)
        assert match == 3900

    def test_scenario_62_new_job_raise(self):
        """§6.2: New job at $170K → higher MTR → RRSP more valuable."""
        # Old: $100K (36% MTR)
        # New: $170K (48% MTR)
        # Bracket gap widens from 16pp to 28pp with $40K spouse
        old_mtr = 0.36
        new_mtr = 0.48
        spouse_mtr = 0.20

        old_spousal_benefit_per_1k = 1000 * (old_mtr - spouse_mtr)
        new_spousal_benefit_per_1k = 1000 * (new_mtr - spouse_mtr)

        assert new_spousal_benefit_per_1k > old_spousal_benefit_per_1k

    # §6.3 (divorce/attribution via apply_life_events) used to live here.
    # Deleted in epic #603 Track C Phase 2 (DP#9) along with LifeEvent/
    # apply_life_events -- life_events.events[] had zero production callers
    # (#593's DEAD_ALLOWLIST), and apply_life_events was itself the host of
    # #620's dead-write bug (a write onto a throwaway dict that nothing
    # running in production could ever have observed).


# =============================================================================
# Input Schema Integration
# =============================================================================

class TestInputSchemaIntegration:
    """Test that SimulationConfig.from_dict parses the internal-shape
    sections the engine actually reads.

    epic #603 Track C Phase 2b: the schema-shape assertions this class used
    to carry (``test_schema_has_portfolio_section`` / ``_merged_schema`` /
    ``test_schema_has_retirement_section`` / ``test_schema_has_return_model_
    section`` / ``test_schema_per_member_retirement_fields``) read the root
    ``input_schema.json`` example-instance file directly, or merged it via
    ``module_registry.merge_config_with_country`` — both deleted in this PR
    (DP#9): the input contract is now ``schema/input_schema.json`` (a real
    JSON Schema, not an instance to grep for keys) plus
    ``schema/countries/canada/input_schema.json``, composed and validated by
    ``input_contract.compose_schema``/``validate_contract``
    (``tests/test_input_contract.py`` covers that shape). What remains here
    is the one assertion that was never about the schema FILE at all — it
    exercises ``SimulationConfig.from_dict``'s internal-shape parsing
    directly, which epic #603 Phase 2b explicitly leaves unchanged.
    """

    def test_simulation_config_from_dict_new_fields(self):
        """SimulationConfig.from_dict parses the sections the engine reads."""
        cfg_dict = {
            'family': {'members': [
                {'role': 'primary', 'gross_income': 130000, 'birth_year': 1979,
                 'cpp_start_age': 65, 'pension_income_annual': 0},
                {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1980},
            ], 'children': []},
            'assumptions': {'projection_years': 10, 'investment_return': 0.07,
                           'salary_growth': 0.02, 'start_year': 2026},
            'savings': {'rate': 0.2},
            'property': {'house_value': 750000, 'mortgage_balance': 300000,
                        'mortgage_rate': 0.05, 'margin_available': 200000},
            'accounts': {'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33810,
                         'tfsa_annual_room_per_person': 7000},
            'portfolio': {'accounts': {'non_reg': {'balance': 200000}}},
            'retirement': {'drawdown_order': ['tfsa', 'non_reg', 'rrsp']},
            'return_model': {'type': 'stressed', 'stressed': {
                'crash_year': 2, 'crash_pct': -0.40, 'recovery_years': 5}},
        }

        config = SimulationConfig.from_dict(cfg_dict)
        assert config.portfolio_data == cfg_dict['portfolio']
        assert config.retirement_data == cfg_dict['retirement']
        assert config.return_model_data == cfg_dict['return_model']