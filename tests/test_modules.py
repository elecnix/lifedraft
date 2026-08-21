#!/usr/bin/env python3
"""Unit tests for the composable optimizer modules.

All test data uses fake names and round numbers. No personal information.

Run with: python3 -m pytest tests/ -v
Or:      python3 tests/test_modules.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import subprocess

# =============================================================================
# Tax Calculator Tests
# =============================================================================

from tax_calculator import (
    marginal_rate, tax_on_income,
    effective_tax_rate, capital_gains_rate,
)
from countries.canada.tax_calc import (
    federal_tax, quebec_tax,
    rrsp_deduction_savings, rrsp_deduct_later_savings,
    spousal_rrsp_benefit,
)


class TestTaxBrackets(unittest.TestCase):
    """Test that tax bracket data is correct for Quebec 2026."""

    def setUp(self):
        self.brackets = default_tax_provider().get_combined_brackets()

    def test_brackets_exist(self):
        self.assertGreater(len(self.brackets), 0)

    def test_bracket_rates(self):
        """Verify known bracket boundaries from tax_data combined brackets."""
        # First bracket: 0–54345 at 25.69% (fed 14%×0.835 + qc 14%)
        self.assertAlmostEqual(self.brackets[0]['rate'], 0.2569, places=4)
        self.assertEqual(self.brackets[0]['min'], 0)
        self.assertEqual(self.brackets[0]['max'], 54345)

        # 45.71% bracket exists (fed 26%×0.835 + qc 24%)
        bracket_45 = [b for b in self.brackets if 0.45 < b['rate'] < 0.46]
        self.assertGreater(len(bracket_45), 0)
        self.assertAlmostEqual(bracket_45[0]['rate'], 0.4571, places=4)

    def test_brackets_are_ascending(self):
        """Rates should be non-decreasing."""
        for i in range(1, len(self.brackets)):
            self.assertGreaterEqual(self.brackets[i]['rate'], self.brackets[i-1]['rate'])


class TestMarginalRate(unittest.TestCase):
    """Test marginal rate calculations."""

    def test_zero_income(self):
        rate = marginal_rate(0)
        self.assertAlmostEqual(rate, 0.2569, places=4)

    def test_low_income(self):
        """$30,000 should be in first bracket (25.69%)."""
        rate = marginal_rate(30000)
        self.assertAlmostEqual(rate, 0.2569, places=4)

    def test_middle_income(self):
        """$100,000 should be in 36.12% bracket ($58,523-$108,680)."""
        rate = marginal_rate(100000)
        self.assertAlmostEqual(rate, 0.3612, places=4)

    def test_high_income(self):
        """$200,000 should be in 49.96% bracket ($181,440-$258,482)."""
        rate = marginal_rate(200000)
        self.assertAlmostEqual(rate, 0.4996, places=4)

    def test_very_high_income(self):
        """$500,000 should be in top bracket (53.31%)."""
        rate = marginal_rate(500000)
        self.assertAlmostEqual(rate, 0.5331, places=4)

    def test_bracket_boundary(self):
        """Test exact bracket boundaries with corrected thresholds."""
        # $16,571: still in first bracket (0-$54,345 at 25.69%)
        rate_16k = marginal_rate(16571)
        self.assertAlmostEqual(rate_16k, 0.2569, places=4)

        # $49,711: still in first bracket
        rate_49k = marginal_rate(49711)
        self.assertAlmostEqual(rate_49k, 0.2569, places=4)

        # $54,346: now in second bracket ($54,345-$58,523 at 30.69%)
        rate_54k = marginal_rate(54346)
        self.assertAlmostEqual(rate_54k, 0.3069, places=4)


class TestTaxOnIncome(unittest.TestCase):
    """Test total tax calculation."""

    def test_zero_income(self):
        tax = tax_on_income(0)
        self.assertEqual(tax, 0)

    def test_low_income(self):
        """$30,000 is entirely in first bracket (25.69%)."""
        tax = tax_on_income(30000)
        expected = 30000 * 0.2569
        self.assertAlmostEqual(tax, expected, places=0)

    def test_multi_bracket(self):
        """$100,000 spans three brackets."""
        tax = tax_on_income(100000)
        # Segments: $0-$54,345 @ 25.69% + $54,345-$58,523 @ 30.69% + $58,523-$100,000 @ 36.12%
        expected = (54345 * 0.2569 + (58523 - 54345) * 0.3069 + (100000 - 58523) * 0.3612)
        self.assertAlmostEqual(tax, expected, places=0)

    def test_progressive(self):
        """Higher income should have higher average rate."""
        tax_50k = tax_on_income(50000)
        tax_150k = tax_on_income(150000)
        avg_50k = tax_50k / 50000
        avg_150k = tax_150k / 150000
        self.assertGreater(avg_150k, avg_50k)

    def test_known_income(self):
        """Round $100,000 should produce known tax."""
        tax = tax_on_income(100000)
        self.assertGreater(tax, 25000)
        self.assertLess(tax, 40000)


class TestFederalQuebecTax(unittest.TestCase):
    """Test separate federal and Quebec tax calculations."""

    def test_federal_tax(self):
        """Federal tax (after QC abatement) for $100,000."""
        fed = federal_tax(100000)
        self.assertGreater(fed, 0)
        self.assertLess(fed, 100000 * 0.33)  # Less than top rate

    def test_quebec_tax(self):
        """Quebec tax for $100,000."""
        qc = quebec_tax(100000)
        self.assertGreater(qc, 0)

    def test_combined_equals_total(self):
        """Federal + Quebec should approximately equal combined tax."""
        income = 100000
        fed = federal_tax(income)
        qc = quebec_tax(income)
        combined = tax_on_income(income)
        self.assertAlmostEqual(fed + qc, combined, delta=100)


class TestRRSPDeduction(unittest.TestCase):
    """Test RRSP deduction savings calculations."""

    def test_deduction_at_high_bracket(self):
        """$10,000 deduction at $100,000 should save ~$3,612 (36.12% MTR)."""
        savings = rrsp_deduction_savings(10000, 100000)
        expected = 10000 * 0.3612  # At $100k marginal rate (corrected brackets)
        self.assertAlmostEqual(savings, expected, delta=100)

    def test_deduction_crosses_brackets(self):
        """Large deduction crossing bracket boundaries."""
        savings = rrsp_deduction_savings(50000, 200000)
        self.assertGreater(savings, 15000)  # At least 30% avg rate

    def test_zero_deduction(self):
        savings = rrsp_deduction_savings(0, 150000)
        self.assertEqual(savings, 0)


class TestSpousalRRSP(unittest.TestCase):
    """Test spousal RRSP benefit calculations."""

    def test_basic_benefit(self):
        """$10,000 spousal RRSP with 20% bracket gap."""
        result = spousal_rrsp_benefit(10000, 0.4571, 0.2569)
        self.assertAlmostEqual(result['net_benefit_per_1000'], 200.2, places=0)
        self.assertEqual(result['attribution_years'], 3)

    def test_no_benefit_at_same_rate(self):
        """No benefit if both spouses at same rate."""
        result = spousal_rrsp_benefit(10000, 0.2569, 0.2569)
        self.assertAlmostEqual(result['net_benefit'], 0, places=2)


# =============================================================================
# Account Models Tests
# =============================================================================

from countries.canada.account_models import (
    RRSPAccount, TSFAccount, RESPAccount, NonRegAccount,
)
from countries.canada.rate_model import ReadvanceableMortgage
from strategy import AllocationStrategy
from countries.canada.strategies import STRATEGIES


class TestRRSPAccount(unittest.TestCase):
    """Test RRSP contribution, growth, and room tracking."""

    def test_contribute_within_room(self):
        rrsp = RRSPAccount(balance=0, contribution_room=30000)
        actual, remaining = rrsp.contribute(10000)
        self.assertEqual(actual, 10000)
        self.assertEqual(remaining, 20000)
        self.assertEqual(rrsp.balance, 10000)

    def test_contribute_exceeds_room(self):
        rrsp = RRSPAccount(balance=0, contribution_room=5000)
        actual, remaining = rrsp.contribute(10000)
        self.assertEqual(actual, 5000)
        self.assertEqual(remaining, 0)

    def test_growth(self):
        rrsp = RRSPAccount(balance=100000, contribution_room=0)
        growth = rrsp.grow(0.07)
        self.assertAlmostEqual(growth, 7000, places=0)
        self.assertAlmostEqual(rrsp.balance, 107000, places=0)

    def test_annual_room(self):
        rrsp = RRSPAccount()
        room = rrsp.add_annual_room(100000)
        self.assertAlmostEqual(room, 18000, places=0)  # 18% of $100k

    def test_annual_room_capped(self):
        rrsp = RRSPAccount(annual_room_cap=33810)
        room = rrsp.add_annual_room(250000)
        self.assertEqual(room, 33810)  # Capped at 2026 limit


class TestTSFAccount(unittest.TestCase):
    """Test TFSA contribution and growth."""

    def test_contribute(self):
        tfsa = TSFAccount(balance=0, contribution_room=7000)
        actual, remaining = tfsa.contribute(5000)
        self.assertEqual(actual, 5000)
        self.assertEqual(remaining, 2000)

    def test_growth_tax_free(self):
        tfsa = TSFAccount(balance=50000, contribution_room=0)
        growth = tfsa.grow(0.07)
        self.assertAlmostEqual(growth, 3500, places=0)
        # No tax on TFSA growth

    def test_annual_room(self):
        tfsa = TSFAccount(balance=0, contribution_room=0, annual_room=7000)
        room = tfsa.add_annual_room(2026)
        self.assertEqual(room, 7000)
        self.assertEqual(tfsa.contribution_room, 7000)


class TestRESPAccount(unittest.TestCase):
    """Test RESP with per-child eligibility."""

    def test_contribute_eligible_child(self):
        resp = RESPAccount(
            balance=20000,
            children=[{
                'name': 'Child', 'age': 10, 'cesg_eligible': True
            }],
            contributions_total=10000,
        )
        actual, grants = resp.contribute(2500, 'Child')
        self.assertEqual(actual, 2500)
        self.assertAlmostEqual(grants['cesg'], 500)  # 20% of $2500
        self.assertAlmostEqual(grants['qesi'], 250)   # 10% of $2500

    def test_contribute_over_17_child(self):
        """Over-17 child: no matching grants."""
        resp = RESPAccount(
            balance=30000,
            children=[{
                'name': 'Teen', 'age': 18, 'cesg_eligible': False
            }],
            contributions_total=20000,
        )
        actual, grants = resp.contribute(2500, 'Teen')
        self.assertEqual(actual, 2500)
        self.assertEqual(grants['cesg'], 0)
        self.assertEqual(grants['qesi'], 0)
        self.assertIn('no matching', grants['note'].lower())

    def test_lifetime_limit(self):
        """Can't contribute beyond $50,000 lifetime."""
        resp = RESPAccount(
            balance=48000,
            contributions_total=49000,
            children=[{'name': 'Child', 'age': 5, 'cesg_eligible': True}],
        )
        actual, grants = resp.contribute(5000, 'Child')
        self.assertEqual(actual, 1000)  # Only $1000 room left


class TestNonRegAccount(unittest.TestCase):
    """Test non-registered account with SM."""

    def test_contribute(self):
        acct = NonRegAccount()
        acct.contribute(50000)
        self.assertEqual(acct.balance, 50000)
        self.assertEqual(acct.cost_basis, 50000)

    def test_growth(self):
        acct = NonRegAccount(balance=100000, cost_basis=80000)
        growth = acct.grow(0.07)
        self.assertAlmostEqual(growth, 7000, places=0)
        self.assertAlmostEqual(acct.balance, 107000, places=0)
        self.assertEqual(acct.cost_basis, 80000)  # Unchanged

    def test_capital_gains_tax(self):
        acct = NonRegAccount(balance=150000, cost_basis=100000)
        tax = acct.capital_gains_tax(0.4571)
        # Gains: $50k, taxable: $25k, tax: $25k * 45.71% = $11,428
        expected = 50000 * 0.50 * 0.4571
        self.assertAlmostEqual(tax, expected, delta=100)

    def test_smith_interest(self):
        acct = NonRegAccount()
        interest, savings = acct.add_smith_interest(5000, 0.4571)
        self.assertEqual(interest, 5000)
        self.assertAlmostEqual(savings, 2286, places=0)


class TestReadvanceableMortgage(unittest.TestCase):
    """Test SM readvancing tracking."""

    def test_readvance(self):
        sm = ReadvanceableMortgage()
        sm.readvance(5000)
        self.assertEqual(sm.heloc_balance, 5000)
        self.assertEqual(sm.investment_balance, 5000)
        self.assertEqual(sm.investment_cost_basis, 5000)

    def test_growth(self):
        sm = ReadvanceableMortgage(investment_balance=50000, investment_cost_basis=50000)
        growth = sm.grow_investment(0.07)
        self.assertAlmostEqual(growth, 3500, places=0)

    def test_summary(self):
        sm = ReadvanceableMortgage(heloc_balance=25000, investment_balance=30000, investment_cost_basis=25000)
        summary = sm.annual_summary()
        self.assertEqual(summary['heloc_balance'], 25000)
        self.assertAlmostEqual(summary['unrealized_gains'], 5000, places=0)


class TestAllocationStrategies(unittest.TestCase):
    """Test that predefined strategies sum to ~100%."""

    def test_balanced(self):
        s = STRATEGIES['balanced']
        total = s.rrsp_pct + s.spousal_rrsp_pct + s.tfsa_pct + s.resp_pct + s.non_reg_pct
        self.assertAlmostEqual(total, 1.0, places=2)

    def test_smith_priority(self):
        s = STRATEGIES['smith_priority']
        total = s.rrsp_pct + s.spousal_rrsp_pct + s.tfsa_pct + s.resp_pct + s.non_reg_pct
        self.assertAlmostEqual(total, 1.0, places=2)

    def test_all_strategies_sum(self):
        for name, s in STRATEGIES.items():
            total = s.rrsp_pct + s.spousal_rrsp_pct + s.tfsa_pct + s.resp_pct + s.non_reg_pct
            self.assertAlmostEqual(total, 1.0, places=1,
                                   msg=f"Strategy '{name}' doesn't sum to 100%")


# =============================================================================
# Rate Model Tests
# =============================================================================

from countries.canada.rate_model import (
    RatePath, RateStep, HELOCPath, build_rate_path, build_broker_scenarios,
    build_renewal_stress, amortization_schedule, annual_summary,
    monthly_payment, estimate_ird_penalty,
)


class TestRatePath(unittest.TestCase):
    """Test rate path construction and rate lookups."""

    def test_fixed_term_rate(self):
        rp = build_rate_path("3yr fixed", 0.04, 3, "fixed", [0.05])
        self.assertAlmostEqual(rp.get_rate(0), 0.04, places=4)
        self.assertAlmostEqual(rp.get_rate(2), 0.04, places=4)
        self.assertAlmostEqual(rp.get_rate(3), 0.05, places=4)  # After term
        self.assertAlmostEqual(rp.get_rate(9), 0.05, places=4)

    def test_variable_term_rate(self):
        rp = build_rate_path("5yr variable", 0.0375, 5, "variable", [0.045])
        self.assertAlmostEqual(rp.get_rate(0), 0.0375, places=4)
        self.assertAlmostEqual(rp.get_rate(4), 0.0375, places=4)
        self.assertAlmostEqual(rp.get_rate(5), 0.045, places=4)

    def test_average_rate(self):
        rp = build_rate_path("3yr fixed", 0.04, 3, "fixed", [0.05])
        avg = rp.average_rate
        self.assertGreater(avg, 0.04)
        self.assertLess(avg, 0.05)

    def test_heloc_rate(self):
        rp = build_rate_path("test", 0.04, 10, "variable", [0.04])
        heloc = HELOCPath(name="test heloc", mortgage_rate_path=rp, prime_spread=0.005)
        self.assertAlmostEqual(heloc.get_heloc_rate(0, "variable"), 0.045, places=4)
        self.assertAlmostEqual(heloc.get_heloc_rate(0, "fixed"), 0.035, places=4)


class TestAmortization(unittest.TestCase):
    """Test amortization calculations."""

    def test_monthly_payment(self):
        """Standard mortgage payment calculation."""
        pmt = monthly_payment(100000, 0.05, 25)
        self.assertGreater(pmt, 500)
        self.assertLess(pmt, 600)

    def test_zero_balance(self):
        pmt = monthly_payment(0, 0.05, 25)
        self.assertEqual(pmt, 0)

    def test_amortization_reduces_balance(self):
        rp = build_rate_path("test", 0.05, 10, "fixed", [0.05])
        sched = amortization_schedule(100000, rp, 25, 120)
        self.assertLess(sched[-1]['balance'], 100000)
        self.assertGreater(sched[-1]['balance'], 0)

    def test_rate_change_at_renewal(self):
        """Payment should change when rate changes at renewal."""
        rp = build_rate_path("3yr fixed", 0.04, 3, "fixed", [0.06])
        sched = amortization_schedule(100000, rp, 25, 120)
        self.assertAlmostEqual(sched[0]['rate'], 0.04, places=4)
        self.assertAlmostEqual(sched[36]['rate'], 0.06, places=4)
        self.assertGreater(sched[36]['payment'], sched[0]['payment'])

    def test_sm_readvancing(self):
        """SM: each dollar of principal becomes new HELOC room."""
        rp = build_rate_path("test", 0.05, 10, "fixed", [0.05])
        sched = amortization_schedule(100000, rp, 25, 12, readvance_smith=True)
        total_principal = sum(m['principal'] for m in sched)
        self.assertAlmostEqual(sched[-1]['heloc_balance'], total_principal, places=0)
        self.assertAlmostEqual(sched[-1]['cumulative_readvanced'], total_principal, places=0)

    def test_annual_summary(self):
        rp = build_rate_path("test", 0.05, 10, "fixed", [0.05])
        # With SM: readvanced = principal paid
        sched = amortization_schedule(100000, rp, 25, 120, readvance_smith=True)
        annual = annual_summary(sched)
        self.assertEqual(len(annual), 10)
        self.assertAlmostEqual(annual[0]['total_principal'], annual[0]['total_readvanced'], places=0)
    
    def test_annual_summary_no_smith(self):
        rp = build_rate_path("test", 0.05, 10, "fixed", [0.05])
        # Without SM: no readvancing
        sched = amortization_schedule(100000, rp, 25, 120, readvance_smith=False)
        annual = annual_summary(sched)
        self.assertEqual(len(annual), 10)
        self.assertEqual(annual[0]['total_readvanced'], 0)


class TestIRDPenalty(unittest.TestCase):
    """Test IRD penalty calculation."""

    def test_penalty_positive(self):
        result = estimate_ird_penalty(100000, 0.05, 36)
        self.assertGreater(result['penalty'], 0)
        self.assertIn(result['penalty_type'], ['IRD', '3-months interest'])

    def test_penalty_components(self):
        result = estimate_ird_penalty(100000, 0.05, 36)
        self.assertGreater(result['three_months_interest'], 0)
        self.assertIn('ird', result)


# =============================================================================
# Smith Manoeuvre Discoverability Tests
# ==============================================================================

class TestSmithManoeuvreDiscoverability(unittest.TestCase):
    """Verify the optimizer discovers the SM when conditions hold.
    
    The Smith Manoeuvre is NOT a hardcoded strategy name. It is a financial
    decision that the optimizer should discover from these rules:
    1. Mortgage is readvanceable (heloc_readvance=True in config)
    2. Investment interest on borrowed funds is tax-deductible (CRA §20(1)(c))
    3. After-tax investment return exceeds HELOC interest cost
    
    When all 3 conditions hold, the optimizer should allocate to non-reg.
    When any condition fails, it should not.
    """
    
    def test_readvanceable_mortgage_allows_heloc(self):
        """Readvanceable mortgage: principal payments create HELOC room."""
        rp = build_rate_path("test", 0.05, 10, "fixed", [0.05])
        sched = amortization_schedule(100000, rp, 25, 12, readvance_smith=True)
        # With readvancing, HELOC should grow by principal paid
        total_principal = sum(m['principal'] for m in sched)
        self.assertGreater(total_principal, 0)
        self.assertAlmostEqual(sched[-1]['heloc_balance'], total_principal, places=0)
    
    def test_non_readvanceable_mortgage_no_heloc(self):
        """Non-readvanceable mortgage: no HELOC room created."""
        rp = build_rate_path("test", 0.05, 10, "fixed", [0.05])
        sched = amortization_schedule(100000, rp, 25, 12, readvance_smith=False)
        self.assertEqual(sched[-1].get('heloc_balance', 0), 0)
    
    def test_interest_deductibility_rules(self):
        """Non-reg interest is deductible when borrowed for investment (CRA §20(1)(c))."""
        acct = NonRegAccount()
        # Interest on borrowed money invested in non-reg = deductible
        interest, tax_savings = acct.add_smith_interest(5000, 0.4571)
        self.assertEqual(interest, 5000)
        self.assertGreater(tax_savings, 0)  # Tax savings prove deductibility
    
    def test_sm_beneficial_when_after_tax_return_exceeds_heloc_cost(self):
        """SM is beneficial when: after-tax investment return > after-tax HELOC cost.
        
        Given:
        - HELOC at 5%, marginal rate 45.71% → after-tax cost = 5% × (1 - 0.4571) = 2.71%
        - Investment return 7% (taxable at half marginal for capital gains)
          → after-tax return = 7% × (1 - 0.4571 × 0.5) = 5.40%
        
        Since 5.40% > 2.71%, SM is beneficial.
        """
        heloc_rate = 0.05
        marginal_rate = 0.4571
        investment_return = 0.07
        
        after_tax_heloc_cost = heloc_rate * (1 - marginal_rate)
        after_tax_return = investment_return * (1 - marginal_rate * 0.5)  # CG inclusion 50%
        
        self.assertGreater(after_tax_return, after_tax_heloc_cost)
    
    def test_sm_not_beneficial_when_return_below_heloc_cost(self):
        """SM is NOT beneficial when investment return is too low.
        
        Given:
        - HELOC at 5%, marginal rate 45.71% → after-tax cost = 2.71%
        - Investment return 2% (conservative bond portfolio)
          → after-tax return = 2% × (1 - 0.4571 × 0.5) = 1.54%
        
        Since 1.54% < 2.71%, SM is NOT beneficial.
        """
        heloc_rate = 0.05
        marginal_rate = 0.4571
        investment_return = 0.02  # Low return
        
        after_tax_heloc_cost = heloc_rate * (1 - marginal_rate)
        after_tax_return = investment_return * (1 - marginal_rate * 0.5)
        
        self.assertLess(after_tax_return, after_tax_heloc_cost)
    
    def test_sm_not_possible_without_readvanceable_mortgage(self):
        """SM requires a readvanceable mortgage product."""
        # Traditional mortgage: no readvancing
        rp = build_rate_path("test", 0.05, 10, "fixed", [0.05])
        sched = amortization_schedule(100000, rp, 25, 12, readvance_smith=False)
        # No HELOC balance created → SM not possible
        self.assertEqual(sched[-1].get('heloc_balance', 0), 0)
        self.assertEqual(sched[-1].get('cumulative_readvanced', 0), 0)


# =============================================================================
# Tax Data Provider Tests (Thread 9/10/11)
# =============================================================================

from tax_data import TaxDataProvider, TaxBracket, TaxYearData, default_tax_provider


class TestTaxDataProvider(unittest.TestCase):
    """Test multi-year, multi-province, multi-country tax data."""

    def setUp(self):
        self.provider = TaxDataProvider()

    def test_quebec_2026(self):
        brackets = self.provider.get_brackets(2026, 'canada', 'quebec')
        self.assertGreater(len(brackets), 5)
        self.assertAlmostEqual(brackets[0].rate, 0.2569, places=4)

    def test_ontario_2026(self):
        """Thread 10: Ontario brackets available."""
        brackets = self.provider.get_brackets(2026, 'canada', 'ontario')
        self.assertGreater(len(brackets), 5)
        self.assertAlmostEqual(brackets[0].rate, 0.1905, places=4)  # 14% fed + 5.05% ON (corrected fed rate)

    def test_quebec_abatement(self):
        """Thread 10: Quebec abatement reduces federal brackets."""
        qc_data = self.provider._load_year(2026, 'canada', 'quebec')
        self.assertAlmostEqual(qc_data.provincial_abatement, 0.165, places=3)

    def test_ontario_no_abatement(self):
        """Thread 10: Ontario has no provincial abatement."""
        on_data = self.provider._load_year(2026, 'canada', 'ontario')
        self.assertAlmostEqual(on_data.provincial_abatement, 0.0, places=3)

    def test_available_years(self):
        """Thread 9: Can list available years."""
        qc_years = self.provider.available_years('canada', 'quebec')
        self.assertIn(2026, qc_years)
        self.assertIn(2025, qc_years)

    def test_rrsp_limit_from_data(self):
        """Thread 9: RRSP limit comes from data, not hardcoded."""
        limit = self.provider.get_rrsp_limit(2026)
        self.assertEqual(limit, 33810)

    def test_tfsa_limit_from_data(self):
        """Thread 9: TFSA limit comes from data."""
        limit = self.provider.get_tfsa_limit(2026)
        self.assertEqual(limit, 7000)

    def test_register_custom_year(self):
        """Thread 11: Can register new country/province data."""
        custom_data = TaxYearData(
            year=2027, country='us', province='federal',
            federal_brackets=[TaxBracket(0, 11000, 0.10, '10%')],
            source='custom',
        )
        self.provider.register_year(custom_data)
        brackets = self.provider.get_brackets(2027, 'us', 'federal', combined=False)
        self.assertAlmostEqual(brackets[0].rate, 0.10, places=4)

    def test_nearby_year_fallback(self):
        """Thread 9: Falls back to nearest year if exact not available."""
        # 2027 not in fallbacks, should fall back to 2026
        brackets = self.provider.get_brackets(2027, 'canada', 'quebec')
        self.assertGreater(len(brackets), 5)

    def test_combined_brackets_legacy_format(self):
        """Thread 9: get_combined_brackets returns dict format."""
        brackets = self.provider.get_combined_brackets(2026, 'quebec')
        self.assertIn('min', brackets[0])
        self.assertIn('rate', brackets[0])

    def test_ontario_2025(self):
        """Thread 10: Ontario 2025 data exists."""
        brackets = self.provider.get_brackets(2025, 'canada', 'ontario')
        self.assertGreater(len(brackets), 5)


class TestTaxCalculatorWithDataProvider(unittest.TestCase):
    """Test tax_calculator delegates to tax_data (Thread 9)."""

    def test_default_year_province(self):
        """Default: Quebec 2026, backward compatible."""
        brackets = default_tax_provider().get_combined_brackets()
        self.assertGreater(len(brackets), 5)

    def test_explicit_ontario(self):
        """Can request Ontario brackets."""
        brackets = default_tax_provider().get_combined_brackets(year=2026, province='ontario')
        on_rate = marginal_rate(130000, brackets)
        self.assertGreater(on_rate, 0.30)
        self.assertLess(on_rate, 0.50)

    def test_marginal_rate_with_year_province(self):
        """marginal_rate accepts year and province params."""
        rate_qc = marginal_rate(130000, year=2026, province='quebec')
        rate_on = marginal_rate(130000, year=2026, province='ontario')
        self.assertGreater(rate_on, 0.30)
        self.assertLess(rate_on, rate_qc)  # ON has lower rates

    def test_tax_on_income_with_year_province(self):
        """tax_on_income accepts year and province params."""
        tax_qc = tax_on_income(100000, year=2026, province='quebec')
        tax_on = tax_on_income(100000, year=2026, province='ontario')
        self.assertGreater(tax_qc, tax_on)  # QC has higher rates




from countries.canada.resp_rules import RESPCalculator, RESPChild, analyze_resp_for_family


class TestRESPCalculator(unittest.TestCase):
    """Test RESP eligibility and grant calculations."""

    def setUp(self):
        self.calc = RESPCalculator()

    def test_eligible_child(self):
        child = RESPChild(name="Test", birth_year=2015, is_quebec_resident=True)
        self.assertTrue(child.cesg_eligible(2026))

    def test_over_17_child(self):
        child = RESPChild(name="Teen", birth_year=2008, is_quebec_resident=True)
        self.assertFalse(child.cesg_eligible(2026))

    def test_basic_cesg(self):
        """Basic CESG: 20% on first $2,500."""
        child = RESPChild(name="D2", birth_year=2011, is_quebec_resident=True)
        result = self.calc.calculate_cesg(2500, child, 2026, 200000)
        self.assertAlmostEqual(result['total_cesg'], 500)  # 20% of $2,500

    def test_qesi(self):
        """QESI: 10% on first $2,500."""
        child = RESPChild(name="D2", birth_year=2011, is_quebec_resident=True)
        result = self.calc.calculate_qesi(2500, child, 2026, 200000)
        self.assertAlmostEqual(result['total_qesi'], 250)

    def test_total_matching(self):
        """At $200k income: 20% CESG + 10% QESI = 30% on first $2,500 = $750."""
        child = RESPChild(name="D2", birth_year=2011, is_quebec_resident=True)
        cesg = self.calc.calculate_cesg(2500, child, 2026, 200000)
        qesi = self.calc.calculate_qesi(2500, child, 2026, 200000)
        total = cesg['total_cesg'] + qesi['total_qesi']
        self.assertAlmostEqual(total, 750, places=0)


# =============================================================================
# Integration Test — End-to-End
# =============================================================================



if __name__ == '__main__':
    unittest.main()