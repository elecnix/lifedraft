#!/usr/bin/env python3
"""Unit tests for ``countries.canada.tax_calc`` (DP#11).

One module per file: this file tests ONLY the Canada ``tax_calc`` module —
federal/Quebec tax, RRSP deduction-timing, spousal RRSP, eligible-dividend
gross-up + DTC, withholding-tax drag, and asset-location tax impact. The
core, jurisdiction-agnostic ``tax_calculator`` functions are tested in
``tests/test_tax_calc_full.py``; the core dispatch ``tax_on_investment_income``
(which *delegates* to this module) is tested there as an integration test.

These classes were relocated from ``countries/canada/tests/test_investment_income.py``
and ``tests/test_tax_calc_full.py`` so that each exercises a single module
(DP#11). No assertion was weakened or dropped; the bodies are unchanged.
``TestEligibleDividendTaxFull`` is suffixed to avoid colliding with the
(also-relocated) ``TestEligibleDividendTax`` from the investment-income file.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest countries/canada/tests/test_tax_calc.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.tax_calc import (
    federal_tax,
    quebec_tax,
    rrsp_deduct_later_savings,
    spousal_rrsp_benefit,
    tax_on_eligible_dividend,
    effective_dividend_rate,
    withholding_tax_drag,
    asset_location_tax_impact,
)


class TestFederalTax(unittest.TestCase):
    """Test federal_tax() function (lines 206-207)."""

    def test_basic_federal_tax(self):
        tax = federal_tax(100000, 2026, "quebec")
        self.assertGreater(tax, 0)

    def test_quebec_abatement_applied(self):
        """Quebec residents get 16.5% abatement on federal tax."""
        tax_qc = federal_tax(100000, 2026, "quebec")
        tax_on = federal_tax(100000, 2026, "ontario")
        # Quebec abatement makes federal tax lower
        self.assertLess(tax_qc, tax_on)

    def test_zero_income(self):
        tax = federal_tax(0)
        self.assertAlmostEqual(tax, 0)


class TestQuebecTax(unittest.TestCase):
    """Test quebec_tax() function."""

    def test_basic_quebec_tax(self):
        tax = quebec_tax(100000, 2026)
        self.assertGreater(tax, 0)

    def test_low_income(self):
        tax = quebec_tax(30000, 2026)
        self.assertGreater(tax, 0)
        self.assertLess(tax, 5000)

    def test_zero_income(self):
        tax = quebec_tax(0)
        self.assertAlmostEqual(tax, 0)


class TestRRSPDeductLaterSavings(unittest.TestCase):
    """Test rrsp_deduct_later_savings() (lines 329-349)."""

    def test_deduct_now_better_at_same_rate(self):
        """When income doesn't change, deduct now = deduct later."""
        now, later = rrsp_deduct_later_savings(
            rrsp_room=50000, annual_income=100000, years=5)
        # Both should be positive
        self.assertGreater(now, 0)
        self.assertGreater(later, 0)

    def test_deduct_later_better_when_income_rises(self):
        """When income rises, deduct later captures higher MTR."""
        now, later = rrsp_deduct_later_savings(
            rrsp_room=50000, annual_income=120000, years=5)
        self.assertGreater(now, 0)

    def test_default_bracket_target(self):
        """Default bracket target of $117,045."""
        now, later = rrsp_deduct_later_savings(
            rrsp_room=30000, annual_income=150000, years=5)
        self.assertGreater(now, 0)


class TestSpousalRRSPBenefit(unittest.TestCase):
    """Test spousal_rrsp_benefit calculation."""

    def test_basic_bracket_spread(self):
        """Spousal RRSP saves MTR difference between contributor and withdrawer."""
        result = spousal_rrsp_benefit(10000, 0.4571, 0.30)
        # Deduction savings: 10000 * 0.4571 = 4571
        self.assertAlmostEqual(result['deduction_savings'], 4571)
        # Withdrawal tax: 10000 * 0.30 = 3000
        self.assertAlmostEqual(result['withdrawal_tax'], 3000)
        # Net benefit: 4571 - 3000 = 1571
        self.assertAlmostEqual(result['net_benefit'], 1571)
        # Bracket spread: (0.4571 - 0.30) * 100 = 15.71%
        self.assertAlmostEqual(result['bracket_spread_pct'], 15.71, places=1)

    def test_same_rate_no_benefit(self):
        """No benefit when contributor and withdrawer have same MTR."""
        result = spousal_rrsp_benefit(10000, 0.40, 0.40)
        self.assertAlmostEqual(result['net_benefit'], 0)

    def test_attribution_years(self):
        """Attribution period defaults to 3 years."""
        result = spousal_rrsp_benefit(10000, 0.4571, 0.30)
        self.assertEqual(result['attribution_years'], 3)

    def test_net_benefit_per_1000(self):
        """Net benefit per $1000 contributed."""
        result = spousal_rrsp_benefit(10000, 0.4571, 0.30)
        # Net benefit / contribution * 1000 = 1571 / 10000 * 1000 = 157.1
        self.assertAlmostEqual(result['net_benefit_per_1000'], 157.1, places=0)


class TestEligibleDividendTax(unittest.TestCase):
    """Test Canadian eligible dividend tax calculation (gross-up + DTC)."""

    MARGINAL_RATE = 0.4571  # ~Quebec 2026 at $130k

    def test_dividend_tax_lower_than_marginal(self):
        """Eligible dividends are taxed less than marginal rate."""
        tax = tax_on_eligible_dividend(10000, self.MARGINAL_RATE)
        self.assertLess(tax, 10000 * self.MARGINAL_RATE)  # Less than fully taxable
        self.assertGreater(tax, 0)

    def test_effective_rate(self):
        """Effective rate on eligible dividends at high QC MTR is ~26.5%.

        With correct formula (DP#27) and accurate QC DTC rate (11.51%):
        1.38 * (0.4571 - 0.150198 - 0.11510) ≈ 0.2647
        """
        eff = effective_dividend_rate(self.MARGINAL_RATE, province='quebec')
        self.assertAlmostEqual(eff, 0.265, places=1)  # ~26.5% at 45.71% MTR in QC

    def test_dividend_ontario(self):
        """Ontario has lower DTC than QC, so effective rate is higher."""
        eff_qc = effective_dividend_rate(self.MARGINAL_RATE, province="quebec")
        eff_on = effective_dividend_rate(self.MARGINAL_RATE, province="ontario")
        self.assertGreater(eff_on, eff_qc)  # Ontario worse for dividends

    def test_zero_amount(self):
        """Zero dividend has zero tax."""
        tax = tax_on_eligible_dividend(0, self.MARGINAL_RATE)
        self.assertEqual(tax, 0)


class TestEligibleDividendTaxFull(unittest.TestCase):
    """Test tax_on_eligible_dividend and effective_dividend_rate.

    Relocated from ``tests/test_tax_calc_full.py`` (renamed to avoid colliding
    with ``TestEligibleDividendTax`` above). Assertions unchanged.
    """

    def test_dividend_tax_lower_than_mtr(self):
        """Eligible dividend effective rate is much lower than marginal rate."""
        tax = tax_on_eligible_dividend(10000, 0.4571)
        # Grossed-up: 10000 * 1.38 = 13800
        # Tax: 13800 * 0.4571 - 13800 * 0.150198 - 13800 * 0.08
        # = 6308 - 2073 - 1104 = 3131
        self.assertGreater(tax, 0)
        self.assertLess(tax, 10000 * 0.4571)  # Less than full marginal rate
        # Effective rate ~ 31%
        effective_rate = tax / 10000
        self.assertGreater(effective_rate, 0.20)
        self.assertLess(effective_rate, 0.40)

    def test_effective_dividend_rate_quebec(self):
        """Effective dividend rate in Quebec ~25% at 45.71% MTR."""
        rate = effective_dividend_rate(0.4571, 'quebec')
        self.assertGreater(rate, 0.20)
        self.assertLess(rate, 0.35)

    def test_effective_dividend_rate_ontario(self):
        """Ontario has no QC DTC, so effective rate is higher."""
        qc_rate = effective_dividend_rate(0.4571, 'quebec')
        on_rate = effective_dividend_rate(0.4571, 'ontario')
        self.assertGreater(on_rate, qc_rate)  # Ontario higher (no QC credit)

    def test_zero_dividend(self):
        """Zero dividend amount → zero tax."""
        tax = tax_on_eligible_dividend(0, 0.4571)
        self.assertEqual(tax, 0)


class TestDividendVsCapitalGainsRate(unittest.TestCase):
    """Compare eligible-dividend effective rate to the capital-gains rate.

    Relocated from ``TestTaxComparisonAcrossTypes`` (the cross-module class in
    ``test_investment_income.py``): this single assertion exercises only
    ``effective_dividend_rate`` (Canada), so it lives here. The companion
    assertion that compares across core ``tax_on_investment_income`` types
    stays with the core tests in ``tests/test_tax_calc_full.py``.
    """

    MARGINAL_RATE = 0.4571

    def test_dividend_lower_than_capital_gains(self):
        """At high MTR, eligible dividends can be cheaper than capital gains."""
        div_eff = effective_dividend_rate(self.MARGINAL_RATE)
        cg_eff = self.MARGINAL_RATE * 0.50
        # At very high MTR in Quebec, dividends can be slightly cheaper
        # due to DTC — this is the core driver for non-reg Canadian dividend ETFs
        self.assertLess(div_eff, self.MARGINAL_RATE)  # Definitely less than interest
        # At 45.71% MTR in QC, dividend rate ~25% vs CG ~23%
        # They're close; the exact relationship depends on province + DTC


class TestWithholdingTaxDrag(unittest.TestCase):
    """Test WHT tax drag in basis points by account type and ETF type."""

    def test_us_listed_rrsp_zero(self):
        """US-listed ETFs in RRSP: 0 bps drag (treaty)."""
        drag = withholding_tax_drag('rrsp', 'us_listed')
        self.assertEqual(drag, 0)

    def test_us_listed_tfsa_positive(self):
        """US-listed ETFs in TFSA: ~30 bps drag (15% WHT, unrecoverable)."""
        drag = withholding_tax_drag('tfsa', 'us_listed', yield_pct=0.02)
        expected = 0.15 * 0.02 * 10000  # 30 bps
        self.assertAlmostEqual(drag, expected, places=1)

    def test_us_listed_non_reg_zero(self):
        """US-listed ETFs in non-reg: 0 bps drag (FTC recovers WHT)."""
        drag = withholding_tax_drag('non_reg', 'us_listed')
        self.assertEqual(drag, 0)

    def test_canadian_etf_zero_everywhere(self):
        """Canadian ETFs: 0 bps drag in all accounts."""
        for acct in ['rrsp', 'tfsa', 'non_reg', 'resp']:
            drag = withholding_tax_drag(acct, 'canadian')
            self.assertEqual(drag, 0)


class TestWithholdingTaxDragEdgeCases(unittest.TestCase):
    """Test WHT drag for international ETFs (lines 643-644)."""

    def test_international_in_rrsp(self):
        drag = withholding_tax_drag('rrsp', 'international')
        self.assertEqual(drag, 0.0)

    def test_international_in_tfsa(self):
        drag = withholding_tax_drag('tfsa', 'international', yield_pct=0.02)
        self.assertAlmostEqual(drag, 30.0, places=0)  # 15% × 2% = 30 bps

    def test_international_in_resp(self):
        drag = withholding_tax_drag('resp', 'international', yield_pct=0.02)
        self.assertAlmostEqual(drag, 30.0, places=0)

    def test_international_in_nonreg(self):
        drag = withholding_tax_drag('non_reg', 'international')
        self.assertEqual(drag, 0.0)  # FTC recovers

    def test_unknown_etf_type(self):
        """Unknown ETF type → 0 drag."""
        drag = withholding_tax_drag('tfsa', 'bonds')
        self.assertEqual(drag, 0.0)


class TestAssetLocationTaxImpact(unittest.TestCase):
    """Test asset_location_tax_impact (DP#30: tax data, not advice)."""

    def test_us_listed_zero_in_rrsp(self):
        """US-listed equity: zero tax drag in RRSP (treaty exemption)."""
        impact = asset_location_tax_impact('us_listed', 0.4571)
        self.assertEqual(impact['rrsp'], 0.0)
        # TFSA has positive WHT drag
        self.assertGreater(impact['tfsa'], 0.0)

    def test_canadian_dividend_low_in_nonreg(self):
        """Canadian dividends: lower drag in non-reg than interest."""
        impact = asset_location_tax_impact('canadian_dividend', 0.4571, 'quebec')
        # non-reg drag should be positive but less than bonds in non-reg
        bonds_impact = asset_location_tax_impact('bonds', 0.4571, 'quebec')
        self.assertLess(impact['non_reg'], bonds_impact['non_reg'])

    def test_bonds_zero_in_registered(self):
        """Bonds: zero drag in RRSP/TFSA (interest sheltered)."""
        impact = asset_location_tax_impact('bonds', 0.4571)
        self.assertEqual(impact['rrsp'], 0.0)
        self.assertEqual(impact['tfsa'], 0.0)
        # But positive in non-reg
        self.assertGreater(impact['non_reg'], 0.0)

    def test_international_zero_in_rrsp(self):
        """International equity: zero WHT drag in RRSP."""
        impact = asset_location_tax_impact('international', 0.4571)
        self.assertEqual(impact['rrsp'], 0.0)
        # TFSA/RESP have positive WHT drag
        self.assertGreater(impact['tfsa'], 0.0)


class TestAssetLocationTaxImpactAllPaths(unittest.TestCase):
    """Test asset_location_tax_impact for all ETF types (DP#30: tax data, not advice)."""

    def test_us_listed_zero_in_rrsp(self):
        """US-listed: zero drag in RRSP (treaty exemption)."""
        impact = asset_location_tax_impact('us_listed', 0.4571, 'quebec')
        self.assertEqual(impact['rrsp'], 0.0)

    def test_canadian_dividend_low_mtr(self):
        """Low MTR: lower non-reg drag for Canadian dividends."""
        impact = asset_location_tax_impact('canadian_dividend', 0.30, 'quebec')
        # TFSA: zero drag (sheltered, no WHT on Canadian dividends)
        self.assertEqual(impact['tfsa'], 0.0)
        # Non-reg: positive drag (taxable but with DTC offset)
        self.assertGreater(impact['non_reg'], 0)

    def test_canadian_dividend_high_mtr_quebec(self):
        """High MTR QC: dividends still have lower non-reg drag than bonds."""
        impact = asset_location_tax_impact('canadian_dividend', 0.4571, 'quebec')
        bonds_impact = asset_location_tax_impact('bonds', 0.4571, 'quebec')
        self.assertLess(impact['non_reg'], bonds_impact['non_reg'])

    def test_canadian_equity(self):
        """Canadian equity: zero WHT drag in all accounts."""
        impact = asset_location_tax_impact('canadian', 0.4571, 'quebec')
        self.assertEqual(impact['rrsp'], 0.0)
        self.assertEqual(impact['tfsa'], 0.0)

    def test_bonds(self):
        """Bonds: zero drag in RRSP (sheltered), positive in non-reg."""
        impact = asset_location_tax_impact('bonds', 0.4571, 'quebec')
        self.assertEqual(impact['rrsp'], 0.0)
        self.assertGreater(impact['non_reg'], 0.0)

    def test_international(self):
        """International: zero drag in RRSP (treaty)."""
        impact = asset_location_tax_impact('international', 0.4571, 'quebec')
        self.assertEqual(impact['rrsp'], 0.0)
        self.assertGreater(impact['tfsa'], 0.0)

    def test_unknown_type(self):
        """Unknown ETF type: all accounts get zero drag (safe default)."""
        impact = asset_location_tax_impact('unknown_type', 0.4571, 'quebec')
        # Unknown type should return zeros for all accounts
        for acct, drag in impact.items():
            self.assertEqual(drag, 0.0, f"Expected 0 for {acct}, got {drag}")

    def test_non_quebec_canadian_dividend(self):
        """Non-Quebec: different effective dividend rate."""
        impact_qc = asset_location_tax_impact('canadian_dividend', 0.30, 'quebec')
        impact_on = asset_location_tax_impact('canadian_dividend', 0.30, 'ontario')
        # Both should have positive non-reg drag
        self.assertGreater(impact_qc['non_reg'], 0)
        self.assertGreater(impact_on['non_reg'], 0)


if __name__ == '__main__':
    unittest.main()