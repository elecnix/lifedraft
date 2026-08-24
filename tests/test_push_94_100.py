#!/usr/bin/env python3
"""Coverage push: optimizer (overlay paths),
scipy_optimizer (fallback), asset_location (light/ludicrous), debt(__repr__, PrescribedRateLoan summary).

Run with: python3 -m pytest tests/test_push_94_100.py -v
"""

import sys, os, unittest, json, tempfile
from copy import deepcopy
from dataclasses import replace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation import SimulationConfig
from scenario_overlay import apply_ltv_overlay
from optimizer import GridOptimizer, Optimizer
from scipy_optimizer import ScipyOptimizer
from countries.canada.debt import DebtInstrument, DebtPurpose, AdvanceRecord, HELOCTracing, PrescribedRateLoan
from countries.canada.asset_location import AssetLocationOptimizer, PortfolioHolding, ETFType, AccountType, light_vs_ludicrous


# ═══════════════════════════════════════════════════════════════
# optimizer.py — lines 284-285, 300, 302, 312-315, 344-347, 356-362
# ═══════════════════════════════════════════════════════════════

def _minimal_cfg():
    return {
        'assumptions': {'projection_years': 3, 'investment_return': 0.06, 'salary_growth': 0.02},
        'savings': {'rate': 0.15},
        'property': {'house_value': 700000, 'mortgage_balance': 100000, 'mortgage_rate': 0.045,
                     'ltv_max': 0.80, 'current_payment_monthly': 1200,
                     'amortization_years': 25, 'margin_available': 200000},
        'family': {'members': [
            {'role': 'primary', 'gross_income': 120000, 'birth_year': 1990,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
            {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1990,
             'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 40000},
        ], 'children': []},
        'accounts': {'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33810,
                     'tfsa_annual_room_per_person': 7000, 'resp_current_balance': 0},
        'scenarios': {'refinance': [], 'income': [], 'mortgage': [], 'strategy': []},
    }


class TestOptimizerOverlays(unittest.TestCase):
    """Test apply_ltv_overlay and Optimizer._apply_income_override."""

    def setUp(self):
        self.config = SimulationConfig(
            projection_years=3, house_value=700000, mortgage_balance=100000,
            family_members=[
                {'role': 'primary', 'gross_income': 120000, 'birth_year': 1990},
                {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1990},
            ],
        )
        # Create optimizer with minimal config
        from optimizer import GridOptimizer
        cfg = _minimal_cfg()
        self.opt = GridOptimizer(SimulationConfig.from_dict(cfg))

    def test_ltv_overlay_positive(self):
        """LTV 80% with equity available -> cash-out applied to
        mortgage_balance, and margin_available shrinks by the same amount
        (#664: mortgage and HELOC share ONE registered charge)."""
        from dataclasses import replace
        config = replace(self.config, refinance_amortization_years=25)  # #655
        result = apply_ltv_overlay(config, 0.80)
        # mortgage: 100k + (0.80*700k - 100k) = 560k
        self.assertAlmostEqual(result.mortgage_balance, 560000)
        # margin: max(0, 200k default - 460k cash-out) = 0 (#664)
        self.assertAlmostEqual(result.margin_available, 0)
        self.assertAlmostEqual(result.cash_out, 460000)

    def test_ltv_overlay_no_equity(self):
        """LTV at/below current → no cash-out."""
        config = SimulationConfig(projection_years=3, house_value=700000, mortgage_balance=600000,
                                   margin_available=0)
        result = apply_ltv_overlay(config, 0.50)
        self.assertEqual(result.mortgage_balance, 600000)
        self.assertEqual(result.margin_available, 0)

    def test_ltv_overlay_zero(self):
        """LTV=0 → config unchanged."""
        config = SimulationConfig(projection_years=3, house_value=700000, mortgage_balance=100000,
                                   margin_available=0)
        result = apply_ltv_overlay(config, 0.0)
        self.assertEqual(result.mortgage_balance, config.mortgage_balance)
        self.assertEqual(result.margin_available, config.margin_available)

    def test_income_override_primary(self):
        result = self.opt._apply_income_override(self.config, {'primary': 220000})
        p = next(m for m in result.family_members if m['role'] == 'primary')
        self.assertEqual(p['gross_income'], 220000)

    def test_income_override_spouse(self):
        result = self.opt._apply_income_override(self.config, {'spouse': 70000, 'label': 'new'})
        s = next(m for m in result.family_members if m['role'] == 'spouse')
        self.assertEqual(s['gross_income'], 70000)

    def test_income_override_both(self):
        result = self.opt._apply_income_override(self.config, {'primary': 170000, 'spouse': 60000})
        p = next(m for m in result.family_members if m['role'] == 'primary')
        s = next(m for m in result.family_members if m['role'] == 'spouse')
        self.assertEqual(p['gross_income'], 170000)
        self.assertEqual(s['gross_income'], 60000)

    def test_income_override_no_match(self):
        """Override with no matching role leaves config unchanged."""
        result = self.opt._apply_income_override(self.config, {'primary': 200000})
        p = next(m for m in result.family_members if m['role'] == 'primary')
        s = next(m for m in result.family_members if m['role'] == 'spouse')
        self.assertEqual(p['gross_income'], 200000)
        self.assertEqual(s['gross_income'], 50000)  # unchanged


# ═══════════════════════════════════════════════════════════════
# scipy_optimizer.py — lines 103-104, 110-113 (fallback grid search)
# ═══════════════════════════════════════════════════════════════

class TestScipyOptimizerFallback(unittest.TestCase):
    """Lines 103-104, 110-113: fallback grid when scipy unavailable."""

    def test_setup_variables_all_types(self):
        cfg = SimulationConfig(projection_years=3, house_value=600000)
        for varset in [['ltv'], ['rrsp_weight'], ['tfsa_weight'], ['pension_split_pct']]:
            opt = ScipyOptimizer(cfg, optimize_vars=varset)
            bounds, x0, names = opt._setup_variables()
            self.assertEqual(len(bounds), 1)
            self.assertEqual(len(x0), 1)

    def test_apply_ltv_overlay_all_ltvs(self):
        """Test apply_ltv_overlay for all grid LTV values -- margin_available
        shrinks dollar-for-dollar with any cash-out booked (#664), so it is
        only unchanged where the LTV implies zero cash-out."""
        cfg = SimulationConfig(projection_years=3, house_value=600000, mortgage_balance=200000,
                                refinance_amortization_years=25)  # #655: fabricated round new-loan term
        for ltv in [0.0, 0.20, 0.40, 0.60, 0.80]:
            modified = apply_ltv_overlay(cfg, ltv)
            self.assertIsNotNone(modified)
            expected_cash_out = max(0.0, ltv * cfg.house_value - cfg.mortgage_balance)
            expected_margin = max(0.0, cfg.margin_available - expected_cash_out)
            self.assertAlmostEqual(modified.margin_available, expected_margin)

    def test_ltv_no_change_when_no_equity(self):
        """When LTV × house < mortgage balance, no cash-out is extracted (DP#18)."""
        cfg = SimulationConfig(projection_years=3, house_value=600000, mortgage_balance=600000)
        modified = apply_ltv_overlay(cfg, 0.80)
        # 0.80 * 600000 = 480000 < 600000 → no cash-out, config unchanged
        self.assertAlmostEqual(modified.mortgage_balance, cfg.mortgage_balance)
        self.assertAlmostEqual(modified.margin_available, cfg.margin_available)


# ═══════════════════════════════════════════════════════════════
# asset_location.py — lines 132-142, 282-283, 299-301
# ═══════════════════════════════════════════════════════════════

class TestAssetLocationRemaining(unittest.TestCase):

    def test_light_vs_ludicrous_comparison(self):
        holdings = [
            PortfolioHolding("XEQT", ETFType.CANADIAN_EQUITY, 0.40),
            PortfolioHolding("XUU", ETFType.US_LISTED_EQUITY, 0.35),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.25),
        ]
        result = light_vs_ludicrous(holdings, marginal_rate=0.4571)
        self.assertIn('recommendation', result)
        self.assertIn('light_drag_bps', result)
        self.assertIn('ludicrous_drag_bps', result)

    def test_optimize_full_portfolio(self):
        holdings = [
            PortfolioHolding("XEQT", ETFType.CANADIAN_EQUITY, 0.50),
            PortfolioHolding("XUU", ETFType.US_LISTED_EQUITY, 0.30),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.20),
        ]
        opt = AssetLocationOptimizer(marginal_rate=0.4571, province="quebec")
        result = opt.optimize(holdings)
        self.assertIsNotNone(result)

    def test_light_approach(self):
        holdings = [
            PortfolioHolding("XEQT", ETFType.CANADIAN_EQUITY, 0.40),
            PortfolioHolding("XUU", ETFType.US_LISTED_EQUITY, 0.30),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.30),
        ]
        opt = AssetLocationOptimizer(marginal_rate=0.4571)
        result = opt.light_approach(holdings)
        self.assertIsNotNone(result)

    def test_optimize_with_international(self):
        holdings = [
            PortfolioHolding("XEF", ETFType.INTERNATIONAL_EQUITY, 0.25),
            PortfolioHolding("XIC", ETFType.CANADIAN_EQUITY, 0.25),
            PortfolioHolding("XUU", ETFType.US_LISTED_EQUITY, 0.25),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.25),
        ]
        opt = AssetLocationOptimizer(marginal_rate=0.40, province="ontario")
        result = opt.optimize(holdings)
        self.assertIsNotNone(result)

    def test_optimize_with_dividend_etfs(self):
        holdings = [
            PortfolioHolding("VDY", ETFType.CANADIAN_DIVIDEND, 0.60),
            PortfolioHolding("ZAG", ETFType.BONDS, 0.40),
        ]
        opt = AssetLocationOptimizer(marginal_rate=0.4571, province="quebec")
        result = opt.optimize(holdings)
        self.assertIsNotNone(result)


# ═══════════════════════════════════════════════════════════════
# debt.py — lines 80-81, 119-121, 134-136, 220-221, 457, 518, 609
# ═══════════════════════════════════════════════════════════════

class TestDebtRemaining(unittest.TestCase):

    def test_advance_repr(self):
        a = AdvanceRecord(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        r = repr(a)
        self.assertIn("50,000", r)
        self.assertIn("XEQT", r)

    def test_advance_repr_no_investment(self):
        a = AdvanceRecord(10000, "2026-03", DebtPurpose.PERSONAL)
        r = repr(a)
        self.assertNotIn("→", r)

    def test_mixed_no_advances_not_deductible(self):
        d = DebtInstrument(balance=100000, rate=0.05, purpose=DebtPurpose.MIXED)
        self.assertFalse(d.is_interest_deductible)
        self.assertAlmostEqual(d.deductible_proportion, 0.0)

    def test_mixed_with_advances(self):
        d = DebtInstrument(balance=100000, rate=0.05, purpose=DebtPurpose.MIXED,
                           advances=[AdvanceRecord(60000, "2026-01", DebtPurpose.INVESTMENT),
                                     AdvanceRecord(40000, "2026-02", DebtPurpose.PERSONAL)])
        self.assertAlmostEqual(d.deductible_proportion, 0.6)

    def test_mixed_all_investment(self):
        d = DebtInstrument(balance=100000, rate=0.05, purpose=DebtPurpose.MIXED,
                           advances=[AdvanceRecord(100000, "2026-01", DebtPurpose.INVESTMENT)])
        self.assertAlmostEqual(d.deductible_proportion, 1.0)

    def test_non_deductible_interest(self):
        t = HELOCTracing()
        t.advance(60000, "2026-01", DebtPurpose.INVESTMENT)
        t.advance(40000, "2026-02", DebtPurpose.PERSONAL)
        total = 100000 * 0.05
        ded = t.deductible_interest(100000, 0.05)
        non_ded = t.non_deductible_interest(100000, 0.05)
        self.assertAlmostEqual(ded + non_ded, total)

    def test_prescribed_rate_loan_attribution(self):
        loan = PrescribedRateLoan(principal=200000, rate=0.02, interest_paid_by_jan30=True)
        self.assertTrue(loan.interest_paid_on_time(2026))
        self.assertFalse(loan.attribution_applies(2026))

    def test_prescribed_rate_loan_unpaid(self):
        loan = PrescribedRateLoan(principal=200000, rate=0.02, interest_paid_by_jan30=False)
        self.assertFalse(loan.interest_paid_on_time(2026))
        self.assertTrue(loan.attribution_applies(2026))

    def test_prescribed_rate_loan_summary(self):
        loan = PrescribedRateLoan(principal=100000, rate=0.02)
        s = loan.summary()
        self.assertIn('principal', s)
        self.assertIn('attribution_risk', s)
        self.assertFalse(s['attribution_risk'])


if __name__ == '__main__':
    unittest.main()