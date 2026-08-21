#!/usr/bin/env python3
"""Unit tests for debt.py module.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest countries/canada/tests/ -v
Or:      python3 countries/canada/tests/test_debt.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.debt import (
    DebtInstrument, DebtPurpose, AdvanceRecord, DispositionRecord, HELOCTracing,
    debt_swap_analysis, cash_dam_analysis, PrescribedRateLoan,
    is_interest_deductible, replacement_property_deductibility,
)


class TestReplacementPropertyS203(unittest.TestCase):
    """ITA s.20(3) replacement-property / refinancing interest deductibility."""

    def test_full_reinvestment_stays_deductible(self):
        """All proceeds reinvested -> 100% of the loan stays deductible."""
        r = replacement_property_deductibility(
            original_balance=100000, disposition_proceeds=120000,
            replacement_cost=120000)
        self.assertAlmostEqual(r['deductible_balance'], 100000)
        self.assertAlmostEqual(r['deductible_proportion'], 1.0)
        self.assertAlmostEqual(r['non_deductible_balance'], 0.0)

    def test_partial_reinvestment_partial_deductible(self):
        """Only $60k of the $100k loan traced to new income property."""
        r = replacement_property_deductibility(
            original_balance=100000, disposition_proceeds=60000,
            replacement_cost=60000)
        self.assertAlmostEqual(r['deductible_balance'], 60000)
        self.assertAlmostEqual(r['non_deductible_balance'], 40000)
        self.assertAlmostEqual(r['deductible_proportion'], 0.6)

    def test_replacement_not_income_earning_disappears(self):
        """Replacement earns no income -> source disappears, nothing deductible."""
        r = replacement_property_deductibility(
            original_balance=100000, disposition_proceeds=100000,
            replacement_cost=100000, replacement_earns_income=False)
        self.assertAlmostEqual(r['deductible_balance'], 0.0)
        self.assertAlmostEqual(r['deductible_proportion'], 0.0)

    def test_personal_original_purpose_not_eligible(self):
        """A personal-purpose original loan never qualifies for s.20(3) relief."""
        r = replacement_property_deductibility(
            original_balance=100000, disposition_proceeds=100000,
            replacement_cost=100000, original_purpose=DebtPurpose.PERSONAL)
        self.assertAlmostEqual(r['deductible_proportion'], 0.0)

    def test_deductible_capped_at_original_balance(self):
        """Reinvesting more than the loan can't create extra deductible interest."""
        r = replacement_property_deductibility(
            original_balance=80000, disposition_proceeds=150000,
            replacement_cost=150000)
        self.assertAlmostEqual(r['deductible_balance'], 80000)


class TestDebtPurpose(unittest.TestCase):
    """Test DebtPurpose enum."""

    def test_purpose_types(self):
        self.assertEqual(DebtPurpose.INVESTMENT.value, "investment")
        self.assertEqual(DebtPurpose.PERSONAL.value, "personal")
        self.assertEqual(DebtPurpose.RENTAL_EXPENSE.value, "rental_expense")

    def test_registered_purposes(self):
        """RRSP/TFSA/RESP purposes are defined."""
        self.assertEqual(DebtPurpose.RRSP_CONTRIBUTION.value, "rrsp")
        self.assertEqual(DebtPurpose.TFSA_CONTRIBUTION.value, "tfsa")
        self.assertEqual(DebtPurpose.RESP_CONTRIBUTION.value, "resp")


class TestAdvanceRecord(unittest.TestCase):
    """Test AdvanceRecord tracing records."""

    def test_investment_advance_is_deductible(self):
        a = AdvanceRecord(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        self.assertTrue(a.is_deductible)

    def test_personal_advance_not_deductible(self):
        a = AdvanceRecord(10000, "2026-03", DebtPurpose.PERSONAL)
        self.assertFalse(a.is_deductible)

    def test_rental_expense_is_deductible(self):
        a = AdvanceRecord(5000, "2026-06", DebtPurpose.RENTAL_EXPENSE)
        self.assertTrue(a.is_deductible)

    def test_rrsp_advance_not_deductible(self):
        a = AdvanceRecord(20000, "2026-02", DebtPurpose.RRSP_CONTRIBUTION)
        self.assertFalse(a.is_deductible)

    def test_tainted_rental_expense_advance_still_deductible(self):
        """Tainted flag does not affect RENTAL_EXPENSE deductibility."""
        a = AdvanceRecord(5000, "2026-06", DebtPurpose.RENTAL_EXPENSE, tainted=True)
        self.assertTrue(a.is_deductible)

    def test_tainted_investment_advance_not_deductible(self):
        """Tainted flag makes INVESTMENT advance non-deductible."""
        a = AdvanceRecord(50000, "2026-01", DebtPurpose.INVESTMENT, tainted=True)
        self.assertFalse(a.is_deductible)


class TestDispositionRecord(unittest.TestCase):
    """Test DispositionRecord class directly."""

    def test_taints_advance_personal_use(self):
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.PERSONAL)
        self.assertTrue(d.taints_advance)

    def test_taints_advance_investment_use_returns_false(self):
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.INVESTMENT)
        self.assertFalse(d.taints_advance)

    def test_taints_advance_full_repayment_returns_false(self):
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.PERSONAL,
                              repaid_to_heloc=50000)
        self.assertFalse(d.taints_advance)

    def test_taints_advance_partial_repayment_returns_true(self):
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.PERSONAL,
                              repaid_to_heloc=20000)
        self.assertTrue(d.taints_advance)

    def test_disposition_repr(self):
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.PERSONAL,
                              repaid_to_heloc=20000)
        r = repr(d)
        self.assertIn("XEQT", r)
        self.assertIn("50,000", r)
        self.assertIn("personal", r)
        self.assertIn("20,000", r)

    def test_disposition_record_repaid_exceeds_amount_raises(self):
        with self.assertRaises(ValueError):
            DispositionRecord(30000, "2027-06", "XEQT",
                              repaid_to_heloc=50000)

    def test_disposition_record_negative_amount_raises(self):
        with self.assertRaises(ValueError):
            DispositionRecord(0, "2027-06", "XEQT")

    def test_disposition_record_negative_repaid_raises(self):
        with self.assertRaises(ValueError):
            DispositionRecord(50000, "2027-06", "XEQT",
                              repaid_to_heloc=-1000)

    def test_taints_advance_rrsp_contribution_returns_true(self):
        """RRSP contribution is non-qualifying → taints."""
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.RRSP_CONTRIBUTION)
        self.assertTrue(d.taints_advance)

    def test_taints_advance_tfsa_contribution_returns_true(self):
        """TFSA contribution is non-qualifying → taints."""
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.TFSA_CONTRIBUTION)
        self.assertTrue(d.taints_advance)

    def test_taints_advance_rental_expense_returns_false(self):
        """Rental expense is qualifying → does not taint."""
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.RENTAL_EXPENSE)
        self.assertFalse(d.taints_advance)

    def test_taints_advance_resp_contribution_returns_true(self):
        """RESP contribution is non-qualifying → taints."""
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.RESP_CONTRIBUTION)
        self.assertTrue(d.taints_advance)

    def test_taints_advance_mixed_returns_true(self):
        """MIXED purpose is non-qualifying for tainting → taints."""
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.MIXED)
        self.assertTrue(d.taints_advance)

    def test_taints_advance_investment_use_with_partial_repayment_returns_false(self):
        """Qualifying use with partial HELOC repayment → does not taint."""
        d = DispositionRecord(50000, "2027-06", "XEQT",
                              proceeds_use=DebtPurpose.INVESTMENT,
                              repaid_to_heloc=20000)
        self.assertFalse(d.taints_advance)


class TestDebtInstrument(unittest.TestCase):
    """Test DebtInstrument dataclass."""

    def test_investment_debt_is_deductible(self):
        debt = DebtInstrument(balance=100000, rate=0.05,
                              purpose=DebtPurpose.INVESTMENT)
        self.assertTrue(debt.is_interest_deductible)

    def test_personal_debt_not_deductible(self):
        debt = DebtInstrument(balance=100000, rate=0.05,
                              purpose=DebtPurpose.PERSONAL)
        self.assertFalse(debt.is_interest_deductible)

    def test_annual_interest(self):
        debt = DebtInstrument(balance=100000, rate=0.05)
        self.assertAlmostEqual(debt.annual_interest(), 5000)

    def test_deductible_interest_pure_investment(self):
        debt = DebtInstrument(balance=100000, rate=0.05,
                              purpose=DebtPurpose.INVESTMENT)
        self.assertAlmostEqual(debt.deductible_interest(), 5000)

    def test_deductible_interest_personal(self):
        debt = DebtInstrument(balance=100000, rate=0.05,
                              purpose=DebtPurpose.PERSONAL)
        self.assertAlmostEqual(debt.deductible_interest(), 0)


class TestHELOCTracing(unittest.TestCase):
    """Test CRA tracing requirements."""

    def test_pure_investment_tracing(self):
        """All advances for investment → 100% deductible."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.advance(50000, "2026-02", DebtPurpose.INVESTMENT, "XAW")
        deductible = tracing.deductible_interest(100000, 0.05)
        self.assertAlmostEqual(deductible, 5000)  # 100% of $5,000

    def test_personal_draw_poisons_deduction(self):
        """Personal draws reduce the deductible proportion."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.advance(50000, "2026-02", DebtPurpose.PERSONAL)  # Poison
        deductible = tracing.deductible_interest(100000, 0.05)
        self.assertAlmostEqual(deductible, 2500)  # 50% of $5,000

    def test_no_advances_zero_deduction(self):
        """No advances → zero deduction."""
        tracing = HELOCTracing()
        deductible = tracing.deductible_interest(100000, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_summary(self):
        tracing = HELOCTracing()
        tracing.advance(80000, "2026-01", DebtPurpose.INVESTMENT)
        tracing.advance(20000, "2026-02", DebtPurpose.PERSONAL)
        s = tracing.summary()
        self.assertAlmostEqual(s['deductible_proportion'], 0.8)


class TestDebtSwapAnalysis(unittest.TestCase):
    """Test debt swap: liquidate non-reg → pay mortgage → re-borrow."""

    def test_swap_beneficial_when_spread_positive(self):
        """Swap is beneficial when deductible HELOC cost < mortgage cost."""
        result = debt_swap_analysis(
            non_reg_balance=200000,
            adjusted_cost_base=100000,
            marginal_rate=0.4571,
            mortgage_balance=300000,
            mortgage_rate=0.05,
            heloc_rate=0.05,
        )
        # Same rate but now deductible → after-tax cost is lower
        self.assertTrue(result['swap_beneficial'])
        self.assertGreater(result['annual_benefit'], 0)
        # Capital gains tax: ($200k - $100k) × 50% × 45.71% = $22,855
        self.assertAlmostEqual(result['capital_gains_tax'], 100000 * 0.50 * 0.4571, places=0)

    def test_swap_not_beneficial_if_heloc_much_higher(self):
        """Swap is not beneficial if HELOC rate is much higher than mortgage."""
        result = debt_swap_analysis(
            non_reg_balance=200000,
            adjusted_cost_base=100000,
            marginal_rate=0.4571,
            mortgage_balance=300000,
            mortgage_rate=0.03,
            heloc_rate=0.08,
        )
        # HELOC after-tax cost: 8% × (1 - 45.71%) = 4.34%
        # Mortgage cost: 3%
        # 4.34% > 3% → swap not beneficial on interest alone
        self.assertFalse(result['condition_met'])

    def test_zero_capital_gain_no_tax(self):
        """No capital gains tax if ACB equals balance."""
        result = debt_swap_analysis(
            non_reg_balance=200000,
            adjusted_cost_base=200000,
            marginal_rate=0.4571,
            mortgage_balance=300000,
            mortgage_rate=0.05,
            heloc_rate=0.05,
        )
        self.assertEqual(result['capital_gains_tax'], 0)
        self.assertTrue(result['swap_beneficial'])


class TestCashDamAnalysis(unittest.TestCase):
    """Test cash damming: pay rental expenses from HELOC."""

    def test_cash_dam_creates_deduction(self):
        """Cash dam shifts rental expenses to deductible HELOC."""
        result = cash_dam_analysis(
            rental_income=24000,
            rental_expenses=12000,
            mortgage_balance=300000,
            mortgage_rate=0.05,
            heloc_rate=0.05,
            marginal_rate=0.4571,
            years=10,
        )
        self.assertGreater(result['total_benefit'], 0)
        self.assertEqual(result['annual_shift'], 12000)

    def test_zero_rental_expenses_no_cash_dam(self):
        """Zero rental expenses means no HELOC shifting, but rental income still pays down mortgage."""
        result = cash_dam_analysis(
            rental_income=24000,
            rental_expenses=0,
            mortgage_balance=300000,
            mortgage_rate=0.05,
            heloc_rate=0.05,
            marginal_rate=0.4571,
        )
        # No rental expenses → no HELOC shifting → no SM deduction
        # But rental income still pays down mortgage → benefit from lower interest
        self.assertEqual(result['annual_shift'], 0)
        self.assertEqual(result['rental_expenses'], 0)


class TestPrescribedRateLoan(unittest.TestCase):
    """Test spousal prescribed-rate loan."""

    def test_loan_benefit_at_spread(self):
        """Prescribed-rate loan benefits from rate spread."""
        loan = PrescribedRateLoan(principal=100000, rate=0.02)
        benefit = loan.net_income_splitting_benefit(
            investment_return=0.07,
            lender_marginal_rate=0.4571,
            borrower_marginal_rate=0.2569,
        )
        self.assertGreater(benefit, 0)

    def test_no_benefit_if_interest_not_paid(self):
        """No benefit if interest not paid by Jan 30 (attribution applies)."""
        loan = PrescribedRateLoan(principal=100000, rate=0.02,
                                 interest_paid_by_jan30=False)
        benefit = loan.net_income_splitting_benefit(
            investment_return=0.07,
            lender_marginal_rate=0.4571,
            borrower_marginal_rate=0.2569,
        )
        self.assertEqual(benefit, 0)

    def test_attribution_applies_if_not_paid(self):
        """Attribution applies when interest not paid on time."""
        loan_paid = PrescribedRateLoan(principal=100000, interest_paid_by_jan30=True)
        loan_unpaid = PrescribedRateLoan(principal=100000, interest_paid_by_jan30=False)
        self.assertFalse(loan_paid.attribution_applies(2026))
        self.assertTrue(loan_unpaid.attribution_applies(2026))

    def test_annual_interest(self):
        """Annual interest = principal × rate."""
        loan = PrescribedRateLoan(principal=100000, rate=0.02)
        self.assertAlmostEqual(loan.annual_interest(), 2000)


class TestInterestDeductibility(unittest.TestCase):
    """Test is_interest_deductible pure function."""

    def test_investment_deductible(self):
        result = is_interest_deductible(DebtPurpose.INVESTMENT)
        self.assertTrue(result['deductible'])
        self.assertAlmostEqual(result['proportion'], 1.0)

    def test_personal_not_deductible(self):
        result = is_interest_deductible(DebtPurpose.PERSONAL)
        self.assertFalse(result['deductible'])

    def test_rrsp_not_deductible(self):
        """Borrowed for RRSP: interest NOT deductible (registered account)."""
        result = is_interest_deductible(DebtPurpose.RRSP_CONTRIBUTION)
        self.assertFalse(result['deductible'])

    def test_rental_expense_deductible(self):
        result = is_interest_deductible(DebtPurpose.RENTAL_EXPENSE)
        self.assertTrue(result['deductible'])

    def test_investment_no_income_not_deductible(self):
        """Investment without reasonable expectation of income → not deductible."""
        result = is_interest_deductible(DebtPurpose.INVESTMENT, earns_income=False)
        self.assertFalse(result['deductible'])

    def test_mixed_with_tracing(self):
        tracing = HELOCTracing()
        tracing.advance(80000, "2026-01", DebtPurpose.INVESTMENT)
        tracing.advance(20000, "2026-02", DebtPurpose.PERSONAL)
        result = is_interest_deductible(DebtPurpose.MIXED, tracing=tracing)
        self.assertTrue(result['deductible'])
        self.assertAlmostEqual(result['proportion'], 0.8)

    def test_mixed_without_tracing(self):
        result = is_interest_deductible(DebtPurpose.MIXED, tracing=None)
        self.assertFalse(result['deductible'])

class TestLudmerReborrowing(unittest.TestCase):
    """Test Ludmer v. The Queen (1985 DTC 5506) re-borrowing rules.

    When an investment purchased with borrowed money is sold and the
    proceeds are used for a personal purpose, the original advance's
    deductibility is lost under the 'direct use' test. If the taxpayer
    re-borrows for investment, the new advance qualifies on its own.

    Key principle: deductibility follows the direct use of the money,
    not the original purpose of the borrowing.
    """

    def test_ludmer_sell_invest_use_proceeds_personally(self):
        """Sell investment, use proceeds personally → original advance tainted."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell XEQT, use $50K proceeds personally (vacation)
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # HELOC balance still $50K, but the advance is now tainted
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_ludmer_reborrow_after_personal_use(self):
        """Re-borrow for investment after personal use → new advance deductible."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell XEQT, use proceeds personally
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # Re-borrow for new investment
        tracing.advance(50000, "2027-07", DebtPurpose.INVESTMENT, "XAW")
        # HELOC balance = $100K, but only $50K (new XAW) is deductible
        deductible = tracing.deductible_interest(100000, 0.05)
        self.assertAlmostEqual(deductible, 2500)  # 50% of $5,000

    def test_ludmer_disposition_proceeds_repay_heloc(self):
        """Sell investment, pay down HELOC → no deductibility loss."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell XEQT, use proceeds to repay HELOC
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL,
                           repaid_to_heloc=50000)
        # HELOC balance is $0, no interest to deduct
        deductible = tracing.deductible_interest(0, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_ludmer_reborrow_after_heloc_repayment(self):
        """Re-borrow after HELOC repayment → new advance fully deductible."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell XEQT, pay down HELOC
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL,
                           repaid_to_heloc=50000)
        # Re-borrow for new investment
        tracing.advance(50000, "2027-07", DebtPurpose.INVESTMENT, "XAW")
        # HELOC balance = $50K, all from new deductible advance
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 2500)  # 100% of $2,500

    def test_ludmer_partial_disposition(self):
        """Partial sale: only the sold portion is tainted."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell half of XEQT, use proceeds personally
        tracing.disposition(25000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # $25K tainted, $25K still investment → 50% deductible
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 1250)  # 50% of $2,500

    def test_ludmer_tainted_advance_is_deductible_property(self):
        """Tainted advance reports is_deductible=False."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # The original advance should now be tainted
        self.assertFalse(tracing.advances[0].is_deductible)

    def test_ludmer_no_tainting_when_proceeds_repay_heloc(self):
        """When disposition proceeds fully repay HELOC, advance is retired (removed)."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL,
                           repaid_to_heloc=50000)
        # The advance was fully repaid — no longer in advances list
        self.assertEqual(len(tracing.advances), 0)
        self.assertAlmostEqual(tracing.total_advanced(), 0)

    def test_ludmer_summary_includes_dispositions(self):
        """Summary includes disposition history."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        s = tracing.summary()
        self.assertEqual(s['num_dispositions'], 1)
        self.assertAlmostEqual(s['tainted_amount'], 50000)

    def test_ludmer_disposition_no_matching_advance_raises(self):
        """Disposition with no matching advance raises ValueError."""
        tracing = HELOCTracing()
        with self.assertRaises(ValueError):
            tracing.disposition(50000, "2027-06", "MISSING",
                               proceeds_use=DebtPurpose.PERSONAL)

    def test_ludmer_reinvestment_no_tainting(self):
        """Sell investment, reinvest proceeds → no tainting."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell XEQT, use proceeds for reinvestment (not personal)
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.INVESTMENT)
        # Reinvestment doesn't taint or retire — advance still deductible
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 2500)  # 100% of $2,500

    def test_ludmer_partial_repaid_to_heloc(self):
        """Partial HELOC repayment + partial personal use."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell $50K XEQT: $20K repays HELOC, $30K used personally
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL,
                           repaid_to_heloc=20000)
        # $20K retired, $30K tainted, $0 clean → total_advanced = $30K, all tainted
        deductible = tracing.deductible_interest(30000, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_ludmer_partial_repaid_then_reborrow(self):
        """Partial HELOC repayment + personal use, then re-borrow."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell $50K XEQT: $20K repays HELOC, $30K used personally
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL,
                           repaid_to_heloc=20000)
        # Re-borrow $30K for new investment
        tracing.advance(30000, "2027-07", DebtPurpose.INVESTMENT, "XAW")
        # $30K tainted + $30K new investment → 50% deductible
        deductible = tracing.deductible_interest(60000, 0.05)
        self.assertAlmostEqual(deductible, 1500)  # 50% of $3,000

    def test_ludmer_disposition_exceeding_advances(self):
        """Disposition amount exceeding total matching advances only taints available."""
        tracing = HELOCTracing()
        tracing.advance(30000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Try to dispose $50K but only $30K advanced → taints $30K
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # Only $30K was available, all tainted
        deductible = tracing.deductible_interest(30000, 0.05)
        self.assertAlmostEqual(deductible, 0)
        self.assertAlmostEqual(tracing.total_advanced(), 30000)

    def test_ludmer_personal_draws_reflects_tainting(self):
        """After tainting, investment_advanced drops to 0; personal_draws stays at initial value."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # After tainting, investment_advanced should be 0
        self.assertAlmostEqual(tracing.investment_advanced(), 0)
        self.assertAlmostEqual(tracing.total_advanced(), 50000)

    def test_ludmer_repr_tainted_flag(self):
        """Tainted advance shows (tainted) in repr."""
        a = AdvanceRecord(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT", tainted=True)
        self.assertIn("(tainted)", repr(a))
        b = AdvanceRecord(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        self.assertNotIn("(tainted)", repr(b))

    def test_ludmer_reborrow_after_full_repayment_correct_proportion(self):
        """After full HELOC repayment and re-borrow, only new advance counts."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Full repayment of HELOC
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL,
                           repaid_to_heloc=50000)
        # Re-borrow for investment
        tracing.advance(50000, "2027-07", DebtPurpose.INVESTMENT, "XAW")
        # Only the new $50K advance exists → 100% deductible
        self.assertEqual(len(tracing.advances), 1)
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 2500)

    def test_ludmer_multiple_advances_same_investment(self):
        """Multiple advances for same investment: taint/retire in order."""
        tracing = HELOCTracing()
        tracing.advance(30000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.advance(20000, "2026-03", DebtPurpose.INVESTMENT, "XEQT")
        # Sell all $50K XEQT, personal use
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # Both advances tainted → 0% deductible
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_ludmer_repaid_exceeds_amount_raises(self):
        """repaid_to_heloc > amount raises ValueError."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        with self.assertRaises(ValueError):
            tracing.disposition(30000, "2027-06", "XEQT",
                               proceeds_use=DebtPurpose.PERSONAL,
                               repaid_to_heloc=50000)

    def test_ludmer_repaid_exceeds_matching_advances_raises(self):
        """repaid_to_heloc exceeding total matching advances raises ValueError."""
        tracing = HELOCTracing()
        tracing.advance(30000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        with self.assertRaises(ValueError):
            tracing.disposition(50000, "2027-06", "XEQT",
                               proceeds_use=DebtPurpose.PERSONAL,
                               repaid_to_heloc=40000)

    def test_ludmer_failed_disposition_preserves_state(self):
        """ValueError during disposition leaves advances and dispositions unchanged."""
        tracing = HELOCTracing()
        tracing.advance(30000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        with self.assertRaises(ValueError):
            tracing.disposition(50000, "2027-06", "MISSING",
                               proceeds_use=DebtPurpose.PERSONAL)
        self.assertEqual(len(tracing.advances), 1)
        self.assertEqual(len(tracing.dispositions), 0)
        self.assertAlmostEqual(tracing.total_advanced(), 30000)

    def test_ludmer_rrsp_proceeds_taints_advance(self):
        """Sell investment, use proceeds for RRSP contribution → taints."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.RRSP_CONTRIBUTION)
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_ludmer_tfsa_proceeds_taints_advance(self):
        """Sell investment, use proceeds for TFSA contribution → taints."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.TFSA_CONTRIBUTION)
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_ludmer_rental_expense_proceeds_no_taint(self):
        """Sell investment, use proceeds for rental expense → no taint."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.RENTAL_EXPENSE)
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 2500)  # 100% of $2,500

    def test_ludmer_mixed_proceeds_taints_advance(self):
        """Sell investment, MIXED purpose proceeds → taints (non-qualifying)."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.MIXED)
        deductible = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible, 0)

    def test_ludmer_summary_consistent_with_draws_and_taints(self):
        """summary() fields are internally consistent: total = investment + non_deductible."""
        tracing = HELOCTracing()
        tracing.advance(80000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        tracing.advance(20000, "2026-02", DebtPurpose.PERSONAL)
        # Taint half of XEQT
        tracing.disposition(40000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        s = tracing.summary()
        # total_advanced = 40K tainted + 40K clean XEQT + 20K personal = 100K
        self.assertAlmostEqual(s['total_advanced'], 100000)
        # investment_advanced = 40K (clean XEQT only)
        self.assertAlmostEqual(s['investment_advanced'], 40000)
        # non_deductible_amount = total - investment = 60K
        self.assertAlmostEqual(s['non_deductible_amount'], 60000)
        # Verify consistency: total == investment + non_deductible
        self.assertAlmostEqual(s['total_advanced'],
                             s['investment_advanced'] + s['non_deductible_amount'])
        # personal_draws tracks only initial draws (20K), not tainted
        self.assertAlmostEqual(s['personal_draws'], 20000)
        # tainted_amount = 40K
        self.assertAlmostEqual(s['tainted_amount'], 40000)

    def test_ludmer_sequential_dispositions(self):
        """Sequential partial dispositions: partial taint then another."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # First: sell $20K, use personally → partial taint
        tracing.disposition(20000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # $20K tainted, $30K clean → 60% deductible
        deductible1 = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible1, 1500)  # 60% of $2,500
        # Second: sell remaining $30K, use personally
        tracing.disposition(30000, "2027-12", "XEQT",
                           proceeds_use=DebtPurpose.PERSONAL)
        # Now all $50K tainted → 0% deductible
        deductible2 = tracing.deductible_interest(50000, 0.05)
        self.assertAlmostEqual(deductible2, 0)

    def test_ludmer_reinvestment_with_repaid_to_heloc(self):
        """Reinvestment with partial HELOC repayment: retire portion, keep rest."""
        tracing = HELOCTracing()
        tracing.advance(50000, "2026-01", DebtPurpose.INVESTMENT, "XEQT")
        # Sell $50K XEQT: $20K repays HELOC, $30K reinvested
        tracing.disposition(50000, "2027-06", "XEQT",
                           proceeds_use=DebtPurpose.INVESTMENT,
                           repaid_to_heloc=20000)
        # $20K retired, $30K still deductible (reinvestment doesn't taint)
        self.assertAlmostEqual(tracing.total_advanced(), 30000)
        deductible = tracing.deductible_interest(30000, 0.05)
        self.assertAlmostEqual(deductible, 1500)  # 100% of $1,500


if __name__ == "__main__":
    unittest.main()
