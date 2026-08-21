#!/usr/bin/env python3
"""Unit tests for cashout_optimizer.py module.

Tests every function and rule in the cash-out optimizer:
- Per-dollar benefit calculations (RRSP, TFSA, non-reg, paydown)
- Minimum extraction logic
- Allocation waterfall
- LTV level comparison
- Edge cases (zero amounts, no room, excess cash)

All test data uses round numbers. No personal information.
"""

from tax_data import default_tax_provider
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.cashout_optimizer import (
    compute_per_dollar_benefit,
    compute_tfsa_per_dollar,
    compute_fhsa_per_dollar,
    compute_nonreg_per_dollar,
    compute_paydown_per_dollar,
    compute_min_extraction,
    _compute_ltv_levels,
    print_cashout_report,
    AccountNeed,
    CashOutPlan,
)


def _make_test_cfg(
    house_value=750000,
    mortgage_balance=100000,
    mortgage_rate=0.05,
    margin_available=200000,
    primary_income=120000,
    spouse_income=50000,
    primary_rrsp_room=150000,
    spouse_rrsp_room=50000,
    primary_tfsa_room=40000,
    spouse_tfsa_room=40000,
    primary_fhsa_room=0,
    spouse_fhsa_room=0,
    non_reg_yield_rate=0.02,
) -> dict:
    """Build a test config dict with round numbers. No personal data."""
    return {
        'assumptions': {'projection_years': 10, 'investment_return': 0.07, 'salary_growth': 0.02, 'deduct_later_bracket_target': 117045, 'non_reg_yield_rate': non_reg_yield_rate},
        'savings': {'rate': 0.20},
        'property': {
            'house_value': house_value,
            'mortgage_balance': mortgage_balance,
            'mortgage_rate': mortgage_rate,
            'margin_available': margin_available,
            'ltv_max': 0.80,
            'current_payment_monthly': 1000,
            'amortization_years': 25,
        },
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': primary_income,
                 'rrsp_room_accumulated': primary_rrsp_room,
                 'tfsa_room_accumulated': primary_tfsa_room,
                 'fhsa_room_accumulated': primary_fhsa_room,
                 'pension_adjustment': 4000},
                {'role': 'spouse', 'gross_income': spouse_income,
                 'rrsp_room_accumulated': spouse_rrsp_room,
                 'tfsa_room_accumulated': spouse_tfsa_room,
                 'fhsa_room_accumulated': spouse_fhsa_room,
                 'pension_adjustment': 4000},
            ],
            'children': [{'name': 'Child1', 'age': 10, 'gross_income': 0}],
        },
        'accounts': {
            'rrsp_annual_percent': 0.18,
            'rrsp_annual_max': 33000,
            'tfsa_annual_room_per_person': 7000,
            'resp_current_balance': 0,
        },
    }


# ── Per-Dollar Benefit Functions ──────────────────────────────────────────

class TestPerDollarRRSP(unittest.TestCase):
    """Test compute_per_dollar_benefit for RRSP contributions."""

    def test_basic_rrsp_benefit(self):
        """$1 at 50% refund, 7% return, 10yr, 30% withdrawal tax."""
        # refund=0.50, growth=(1.07)^10=1.967, withdrawal=0.30
        # benefit = 0.50 + 1.967 * (1 - 0.30) = 0.50 + 1.377 = 1.877
        bpd = compute_per_dollar_benefit(1.0, 0.50, 0.07, 10, 0.30)
        self.assertGreater(bpd, 1.5)
        self.assertLess(bpd, 2.5)

    def test_zero_refund(self):
        """No refund: benefit comes only from tax-free growth minus withdrawal."""
        bpd = compute_per_dollar_benefit(1.0, 0.0, 0.07, 10, 0.30)
        expected = (1.07 ** 10) * (1 - 0.30)
        self.assertAlmostEqual(bpd, expected, places=3)

    def test_full_refund_immediate_payback(self):
        """100% refund = you get $1 back immediately + future growth minus tax."""
        bpd = compute_per_dollar_benefit(1.0, 1.0, 0.07, 10, 0.30)
        expected = 1.0 + (1.07 ** 10) * (1 - 0.30)
        self.assertAlmostEqual(bpd, expected, places=3)

    def test_zero_return(self):
        """Zero investment return: benefit is just refund minus withdrawal tax."""
        bpd = compute_per_dollar_benefit(1.0, 0.50, 0.0, 10, 0.30)
        expected = 0.50 + 1.0 * (1 - 0.30)  # 0.50 + 0.70 = 1.20
        self.assertAlmostEqual(bpd, expected, places=3)

    def test_short_timeframe(self):
        """1-year: minimal growth benefit."""
        bpd_1yr = compute_per_dollar_benefit(1.0, 0.50, 0.07, 1, 0.30)
        bpd_10yr = compute_per_dollar_benefit(1.0, 0.50, 0.07, 10, 0.30)
        self.assertLess(bpd_1yr, bpd_10yr)

    def test_high_withdrawal_tax_reduces_benefit(self):
        """Higher withdrawal tax reduces per-dollar benefit."""
        bpd_low = compute_per_dollar_benefit(1.0, 0.50, 0.07, 10, 0.20)
        bpd_high = compute_per_dollar_benefit(1.0, 0.50, 0.07, 10, 0.50)
        self.assertGreater(bpd_low, bpd_high)


class TestPerDollarTFSA(unittest.TestCase):
    """Test compute_tfsa_per_dollar for TFSA contributions."""

    def test_basic_tfsa(self):
        """TFSA: fully tax-free growth. $1 → (1.07)^10 = $1.967."""
        bpd = compute_tfsa_per_dollar(0.07, 10)
        expected = 1.07 ** 10
        self.assertAlmostEqual(bpd, expected, places=3)

    def test_zero_return(self):
        """Zero return: $1 stays $1 in TFSA."""
        bpd = compute_tfsa_per_dollar(0.0, 10)
        self.assertAlmostEqual(bpd, 1.0)

    def test_5yr(self):
        """5-year TFSA growth."""
        bpd = compute_tfsa_per_dollar(0.07, 5)
        expected = 1.07 ** 5
        self.assertAlmostEqual(bpd, expected, places=3)

    def test_tfsa_beats_rrsp_at_low_refund(self):
        """TFSA beats RRSP when RRSP refund rate is low and withdrawal tax is high."""
        tfsa_bpd = compute_tfsa_per_dollar(0.07, 10)
        rrsp_bpd = compute_per_dollar_benefit(1.0, 0.20, 0.07, 10, 0.50)
        self.assertGreater(tfsa_bpd, rrsp_bpd)

    def test_rrsp_beats_tfsa_at_high_refund(self):
        """RRSP beats TFSA when refund rate is high and withdrawal tax is low."""
        tfsa_bpd = compute_tfsa_per_dollar(0.07, 10)
        rrsp_bpd = compute_per_dollar_benefit(1.0, 0.50, 0.07, 10, 0.20)
        self.assertGreater(rrsp_bpd, tfsa_bpd)


class TestPerDollarNonReg(unittest.TestCase):
    """Test compute_nonreg_per_dollar for non-registered investments."""

    def test_basic_nonreg(self):
        """Non-reg: growth taxed as capital gains on realization."""
        bpd = compute_nonreg_per_dollar(0.07, 10, 0.50, 0.50)
        growth = 1.07 ** 10
        gains = growth - 1
        tax = gains * 0.50 * 0.50
        expected = growth - tax
        self.assertAlmostEqual(bpd, expected, places=3)

    def test_zero_return_nonreg(self):
        """Zero return: no gains, no tax, $1 = $1."""
        bpd = compute_nonreg_per_dollar(0.0, 10)
        self.assertAlmostEqual(bpd, 1.0)

    def test_high_marginal_reduces_nonreg(self):
        """Higher marginal rate reduces non-reg benefit via CG tax."""
        bpd_low_mtr = compute_nonreg_per_dollar(0.07, 10, 0.50, 0.20)
        bpd_high_mtr = compute_nonreg_per_dollar(0.07, 10, 0.50, 0.50)
        self.assertGreater(bpd_low_mtr, bpd_high_mtr)

    def test_tfsa_always_beats_nonreg(self):
        """TFSA (tax-free) always beats non-reg (taxed gains)."""
        tfsa = compute_tfsa_per_dollar(0.07, 10)
        nonreg = compute_nonreg_per_dollar(0.07, 10, 0.50, 0.50)
        self.assertGreater(tfsa, nonreg)


class TestPerDollarPaydown(unittest.TestCase):
    """Test compute_paydown_per_dollar for mortgage paydown."""

    def test_basic_paydown(self):
        """Paying down 5% mortgage: guaranteed (1.05)^10."""
        bpd = compute_paydown_per_dollar(0.05, 10)
        expected = 1.05 ** 10
        self.assertAlmostEqual(bpd, expected, places=3)

    def test_zero_rate(self):
        """Zero mortgage rate: paying down returns exactly $1."""
        bpd = compute_paydown_per_dollar(0.0, 10)
        self.assertAlmostEqual(bpd, 1.0)

    def test_higher_rate_higher_return(self):
        """Higher mortgage rate → higher guaranteed return from paydown."""
        bpd_3 = compute_paydown_per_dollar(0.03, 10)
        bpd_6 = compute_paydown_per_dollar(0.06, 10)
        self.assertGreater(bpd_6, bpd_3)

    def test_paydown_is_guaranteed(self):
        """Mortgage paydown return equals (1+rate)^years exactly (no variance)."""
        for rate in [0.02, 0.04, 0.06, 0.08]:
            for years in [1, 5, 10, 25]:
                bpd = compute_paydown_per_dollar(rate, years)
                self.assertAlmostEqual(bpd, (1 + rate) ** years, places=10)


# ── Minimum Extraction Logic ──────────────────────────────────────────────

class TestComputeMinExtraction(unittest.TestCase):
    """Test the core minimum-extraction calculator."""

    def test_margin_covers_all_room(self):
        """When margin exceeds registered room, no cash-out needed."""
        cfg = _make_test_cfg(margin_available=500000, primary_rrsp_room=100000,
                              spouse_rrsp_room=50000, primary_tfsa_room=30000,
                              spouse_tfsa_room=30000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        # No cash-out needed when margin covers all room
        self.assertAlmostEqual(plan.cashout_required, 0)
        # Required LTV = existing mortgage / house value (no new borrowing)
        self.assertGreater(plan.required_ltv, 0)  # Existing mortgage still counts

    def test_margin_insufficient_needs_cashout(self):
        """When margin < registered room, cash-out needed for gap."""
        cfg = _make_test_cfg(margin_available=100000, primary_rrsp_room=150000,
                              spouse_rrsp_room=50000, primary_tfsa_room=40000,
                              spouse_tfsa_room=40000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        # Total room = 150+50+40+40 = 280k, margin=100k, gap=180k
        self.assertGreater(plan.cashout_required, 0)
        self.assertGreater(plan.required_ltv, 0)

    def test_required_ltv_formula(self):
        """Required LTV = (mortgage + cashout_needed) / house_value."""
        cfg = _make_test_cfg(house_value=500000, mortgage_balance=100000,
                              margin_available=0, primary_rrsp_room=100000,
                              spouse_rrsp_room=0, primary_tfsa_room=0,
                              spouse_tfsa_room=0)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        expected_ltv = (100000 + 100000) / 500000  # 40%
        self.assertAlmostEqual(plan.required_ltv, expected_ltv, places=2)

    def test_refund_calculation(self):
        """RRSP refunds are computed using bracket-aware tax math."""
        cfg = _make_test_cfg(primary_income=120000, spouse_income=50000,
                              primary_rrsp_room=50000, spouse_rrsp_room=30000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        # Refunds should be > 0 (both incomes have MTR > 0)
        self.assertGreater(plan.refund_available, 0)

    def test_zero_margin_needs_full_cashout(self):
        """Zero margin: all registered room must come from cash-out."""
        cfg = _make_test_cfg(margin_available=0, primary_rrsp_room=100000,
                              spouse_rrsp_room=50000, primary_tfsa_room=40000,
                              spouse_tfsa_room=40000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        self.assertAlmostEqual(plan.cashout_required, plan.total_registered_room)
        self.assertAlmostEqual(plan.margin_to_registered, 0)

    def test_excess_cashout_at_max_ltv(self):
        """Excess = 80% LTV cash-out - actually needed."""
        cfg = _make_test_cfg(house_value=500000, mortgage_balance=100000,
                              margin_available=300000, primary_rrsp_room=50000,
                              spouse_rrsp_room=20000, primary_tfsa_room=20000,
                              spouse_tfsa_room=20000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        max_cashout = 500000 * 0.80 - 100000  # 300k
        if plan.cashout_required < max_cashout:
            self.assertGreater(plan.excess_cashout, 0)

    def test_net_debt_after_refund(self):
        """Net debt = total borrowed - refund used to pay down debt."""
        cfg = _make_test_cfg(margin_available=100000, primary_rrsp_room=100000,
                              spouse_rrsp_room=50000, primary_tfsa_room=20000,
                              spouse_tfsa_room=20000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        total_borrowed = plan.margin_to_registered + plan.cashout_to_registered
        expected_net = total_borrowed - plan.refund_available
        self.assertAlmostEqual(plan.net_debt_after_refund, expected_net, places=0)

    def test_nonreg_spread_computed(self):
        """Non-reg spread = after-tax return - after-tax cost."""
        cfg = _make_test_cfg(mortgage_rate=0.05)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        # Spread should be small (a few percent)
        self.assertGreater(plan.nonreg_spread, -0.05)
        self.assertLess(plan.nonreg_spread, 0.05)

    def test_nonreg_cost_deductible_for_sm_with_yield(self):
        """Issue #264/#549: SM borrowing is deductible only when income-producing.

        CRA §20(1)(c) deductibility requires BOTH a readvanceable structure AND a
        reasonable expectation of income (yield > 0). When both hold, the carrying
        cost is the after-tax rate, mortgage_rate × (1 − MTR), and the spread is
        positive — consistent with the full simulation's after-tax HELOC treatment.
        Relational, not hardcoded.
        """
        from countries.canada.cashout_optimizer import (
            _sm_deductible, marginal_rate, )

        cfg = _make_test_cfg(mortgage_rate=0.05, non_reg_yield_rate=0.02)
        cfg['property']['heloc_readvance'] = True
        self.assertTrue(_sm_deductible(cfg))

        brackets = default_tax_provider().get_combined_brackets()
        primary_income = cfg['family']['members'][0]['gross_income']
        mtr = marginal_rate(primary_income, brackets)

        plan = compute_min_extraction(cfg, investment_return=0.07)

        # No deductibility risk flagged when the holding is income-producing.
        self.assertEqual(plan.deductibility_risk, "")

        # Expected return leg (unchanged): CG taxed at 50% inclusion.
        expected_return = 0.07 * (1 - mtr * 0.50)
        # Cost leg must use the deductible (after-tax) rate.
        expected_cost = 0.05 * (1 - mtr)
        expected_spread = expected_return - expected_cost

        self.assertAlmostEqual(plan.nonreg_spread, expected_spread, places=6)
        # Deductibility makes the spread meaningfully positive (favours leverage).
        self.assertGreater(plan.nonreg_spread, 0)
        # And the recommendation should favour non-reg investing, not paydown.
        self.assertIn("non-reg", plan.recommendation.lower())

    def test_nonreg_cost_not_deductible_for_zero_yield_sm(self):
        """Issue #549: a readvanceable but ZERO-yield SM holding is NOT deductible.

        Under CRA §20(1)(c) a pure-growth, zero-yield holding has no reasonable
        expectation of income, so the interest does not qualify federally —
        regardless of the readvanceable structure. The carrying cost must fall
        back to the FULL mortgage rate (no after-tax discount), and the tool must
        flag the deductibility risk.
        """
        from countries.canada.cashout_optimizer import (
            _sm_deductible, _sm_readvanceable, marginal_rate, )

        cfg = _make_test_cfg(mortgage_rate=0.05, non_reg_yield_rate=0.0)
        cfg['property']['heloc_readvance'] = True
        # Structurally readvanceable, but fails the income-producing test.
        self.assertTrue(_sm_readvanceable(cfg))
        self.assertFalse(_sm_deductible(cfg))

        brackets = default_tax_provider().get_combined_brackets()
        primary_income = cfg['family']['members'][0]['gross_income']
        mtr = marginal_rate(primary_income, brackets)

        plan = compute_min_extraction(cfg, investment_return=0.07)

        # Cost leg uses the FULL mortgage rate (no §20(1)(c) discount).
        expected_return = 0.07 * (1 - mtr * 0.50)
        expected_spread = expected_return - 0.05  # full rate, no deduction
        self.assertAlmostEqual(plan.nonreg_spread, expected_spread, places=6)

        # The deductibility risk must be surfaced.
        self.assertTrue(plan.deductibility_risk)
        self.assertIn("20(1)(c)", plan.deductibility_risk)

    def test_nonreg_cost_full_rate_when_not_sm(self):
        """Without the readvanceable precondition, borrowing is non-deductible.

        Branch must not blanket-assume deductibility: when `heloc_readvance`
        is not set, the carrying cost is the full mortgage rate.
        """
        from countries.canada.cashout_optimizer import (
            _sm_deductible, marginal_rate, )

        cfg = _make_test_cfg(mortgage_rate=0.05)
        self.assertFalse(_sm_deductible(cfg))

        brackets = default_tax_provider().get_combined_brackets()
        primary_income = cfg['family']['members'][0]['gross_income']
        mtr = marginal_rate(primary_income, brackets)

        plan = compute_min_extraction(cfg, investment_return=0.07)
        expected_return = 0.07 * (1 - mtr * 0.50)
        expected_spread = expected_return - 0.05  # full rate, no deduction
        self.assertAlmostEqual(plan.nonreg_spread, expected_spread, places=6)

    def test_readvanceable_alias_deleted_heloc_readvance_is_sole_spelling(self):
        """DP#9 (#621): 'property.readvanceable' was an undocumented third
        spelling of the readvanceable flag (property.heloc_readvance /
        heloc.readvanceable were the other two). It is deleted, not a
        fallthrough -- _sm_readvanceable reads ONLY property.heloc_readvance.
        An explicit heloc_readvance=False must not be overridden by a
        (now-inert) 'readvanceable' key sitting alongside it."""
        from countries.canada.cashout_optimizer import _sm_readvanceable

        cfg = _make_test_cfg()
        cfg['property']['heloc_readvance'] = False
        cfg['property']['readvanceable'] = True  # dead alias, must be ignored
        self.assertFalse(_sm_readvanceable(cfg))

        cfg2 = _make_test_cfg()
        cfg2['property']['heloc_readvance'] = True
        self.assertTrue(_sm_readvanceable(cfg2))

    def test_paydown_guaranteed_equals_mortgage_rate(self):
        """Mortgage paydown guaranteed return = mortgage rate."""
        cfg = _make_test_cfg(mortgage_rate=0.05)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        self.assertAlmostEqual(plan.paydown_guaranteed, 0.05)

    def test_recommendation_present(self):
        """Recommendation string is always non-empty."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07)
        self.assertTrue(len(plan.recommendation) > 0)

    def test_recommendation_negative_spread(self):
        """When non-reg spread is negative, recommendation should say not worth it."""
        cfg = _make_test_cfg(mortgage_rate=0.08)  # High rate → negative spread
        plan = compute_min_extraction(cfg, investment_return=0.07)
        if plan.nonreg_spread <= 0:
            self.assertIn("negative", plan.recommendation.lower())


class TestAllocationWaterfall(unittest.TestCase):
    """Test the margin-first, then cash-out waterfall."""

    def test_margin_used_first(self):
        """Margin is consumed before cash-out."""
        cfg = _make_test_cfg(margin_available=200000, primary_rrsp_room=100000,
                              spouse_rrsp_room=50000, primary_tfsa_room=30000,
                              spouse_tfsa_room=30000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        # Total room = 210k, margin = 200k → cashout for 10k
        self.assertEqual(plan.margin_to_registered, 200000)
        self.assertAlmostEqual(plan.cashout_to_registered, 10000)

    def test_rrsp_filled_before_tfsa_priority(self):
        """Account needs are ordered: RRSP → TFSA → spouse RRSP."""
        cfg = _make_test_cfg(primary_rrsp_room=100000, primary_tfsa_room=40000,
                              spouse_rrsp_room=50000, spouse_tfsa_room=40000,
                              margin_available=150000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        # First account need should be primary RRSP
        self.assertEqual(plan.account_needs[0].account, "Primary RRSP")
        # TFSA should be before spouse RRSP
        names = [n.account for n in plan.account_needs]
        self.assertIn("TFSA (both)", names)
        self.assertIn("Spouse RRSP", names)

    def test_waterfall_exhausts_margin(self):
        """The waterfall allocates ALL available margin."""
        cfg = _make_test_cfg(margin_available=300000, primary_rrsp_room=150000,
                              spouse_rrsp_room=50000, primary_tfsa_room=40000,
                              spouse_tfsa_room=40000)
        plan = compute_min_extraction(cfg, investment_return=0.07)
        total_allocated = plan.margin_to_registered + plan.cashout_to_registered
        self.assertAlmostEqual(total_allocated, plan.total_registered_room, places=0)


class TestLTVLevels(unittest.TestCase):
    """Test _compute_ltv_levels."""

    def test_default_ltv_steps(self):
        """Default LTV steps are 0%, 30%, 40%, 50%, 60%, 70%, 80%."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07, explore_ltv_levels=True)
        ltvs = [lvl['ltv'] for lvl in plan.ltv_levels]
        self.assertIn(0.0, ltvs)
        self.assertIn(0.80, ltvs)
        self.assertEqual(len(plan.ltv_levels), 7)

    def test_ltv_zero_no_cashout(self):
        """0% LTV: no cash-out."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07, explore_ltv_levels=True)
        lvl_0 = next(l for l in plan.ltv_levels if l['ltv'] == 0.0)
        self.assertAlmostEqual(lvl_0['cashout'], 0)

    def test_ltv_80_max_cashout(self):
        """80% LTV: cash-out = house * 0.80 - mortgage."""
        cfg = _make_test_cfg(house_value=500000, mortgage_balance=100000)
        plan = compute_min_extraction(cfg, investment_return=0.07, explore_ltv_levels=True)
        lvl_80 = next(l for l in plan.ltv_levels if l['ltv'] == 0.80)
        expected_cashout = 500000 * 0.80 - 100000  # 300k
        self.assertAlmostEqual(lvl_80['cashout'], expected_cashout)

    def test_higher_ltv_more_excess(self):
        """Higher LTV → more excess cash (above registered room needs)."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07, explore_ltv_levels=True)
        excesses = [lvl['excess'] for lvl in plan.ltv_levels]
        # Excess should increase (or stay 0) with LTV
        for i in range(1, len(excesses)):
            self.assertGreaterEqual(excesses[i], excesses[i-1])

    def test_registered_fv_constant_across_ltv(self):
        """Registered account FV is the same at all LTV (room is the same)."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07, explore_ltv_levels=True)
        fv_set = set(round(lvl['registered_fv']) for lvl in plan.ltv_levels)
        self.assertLessEqual(len(fv_set), 2)  # Allow minor rounding

    def test_no_explore_disabled(self):
        """explore_ltv_levels=False: no LTV levels computed."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07, explore_ltv_levels=False)
        self.assertEqual(len(plan.ltv_levels), 0)


class TestPrintCashoutReport(unittest.TestCase):
    """Test that print_cashout_report runs without errors."""

    def test_report_runs(self):
        """Report prints without exception."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07)
        import io
        import sys
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            print_cashout_report(plan)
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        self.assertIn("CASH-OUT OPTIMIZER", output)
        self.assertIn("RECOMMENDATION", output)

    def test_report_shows_waterfall(self):
        """Report includes allocation waterfall section."""
        cfg = _make_test_cfg()
        plan = compute_min_extraction(cfg, investment_return=0.07)
        import io
        import sys
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            print_cashout_report(plan)
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        self.assertIn("ALLOCATION WATERFALL", output)


class TestAccountNeed(unittest.TestCase):
    """Test AccountNeed dataclass."""

    def test_fields(self):
        need = AccountNeed(
            account="RRSP", role="primary", room=50000,
            priority=1, refund_rate=0.50, refund_amount=25000,
            source="margin", benefit_per_dollar=1.88,
        )
        self.assertEqual(need.account, "RRSP")
        self.assertEqual(need.room, 50000)
        self.assertEqual(need.refund_amount, 25000)


class TestCashOutPlan(unittest.TestCase):
    """Test CashOutPlan dataclass."""

    def test_defaults(self):
        plan = CashOutPlan()
        self.assertEqual(plan.total_registered_room, 0)
        self.assertEqual(plan.cashout_required, 0)
        self.assertEqual(plan.margin_available, 0)

    def test_ltv_levels_empty(self):
        plan = CashOutPlan()
        self.assertEqual(len(plan.ltv_levels), 0)


class TestFHSAperDollar(unittest.TestCase):
    """Test compute_fhsa_per_dollar for FHSA contributions (DP#69)."""

    def test_fhsa_qualifying_better_than_rrsp(self):
        """FHSA qualifying withdrawal is strictly better than RRSP for first-home buyers.
        
        FHSA provides deduction + tax-free withdrawal, while RRSP provides
        deduction but taxes withdrawal. So FHSA benefit > RRSP benefit.
        """
        mtr = 0.4571  # Quebec combined rate at ~$150k
        contribution = 8000  # FHSA annual limit
        
        # FHSA qualifying withdrawal: deduction + tax-free growth
        fhsa_benefit = compute_fhsa_per_dollar(
            contribution, refund_rate=mtr,
            investment_return=0.07, years=10,
            qualifying_withdrawal=True,
        )
        
        # RRSP: deduction + taxed growth
        rrsp_benefit = compute_per_dollar_benefit(
            contribution, refund_rate=mtr,
            investment_return=0.07, years=10,
            withdrawal_tax_rate=0.30,  # Assumed retirement rate
        )
        
        # FHSA should be strictly better for qualifying withdrawals
        self.assertGreater(fhsa_benefit, rrsp_benefit,
                          "FHSA qualifying benefit should exceed RRSP benefit")

    def test_fhsa_qualifying_better_than_tfsa(self):
        """FHSA qualifying withdrawal is better than TFSA for first-home buyers.
        
        Both are tax-free on withdrawal, but FHSA also provides an immediate
        tax deduction that TFSA does not.
        """
        mtr = 0.4571
        contribution = 8000
        
        fhsa_benefit = compute_fhsa_per_dollar(
            contribution, refund_rate=mtr,
            investment_return=0.07, years=10,
            qualifying_withdrawal=True,
        )
        
        tfsa_benefit = compute_tfsa_per_dollar(
            investment_return=0.07, years=10,
        )
        
        # FHSA benefit = refund_rate + growth > growth alone (TFSA)
        self.assertGreater(fhsa_benefit, tfsa_benefit,
                          "FHSA qualifying benefit should exceed TFSA benefit")

    def test_fhsa_non_qualifying_like_rrsp(self):
        """FHSA non-qualifying withdrawal is similar to RRSP (deduction but taxed withdrawal)."""
        mtr = 0.4571
        contribution = 8000
        
        fhsa_nonqual = compute_fhsa_per_dollar(
            contribution, refund_rate=mtr,
            investment_return=0.07, years=10,
            qualifying_withdrawal=False,
        )
        
        # Non-qualifying should still be positive (deduction + growth after tax)
        self.assertGreater(fhsa_nonqual, 0,
                          "FHSA non-qualifying benefit should be positive")
        
        # Non-qualifying should be less than qualifying
        fhsa_qual = compute_fhsa_per_dollar(
            contribution, refund_rate=mtr,
            investment_return=0.07, years=10,
            qualifying_withdrawal=True,
        )
        self.assertGreater(fhsa_qual, fhsa_nonqual,
                          "Qualifying withdrawal should be better than non-qualifying")

    def test_fhsa_zero_contribution(self):
        """Edge case: zero contribution should still compute without error."""
        benefit = compute_fhsa_per_dollar(0, refund_rate=0.4571, investment_return=0.07)
        self.assertGreater(benefit, 0)  # Still has the growth component

    def test_fhsa_in_cashout_plan(self):
        """FHSA room should appear in the cash-out plan with dual benefit."""
        cfg = _make_test_cfg(
            primary_fhsa_room=16000,  # 2 years of FHSA room
            spouse_fhsa_room=8000,
        )
        plan = compute_min_extraction(cfg, investment_return=0.07)
        
        # FHSA should appear in account needs
        fhsa_need = [n for n in plan.account_needs if 'FHSA' in n.account]
        self.assertGreater(len(fhsa_need), 0,
                          "FHSA should appear in account needs when room is available")
        
        # FHSA refund should be positive (deduction benefit)
        fhsa = fhsa_need[0]
        self.assertGreater(fhsa.refund_amount, 0,
                          "FHSA should have a positive refund (deduction benefit)")
        
        # FHSA benefit_per_dollar should reflect dual benefit
        self.assertGreater(fhsa.benefit_per_dollar, 0,
                          "FHSA benefit_per_dollar should be positive")

    def test_fhsa_priority_above_tfsa(self):
        """FHSA should be prioritized above TFSA due to dual benefit (DP#69)."""
        mtr = 0.4571
        contribution = 8000
        
        fhsa_bpd = compute_fhsa_per_dollar(
            contribution, refund_rate=mtr,
            investment_return=0.07, years=10,
            qualifying_withdrawal=True,
        )
        tfsa_bpd = compute_tfsa_per_dollar(
            investment_return=0.07, years=10,
        )
        
        # FHSA benefit per dollar should exceed TFSA because it has
        # both deduction AND tax-free withdrawal
        self.assertGreater(fhsa_bpd, tfsa_bpd,
                          "FHSA BPD should exceed TFSA BPD (dual benefit)")


if __name__ == '__main__':
    unittest.main()
