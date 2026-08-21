#!/usr/bin/env python3
"""Issue #294: retirement transition tests.

A projection that crosses a member's retirement_age must:
  - stop that member's employment income (no salary / no growth), and
  - begin CPP + OAS (from the member's estimate / OAS max), surfaced in YearResult.

The CPP start-age adjustment must reward deferral (70 > 65). And a pure
pre-retirement horizon must be byte-for-byte unchanged.

Lean, single-responsibility, relational (DP#3). Round numbers, no PII.

Run: uv run pytest tests/test_retirement_transition.py -q
"""

from tax_data import default_tax_provider
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import FamilySimulation, SimulationConfig
from countries.canada.retirement_transition import (
    cpp_from_estimate, oas_gross, oas_after_clawback,
    plan_drawdown_net, retirement_spending_target,
    member_retirement_income,
    DEFAULT_NET_REPLACEMENT_RATE,
)
from tax_calculator import tax_on_income


START_YEAR = 2026


def _base_dict(projection_years, primary_birth_year=1979, retirement_age=65):
    """Minimal config that crosses retirement when projection_years is large."""
    return {
        'assumptions': {
            'projection_years': projection_years,
            'investment_return': 0.05,
            'salary_growth': 0.02,
            'start_year': START_YEAR,
            'frozen_brackets': True,
            'time_step': 'yearly',
        },
        'savings': {'rate': 0.10},
        'property': {
            'house_value': 600000, 'mortgage_balance': 200000,
            'mortgage_rate': 0.045, 'ltv_max': 0.80,
            'current_payment_monthly': 1200, 'amortization_years': 25,
            'margin_available': 0,
        },
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 120000,
                 'birth_year': primary_birth_year, 'retirement_age': retirement_age,
                 'cpp_monthly_estimated': 1000, 'cpp_start_age': 65,
                 'oas_start_age': 65, 'oas_defer_months': 0,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
                # Spouse with no birth_year: never retires within the horizon,
                # so its employment income is a clean control.
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 40000},
            ],
            'children': [],
        },
        'accounts': {
            'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33810,
            'tfsa_annual_room_per_person': 7000, 'resp_current_balance': 0,
        },
        'retirement': {
            'oas_annual_max': 8908, 'oas_clawback_threshold': 95323,
            'drawdown_order': ['tfsa', 'non_reg', 'rrsp'],
        },
        'cash_flows': [],
    }


class TestRetirementAgeConfig(unittest.TestCase):
    """retirement_age is a per-member input (default 65) that round-trips."""

    def test_default_65_when_absent(self):
        # Member without retirement_age gets the default 65.
        d = _base_dict(5)
        del d['family']['members'][0]['retirement_age']
        cfg = SimulationConfig.from_dict(d)
        self.assertEqual(cfg.family_members[0]['retirement_age'], 65)

    def test_roundtrip_preserves_retirement_age(self):
        d = _base_dict(5)
        d['family']['members'][0]['retirement_age'] = 62
        cfg = SimulationConfig.from_dict(d)
        cfg2 = SimulationConfig.from_dict(cfg.to_dict())
        self.assertEqual(cfg2.family_members[0]['retirement_age'], 62)


class TestRetirementCrossing(unittest.TestCase):
    """A horizon crossing retirement_age stops income and starts CPP/OAS."""

    def test_employment_stops_and_cpp_oas_appear(self):
        # primary b.1979, retires at 65 → retired in 2044 (year index 18).
        cfg = SimulationConfig.from_dict(_base_dict(projection_years=20))
        results = FamilySimulation(cfg).run()

        pre = results[10]   # 2036, primary age 57: working
        post = results[18]  # 2044, primary age 65: retired

        # Pre-retirement: primary employed, no government income.
        self.assertGreater(pre.primary_income, 0)
        self.assertEqual(pre.cpp_income, 0.0)
        self.assertEqual(pre.oas_income, 0.0)

        # Relational: post-retirement primary employment income is 0,
        # while CPP and OAS are both positive.
        self.assertEqual(post.primary_income, 0.0)
        self.assertGreater(post.cpp_income, 0.0)
        self.assertGreater(post.oas_income, 0.0)

    def test_spouse_without_birth_year_keeps_working(self):
        # Control: the spouse has no birth_year, so it never retires and its
        # employment income keeps flowing even after the primary retires.
        cfg = SimulationConfig.from_dict(_base_dict(projection_years=20))
        results = FamilySimulation(cfg).run()
        post = results[18]
        self.assertGreater(post.spouse_income, 0.0)

    def test_total_income_includes_government_and_drawdown(self):
        cfg = SimulationConfig.from_dict(_base_dict(projection_years=20))
        results = FamilySimulation(cfg).run()
        post = results[18]
        # total_family_income spans employment + CPP + OAS + pension + drawdown.
        expected = (post.employment_income + post.cpp_income + post.oas_income
                    + post.pension_income + post.drawdown_income)
        self.assertAlmostEqual(post.total_family_income, expected, places=2)


class TestTenYearUnchanged(unittest.TestCase):
    """A pure pre-retirement horizon is byte-for-byte unchanged (#294 req 4)."""

    def test_ten_year_equals_first_ten_of_twenty(self):
        # primary retires only at 2044 (year 18), so the first 10 years never
        # touch retirement logic and must be identical run-to-run.
        r10 = FamilySimulation(SimulationConfig.from_dict(_base_dict(10))).run()
        r20 = FamilySimulation(SimulationConfig.from_dict(_base_dict(20))).run()
        self.assertEqual(len(r10), 10)
        for a, b in zip(r10, r20[:10]):
            self.assertAlmostEqual(a.total_family_income, b.total_family_income, places=6)
            self.assertAlmostEqual(a.total_assets, b.total_assets, places=6)
            self.assertEqual(a.cpp_income, 0.0)
            self.assertEqual(a.oas_income, 0.0)
            self.assertEqual(a.drawdown_income, 0.0)


class TestCPPStartAge(unittest.TestCase):
    """CPP start-age adjustment: deferring to 70 beats starting at 65."""

    def test_defer_to_70_higher_than_65(self):
        at_65 = cpp_from_estimate(monthly_estimate=1000, start_age=65, claim_age=70)
        at_70 = cpp_from_estimate(monthly_estimate=1000, start_age=70, claim_age=70)
        self.assertGreater(at_70, at_65)
        # And starting at 60 is lower than starting at 65.
        at_60 = cpp_from_estimate(monthly_estimate=1000, start_age=60, claim_age=65)
        self.assertLess(at_60, at_65)

    def test_no_cpp_before_start_age(self):
        # Member claim_age below cpp_start_age → no CPP yet.
        self.assertEqual(
            cpp_from_estimate(monthly_estimate=1000, start_age=65, claim_age=63), 0.0)


class TestClaimAgeZeroIsNotSilentlyCoercedTo65(unittest.TestCase):
    """DP#32 (#606): cpp_start_age / oas_start_age of 0 is a data bug (never
    a legitimate claim age), not "unset" -- it must not be silently coerced
    to 65. Only a genuinely absent key defaults to 65."""

    def _member(self, **overrides):
        m = {'birth_year': 1960, 'retirement_age': 65, 'cpp_monthly_estimated': 1000}
        m.update(overrides)
        return m

    def test_absent_cpp_start_age_still_defaults_to_65(self):
        # sim_year 2026: member is 66, at/after the implicit 65 default.
        result = member_retirement_income(
            self._member(), sim_year=2026, oas_annual_max=8500, oas_clawback_threshold=90000)
        self.assertAlmostEqual(result.cpp, 12000)  # 1000/mo * 12, no age adjustment

    def test_explicit_cpp_start_age_zero_is_not_coerced_to_65(self):
        """An explicit cpp_start_age=0 must not read identically to the
        implicit 65 default -- the erroneous claim age must be visible in
        the result, not silently absorbed as 'no adjustment'."""
        at_65_default = member_retirement_income(
            self._member(), sim_year=2026, oas_annual_max=8500, oas_clawback_threshold=90000)
        at_explicit_zero = member_retirement_income(
            self._member(cpp_start_age=0), sim_year=2026,
            oas_annual_max=8500, oas_clawback_threshold=90000)
        self.assertNotAlmostEqual(at_explicit_zero.cpp, at_65_default.cpp)

    def test_absent_oas_start_age_still_defaults_to_65(self):
        # Member age 66 >= implicit 65 default -> OAS flows.
        result = member_retirement_income(
            self._member(), sim_year=2026, oas_annual_max=8500, oas_clawback_threshold=90000)
        self.assertGreater(result.oas, 0)

    def test_explicit_oas_start_age_zero_is_not_coerced_to_65(self):
        """An explicit oas_start_age=0 must not be silently treated as 65 --
        with the coercion removed, OAS starts as soon as the member is
        retired (age >= 0 is always true), which is visibly different from
        the age-gated 65 default."""
        # Member retires and is age 61 in this sim_year -- below the implicit
        # 65 default (no OAS) but at/above an honored explicit 0 (OAS flows).
        member = self._member(retirement_age=61, oas_start_age=0)
        result_zero = member_retirement_income(
            member, sim_year=2021, oas_annual_max=8500, oas_clawback_threshold=90000)
        self.assertGreater(result_zero.oas, 0)

        member_default = self._member(retirement_age=61)
        result_default = member_retirement_income(
            member_default, sim_year=2021, oas_annual_max=8500, oas_clawback_threshold=90000)
        self.assertEqual(result_default.oas, 0.0)


class TestOASClawback(unittest.TestCase):
    """OAS deferral bonus and clawback arithmetic."""

    def test_defer_increases_oas(self):
        self.assertGreater(oas_gross(8908, defer_months=60), oas_gross(8908, defer_months=0))

    def test_clawback_above_threshold(self):
        full = oas_after_clawback(8908, net_income=50000, clawback_threshold=95323)
        clawed = oas_after_clawback(8908, net_income=120000, clawback_threshold=95323)
        self.assertEqual(full, 8908)
        self.assertLess(clawed, full)


class TestDrawdown(unittest.TestCase):
    """Drawdown (plan_drawdown_net, #363/#579) follows drawdown_order, fills a
    NET target, and grosses up only the taxable portion of each source."""

    def test_order_tfsa_then_rrsp(self):
        canada = {'tfsa_primary_balance': 30000, 'rrsp_balance': 100000}
        # Net need exceeds the (tax-free) TFSA → spills into RRSP (taxable,
        # grossed up at the 40% marginal rate so the after-tax proceeds meet
        # the remaining net need).
        plan = plan_drawdown_net(50000, ['tfsa', 'non_reg', 'rrsp'], canada,
                                 non_reg_balance=0, non_reg_acb=0, marginal_rate=0.40)
        # TFSA delivers $1 net per $1 withdrawn (30000 net); the remaining
        # 20000 net comes from RRSP, grossed up to 20000 / (1 - 0.40).
        expected_rrsp_gross = 20000 / 0.60
        self.assertAlmostEqual(plan.total_withdrawn, 30000 + expected_rrsp_gross)
        self.assertAlmostEqual(plan.taxable_withdrawn, expected_rrsp_gross)
        self.assertAlmostEqual(plan.balance_deltas['tfsa_primary_balance'], -30000)
        self.assertAlmostEqual(plan.balance_deltas['rrsp_balance'], -expected_rrsp_gross)
        # The whole point of #363/#579: after-tax proceeds meet the net need.
        self.assertAlmostEqual(plan.net_delivered, 50000)

    def test_capped_by_available_balances(self):
        canada = {'tfsa_primary_balance': 5000}
        plan = plan_drawdown_net(50000, ['tfsa'], canada, non_reg_balance=0,
                                 non_reg_acb=0, marginal_rate=0.40)
        self.assertAlmostEqual(plan.total_withdrawn, 5000)
        # Balance-capped: net delivered is less than the requested net need.
        self.assertAlmostEqual(plan.net_delivered, 5000)
        self.assertLess(plan.net_delivered, 50000)


class TestNetReplacementTarget(unittest.TestCase):
    """Issue #301: target NET spending, not a gross-income proxy."""

    def test_spending_target_overrides_rate(self):
        target = retirement_spending_target(
            pre_retirement_net_income=100000, net_replacement_rate=0.65,
            spending_target=140000)
        self.assertEqual(target, 140000)

    def test_rate_applies_when_no_absolute_target(self):
        # 0.65 × pre-retirement net, no absolute override.
        target = retirement_spending_target(
            pre_retirement_net_income=100000, net_replacement_rate=0.65)
        self.assertAlmostEqual(target, 65000)

    def test_default_rate_is_net_not_full(self):
        self.assertLess(DEFAULT_NET_REPLACEMENT_RATE, 1.0)

    def test_gross_up_meets_net_after_tax(self):
        # #363/#579: plan_drawdown_net grosses up a fully-taxable draw (RRSP)
        # so the after-tax proceeds exactly meet the net need (within rounding),
        # and the gross withdrawal always exceeds the net need for rate > 0.
        canada = {'rrsp_balance': 1_000_000}
        plan = plan_drawdown_net(30000, ['rrsp'], canada, non_reg_balance=0,
                                 non_reg_acb=0, marginal_rate=0.40)
        gross = plan.total_withdrawn
        self.assertAlmostEqual(gross * (1 - 0.40), 30000)
        self.assertGreater(gross, 30000)
        self.assertAlmostEqual(plan.net_delivered, 30000)


class TestDrawdownIsNetReplacement(unittest.TestCase):
    """The wired retirement-year drawdown tracks 0.65×net − CPP − OAS (#301)."""

    def test_drawdown_lower_than_gross_proxy_and_tracks_net(self):
        cfg = SimulationConfig.from_dict(_base_dict(projection_years=20))
        post = FamilySimulation(cfg).run()[18]  # 2044, primary retired

        brackets = default_tax_provider().get_combined_brackets(START_YEAR, 'quebec')
        # Pre-retirement NET of the retiring primary (base gross 120000).
        primary_net = 120000 - tax_on_income(120000, brackets)
        old_gross_proxy = 120000  # the pre-#301 model replaced GROSS income

        # Relational: new drawdown is materially below the old gross proxy.
        self.assertLess(post.drawdown_income, old_gross_proxy)

        # The drawdown is sized to a NET shortfall after CPP, OAS, and
        # remaining employment income are netted off.  The spouse (b.~1960)
        # still works in year 18, so their after-tax pay covers part of the
        # target, leaving a smaller drawdown than CPP+OAS alone would imply.
        net_target = DEFAULT_NET_REPLACEMENT_RATE * primary_net
        remaining_net = (post.primary_income + post.spouse_income) - (
            tax_on_income(post.primary_income, brackets)
            + tax_on_income(post.spouse_income, brackets)
        )
        covered_by_benefits_and_income = post.cpp_income + post.oas_income + remaining_net
        net_shortfall = max(0.0, net_target - covered_by_benefits_and_income)

        if net_shortfall > 0:
            # Drawdown > 0 when there's a shortfall (unless balances are zero).
            self.assertGreater(post.drawdown_income, 0.0)
            # The grossed-up drawdown is >= the net shortfall because of tax
            # gross-up, but may be capped by available balances.
            if post.drawdown_income < net_shortfall:
                # Drawdown was balance-capped below the shortfall — still > 0.
                self.assertGreater(post.drawdown_income, 0.0)
            else:
                self.assertGreaterEqual(post.drawdown_income, net_shortfall)

    def test_absolute_spending_target_changes_drawdown(self):
        # A higher absolute spending_target draws more than the 0.65 rate.
        d_rate = _base_dict(projection_years=20)
        d_target = _base_dict(projection_years=20)
        d_target['retirement']['spending_target'] = 200000
        r_rate = FamilySimulation(SimulationConfig.from_dict(d_rate)).run()[18]
        r_target = FamilySimulation(SimulationConfig.from_dict(d_target)).run()[18]
        self.assertGreater(r_target.drawdown_income, r_rate.drawdown_income)

    def test_config_roundtrip_preserves_targets(self):
        d = _base_dict(5)
        d['retirement']['net_replacement_rate'] = 0.7
        d['retirement']['spending_target'] = 150000
        cfg2 = SimulationConfig.from_dict(
            SimulationConfig.from_dict(d).to_dict())
        self.assertEqual(cfg2.retirement_data['net_replacement_rate'], 0.7)
        self.assertEqual(cfg2.retirement_data['spending_target'], 150000)


if __name__ == '__main__':
    unittest.main()
