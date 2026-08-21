#!/usr/bin/env python3
"""Unit tests for income_type.py — DP#27 investment income type taxonomy.

Tests verify:
- Each income type has a distinct effective tax rate
- Correct ordering at various MTR levels
- Tiered capital gains inclusion rate (50% below $250K, 66.67% above, for 2024+)
- Year-versioned DTC rates (DP#20, DP#12)
- Correct effective dividend rate formula: gross_up * (MTR - DTCs) not MTR - gross_up*DTCs
- RRSP treaty eliminates US WHT
- TFSA has unrecoverable WHT drag
- Zero MTR → zero effective tax (except WHT)
- Provincial DTC varies by province
- after_tax_return is consistent with effective_tax_rate
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest
from countries.canada.income_type import (
    IncomeType, effective_tax_rate, capital_gains_inclusion_rate,
    wht_drag, after_tax_return,
    return_of_capital_treatment, stock_option_benefit, foreign_tax_credit,
)


class TestReturnOfCapital(unittest.TestCase):
    """ROC reduces ACB and is not income (ITA s.53(2), issue #316)."""

    def test_roc_reduces_acb_not_taxed(self):
        r = return_of_capital_treatment(distribution=3000, adjusted_cost_base=10000)
        self.assertAlmostEqual(r['new_acb'], 7000)
        self.assertAlmostEqual(r['taxable_income'], 0.0)
        self.assertAlmostEqual(r['taxable_now'], 0.0)

    def test_roc_below_zero_acb_triggers_gain(self):
        """ROC beyond remaining ACB is an immediate capital gain (s.40(3))."""
        r = return_of_capital_treatment(distribution=5000, adjusted_cost_base=2000)
        self.assertAlmostEqual(r['new_acb'], 0.0)
        self.assertAlmostEqual(r['taxable_now'], 3000)


class TestStockOptionBenefit(unittest.TestCase):
    """Employee stock option benefit and 50% deduction (ITA s.7/s.110, issue #316)."""

    def test_qualifying_option_half_taxed(self):
        r = stock_option_benefit(fmv_at_exercise=50, exercise_price=30, shares=1000)
        self.assertAlmostEqual(r['gross_benefit'], 20000)
        self.assertAlmostEqual(r['deduction'], 10000)
        self.assertAlmostEqual(r['taxable_benefit'], 10000)

    def test_non_qualifying_fully_taxed(self):
        r = stock_option_benefit(fmv_at_exercise=50, exercise_price=30, shares=1000,
                                 qualifies_for_deduction=False)
        self.assertAlmostEqual(r['taxable_benefit'], 20000)

    def test_effective_rate_matches_half_inclusion(self):
        mtr = 0.4571
        self.assertAlmostEqual(
            effective_tax_rate(IncomeType.STOCK_OPTION, mtr), mtr * 0.50, places=4)


class TestForeignTaxCredit(unittest.TestCase):
    """Foreign tax credit (ITA s.126, line 40500, issue #316)."""

    def test_ftc_capped_at_canadian_tax(self):
        """Foreign tax above Canadian tax on the income is not fully creditable."""
        r = foreign_tax_credit(foreign_income=1000, foreign_tax_paid=250,
                               marginal_rate=0.20)
        # Canadian tax = 200; credit capped at 200; 50 unrecoverable.
        self.assertAlmostEqual(r['credit'], 200)
        self.assertAlmostEqual(r['unrecoverable'], 50)

    def test_ftc_full_when_below_canadian_tax(self):
        r = foreign_tax_credit(foreign_income=1000, foreign_tax_paid=150,
                               marginal_rate=0.45)
        self.assertAlmostEqual(r['credit'], 150)
        self.assertAlmostEqual(r['unrecoverable'], 0.0)


class TestEffectiveTaxRates(unittest.TestCase):
    """Test effective_tax_rate for each income type."""
    
    def setUp(self):
        self.mtr = 0.4571  # Quebec ~$150k
    
    def test_interest_equals_mtr(self):
        self.assertAlmostEqual(effective_tax_rate(IncomeType.INTEREST, self.mtr), self.mtr)
    
    def test_capital_gains_below_threshold(self):
        """Capital gains below $250K threshold: 50% inclusion rate."""
        expected = self.mtr * 0.50
        self.assertAlmostEqual(
            effective_tax_rate(IncomeType.CAPITAL_GAIN, self.mtr, gain_amount=100000),
            expected, places=4)
    
    def test_eligible_dividend_lower_than_mtr(self):
        rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, self.mtr, 'quebec')
        self.assertLess(rate, self.mtr)
        self.assertGreater(rate, 0)
    
    def test_non_eligible_lower_than_mtr_higher_than_eligible(self):
        eligible = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, self.mtr, 'quebec')
        non_eligible = effective_tax_rate(IncomeType.NON_ELIGIBLE_DIVIDEND, self.mtr, 'quebec')
        self.assertGreater(non_eligible, eligible)
        self.assertLess(non_eligible, self.mtr)
    
    def test_roc_zero_tax(self):
        self.assertEqual(effective_tax_rate(IncomeType.RETURN_OF_CAPITAL, self.mtr), 0.0)
    
    def test_foreign_income_non_reg_has_wht(self):
        rate = effective_tax_rate(IncomeType.FOREIGN_INCOME, self.mtr, 'quebec',
                                  account_type='non_reg', wht_recoverable=True)
        # Recoverable WHT → no extra drag
        self.assertAlmostEqual(rate, self.mtr)
    
    def test_foreign_income_tfsa_has_unrecoverable_wht(self):
        rate = effective_tax_rate(IncomeType.FOREIGN_INCOME, self.mtr, 'quebec',
                                  account_type='tfsa', wht_recoverable=False)
        self.assertGreater(rate, self.mtr)  # WHT adds drag
    
    def test_ordering_at_high_mtr(self):
        """At high MTR (~45%): interest > non-eligible > eligible > capital gains > ROC.
        
        At high brackets, eligible dividends (38% gross-up + DTC) are taxed
        more than capital gains (50% inclusion). The ordering between eligible
        and capital gains flips depending on MTR level.
        """
        rates = {
            'interest': effective_tax_rate(IncomeType.INTEREST, self.mtr, 'quebec'),
            'non_eligible': effective_tax_rate(IncomeType.NON_ELIGIBLE_DIVIDEND, self.mtr, 'quebec'),
            'eligible': effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, self.mtr, 'quebec'),
            'capital_gains': effective_tax_rate(IncomeType.CAPITAL_GAIN, self.mtr, 'quebec'),
            'roc': effective_tax_rate(IncomeType.RETURN_OF_CAPITAL, self.mtr, 'quebec'),
        }
        self.assertGreater(rates['interest'], rates['non_eligible'])
        self.assertGreater(rates['non_eligible'], rates['eligible'])
        self.assertGreater(rates['eligible'], rates['capital_gains'])
        self.assertGreater(rates['capital_gains'], rates['roc'])


class TestCorrectedDividendFormula(unittest.TestCase):
    """Test that effective dividend rate uses the correct formula.
    
    The correct formula is:
        effective = gross_up_factor * (MTR - federal_dtc_rate - prov_dtc_rate)
    
    NOT the old incorrect formula:
        effective = MTR - gross_up_factor * (federal_dtc_rate + prov_dtc_rate)
    
    The key difference: both tax AND credits apply to the grossed-up amount,
    so the gross-up factor multiplies (MTR - DTCs), not just the DTCs.
    """

    def test_eligible_dividend_formula_manual_calc(self):
        """Verify QC eligible dividend rate matches manual calculation.
        
        Manual: 1.38 * (0.4571 - 0.150198 - 0.11510) = 1.38 * 0.191802 = 0.2647
        """
        mtr = 0.4571
        rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec', year=2026)
        # 1.38 * (0.4571 - 0.150198 - 0.11510) ≈ 0.2647
        expected = 1.38 * (mtr - 0.150198 - 0.11510)
        self.assertAlmostEqual(rate, expected, places=3)

    def test_non_eligible_dividend_formula_manual_calc(self):
        """Verify QC non-eligible dividend rate matches manual calculation.
        
        Manual: 1.15 * (0.4571 - 0.090301 - 0.05575) = 1.15 * 0.311049 = 0.3577
        """
        mtr = 0.4571
        rate = effective_tax_rate(IncomeType.NON_ELIGIBLE_DIVIDEND, mtr, 'quebec', year=2026)
        expected = 1.15 * (mtr - 0.090301 - 0.05575)
        self.assertAlmostEqual(rate, expected, places=3)

    def test_lower_bracket_eligible_dividend_not_zero(self):
        """At moderate MTR where DTC < tax, effective rate should be > 0.
        
        At MTR=0.30 in QC:
        Correct: 1.38 * (0.30 - 0.150198 - 0.11510) = 1.38 * 0.034702 = 0.0479
        Old (wrong): 0.30 - 1.38*(0.150198+0.11510) = 0.30 - 0.3661 = 0 → max(0,...) = 0
        """
        mtr = 0.30
        rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        # Should NOT be zero — the correct formula gives ~4.8%
        self.assertGreater(rate, 0.01, 
            "At 30% MTR, eligible dividends should have positive effective rate")

    def test_very_low_mtr_eligible_dividend_is_zero(self):
        """At very low MTR where DTC exceeds MTR, effective rate should be 0.
        
        At MTR=0.15 in QC: 1.38 * (0.15 - 0.150198 - 0.11510) < 0
        """
        mtr = 0.15
        rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        self.assertAlmostEqual(rate, 0.0, places=4)

    def test_dividend_formula_grossed_up_tax_minus_credits(self):
        """The effective rate must equal (grossed_up_tax - credits) / actual_dividend.
        
        For a $10,000 eligible dividend at MTR=0.4571 in QC:
        - Grossed-up amount = $10,000 * 1.38 = $13,800
        - Tax on grossed-up = $13,800 * 0.4571 = $6,308
        - Federal DTC = $13,800 * 0.150198 = $2,073
        - QC DTC = $13,800 * 0.11510 = $1,588
        - Net tax = $6,308 - $2,073 - $1,588 = $2,647
        - Effective rate = $2,647 / $10,000 = 0.2647
        """
        mtr = 0.4571
        dividend = 10000
        rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, mtr, 'quebec')
        # Verify it matches the detailed calculation
        grossed_up = dividend * 1.38
        tax_on_grossed_up = grossed_up * mtr
        fed_dtc = grossed_up * 0.150198
        qc_dtc = grossed_up * 0.11510
        net_tax = tax_on_grossed_up - fed_dtc - qc_dtc
        expected_rate = max(0, net_tax / dividend)
        self.assertAlmostEqual(rate, expected_rate, places=3)


class TestTieredCapitalGainsInclusion(unittest.TestCase):
    """Test tiered capital gains inclusion rate (DP#27).
    
    For 2024+: 50% inclusion for first $250K, 66.67% for amounts above $250K.
    For years before 2024: flat 50% inclusion rate.
    """

    def test_2023_flat_50pct_inclusion(self):
        """Before 2024, capital gains inclusion is flat 50%."""
        rate = capital_gains_inclusion_rate(gain_amount=500000, year=2023)
        self.assertAlmostEqual(rate, 0.50, places=4)

    def test_2024_below_threshold(self):
        """In 2024, gains below $250K: 50% inclusion."""
        rate = capital_gains_inclusion_rate(gain_amount=200000, year=2024)
        self.assertAlmostEqual(rate, 0.50, places=4)

    def test_2024_at_threshold(self):
        """In 2024, gains exactly at $250K: 50% inclusion."""
        rate = capital_gains_inclusion_rate(gain_amount=250000, year=2024)
        self.assertAlmostEqual(rate, 0.50, places=4)

    def test_2024_above_threshold(self):
        """In 2024, gains above $250K: blended inclusion rate.
        
        $300K gain: first $250K at 50%, next $50K at 66.67%.
        Taxable = $125K + $33.33K = $158.33K
        Effective inclusion = $158.33K / $300K = 0.5278
        """
        rate = capital_gains_inclusion_rate(gain_amount=300000, year=2024)
        # (250000 * 0.50 + 50000 * 2/3) / 300000 = 0.5278
        expected = (250000 * 0.50 + 50000 * (2/3)) / 300000
        self.assertAlmostEqual(rate, expected, places=4)

    def test_2026_high_gain_blended(self):
        """In 2026, large gains: mostly at 66.67% rate.
        
        $1M gain: first $250K at 50%, next $750K at 66.67%.
        Effective = (125000 + 500000) / 1000000 = 0.625
        """
        rate = capital_gains_inclusion_rate(gain_amount=1000000, year=2026)
        expected = (250000 * 0.50 + 750000 * (2/3)) / 1000000
        self.assertAlmostEqual(rate, expected, places=4)

    def test_effective_tax_rate_capital_gains_tiered(self):
        """effective_tax_rate for capital gains uses tiered inclusion for 2024+."""
        mtr = 0.4571
        # $300K gain in 2024+: blended inclusion
        rate = effective_tax_rate(IncomeType.CAPITAL_GAIN, mtr, gain_amount=300000, year=2024)
        inclusion = capital_gains_inclusion_rate(300000, 2024)
        self.assertAlmostEqual(rate, mtr * inclusion, places=4)

    def test_effective_tax_rate_capital_gains_pre2024(self):
        """effective_tax_rate for capital gains uses flat 50% before 2024."""
        mtr = 0.4571
        rate = effective_tax_rate(IncomeType.CAPITAL_GAIN, mtr, gain_amount=500000, year=2023)
        self.assertAlmostEqual(rate, mtr * 0.50, places=4)

    def test_zero_gain(self):
        """Zero capital gain → zero inclusion rate (edge case)."""
        rate = capital_gains_inclusion_rate(gain_amount=0, year=2024)
        self.assertAlmostEqual(rate, 0.50, places=4)  # Rate is 50%, just no taxable amount


class TestYearVersionedDTCRates(unittest.TestCase):
    """Test that DTC rates are year-versioned (DP#20, DP#12).
    
    DTC rates should come from tax_data, not hardcoded constants.
    Different years may have different DTC rates.
    """

    def test_federal_eligible_dtc_2026(self):
        """Federal eligible DTC rate is 15.0198% for 2026."""
        rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.4571, 'quebec', year=2026)
        # Verify the rate uses the 2026 DTC: 1.38*(0.4571 - 0.150198 - 0.11510)
        expected = 1.38 * (0.4571 - 0.150198 - 0.11510)
        self.assertAlmostEqual(rate, expected, places=3)

    def test_federal_eligible_dtc_2023(self):
        """Federal eligible DTC rate for 2023 should also be accessible."""
        # 2023 used same federal DTC rates but should be retrieved from year-versioned data
        rate = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.4571, 'quebec', year=2023)
        # Federal DTC for 2023 was also 15.0198%
        expected = 1.38 * (0.4571 - 0.150198 - 0.11510)
        self.assertAlmostEqual(rate, expected, places=3)

    def test_effective_tax_rate_accepts_year_parameter(self):
        """effective_tax_rate accepts a year parameter for year-versioned DTC."""
        # This should not raise
        rate_2026 = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.4571, 'quebec', year=2026)
        rate_2024 = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.4571, 'quebec', year=2024)
        # Both should return valid rates
        self.assertGreater(rate_2026, 0)
        self.assertGreater(rate_2024, 0)

    def test_non_eligible_dtc_year_versioned(self):
        """Non-eligible dividend DTC rates are also year-versioned."""
        rate = effective_tax_rate(IncomeType.NON_ELIGIBLE_DIVIDEND, 0.4571, 'quebec', year=2026)
        expected = 1.15 * (0.4571 - 0.090301 - 0.05575)
        self.assertAlmostEqual(rate, expected, places=3)


class TestZeroMTR(unittest.TestCase):
    def test_zero_mtr_interest(self):
        self.assertAlmostEqual(effective_tax_rate(IncomeType.INTEREST, 0.0), 0.0)
    
    def test_zero_mtr_capital_gains(self):
        self.assertAlmostEqual(effective_tax_rate(IncomeType.CAPITAL_GAIN, 0.0), 0.0)
    
    def test_zero_mtr_eligible_dividend(self):
        """Zero MTR → zero effective tax on eligible dividends."""
        self.assertAlmostEqual(effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.0, 'quebec'), 0.0)
    
    def test_zero_mtr_non_eligible_dividend(self):
        """Zero MTR → zero effective tax on non-eligible dividends."""
        self.assertAlmostEqual(effective_tax_rate(IncomeType.NON_ELIGIBLE_DIVIDEND, 0.0, 'quebec'), 0.0)
    
    def test_zero_mtr_roc(self):
        self.assertAlmostEqual(effective_tax_rate(IncomeType.RETURN_OF_CAPITAL, 0.0), 0.0)


class TestWHTDrag(unittest.TestCase):
    def test_rrsp_treaty_exempt(self):
        self.assertEqual(wht_drag('rrsp', 'us'), 0.0)
    
    def test_tfsa_unrecoverable(self):
        drag = wht_drag('tfsa', 'us', wht_recoverable=False, dividend_yield=0.02)
        self.assertGreater(drag, 0)
    
    def test_non_reg_recoverable(self):
        drag = wht_drag('non_reg', 'us', wht_recoverable=True)
        self.assertEqual(drag, 0.0)


class TestProvincialVariation(unittest.TestCase):
    def test_quebec_vs_ontario_eligible(self):
        qc = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.4571, 'quebec')
        on = effective_tax_rate(IncomeType.ELIGIBLE_DIVIDEND, 0.4571, 'ontario')
        # Different provinces → different DTCs → different effective rates
        self.assertLess(qc, 0.4571)
        self.assertLess(on, 0.4571)
        # QC has higher DTC than ON → lower effective rate
        self.assertLess(qc, on)


class TestAfterTaxReturn(unittest.TestCase):
    def test_consistent_with_effective_rate(self):
        mtr = 0.4571
        gross = 0.07
        for it in IncomeType:
            eff = effective_tax_rate(it, mtr, 'quebec')
            atr = after_tax_return(gross, it, mtr, 'quebec')
            self.assertAlmostEqual(atr, gross * (1 - eff), places=6)


if __name__ == '__main__':
    unittest.main()
