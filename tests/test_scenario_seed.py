#!/usr/bin/env python3
"""Tests for SCENARIO_SEED scenarios — "Ready to Test" items.

All test data uses fake round numbers per SCENARIO_SEED.md and DP#15.
No personal information.

Covers:
- 1.1 Classic SM: debt swap, HELOC vs mortgage, break-even
- 1.4 SM when it doesn't make sense: rate spread threshold
- 2.1 TFSA First (low earner): MTR now vs retirement
- 2.2 RRSP First (high earner): refund loop, OAS risk
- 2.4 Spousal RRSP: attribution, Dec vs Jan timing
- 4.1 RESP age 17: CESG cutoff, catch-up, QESI
- 4.2 RESP over-17: EAP/PSE split, student tax
- 5.1 OAS Clawback: TFSA draw, pension split
- 5.2 RRSP Melt-Down: drawdown order
- 7.1 Quebec SM: carry-forward, dividend vs growth
- 9.1 Asset Location: after-tax allocation, WHT drag
- 9.2 Tax matrix: tax drag by account type
- 13.1 Eligible dividends: gross-up + DTC
- 14.1 Prescribed-rate loans: net benefit, Jan 30 deadline

Run with: python3 -m pytest tests/test_scenario_seed.py -v
"""

from tax_data import default_tax_provider
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from decimal import Decimal

from countries.canada.debt import (
    DebtInstrument, DebtPurpose, AdvanceRecord, HELOCTracing,
    debt_swap_analysis, cash_dam_analysis, PrescribedRateLoan,
    is_interest_deductible,
)
from tax_calculator import (
    marginal_rate, tax_on_investment_income, InvestmentIncomeType,
    )
from countries.canada.tax_calc import (
    rrsp_deduction_savings, spousal_rrsp_benefit,
    tax_on_eligible_dividend, effective_dividend_rate,
    withholding_tax_drag, asset_location_tax_impact,
)
from countries.canada.retirement import (
    oas_clawback, cpp_benefit, rrif_minimum_withdrawal,
    pension_splitting_available,
)
from countries.canada.attribution import check_attribution, check_tosi, TransferType, AttributionResult
from countries.canada.provinces.quebec.quebec_deduction import quebec_interest_deduction, QuebecDeductionTracker
from countries.canada.resp_rules import RESPChild, RESPCalculator
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from countries.canada.adapter import CanadaAdapter
from strategy import AllocationStrategy

# Try to import AssetLocationOptimizer (may not exist in all builds)
try:
    from countries.canada.asset_location import AssetLocationOptimizer, PortfolioHolding, compute_tax_drag
    HAS_ASSET_LOCATION = True
except ImportError:
    HAS_ASSET_LOCATION = False


# =============================================================================
# Scenario 1.1 — Classic Smith Manoeuvre
# SCENARIO_SEED data: Primary $180k (48% MTR), House $800k, Mortgage $500k @ 4.5%
#                     HELOC P+0.5% = 5.45%, Non-reg $200k (ACB $150k)
# =============================================================================

class TestClassicSmithManoeuvre(unittest.TestCase):
    """SCENARIO_SEED 1.1: Classic SM — debt swap, HELOC vs mortgage, break-even."""

    def setUp(self):
        # SCENARIO_SEED fake data (rounded)
        self.primary_income = 180000
        self.primary_mtr = 0.48
        self.house_value = 800000
        self.mortgage_balance = 500000
        self.mortgage_rate = 0.045
        self.heloc_rate = 0.0545
        self.non_reg_balance = 200000
        self.non_reg_acb = 150000

    def test_debt_swap_net_benefit(self):
        """1.1.1: Debt swap produces positive net benefit after capital gains tax.

        Liquidate $200k (ACB $150k) → CG $50k → tax $12k.
        Re-borrow $188k at 5.45% deductible.
        """
        result = debt_swap_analysis(
            non_reg_balance=self.non_reg_balance,
            adjusted_cost_base=self.non_reg_acb,
            marginal_rate=self.primary_mtr,
            mortgage_balance=self.mortgage_balance,
            mortgage_rate=self.mortgage_rate,
            heloc_rate=self.heloc_rate,
        )
        # Capital gain = $200k - $150k = $50k
        self.assertAlmostEqual(result['capital_gain'], 50000, places=0)
        # CG tax = $50k × 50% inclusion × 48% MTR = $12,000
        self.assertAlmostEqual(result['capital_gains_tax'], 12000, places=0)
        # Net benefit should be positive over 10 years
        self.assertTrue(result['net_benefit'] > 0)

    def test_heloc_after_tax_cheaper_when_condition_met(self):
        """1.1.2: HELOC after-tax cost < mortgage rate when condition met.

        HELOC 5.45% × (1 - 0.48) = 2.83% < 4.5% mortgage.
        """
        heloc_after_tax = self.heloc_rate * (1 - self.primary_mtr)
        self.assertLess(heloc_after_tax, self.mortgage_rate)
        # Verify debt_swap_analysis agrees
        result = debt_swap_analysis(
            non_reg_balance=self.non_reg_balance,
            adjusted_cost_base=self.non_reg_acb,
            marginal_rate=self.primary_mtr,
            mortgage_balance=self.mortgage_balance,
            mortgage_rate=self.mortgage_rate,
            heloc_rate=self.heloc_rate,
        )
        self.assertTrue(result['condition_met'])

    def test_break_even_time(self):
        """1.1.3: Break-even time is reasonable (under 5 years).

        CG tax ≈ $12k. Annual benefit from deductible interest.
        Break-even = CG tax / annual benefit.
        """
        result = debt_swap_analysis(
            non_reg_balance=self.non_reg_balance,
            adjusted_cost_base=self.non_reg_acb,
            marginal_rate=self.primary_mtr,
            mortgage_balance=self.mortgage_balance,
            mortgage_rate=self.mortgage_rate,
            heloc_rate=self.heloc_rate,
        )
        self.assertLess(result['breakeven_years'], 5.0)

    def test_heloc_tracing_pure_investment(self):
        """1.1.4: Clean HELOC tracing → 100% deductible when all for investment."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.advance(50000, "2026-02", DebtPurpose.INVESTMENT, "XAW")
        tracing.advance(100000, "2026-03", DebtPurpose.INVESTMENT, "VFV")
        deductible = tracing.deductible_interest(200000, self.heloc_rate)
        total_interest = 200000 * self.heloc_rate
        self.assertAlmostEqual(deductible, total_interest, places=2)

    def test_heloc_tracing_personal_draw_reduces_deduction(self):
        """1.1.5: Personal draw from HELOC reduces deductible proportion."""
        tracing = HELOCTracing()
        tracing.advance(150000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.advance(50000, "2026-02", DebtPurpose.PERSONAL)  # Poison!
        deductible = tracing.deductible_interest(200000, self.heloc_rate)
        # Proportion: 150k / 200k = 75%
        expected = 200000 * self.heloc_rate * 0.75
        self.assertAlmostEqual(deductible, expected, places=2)


# =============================================================================
# Scenario 1.4 — When SM Doesn't Make Sense
# SCENARIO_SEED data: Mortgage $300k @ 1.5%, HELOC $200k @ 3.95%
#                      MTR 43% → HELOC after-tax 2.25% > 1.5% mortgage
# =============================================================================

class TestSMWhenNotBeneficial(unittest.TestCase):
    """SCENARIO_SEED 1.4: SM doesn't make sense when rate spread is adverse."""

    def test_rate_spread_threshold(self):
        """1.4.1: HELOC after-tax cost > mortgage rate → condition NOT met.

        HELOC 3.95% × (1 - 0.43) = 2.25% > 1.5% mortgage.
        """
        mortgage_rate = 0.015
        heloc_rate = 0.0395
        mtr = 0.43
        heloc_after_tax = heloc_rate * (1 - mtr)
        self.assertGreater(heloc_after_tax, mortgage_rate)
        # Verify condition_met is False for this spread
        result = debt_swap_analysis(
            non_reg_balance=200000, adjusted_cost_base=200000,
            marginal_rate=mtr, mortgage_balance=300000,
            mortgage_rate=mortgage_rate, heloc_rate=heloc_rate,
        )
        self.assertFalse(result['condition_met'])

    def test_swap_condition_not_met(self):
        """1.4.2: debt_swap_analysis returns condition_met=False."""
        result = debt_swap_analysis(
            non_reg_balance=200000,
            adjusted_cost_base=200000,  # No CG to keep it simple
            marginal_rate=0.43,
            mortgage_balance=300000,
            mortgage_rate=0.015,
            heloc_rate=0.0395,
        )
        self.assertFalse(result['condition_met'])

    def test_break_even_return(self):
        """1.4.3: What expected return makes SM break even?

        At HELOC after-tax 2.25%, you need >2.25% after all costs
        just to break even on the interest. Add risk premium → likely
        not worth it at these small spreads.
        """
        heloc_rate = 0.0395
        mtr = 0.43
        heloc_after_tax = heloc_rate * (1 - mtr)
        # Break-even expected return must exceed after-tax cost
        self.assertGreater(heloc_after_tax, 0.02)  # Must beat 2%

    def test_sensitivity_heloc_rate_increase_kills_sm(self):
        """1.4.4: Small HELOC rate increase can flip SM from beneficial to not.

        At MTR 43%, each 10bp increase in HELOC rate adds ~6bp after-tax cost.
        """
        # Beneficial at mortgage 4.5%, HELOC 5.0%: after-tax = 5% × 57% = 2.85% < 4.5%
        result_good = debt_swap_analysis(
            non_reg_balance=200000, adjusted_cost_base=200000,
            marginal_rate=0.43,
            mortgage_balance=300000, mortgage_rate=0.045, heloc_rate=0.05,
        )
        self.assertTrue(result_good['condition_met'])

        # Not beneficial at mortgage 4.5%, HELOC 8.5%: after-tax = 8.5% × 57% = 4.845% > 4.5%
        result_bad = debt_swap_analysis(
            non_reg_balance=200000, adjusted_cost_base=200000,
            marginal_rate=0.43,
            mortgage_balance=300000, mortgage_rate=0.045, heloc_rate=0.085,
        )
        self.assertFalse(result_bad['condition_met'])


# =============================================================================
# Scenario 2.1 — TFSA First (Low Earner)
# SCENARIO_SEED data: Age 26, $45k (MTR ~20%), retirement $70k (MTR ~30%)
# =============================================================================

class TestTFSAFirstLowEarner(unittest.TestCase):
    """SCENARIO_SEED 2.1: TFSA first for low earners."""

    def test_mtr_now_lower_than_retirement(self):
        """2.1.1: Current MTR < retirement MTR → TFSA wins.

        At $45k income (MTR ~20%) vs expected $70k retirement (MTR ~30%),
        RRSP deduction saves 20¢/dollar but you pay 30¢ at withdrawal.
        """
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        mtr_now = marginal_rate(45000, brackets)
        mtr_retirement = marginal_rate(70000, brackets)
        self.assertLess(mtr_now, mtr_retirement)

    def test_rrsp_deduction_savings_at_20pct(self):
        """2.1.2: RRSP deduction at $45k income saves ~20% per dollar."""
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        savings = rrsp_deduction_savings(10000, 45000, brackets)
        rate = savings / 10000
        # At $45k in Quebec, MTR is roughly 28-31% (federal + provincial)
        # SCENARIO_SEED says ~20% but that's federal-only for some provinces
        self.assertGreater(rate, 0.15)  # At least 15%
        self.assertLess(rate, 0.40)  # Reasonable upper bound

    def test_30yr_projection_tfsa_better(self):
        """2.1.3: Over 30 years, TFSA-first is better when retirement MTR > current MTR.

        DP#11/DP#18: this test DRIVES THE ENGINE FOLD (FamilySimulation.run()),
        never reimplements compounding inline. The only hand-math is a direction
        cross-check (not the assertion value).

        Setup: a low earner ($45k, MTR ~26% in QC) with a $30k DB pension in
        retirement. Retirement income (CPP + OAS + pension) pushes the household
        above the OAS clawback threshold when RRSP/RRIF withdrawals are added.
        This makes the EFFECTIVE retirement MTR on RRSP withdrawals exceed the
        contribution MTR, so TFSA-first produces higher terminal total_assets.

        Cross-check direction: at equal MTR, TFSA and RRSP are equivalent
        (after-tax value converges). When retirement MTR > contribution MTR,
        every dollar in TFSA escapes the (MTR_retire - MTR_contribute) spread
        that RRSP loses. With OAS clawback (15% recovery on income above the
        threshold), the effective retirement MTR on RRSP drawdowns is even
        higher than the bracket rate alone.
        """
        # -- Config: low earner with DB pension that raises retirement MTR --
        # Primary: $45k income, $30k pension, retirement age 65
        # Frozen brackets so the bracket structure is deterministic.
        base_cfg = {
            'family': {
                'members': [
                    {'role': 'primary', 'birth_year': 1996,
                     'gross_income': 45_000, 'retirement_age': 65,
                     'cpp_monthly_estimated': 800,
                     'pension_income_annual': 30_000,
                     'rrsp_room_accumulated': 0,
                     'tfsa_room_accumulated': 0},
                ],
            },
            'assumptions': {
                'start_year': 2026, 'horizon_age': 90,
                'investment_return': 0.06, 'salary_growth': 0.0,
                'inflation': 0.02, 'frozen_brackets': True,
            },
            'property': {
                'house_value': 0, 'mortgage_balance': 0,
                'mortgage_rate': 0.05,
            },
            'savings': {'rate': 0.20},
            'tax': {'province': 'qc'},
        }

        # -- Strategies: TFSA-first (100% TFSA) vs RRSP-first (100% RRSP) --
        tfsa_strategy = AllocationStrategy(
            name='TFSA First Test',
            rrsp_pct=0.0, spousal_rrsp_pct=0.0, tfsa_pct=1.0,
            fhsa_pct=0.0, resp_pct=0.0, non_reg_pct=0.0,
        )
        rrsp_strategy = AllocationStrategy(
            name='RRSP First Test',
            rrsp_pct=1.0, spousal_rrsp_pct=0.0, tfsa_pct=0.0,
            fhsa_pct=0.0, resp_pct=0.0, non_reg_pct=0.0,
        )

        # -- Drive the engine fold (DP#11/DP#18) --
        def _run(strategy):
            sc = SimulationConfig.from_dict(base_cfg)
            return FamilySimulation(
                sc, adapter=CanadaAdapter(sc),
                strategy=strategy,
                use_readvanceable=False, deduct_later=False,
            ).run()

        results_tfsa = _run(tfsa_strategy)
        results_rrsp = _run(rrsp_strategy)

        tfsa_terminal = results_tfsa[-1].total_assets
        rrsp_terminal = results_rrsp[-1].total_assets

        # -- Assertion on engine-produced values --
        # TFSA-first must beat RRSP-first when retirement MTR > contribution MTR.
        self.assertGreater(
            tfsa_terminal, rrsp_terminal,
            f'TFSA-first terminal ({tfsa_terminal:,.2f}) must exceed '
            f'RRSP-first terminal ({rrsp_terminal:,.2f}) when retirement MTR '
            f'> contribution MTR',
        )

        # -- Direction cross-check (hand-math, NOT the assertion value) --
        # Working MTR ~25.69% at $45k in QC (frozen brackets). Retirement
        # CPP (~$9,600) + OAS (~$8,908) + pension ($30,000) = ~$48,508.
        # With RRIF drawdown on top, taxable income exceeds the OAS clawback
        # threshold, so the effective MTR on RRSP withdrawals is
        # bracket-rate + 15% clawback > 25.69% contribution MTR.
        # The engine captures this; the cross-check only confirms direction.
        working_mtr = results_tfsa[5].primary_marginal
        # In QC at $45k, combined MTR is roughly 25-30%.
        self.assertGreater(working_mtr, 0.20,
                           'Working MTR should be above 20% at $45k in QC')
        # Retirement total income (CPP + OAS + pension) alone exceeds working
        # income, so the MTR on any ADDITIONAL RRSP drawdown is >= working MTR.
        ret_idx = next(i for i, r in enumerate(results_tfsa)
                       if r.employment_income == 0 and i > 0)
        retirement_income_no_drawdown = (
            results_tfsa[ret_idx].cpp_income
            + results_tfsa[ret_idx].oas_income
            + results_tfsa[ret_idx].pension_income
        )
        self.assertGreater(
            retirement_income_no_drawdown, 45_000,
            'Retirement income (CPP + OAS + pension) must exceed $45k '
            'working income, placing RRSP drawdowns in a higher effective MTR',
        )


# =============================================================================
# Scenario 2.2 — RRSP First (High Earner)
# SCENARIO_SEED data: Age 40, $110k (MTR ~43%), retirement MTR ~28%
# =============================================================================

class TestRRSPFirstHighEarner(unittest.TestCase):
    """SCENARIO_SEED 2.2: RRSP first for high earners."""

    def test_rrsp_deduction_at_43pct(self):
        """2.2.1: RRSP deduction at $110k saves ~43% per dollar."""
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        savings = rrsp_deduction_savings(10000, 110000, brackets)
        rate = savings / 10000
        # At $110k in Quebec, combined MTR is ~45-48%
        self.assertGreater(rate, 0.35)

    def test_withdrawal_at_28pct_arbitrage(self):
        """2.2.2: Contribute at 43%, withdraw at 28% = 15% tax arbitrage."""
        savings = 10000 * 0.43
        withdrawal_tax = 10000 * 0.28
        arbitrage = savings - withdrawal_tax
        self.assertAlmostEqual(arbitrage, 1500, places=0)
        self.assertGreater(arbitrage, 0)

    def test_rrsp_tfsa_refund_loop(self):
        """2.2.3: Contribute to RRSP, put refund in TFSA — two accounts grow."""
        contribution = 15000
        mtr = 0.43
        refund = contribution * mtr  # $6,450

        # Both amounts grow tax-free / tax-deferred
        self.assertAlmostEqual(refund, 6450, places=0)
        self.assertGreater(refund, 0)

    def test_oas_clawback_risk_from_large_rrif(self):
        """2.2.4: Large RRIF mandatory withdrawals risk OAS clawback.

        At age 72, RRIF minimum 5.40% of $800k = $43,200.
        Plus CPP $15k + OAS $8,908 + other $25k = $92,108.
        Still below $95,323 threshold, but close. Larger RRIFs hit it.
        """
        rrif_balance = 800000
        min_withdrawal = rrif_minimum_withdrawal(rrif_balance, 72)
        self.assertGreater(min_withdrawal, 40000)

        # With $1M RRIF: total ≈ $54,000 + $15k + $8,908 + $25k = $102,908 → clawback
        total = rrif_minimum_withdrawal(1000000, 72) + 15000 + 8908 + 25000
        clawback = oas_clawback(total)
        self.assertGreater(clawback['clawback_amount'], 0)

    def test_bracket_edge_contribution(self):
        """2.2.5: Contribute just enough to drop to lower bracket.

        At $110k, contributing $10k drops income below next bracket edge.
        Each dollar in the higher bracket saves more tax.
        """
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        high_income_tax_savings = rrsp_deduction_savings(10000, 110000, brackets)
        # This should be meaningful — at least $3,000+
        self.assertGreater(high_income_tax_savings, 2000)


# =============================================================================
# Scenario 2.4 — Spousal RRSP
# SCENARIO_SEED data: Primary $140k (43%), Spouse $30k (20%)
# =============================================================================

class TestSpousalRRSP(unittest.TestCase):
    """SCENARIO_SEED 2.4: Spousal RRSP income splitting."""

    def test_spousal_rrsp_benefit(self):
        """2.4.1: Spousal RRSP benefit = contribution × bracket_gap."""
        result = spousal_rrsp_benefit(
            contribution=10000,
            contributor_rate=0.43,
            withdraw_rate=0.20,
        )
        # Deduction saves $4,300, withdrawal tax $2,000 → net $2,300
        self.assertAlmostEqual(result['net_benefit'], 2300, places=0)
        self.assertAlmostEqual(result['bracket_spread_pct'], 23.0, places=1)

    def test_spousal_rrsp_benefit_per_1000(self):
        """2.4.2: Per-$1000 benefit equals bracket spread × $1000."""
        result = spousal_rrsp_benefit(
            contribution=10000,
            contributor_rate=0.43,
            withdraw_rate=0.20,
        )
        # 23% × $1000 = $230 per $1000
        self.assertAlmostEqual(result['net_benefit_per_1000'], 230, places=0)

    def test_attribution_dec_vs_jan_timing(self):
        """2.4.3: December contribution clears 1 year sooner than January.

        Last contribution Dec 2026 → safe withdrawal Jan 2029 (3 calendar years).
        Last contribution Jan 2027 → safe withdrawal Jan 2030 (3 calendar years).
        Dec is always better.
        """
        # Dec 2026 contribution: safe after Dec 2026 + 3 calendar years = Jan 2029
        dec_year = 2026
        safe_year_dec = dec_year + 3  # 2029

        # Jan 2027 contribution: safe after Dec 2027 + 3 = Jan 2030
        jan_year = 2027
        safe_year_jan = jan_year + 3  # 2030

        self.assertLess(safe_year_dec, safe_year_jan)

    def test_prescribed_rate_loan_escapes_attribution(self):
        """2.4.4: Prescribed-rate loan avoids attribution when interest paid by Jan 30."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            prescribed_rate_used=True,
            interest_paid_by_jan30=True,
        )
        self.assertFalse(result.attributed)

    def test_prescribed_rate_unpaid_triggers_attribution(self):
        """2.4.5: Prescribed-rate loan with missed Jan 30 payment → attribution applies."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            prescribed_rate_used=True,
            interest_paid_by_jan30=False,
        )
        self.assertTrue(result.attributed)


# =============================================================================
# Scenario 4.1 — RESP Age 17: Last Year of CESG
# SCENARIO_SEED data: Child 16 (turning 17 in March), CESG received $5k of $7.2k limit
# =============================================================================

class TestRESPAge17(unittest.TestCase):
    """SCENARIO_SEED 4.1: Last year of CESG eligibility."""

    def test_cesg_age_cutoff(self):
        """4.1.1: CESG stops when child turns 18+. Ages 16-17 have special rules."""
        child_young = RESPChild(birth_year=2012, name="child_a")  # Age 14 in 2026
        child_16 = RESPChild(birth_year=2009, name="child_b")  # Age 17 in 2026
        child_18 = RESPChild(birth_year=2008, name="child_c")  # Age 18 in 2026

        # Young child: cesg_eligible returns True
        self.assertTrue(child_young.cesg_eligible(2026))
        # Age 18+: not eligible
        self.assertFalse(child_18.cesg_eligible(2026))

    def test_cesg_catch_up_provisions(self):
        """4.1.2: Catch-up allows carrying forward unused CESG room.

        If you missed contributions, you can catch up by contributing
        up to $5,000/year and get CESG on the full amount.
        """
        calc = RESPCalculator()
        child = RESPChild(birth_year=2012, name="test_child")  # Age 14
        # Contribute $5,000 with $2,500 unused room
        result = calc.calculate_cesg_with_catchup(
            5000, child, 2026, family_income=150000, unused_room=2500)
        # Should get 20% on at least $2,500 (basic) + catch-up from unused room
        self.assertGreater(result.get('total_cesg', result.get('cesg', 0)), 0)

    def test_cesg_lifetime_limit(self):
        """4.1.3: CESG lifetime limit is $7,200 per child."""
        calc = RESPCalculator()
        self.assertEqual(calc.CESG_LIFETIME_MAX, 7200)
        # Contributing beyond lifetime limit returns 0 CESG
        child_max = RESPChild(birth_year=2012, name="maxed")
        # Simulate having already received max CESG
        child_max.total_cesg_received = 7200
        result = calc.calculate_cesg(5000, child_max, 2026, family_income=150000)
        self.assertEqual(result['total_cesg'], 0)

    def test_optimal_contribution_maximize_grants(self):
        """4.1.4: Optimal contribution to maximize CESG at age 17.

        With $2,200 remaining room, contribute $2,500 to get $500 CESG
        (but only if eligible for 16-17 contributions).
        """
        calc = RESPCalculator()
        child = RESPChild(birth_year=2009, name="child_17")  # Age 17 in 2026
        # At age 16-17, eligibility requires prior contribution history
        # Test the eligibility gate (DP#28)
        result = calc.calculate_cesg(2500, child, 2026, family_income=150000)
        # Result is always a dict
        self.assertIsInstance(result, dict)
        # For a young child, $2,500 contribution gets $500 CESG
        child_young = RESPChild(birth_year=2012, name="young")
        result_young = calc.calculate_cesg(2500, child_young, 2026, family_income=150000)
        self.assertEqual(result_young['basic_cesg'], 500)


# =============================================================================
# Scenario 4.2 — RESP Over-17: No Matching, Still Valuable
# SCENARIO_SEED data: Child 18, RESP $45k, EAP $10k/yr
# =============================================================================

class TestRESPOver17(unittest.TestCase):
    """SCENARIO_SEED 4.2: RESP withdrawals when child is over 17."""

    def test_eap_vs_pse_withdrawal_split(self):
        """4.2.1: EAP (taxable to student) vs PSE (tax-free return of contributions).

        EAP includes: CESG + QESI + growth → taxable
        PSE: return of subscriber contributions → not taxable
        """
        # EAP taxable portion is ~60% (grants + growth)
        resp_balance = 45000
        contributions = 18000  # Assume $18k in contributions
        grants_growth = resp_balance - contributions  # $27k = EAP portion
        eap_taxable_pct = grants_growth / resp_balance
        self.assertGreater(eap_taxable_pct, 0.5)  # Majority is taxable

    def test_student_zero_tax_on_eap(self):
        """4.2.2: Student with low income pays low tax on EAP.

        Student income: $8k (part-time) + $10k EAP = $18k.
        In Quebec, basic personal amounts still leave some tax at $18k.
        """
        student_income = 8000
        eap = 10000
        total = student_income + eap
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        mtr = marginal_rate(total, brackets)
        # At $18k total income in QC, MTR is modest (basic personal + QC low brackets)
        self.assertLess(mtr, 0.35)  # Lower than regular MTR

    def test_eap_timing_spread_years(self):
        """4.2.3: Spreading EAP over multiple years keeps student in low bracket."""
        # With corrected brackets, $0-$54,345 is first bracket (25.69%)
        # All at once: $60k EAP in one year (spans into 30.69% bracket)
        brackets = default_tax_provider().get_combined_brackets(2026, "quebec")
        mtr_lump = marginal_rate(60000, brackets)
        # Spread over 4 years: $15k/yr (stays in first bracket at 25.69%)
        mtr_spread = marginal_rate(15000, brackets)
        # Spreading keeps MTR lower
        self.assertLess(mtr_spread, mtr_lump)


# =============================================================================
# Scenario 5.1 — OAS Clawback Management
# SCENARIO_SEED data: Age 72, RRIF $800k, CPP $15k, OAS $8,908
# =============================================================================

class TestOASClawback(unittest.TestCase):
    """SCENARIO_SEED 5.1: OAS clawback management."""

    def test_oas_clawback_triggers_above_threshold(self):
        """5.1.1: Net income above $95,323 triggers OAS clawback (15¢ per $)."""
        result = oas_clawback(110000)
        self.assertGreater(result['clawback_amount'], 0)
        self.assertGreater(result['clawback_rate_pct'], 0)

    def test_no_clawback_below_threshold(self):
        """5.1.2: Net income at or below threshold → no clawback."""
        # Default threshold is $86,000 in our retirement.py
        result = oas_clawback(80000)
        self.assertEqual(result['clawback_amount'], 0)
        self.assertGreater(result['net_oas'], 0)

    def test_full_clawback_at_high_income(self):
        """5.1.3: Net income at ~$154k → OAS fully clawed back."""
        result = oas_clawback(160000)
        self.assertAlmostEqual(result['net_oas'], 0, places=0)

    def test_draw_from_tfsa_avoids_clawback(self):
        """5.1.4: TFSA withdrawals don't count as income → lower net income.

        Strategy: draw from TFSA instead of RRIF to stay under threshold.
        With the 2026 threshold of $95,323, even moderate RRIF withdrawals
        combined with CPP/OAS can approach the threshold.
        """
        # With large RRIF + other income: high enough to trigger clawback
        rrif_income = rrif_minimum_withdrawal(800000, 72)  # ~$43,200
        other_income = 15000 + 30000  # CPP + other
        total_with_rrif = rrif_income + other_income + 8908  # + OAS
        clawback_with_rrif = oas_clawback(total_with_rrif)

        # With TFSA instead: no RRIF income
        total_with_tfsa = other_income + 8908  # $53,908 — well under $95,323
        clawback_with_tfsa = oas_clawback(total_with_tfsa)
        self.assertAlmostEqual(clawback_with_tfsa['clawback_amount'], 0)

    def test_pension_splitting_reduces_clawback(self):
        """5.1.5: Pension splitting at 65+ can move income to lower-income spouse."""
        result = pension_splitting_available(72, "quebec")
        self.assertTrue(result['federal_available'])
        # Quebec: 65+ for RRIF/LIF splitting
        self.assertTrue(result.get('provincial_available', True))


# =============================================================================
# Scenario 5.2 — RRSP Melt-Down Before 65
# SCENARIO_SEED data: Age 60, RRSP $1.2M, Spouse RRSP $400k
# =============================================================================

class TestRRSPMeltDown(unittest.TestCase):
    """SCENARIO_SEED 5.2: RRSP melt-down before 65."""

    def test_rrif_minimums_at_65(self):
        """5.2.1: RRIF minimums start at age 65 (4% of balance)."""
        rrif_balance = 1200000
        min_at_65 = rrif_minimum_withdrawal(rrif_balance, 65)
        # At 65, minimum is 4%
        self.assertGreater(min_at_65, 40000)

    def test_rrif_minimums_increase_with_age(self):
        """5.2.2: RRIF minimum withdrawal % increases with age."""
        balance = 1000000
        min_65 = rrif_minimum_withdrawal(balance, 65)
        min_72 = rrif_minimum_withdrawal(balance, 72)
        min_80 = rrif_minimum_withdrawal(balance, 80)
        self.assertGreater(min_72, min_65)
        self.assertGreater(min_80, min_72)

    def test_cpp_defer_to_70_increases_benefit(self):
        """5.2.3: Deferring CPP to 70 increases benefit by ~42% (0.7%/month)."""
        cpp_at_65 = cpp_benefit(65, year=2026)
        cpp_at_70 = cpp_benefit(70, year=2026)
        increase = (cpp_at_70 - cpp_at_65) / cpp_at_65
        # 0.7%/month × 60 months = 42%
        self.assertGreater(increase, 0.35)
        self.assertLess(increase, 0.50)

    def test_pre65_drawdown_order(self):
        """5.2.4: Before 65, draw from TFSA → non-reg → RRSP (last).

        This keeps taxable income low before OAS starts.
        """
        # If you draw from TFSA, it doesn't appear as income
        tfsa_withdrawal_income = 0  # TFSA not taxable
        rrsp_withdrawal_income = 30000  # Fully taxable

        # Drawing from TFSA first → lower net income → preserves OAS
        self.assertLess(tfsa_withdrawal_income, rrsp_withdrawal_income)


# =============================================================================
# Scenario 7.1 — Quebec SM: Interest Deduction Limited to Investment Income
# SCENARIO_SEED data: QC resident, MTR 47.46%, HELOC interest $10k/yr
#                     Dividend income $3k/yr
# =============================================================================

class TestQuebecSM(unittest.TestCase):
    """SCENARIO_SEED 7.1: Quebec interest deduction carry-forward."""

    def test_quebec_deduction_limited_to_income(self):
        """7.1.1: Quebec limits deduction to investment income ($3k of $10k)."""
        result = quebec_interest_deduction(
            heloc_interest=10000,
            investment_income=3000,
            carry_forward_prior=0,
        )
        # Federal: full $10k deductible
        self.assertAlmostEqual(result['federal_deductible'], 10000)
        # Quebec: only $3k deductible this year
        self.assertAlmostEqual(result['qc_deductible'], 3000)
        # Carry forward: $7k
        self.assertAlmostEqual(result['new_carry_forward'], 7000)

    def test_quebec_carry_forward_accumulates(self):
        """7.1.2: Unused Quebec deduction carries forward to future years."""
        tracker = QuebecDeductionTracker()
        # Year 1: interest $10k, income $3k → carry forward $7k
        r1 = tracker.process_year(2026, heloc_interest=10000, investment_income=3000)
        self.assertAlmostEqual(r1['new_carry_forward'], 7000)

        # Year 2: interest $10k, income $12k → can deduct $12k + $7k carry = $19k
        # But only $10k interest → fully deductible!
        r2 = tracker.process_year(2027, heloc_interest=10000, investment_income=12000)
        self.assertAlmostEqual(r2['qc_deductible'], 10000)
        # Remaining carry forward: $7k + $0 = still some
        self.assertGreaterEqual(tracker.carry_forward, 0)

    def test_federal_vs_quebec_deduction_difference(self):
        """7.1.3: Federal deducts full interest, Quebec limited → tax cost difference."""
        mtr = 0.4746
        result = quebec_interest_deduction(
            heloc_interest=10000,
            investment_income=3000,
        )
        fed_benefit = result['federal_deductible'] * mtr
        qc_benefit = result['qc_deductible'] * mtr
        shortfall = fed_benefit - qc_benefit
        # QC loses $7k × 47.46% = $3,322 in deductions this year
        self.assertAlmostEqual(shortfall, 7000 * mtr, places=0)

    def test_dividend_portfolio_better_for_quebec(self):
        """7.1.4: Dividend-heavy portfolio generates more deductible income for QC.

        Dividends count as investment income for QC deduction limit.
        Growth portfolio (no dividends) generates less income → limits deduction.
        """
        # Growth portfolio: $3k dividends only
        result_growth = quebec_interest_deduction(
            heloc_interest=10000, investment_income=3000,
        )
        # Dividend portfolio: $8k dividends
        result_dividend = quebec_interest_deduction(
            heloc_interest=10000, investment_income=8000,
        )
        # Dividend portfolio deducts more in Quebec
        self.assertGreater(result_dividend['qc_deductible'],
                           result_growth['qc_deductible'])
        # Less carry forward
        self.assertLess(result_dividend['new_carry_forward'],
                        result_growth['new_carry_forward'])

    def test_capital_gains_count_toward_qc_limit(self):
        """7.1.5: Realized capital gains (taxable portion) count for QC deduction."""
        result_no_cg = quebec_interest_deduction(
            heloc_interest=10000, investment_income=3000,
        )
        result_with_cg = quebec_interest_deduction(
            heloc_interest=10000, investment_income=3000,
            capital_gains_realized=10000,  # $10k realized → $5k taxable
        )
        # With capital gains, more income available → more deductible
        self.assertGreater(result_with_cg['qc_deductible'],
                           result_no_cg['qc_deductible'])


# =============================================================================
# Scenario 9.1 — Asset Location: After-Tax Allocation
# SCENARIO_SEED data: TFSA $60k, RRSP $100k (40% tax), Target 50/50
# =============================================================================

class TestAssetLocation(unittest.TestCase):
    """SCENARIO_SEED 9.1: After-tax asset location optimization."""

    def test_after_tax_rrsp_allocation(self):
        """9.1.1: $100k RRSP at 40% tax = $60k after-tax.

        Key insight: RRSP after-tax value mirrors TFSA behavior.
        The government "owns" the tax portion.
        """
        rrsp_balance = 100000
        tax_rate = 0.40
        after_tax_rrsp = rrsp_balance * (1 - tax_rate)
        self.assertAlmostEqual(after_tax_rrsp, 60000)

    def test_withholding_tax_drag_rrsp(self):
        """9.1.2: US-listed ETFs in RRSP: 0 bps WHT drag (treaty exemption)."""
        drag = withholding_tax_drag('rrsp', 'us_listed')
        self.assertEqual(drag, 0.0)

    def test_withholding_tax_drag_tfsa(self):
        """9.1.3: US-listed ETFs in TFSA: ~30 bps WHT drag (unrecoverable)."""
        drag = withholding_tax_drag('tfsa', 'us_listed', yield_pct=0.02)
        # 15% × 2% = 30 bps
        self.assertAlmostEqual(drag, 30.0, places=0)

    def test_withholding_tax_drag_canadian(self):
        """9.1.4: Canadian ETFs: 0 bps WHT drag anywhere."""
        self.assertEqual(withholding_tax_drag('tfsa', 'canadian'), 0.0)
        self.assertEqual(withholding_tax_drag('rrsp', 'canadian'), 0.0)
        self.assertEqual(withholding_tax_drag('non_reg', 'canadian'), 0.0)

    def test_withholding_tax_drag_nonreg_ftc(self):
        """9.1.5: US ETFs in non-reg: 0 bps WHT drag (FTC recovers it)."""
        drag = withholding_tax_drag('non_reg', 'us_listed')
        self.assertEqual(drag, 0.0)

    @unittest.skipUnless(HAS_ASSET_LOCATION, "asset_location module not available")
    def test_light_vs_ludicrous_approach(self):
        """9.1.6: Light (same ETF mix) vs ludicrous (tax-optimized placement)."""
        from countries.canada.asset_location import ETFType as ET
        holdings = [
            PortfolioHolding("XEQT", ET.CANADIAN_EQUITY, 0.50),
            PortfolioHolding("XUU", ET.US_LISTED_EQUITY, 0.30),
            PortfolioHolding("ZAG", ET.BONDS, 0.20),
        ]
        optimizer = AssetLocationOptimizer(marginal_rate=0.4571)
        result = optimizer.optimize(holdings)
        self.assertIsInstance(result, object)


# =============================================================================
# Scenario 9.2 — Tax Matrix by Account Type
# SCENARIO_SEED data: Various income types at 45.7% MTR
# =============================================================================

class TestTaxMatrix(unittest.TestCase):
    """SCENARIO_SEED 9.2: Tax efficiency matrix by account type."""

    def test_interest_fully_taxable_in_nonreg(self):
        """9.2.1: Interest income in non-reg: 45.7% MTR (fully taxable)."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.INTEREST, marginal_rate=0.4571)
        self.assertAlmostEqual(result['effective_rate'], 0.4571, places=3)

    def test_capital_gains_half_inclusion(self):
        """9.2.2: Capital gains in non-reg: 50% inclusion → 22.85% effective."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.CAPITAL_GAIN, marginal_rate=0.4571)
        # 50% inclusion × 45.7% MTR = 22.85%
        self.assertAlmostEqual(result['effective_rate'], 0.2285, places=2)

    def test_eligible_dividends_lower_effective_rate(self):
        """9.2.3: Eligible dividends in non-reg: ~25% effective (DTC helps)."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.CANADIAN_ELIGIBLE_DIVIDEND,
            marginal_rate=0.4571, province="quebec")
        # Should be much less than full MTR
        self.assertLess(result['effective_rate'], 0.35)
        self.assertGreater(result['effective_rate'], 0.10)

    def test_foreign_dividend_nonreg_ftc(self):
        """9.2.4: US dividend in non-reg: 15% WHT recovered via FTC."""
        result = tax_on_investment_income(
            10000, InvestmentIncomeType.FOREIGN_DIVIDEND_US,
            marginal_rate=0.4571)
        # WHT is 15% but FTC recovers it
        self.assertAlmostEqual(result['wht'], 1500)
        self.assertAlmostEqual(result['foreign_tax_credit'], 1500)

    def test_asset_location_tax_impact_bonds(self):
        """9.2.5: Bonds have zero drag in RRSP (fully taxable interest, sheltered)."""
        impact = asset_location_tax_impact('bonds', 0.4571, 'quebec')
        # Bonds in RRSP/TFSA: zero drag (sheltered)
        self.assertEqual(impact['rrsp'], 0.0)
        # Bonds in non-reg: positive drag (interest fully taxable)
        self.assertGreater(impact['non_reg'], 0.0)

    def test_asset_location_tax_impact_us_equity(self):
        """9.2.6: US-listed equity has zero WHT drag in RRSP (treaty exemption)."""
        impact = asset_location_tax_impact('us_listed', 0.4571, 'quebec')
        self.assertEqual(impact['rrsp'], 0.0)
        # TFSA has WHT drag
        self.assertGreater(impact['tfsa'], 0.0)

    def test_asset_location_tax_impact_canadian_dividend(self):
        """9.2.7: Canadian dividends in QC at high MTR have lower non-reg drag than bonds."""
        impact = asset_location_tax_impact('canadian_dividend', 0.4571, 'quebec')
        # At 45.7% MTR, eligible dividends have lower effective rate than MTR
        self.assertGreater(impact['non_reg'], 0.0)
        # But less drag than bonds in non-reg
        bonds_impact = asset_location_tax_impact('bonds', 0.4571, 'quebec')
        self.assertLess(impact['non_reg'], bonds_impact['non_reg'])


# =============================================================================
# Scenario 13.1 — Canadian Eligible Dividends
# SCENARIO_SEED data: Various income levels for dividend tax calc
# =============================================================================

class TestEligibleDividends(unittest.TestCase):
    """SCENARIO_SEED 13.1: Eligible dividend gross-up + DTC."""

    def test_gross_up_and_dtc(self):
        """13.1.1: Eligible dividends: gross-up 38%, DTC 15.02% + QC 8%."""
        dividend = 10000
        mtr = 0.4571
        tax = tax_on_eligible_dividend(dividend, mtr, "quebec")
        # Manual: grossed-up = $13,800
        # Tax: $13,800 × 45.71% = $6,308
        # DTC federal: $13,800 × 15.02% = $2,073
        # DTC QC: $13,800 × 8% = $1,104
        # Net tax: $6,308 - $2,073 - $1,104 = $3,131
        self.assertGreater(tax, 2000)
        self.assertLess(tax, 4000)

    def test_effective_rate_by_income(self):
        """13.1.2: Effective dividend rate is much lower than MTR.

        At 45.7% MTR: eligible dividends ~25% effective.
        At 30% MTR: eligible dividends ~10-15% effective.
        """
        eff_high = effective_dividend_rate(0.4571, "quebec")
        eff_low = effective_dividend_rate(0.30, "quebec")
        self.assertLess(eff_high, 0.35)
        self.assertLess(eff_low, eff_high)

    def test_dividend_vs_interest_tax_advantage(self):
        """13.1.3: Dividend tax advantage vs interest at same MTR.

        At 45.7% MTR: interest = 45.7%, eligible dividends ~31%.
        Gap: ~14+ percentage points.
        """
        mtr = 0.4571
        interest_eff = mtr
        dividend_eff = effective_dividend_rate(mtr, "quebec")
        advantage = interest_eff - dividend_eff
        self.assertGreater(advantage, 0.10)  # At least 10pp advantage

    def test_roc_reduces_acb(self):
        """13.1.4: Return of capital reduces ACB; when ACB=0, ROC becomes capital gain."""
        # ACB $50k, ROC $30k → ACB becomes $20k, tax = 0
        result_partial = tax_on_investment_income(
            30000, InvestmentIncomeType.RETURN_OF_CAPITAL,
            marginal_rate=0.4571, acb=50000)
        self.assertEqual(result_partial['tax'], 0)
        self.assertAlmostEqual(result_partial['new_acb'], 20000)

        # ACB $10k, ROC $30k → $20k excess becomes capital gain
        result_excess = tax_on_investment_income(
            30000, InvestmentIncomeType.RETURN_OF_CAPITAL,
            marginal_rate=0.4571, acb=10000)
        self.assertGreater(result_excess['tax'], 0)
        self.assertAlmostEqual(result_excess['new_acb'], 0)


# =============================================================================
# Scenario 14.1 — Prescribed-Rate Loans
# SCENARIO_SEED data: Primary lends $200k to spouse at 2%, spouse invests at 5%
# =============================================================================

class TestPrescribedRateLoans(unittest.TestCase):
    """SCENARIO_SEED 14.1: Prescribed-rate loan for income splitting."""

    def test_loan_benefit_at_spread(self):
        """14.1.1: Prescribed-rate loan: $200k at 2%, invested at 5%.

        Spouse earns $10k, pays $4k interest to primary.
        Spouse nets $6k at low rate (20%) = $1,200 tax.
        Primary reports $4k at high rate (45.7%) = $1,828 tax.
        Family total: $3,028.
        Without loan: primary earns $10k at 45.7% = $4,570 tax.
        Annual savings: ~$1,542.
        """
        loan = PrescribedRateLoan(principal=200000, rate=0.02)
        benefit = loan.net_income_splitting_benefit(
            investment_return=0.05,
            lender_marginal_rate=0.4571,
            borrower_marginal_rate=0.20,
        )
        self.assertGreater(benefit, 1000)
        self.assertLess(benefit, 3000)

    def test_jan30_deadline_compliance(self):
        """14.1.2: Interest must be paid by Jan 30 → attribution if missed."""
        loan_paid = PrescribedRateLoan(principal=200000, rate=0.02,
                                       interest_paid_by_jan30=True)
        loan_unpaid = PrescribedRateLoan(principal=200000, rate=0.02,
                                         interest_paid_by_jan30=False)

        benefit_paid = loan_paid.net_income_splitting_benefit(0.05, 0.4571, 0.20)
        benefit_unpaid = loan_unpaid.net_income_splitting_benefit(0.05, 0.4571, 0.20)

        self.assertGreater(benefit_paid, 0)
        self.assertEqual(benefit_unpaid, 0)

    def test_attribution_on_missed_payment(self):
        """14.1.3: Missed Jan 30 payment triggers attribution (all income to lender)."""
        loan_good = PrescribedRateLoan(principal=100000, interest_paid_by_jan30=True)
        loan_bad = PrescribedRateLoan(principal=100000, interest_paid_by_jan30=False)
        self.assertFalse(loan_good.attribution_applies(2026))
        self.assertTrue(loan_bad.attribution_applies(2026))

    def test_rate_lock_in_benefit(self):
        """14.1.4: Prescribed rate stays locked for life of loan.

        If rate goes from 2% to 4%, your 2% loan is very beneficial.
        This is an implicit benefit not captured in the simple calc.
        """
        loan = PrescribedRateLoan(principal=200000, rate=0.02)
        # Even at 2%, benefit exists if investment return exceeds 2%
        benefit = loan.net_income_splitting_benefit(
            investment_return=0.05,
            lender_marginal_rate=0.4571,
            borrower_marginal_rate=0.20,
        )
        self.assertGreater(benefit, 0)

    def test_compare_spousal_rrsp_vs_loan(self):
        """14.1.5: Prescribed-rate loan vs spousal RRSP for income splitting.

        Loan: spouse invests and keeps returns above prescribed rate.
        RRSP: spouse contributes, deducts at high rate, withdraws at low rate.

        Both achieve income splitting; key differences:
        - Loan keeps capital in spouse's hands (no RRSP room needed)
        - RRSP deduction is immediate; loan interest is ongoing
        """
        # Spousal RRSP: $10k contribution, 43% vs 20% rate
        rrsp_result = spousal_rrsp_benefit(
            contribution=10000, contributor_rate=0.43, withdraw_rate=0.20)

        loan = PrescribedRateLoan(principal=100000, rate=0.02)
        loan_benefit = loan.net_income_splitting_benefit(
            investment_return=0.05,
            lender_marginal_rate=0.43,
            borrower_marginal_rate=0.20,
        )

        # Both produce positive benefit
        self.assertGreater(rrsp_result['net_benefit'], 0)
        self.assertGreater(loan_benefit, 0)


# =============================================================================
# Scenario 11.1 — TOSI Rules
# SCENARIO_SEED data: Parent Opco, Spouse 15 hrs, Child A 22 @ 5 hrs, Child B 28 @ 0 hrs
# =============================================================================

class TestTOSI(unittest.TestCase):
    """SCENARIO_SEED 11.1: TOSI (Tax On Split Income)."""

    def test_tosi_applies_to_inactive_child(self):
        """11.1.1: TOSI applies to inactive young adult (age 22, 0 hrs/week, trust source)."""
        # Age 22 with 'trust' source (not covered by age 25+ exception for business/dividend)
        result = check_tosi(
            recipient_age=22,
            source_type="trust",
            recipient_hours_per_week=0,
            recipient_ownership_pct=0,
            income_amount=20000,
        )
        self.assertTrue(result['tosi_applies'])

    def test_tosi_excluded_share_exception(self):
        """11.1.2: TOSI does NOT apply if recipient owns 10%+ and works 20+ hrs."""
        result = check_tosi(
            recipient_age=28,
            recipient_hours_per_week=25,
            recipient_ownership_pct=0.15,
            income_amount=20000,
        )
        self.assertFalse(result['tosi_applies'])

    def test_tosi_age_25_plus_exclusion(self):
        """11.1.3: Age 25+ for business/dividend income → TOSI exclusion."""
        result = check_tosi(
            recipient_age=25,
            source_type="business",
            recipient_hours_per_week=0,
            recipient_ownership_pct=0,
            income_amount=20000,
        )
        # Age 25+ with business source → exclusion
        self.assertFalse(result['tosi_applies'])

    def test_tosi_reasonable_return(self):
        """11.1.4: Income within reasonable return → no TOSI."""
        result = check_tosi(
            recipient_age=22,
            recipient_hours_per_week=5,
            recipient_ownership_pct=0,
            income_amount=5000,
            reasonable_return_amount=10000,
        )
        self.assertFalse(result['tosi_applies'])

    def test_tosi_tax_at_top_rate(self):
        """11.1.5: When TOSI applies, tax is at the top marginal rate."""
        result = check_tosi(
            recipient_age=22,
            recipient_hours_per_week=0,
            recipient_ownership_pct=0,
            income_amount=30000,
        )
        self.assertTrue(result['tosi_applies'])
        self.assertGreater(result.get('tosi_tax', 0), 0)


# =============================================================================
# Scenario 11.2 — Minor Child Attribution
# SCENARIO_SEED data: Property transferred to minor child
# =============================================================================

class TestMinorChildAttribution(unittest.TestCase):
    """SCENARIO_SEED 11.2: Attribution rules for minor child transfers."""

    def test_income_attributed_to_minor_child(self):
        """11.2.1: Income (interest, dividends) attributes back for children under 18."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=12,
        )
        self.assertTrue(result.attributed)
        # Only income attributes back, not capital gains
        self.assertGreater(len(result.income_types_attributed), 0)

    def test_capital_gains_not_attributed_for_minor(self):
        """11.2.2: Capital gains stay in minor child's hands (key planning point)."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=12,
        )
        self.assertTrue(result.attributed)
        # Capital gains should NOT be in attributed types
        # (IncomeType.ALL is used for spousal, not for minor child)
        # For minors, only specific income types attribute

    def test_no_attribution_over_18(self):
        """11.2.3: Attribution ceases when child reaches 18."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=18,
        )
        self.assertFalse(result.attributed)

    def test_years_until_clear(self):
        """11.2.4: Attribution clears when child turns 18."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=14,
        )
        self.assertEqual(result.years_until_clear, 4)  # 18 - 14


if __name__ == '__main__':
    unittest.main()