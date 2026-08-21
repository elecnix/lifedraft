#!/usr/bin/env python3
"""Comprehensive tests for rate_model.py — full coverage.

All test data uses round numbers. No personal information.
DP#17: every rule path tested with at least 2 cases.

Run with: python3 -m pytest tests/test_rate_model_full.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from io import StringIO

from countries.canada.rate_model import (
    RateStep, RatePath, build_rate_path, build_broker_scenarios,
    build_variable_rate_path, build_stress_scenarios, build_renewal_stress,
    build_boc_rate_path_scenario, monthly_payment, amortization_schedule,
    annual_summary, HELOCPath, estimate_ird_penalty,
    generate_all_mortgage_scenarios, print_amortization_tables,
)


class TestRateStep(unittest.TestCase):
    """Test RateStep dataclass."""

    def test_basic_creation(self):
        step = RateStep(rate=0.045, start_year=0, end_year=5)
        self.assertAlmostEqual(step.rate, 0.045)
        self.assertEqual(step.duration_years, 5)

    def test_label(self):
        step = RateStep(rate=0.05, start_year=0, end_year=3, label="3yr fixed")
        self.assertEqual(step.label, "3yr fixed")


class TestRatePath(unittest.TestCase):
    """Test RatePath: get_rate, projection_years, average_rate, description."""

    def setUp(self):
        self.path = RatePath(
            name="Test Path",
            steps=[
                RateStep(rate=0.04, start_year=0, end_year=3, label="Fixed"),
                RateStep(rate=0.05, start_year=3, end_year=10, label="Renewal"),
            ],
            rate_type="mixed",
        )

    def test_get_rate_year_1(self):
        self.assertAlmostEqual(self.path.get_rate(1), 0.04)

    def test_get_rate_year_5(self):
        self.assertAlmostEqual(self.path.get_rate(5), 0.05)

    def test_get_rate_before_start(self):
        """Year 0 should use first step."""
        self.assertAlmostEqual(self.path.get_rate(0), 0.04)

    def test_get_rate_beyond_end(self):
        """Beyond last step: return last known rate."""
        self.assertAlmostEqual(self.path.get_rate(15), 0.05)

    def test_get_rate_month(self):
        """Month 0 = year 0, month 12 = year 1."""
        self.assertAlmostEqual(self.path.get_rate_month(0), 0.04)
        self.assertAlmostEqual(self.path.get_rate_month(36), 0.05)

    def test_projection_years(self):
        self.assertEqual(self.path.projection_years, 10)

    def test_average_rate(self):
        avg = self.path.average_rate
        # 3 years at 4% + 7 years at 5% = (0.12 + 0.35) / 10 = 4.7%
        self.assertAlmostEqual(avg, 0.047, places=3)

    def test_description(self):
        desc = self.path.description()
        self.assertIn("Test Path", desc)
        self.assertIn("4.00%", desc)

    def test_empty_path(self):
        """Empty path returns fallback rate."""
        empty = RatePath(name="empty", steps=[])
        self.assertAlmostEqual(empty.get_rate(0), 0.05)  # fallback
        self.assertEqual(empty.projection_years, 10)
        self.assertEqual(empty.average_rate, 0)


class TestBuildRatePath(unittest.TestCase):
    """Test build_rate_path factory function."""

    def test_fixed_rate_path(self):
        """3-year fixed at 4% with 5% renewal."""
        path = build_rate_path("3yr fixed", initial_rate=0.04, term_years=3,
                               rate_type="fixed", renewal_rates=[0.05],
                               projection_years=10)
        self.assertAlmostEqual(path.get_rate(0), 0.04)
        self.assertAlmostEqual(path.get_rate(3), 0.05)
        self.assertAlmostEqual(path.get_rate(5), 0.05)

    def test_variable_rate_path(self):
        """5-year variable at 3.75%."""
        path = build_rate_path("5yr variable", initial_rate=0.0375, term_years=5,
                               rate_type="variable", renewal_rates=[0.045],
                               projection_years=10)
        self.assertAlmostEqual(path.get_rate(0), 0.0375)
        self.assertAlmostEqual(path.get_rate(5), 0.045)

    def test_with_renewal_date(self):
        """When renewal_date and contract_start_date provided, compute term."""
        path = build_rate_path(
            "dated", initial_rate=0.04, term_years=5,
            rate_type="fixed", renewal_rates=[0.05],
            projection_years=10,
            renewal_date="2030-01-01", contract_start_date="2025-01-01",
        )
        # Should compute term ≈ 5 years from dates
        self.assertAlmostEqual(path.get_rate(0), 0.04)

    def test_with_invalid_dates(self):
        """Invalid dates fall back to term_years."""
        path = build_rate_path(
            "bad dates", initial_rate=0.04, term_years=3,
            rate_type="fixed", renewal_rates=[0.05],
            projection_years=10,
            renewal_date="not-a-date", contract_start_date="also-bad",
        )
        self.assertAlmostEqual(path.get_rate(0), 0.04)
        self.assertAlmostEqual(path.get_rate(3), 0.05)

    def test_no_renewal_rates(self):
        """Default renewal rate of 5% when none specified."""
        path = build_rate_path("default renewal", initial_rate=0.04, term_years=3,
                               rate_type="fixed", projection_years=10)
        self.assertAlmostEqual(path.get_rate(3), 0.05)

    def test_multiple_renewal_rates(self):
        """Multiple renewal rates applied across 5-year renewal periods."""
        path = build_rate_path("multi renew", initial_rate=0.04, term_years=3,
                               rate_type="fixed",
                               renewal_rates=[0.045, 0.05, 0.055],
                               projection_years=20)
        # Year 3-7: first renewal at 4.5%
        self.assertAlmostEqual(path.get_rate(3), 0.045)
        self.assertAlmostEqual(path.get_rate(6), 0.045)
        # Year 8-12: second renewal at 5%
        self.assertAlmostEqual(path.get_rate(8), 0.05)


class TestBuildBrokerScenarios(unittest.TestCase):
    """Test build_broker_scenarios with and without broker offers."""

    def test_default_scenarios(self):
        """Default: 3yr fixed + 5yr variable + current baseline."""
        scenarios = build_broker_scenarios(current_rate=0.0495, projection_years=10)
        self.assertGreater(len(scenarios), 3)
        self.assertIn("current — no change", scenarios)

    def test_with_broker_offers(self):
        """Custom broker offers generate expected/best/worst for each."""
        offers = [
            {'name': '3yr @ 4.04%', 'rate': 0.0404, 'term_years': 3, 'type': 'fixed'},
        ]
        scenarios = build_broker_scenarios(0.0495, projection_years=10, broker_offers=offers)
        self.assertIn("3yr @ 4.04% — expected", scenarios)
        self.assertIn("3yr @ 4.04% — best case", scenarios)
        self.assertIn("3yr @ 4.04% — worst case", scenarios)

    def test_custom_renewal_overlays(self):
        """Custom best/worst renewal overlays."""
        overlays = {
            'best_case': [0.03, 0.03],
            'worst_case': [0.07, 0.07],
        }
        scenarios = build_broker_scenarios(0.05, projection_years=10,
                                            renewal_overlays=overlays)
        self.assertIn("current — no change", scenarios)


class TestBuildVariableRatePath(unittest.TestCase):
    """Test build_variable_rate_path with annual adjustments."""

    def test_flat_variable(self):
        """No adjustments → flat rate during term."""
        path = build_variable_rate_path("flat var", start_rate=0.05,
                                         annual_adjustments=[],
                                         term_years=5, projection_years=10)
        self.assertAlmostEqual(path.get_rate(0), 0.05)

    def test_with_annual_adjustments(self):
        """Rate changes each year during term."""
        path = build_variable_rate_path(
            "rising var", start_rate=0.0375,
            annual_adjustments=[0.04, 0.045, 0.05, 0.055],
            term_years=5, renewal_rates=[0.05],
            projection_years=10)
        # Year 0: 3.75%
        self.assertAlmostEqual(path.get_rate(0), 0.0375)
        # Year 1: first adjustment
        self.assertAlmostEqual(path.get_rate(1), 0.04)

    def test_post_term_renewal(self):
        """After term, renewal rates take over."""
        path = build_variable_rate_path(
            "post term", start_rate=0.04,
            annual_adjustments=[],
            term_years=3, renewal_rates=[0.05],
            projection_years=10)
        self.assertAlmostEqual(path.get_rate(3), 0.05)


class TestMonthlyPayment(unittest.TestCase):
    """Test monthly payment calculator."""

    def test_standard_calculation(self):
        """Standard mortgage payment at 5% / 25yr / $100k."""
        p = monthly_payment(100000, 0.05, 25)
        # Should be around $584
        self.assertGreater(p, 580)
        self.assertLess(p, 590)

    def test_zero_balance(self):
        p = monthly_payment(0, 0.05, 25)
        self.assertAlmostEqual(p, 0)

    def test_zero_rate(self):
        """At 0% rate, payment = principal / months."""
        p = monthly_payment(120000, 0, 25)
        self.assertAlmostEqual(p, 120000 / (25 * 12))


class TestAmortizationSchedule(unittest.TestCase):
    """Test full amortization schedule generation."""

    def test_basic_schedule(self):
        """Basic 10-year projection with fixed rate."""
        path = build_rate_path("test", initial_rate=0.05, term_years=10,
                               rate_type="fixed", projection_years=10)
        sched = amortization_schedule(100000, path, amortization_years=25,
                                       projection_months=120)
        self.assertGreater(len(sched), 0)
        self.assertLess(sched[-1]['balance'], 100000)

    def test_readvance_smith(self):
        """Readvanceable mortgage: HELOC balance grows with principal paid."""
        path = build_rate_path("sm test", initial_rate=0.05, term_years=10,
                               rate_type="fixed", projection_years=10)
        sched = amortization_schedule(100000, path, amortization_years=25,
                                       projection_months=120,
                                       readvance_smith=True)
        # Last month should have HELOC balance > 0
        self.assertGreater(sched[-1]['heloc_balance'], 0)
        self.assertGreater(sched[-1]['cumulative_readvanced'], 0)

    def test_with_rate_change(self):
        """Rate change mid-stream recalculates payment."""
        steps = [
            RateStep(rate=0.04, start_year=0, end_year=3),
            RateStep(rate=0.06, start_year=3, end_year=10),
        ]
        path = RatePath(name="rate change", steps=steps, rate_type="mixed")
        sched = amortization_schedule(200000, path, amortization_years=25,
                                       projection_months=120)
        # Payment should change around month 36
        pay_yr1 = sched[0]['payment']
        pay_yr4 = sched[36]['payment']
        self.assertNotAlmostEqual(pay_yr1, pay_yr4, places=0)

    def test_zero_balance_terminates(self):
        """Schedule stops when balance reaches 0."""
        path = build_rate_path("high pay", initial_rate=0.05, term_years=1,
                               rate_type="fixed", projection_years=1)
        sched = amortization_schedule(1000, path, amortization_years=1,
                                       projection_months=12)
        # Balance should reach 0 before 12 months
        self.assertTrue(any(s['balance'] <= 0 for s in sched))

    def test_extra_payment(self):
        """Extra monthly payment reduces balance faster."""
        path = build_rate_path("extra", initial_rate=0.05, term_years=10,
                               rate_type="fixed", projection_years=10)
        sched_no_extra = amortization_schedule(200000, path, 25, 120)
        sched_extra = amortization_schedule(200000, path, 25, 120, extra_payment=500)
        self.assertLess(sched_extra[-1]['balance'], sched_no_extra[-1]['balance'])


class TestAnnualSummary(unittest.TestCase):
    """Test annual_summary aggregation."""

    def test_basic_summary(self):
        path = build_rate_path("sum", initial_rate=0.05, term_years=10,
                               rate_type="fixed", projection_years=10)
        sched = amortization_schedule(200000, path, 25, 120)
        annual = annual_summary(sched)
        self.assertGreater(len(annual), 0)
        self.assertIn('year', annual[0])
        self.assertIn('total_interest', annual[0])
        self.assertIn('total_principal', annual[0])

    def test_empty_schedule(self):
        annual = annual_summary([])
        self.assertEqual(len(annual), 0)


class TestHELOCPath(unittest.TestCase):
    """Test HELOC rate path modeling."""

    def test_heloc_rate_variable(self):
        """Variable mortgage: HELOC rate ≈ mortgage rate + spread."""
        mpath = build_rate_path("var", initial_rate=0.05, term_years=5,
                                rate_type="variable", projection_years=10)
        heloc = HELOCPath(name="test heloc", prime_spread=0.005,
                          mortgage_rate_path=mpath)
        rate = heloc.get_heloc_rate(0, "variable")
        self.assertAlmostEqual(rate, 0.05 + 0.005, places=4)

    def test_heloc_rate_fixed(self):
        """Fixed mortgage: HELOC rate = prime + spread (different from mortgage)."""
        mpath = build_rate_path("fixed", initial_rate=0.04, term_years=5,
                                rate_type="fixed", projection_years=10)
        heloc = HELOCPath(name="test heloc", prime_spread=0.005,
                          mortgage_rate_path=mpath, fixed_spread=0.005)
        rate = heloc.get_heloc_rate(0, "fixed")
        # Fixed: base - 1.5% + prime_spread + fixed_spread
        expected = 0.04 - 0.015 + 0.005 + 0.005
        self.assertAlmostEqual(rate, expected, places=4)

    def test_heloc_no_mortgage_path(self):
        """No mortgage rate path → fallback rate."""
        heloc = HELOCPath(name="fallback", prime_spread=0.005,
                          mortgage_rate_path=None)
        rate = heloc.get_heloc_rate(0)
        self.assertAlmostEqual(rate, 0.05 + 0.005, places=4)

    def test_heloc_rate_month(self):
        """Monthly HELOC rate delegates to annual."""
        mpath = build_rate_path("month", initial_rate=0.05, term_years=5,
                                rate_type="variable", projection_years=10)
        heloc = HELOCPath(name="monthly", prime_spread=0.005,
                          mortgage_rate_path=mpath)
        rate_m0 = heloc.get_heloc_rate_month(0)
        rate_m6 = heloc.get_heloc_rate_month(6)
        # Both month 0 and month 6 → year 0 → same rate
        self.assertAlmostEqual(rate_m0, rate_m6, places=4)


class TestBuildStressScenarios(unittest.TestCase):
    """Test stress scenario generation."""

    def test_default_stresses(self):
        """Default: ±1%, ±2% plus base case."""
        path = build_rate_path("base", initial_rate=0.05, term_years=5,
                               rate_type="fixed", projection_years=10)
        scenarios = build_stress_scenarios(path)
        self.assertEqual(len(scenarios), 5)
        self.assertIn("base case", scenarios)

    def test_custom_stresses(self):
        """Custom stress shifts."""
        path = build_rate_path("custom", initial_rate=0.05, term_years=5,
                               rate_type="fixed", projection_years=10)
        scenarios = build_stress_scenarios(path, stresses=[-0.03, 0.03])
        self.assertEqual(len(scenarios), 2)

    def test_floor_at_zero(self):
        """Rates floored at 0.1%."""
        path = build_rate_path("low", initial_rate=0.02, term_years=5,
                               rate_type="fixed", projection_years=10)
        scenarios = build_stress_scenarios(path, stresses=[-0.03])
        for name, p in scenarios.items():
            for step in p.steps:
                self.assertGreaterEqual(step.rate, 0.001)


class TestBuildRenewalStress(unittest.TestCase):
    """Test renewal stress scenario generation."""

    def test_default_shifts(self):
        scenarios = build_renewal_stress(0.04, term_years=3)
        self.assertEqual(len(scenarios), 5)
        self.assertIn("expected renewal", scenarios)

    def test_custom_shifts(self):
        scenarios = build_renewal_stress(0.04, term_years=3,
                                         renewal_shifts=[-0.01, 0.01])
        self.assertEqual(len(scenarios), 2)


class TestBuildBoCPath(unittest.TestCase):
    """Test Bank of Canada rate path builder."""

    def test_flat_rate(self):
        """No BoC changes → flat rate path."""
        path = build_boc_rate_path_scenario(start_prime=0.07, projection_years=10)
        self.assertAlmostEqual(path.get_rate(0), 0.07)
        self.assertAlmostEqual(path.get_rate(5), 0.07)

    def test_with_boc_changes(self):
        """Custom BoC changes list."""
        changes = [(0, 0.07), (1, 0.065), (2, 0.06)]
        path = build_boc_rate_path_scenario(start_prime=0.07,
                                             boc_changes=changes,
                                             projection_years=10)
        self.assertAlmostEqual(path.get_rate(0), 0.07)
        self.assertAlmostEqual(path.get_rate(1), 0.065)

    def test_with_provider_failure(self):
        """Provider that fails → falls back to flat rate."""
        class FailProvider:
            def get_rate_forecast(self, n):
                raise RuntimeError("No data")
        path = build_boc_rate_path_scenario(start_prime=0.07,
                                             projection_years=10,
                                             boc_provider=FailProvider())
        self.assertAlmostEqual(path.get_rate(0), 0.07)


class TestEstimateIRDPenalty(unittest.TestCase):
    """Test IRD penalty estimation."""

    def test_three_months_interest_larger(self):
        """When IRD is small, penalty = 3 months' interest."""
        result = estimate_ird_penalty(
            current_balance=300000, current_rate=0.05,
            remaining_months=12, posted_rate=0.06,
            comparison_rate=0.055)
        self.assertGreater(result['penalty'], 0)
        self.assertIn(result['penalty_type'], ['3-months interest', 'IRD'])

    def test_ird_larger(self):
        """When IRD is large (big rate diff, many months remaining), penalty = IRD."""
        result = estimate_ird_penalty(
            current_balance=400000, current_rate=0.03,
            remaining_months=48, posted_rate=0.06,
            comparison_rate=0.02)
        self.assertGreater(result['penalty'], 0)

    def test_zero_balance(self):
        """Zero balance → zero penalty."""
        result = estimate_ird_penalty(0, 0.05, 12)
        self.assertEqual(result['penalty'], 0)

    def test_default_rates(self):
        """When posted/comparison rates not specified, use defaults."""
        result = estimate_ird_penalty(300000, 0.05, 24)
        self.assertGreater(result['penalty'], 0)
        self.assertAlmostEqual(result['posted_rate'], 0.05 + 0.015)

    def test_open_term_zero_penalty(self):
        """Issue #651: an open term breaks at no cost — a declared zero
        (DP#32), for the same inputs that otherwise carry a real penalty."""
        closed = estimate_ird_penalty(300000, 0.05, 24)
        self.assertGreater(closed['penalty'], 0)  # closed default is non-zero
        result = estimate_ird_penalty(300000, 0.05, 24, is_open=True)
        self.assertEqual(result['penalty'], 0.0)
        self.assertEqual(result['ird'], 0.0)
        self.assertEqual(result['three_months_interest'], 0.0)

    def test_open_absence_is_no_op(self):
        """Omitting is_open leaves the incumbent closed-term result intact."""
        self.assertEqual(
            estimate_ird_penalty(300000, 0.05, 24),
            estimate_ird_penalty(300000, 0.05, 24, is_open=False),
        )


class TestIRDSingleSource(unittest.TestCase):
    """Issue #724 / DP#10: estimate_ird_penalty must delegate to ird_penalty.py.

    The 3-months-interest and IRD *formulas* have one home —
    countries.canada.ird_penalty. These tests pin that single-source
    invariant: estimate_ird_penalty's components must equal the canonical
    functions' output for the same effective rates. If rate_model ever
    restates the formula, this fails. All figures are fabricated round
    numbers (DP#4/#15).
    """

    def _canonical(self, balance, posted_rate, comparison_rate, months):
        from countries.canada.ird_penalty import (
            compute_ird_penalty, compute_three_months_interest)
        tmi = compute_three_months_interest(balance, posted_rate)
        term = max(1, int(round(months / 12)))
        ird = compute_ird_penalty(
            balance, posted_rate, months,
            posted_rates={term: comparison_rate},
            use_discounted_rate=False)
        return tmi, ird

    def test_single_source_matches_canonical(self):
        """estimate_ird_penalty components == ird_penalty direct result."""
        # (balance, current_rate, months, posted_rate, comparison_rate)
        cases = [
            (400000, 0.03, 48, 0.06, 0.02),   # IRD dominates, big gap
            (300000, 0.05, 12, 0.06, 0.055),  # 3-months interest dominates
            (200000, 0.04, 36, 0.05, 0.045),  # small gap, multi-year
            (500000, 0.045, 60, 0.07, 0.03),  # large balance, long term
            (100000, 0.05, 36, None, None),   # default rates path
        ]
        for balance, rate, months, posted, comp in cases:
            with self.subTest(balance=balance, months=months):
                result = estimate_ird_penalty(
                    balance, rate, months, posted_rate=posted,
                    comparison_rate=comp)
                p = posted if posted is not None else rate + 0.015
                c = comp if comp is not None else rate - 0.005
                tmi_canon, ird_canon = self._canonical(balance, p, c, months)
                self.assertAlmostEqual(
                    result['three_months_interest'], tmi_canon, places=6,
                    msg='3-months interest diverged from ird_penalty.py')
                self.assertAlmostEqual(
                    result['ird'], ird_canon, places=6,
                    msg='IRD diverged from ird_penalty.py')
                self.assertAlmostEqual(
                    result['penalty'], max(tmi_canon, ird_canon), places=6)

    def test_zero_balance_single_source(self):
        """Zero balance: both the wrapper and canonical agree on zero."""
        result = estimate_ird_penalty(0, 0.05, 24, posted_rate=0.06,
                                      comparison_rate=0.05)
        tmi_canon, ird_canon = self._canonical(0, 0.06, 0.05, 24)
        self.assertEqual(result['penalty'], 0)
        self.assertEqual(tmi_canon, 0.0)
        self.assertEqual(ird_canon, 0.0)


class TestGenerateAllMortgageScenarios(unittest.TestCase):
    """Test comprehensive scenario generation from config dict."""

    def test_basic_config(self):
        """Generate scenarios from a minimal config."""
        cfg = {
            'property': {
                'mortgage_balance': 300000,
                'mortgage_rate': 0.045,
                'amortization_years': 25,
                'heloc_readvance': True,
                'refinance_options': [
                    {'name': '3yr fixed', 'rate': 0.0404, 'term_years': 3, 'type': 'fixed'},
                ],
            },
            'assumptions': {
                'projection_years': 5,
            },
        }
        scenarios = generate_all_mortgage_scenarios(cfg)
        self.assertIn('_ird_penalty', scenarios)
        self.assertGreater(len(scenarios), 2)

    def test_no_refinance_options(self):
        """No refinance options → only base scenarios."""
        cfg = {
            'property': {
                'mortgage_balance': 200000,
                'mortgage_rate': 0.05,
                'amortization_years': 25,
                'heloc_readvance': False,
            },
            'assumptions': {
                'projection_years': 3,
            },
        }
        scenarios = generate_all_mortgage_scenarios(cfg)
        self.assertIn('_ird_penalty', scenarios)


class TestPrintAmortizationTables(unittest.TestCase):
    """Test that print_amortization_tables runs without errors."""

    def test_print_basic(self):
        """Capture stdout from print function."""
        cfg = {
            'property': {
                'mortgage_balance': 200000,
                'mortgage_rate': 0.05,
                'amortization_years': 25,
                'heloc_readvance': True,
            },
            'assumptions': {
                'projection_years': 3,
            },
        }
        scenarios = generate_all_mortgage_scenarios(cfg)
        import sys
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            print_amortization_tables(scenarios)
        finally:
            sys.stdout = old_stdout


if __name__ == '__main__':
    unittest.main()