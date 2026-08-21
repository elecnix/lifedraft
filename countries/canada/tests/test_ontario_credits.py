#!/usr/bin/env python3
"""Unit tests for Ontario Tax Credits & Surtax (issue #375).

Per DP#17: tests exercise every rule path, not just every module. The
ontario_credits.py module has zero test coverage despite production code.
This file tests:
- ontario_surtax: two-tier surtax with threshold boundary tests
- ontario_health_premium: five-tier progressive levy
- ontario_sales_tax_credit: refundable Trillium component
- ontario_lift_credit: Low-income Individuals and Families Tax credit

Run with: PYTHONPATH=. uv run pytest countries/canada/tests/test_ontario_credits.py -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))

from tax_data import TaxDataProvider
from countries.canada.provinces.ontario_credits import (
    ontario_surtax,
    ontario_health_premium,
    ontario_sales_tax_credit,
    ontario_trillium_benefit,
    ontario_lift_credit,
)


def _provider() -> TaxDataProvider:
    return TaxDataProvider()


# =============================================================================
# Ontario Surtax Tests (Two-Tier)
# =============================================================================

class TestOntarioSurtax(unittest.TestCase):
    """Ontario two-tier surtax levied on basic Ontario tax (DP#17)."""

    def setUp(self):
        self.provider = _provider()
        self.data = self.provider.get_year_data(2026, 'canada', 'ontario')

    def test_no_tax_no_surtax(self):
        """Zero basic tax means no surtax."""
        result = ontario_surtax(0.0, year=2026, provider=self.provider)
        self.assertEqual(result, 0.0)

    def test_below_first_threshold_no_surtax(self):
        """Tax below first threshold pays no surtax."""
        result = ontario_surtax(self.data.on_surtax_threshold_1 - 100, year=2026, provider=self.provider)
        self.assertEqual(result, 0.0)

    def test_at_first_threshold_boundary(self):
        """Tax above threshold_1 triggers first tier."""
        tax = self.data.on_surtax_threshold_1 + 1000
        result = ontario_surtax(tax, year=2026, provider=self.provider)
        self.assertGreater(result, 0.0)
        expected = self.data.on_surtax_rate_1 * 1000
        self.assertAlmostEqual(result, expected, places=2)

    def test_at_second_threshold_boundary(self):
        """Tax at threshold_2 pays both tiers."""
        result = ontario_surtax(self.data.on_surtax_threshold_2, year=2026, provider=self.provider)
        expected = self.data.on_surtax_rate_1 * (self.data.on_surtax_threshold_2 - self.data.on_surtax_threshold_1)
        self.assertAlmostEqual(result, expected, places=2)

    def test_above_second_threshold(self):
        """Tax above threshold_2 pays both tier rates."""
        tax = self.data.on_surtax_threshold_2 + 5000
        result = ontario_surtax(tax, year=2026, provider=self.provider)
        expected = (
            self.data.on_surtax_rate_1 * (tax - self.data.on_surtax_threshold_1) +
            self.data.on_surtax_rate_2 * (tax - self.data.on_surtax_threshold_2)
        )
        self.assertAlmostEqual(result, expected, places=2)

    def test_year_versioned_rates(self):
        """Surtax thresholds/rates vary by year (DP#20)."""
        self.assertEqual(self.data.on_surtax_rate_1, 0.20)
        self.assertEqual(self.data.on_surtax_rate_2, 0.36)


# =============================================================================
# Ontario Health Premium Tests (Progressive Levy)
# =============================================================================

class TestOntarioHealthPremium(unittest.TestCase):
    """Ontario Health Premium — progressive levy based on taxable income (DP#17)."""

    def setUp(self):
        self.provider = _provider()
        self.data = self.provider.get_year_data(2026, 'canada', 'ontario')

    def test_zero_income_zero_premium(self):
        """No taxable income means no health premium."""
        result = ontario_health_premium(0.0, year=2026, provider=self.provider)
        self.assertEqual(result, 0.0)

    def test_below_floor_zero_premium(self):
        """Income at floor ($20,000) pays $300 (first tier base)."""
        result = ontario_health_premium(20000, year=2026, provider=self.provider)
        self.assertEqual(result, 0.0)

    def test_five_tier_structure(self):
        """Health premium has five progressive tiers (DP#17)."""
        self.assertEqual(len(self.data.on_health_premium_tiers), 5)
        for tier in self.data.on_health_premium_tiers:
            self.assertEqual(len(tier), 4)

    def test_middle_tier_calculation(self):
        """Health premium calculation uses: min(cap, base + rate * (income - lower))."""
        premium = ontario_health_premium(36000, year=2026, provider=self.provider)
        self.assertGreaterEqual(premium, 300.0)

    def test_fifth_tier_high_income(self):
        """High income triggers top tier premium."""
        premium = ontario_health_premium(100000, year=2026, provider=self.provider)
        self.assertGreater(premium, 500)


# =============================================================================
# Ontario Sales Tax Credit (OSTC) Tests
# =============================================================================

class TestOntarioSalesTaxCredit(unittest.TestCase):
    """Ontario Sales Tax Credit (refundable component of Trillium) (DP#17)."""

    def setUp(self):
        self.provider = _provider()

    def test_no_income_positive_credit(self):
        """Zero income qualifies for full credit (OSTC is refundable)."""
        result = ontario_sales_tax_credit(0.0, year=2026, provider=self.provider)
        self.assertEqual(result, 378.0)

    def test_single_with_no_children(self):
        """Single person with no children uses single threshold."""
        provider = _provider()
        data = provider.get_year_data(2026, 'canada', 'ontario')
        result = ontario_sales_tax_credit(
            data.on_ostc_single_threshold - 5000,
            num_adults=1, num_children=0,
            year=2026, provider=provider
        )
        expected = data.on_ostc_amount_per_person
        self.assertAlmostEqual(result, expected, places=2)

    def test_family_with_children(self):
        """Family with children: credit scales with family size."""
        provider = _provider()
        result = ontario_sales_tax_credit(
            30000, num_adults=2, num_children=1,
            year=2026, provider=provider
        )
        self.assertGreater(result, 0.0)

    def test_phase_out_at_threshold(self):
        """Credit reduces when income exceeds threshold."""
        provider = _provider()
        data = provider.get_year_data(2026, 'canada', 'ontario')
        result = ontario_sales_tax_credit(
            data.on_ostc_single_threshold + 10000,
            num_adults=1, num_children=0,
            year=2026, provider=provider
        )
        self.assertLess(result, data.on_ostc_amount_per_person)


# =============================================================================
# Ontario LIFT Credit Tests
# =============================================================================

class TestOntarioLiftCredit(unittest.TestCase):
    """Ontario Low-income Individuals and Families Tax Credit (DP#17)."""

    def setUp(self):
        self.provider = _provider()
        self.data = self.provider.get_year_data(2026, 'canada', 'ontario')

    def test_no_employment_income_no_credit(self):
        """Without employment income, no LIFT credit."""
        result = ontario_lift_credit(0.0, 50000, year=2026, provider=self.provider)
        self.assertEqual(result, 0.0)

    def test_low_employment_income_above_lift_threshold(self):
        """Employment income at LIFT threshold with low net income gets credit."""
        result = ontario_lift_credit(
            30000, self.data.on_lift_individual_threshold - 5000,
            year=2026, provider=self.provider
        )
        self.assertGreater(result, 0.0)

    def test_employment_income_capped_at_max(self):
        """LIFT credit caps at maximum regardless of employment income."""
        result = ontario_lift_credit(
            200000, 50000, year=2026, provider=self.provider
        )
        self.assertLessEqual(result, self.data.on_lift_max)


# =============================================================================
# Exact-value / boundary coverage moved from test_ontario_tax_data.py (DP#11)
# =============================================================================
# These exercise the same ``ontario_credits`` functions as the data-driven
# classes above, but assert hardcoded dollar values and year-versioned
# thresholds at exact boundaries (DP#17). They are relocated here so this
# file is the single home for ``ontario_credits`` unit tests; the assertions
# are unchanged (no weakening). Class names are suffixed ``Exact`` where they
# would otherwise collide with the data-driven classes above.
# =============================================================================

class TestOntarioSurtaxExact(unittest.TestCase):
    """Surtax on basic Ontario tax — two stacking tiers."""

    def test_no_surtax_below_first_threshold(self):
        self.assertEqual(ontario_surtax(5000, 2026), 0.0)

    def test_zero_for_nonpositive_tax(self):
        self.assertEqual(ontario_surtax(0, 2026), 0.0)
        self.assertEqual(ontario_surtax(-100, 2026), 0.0)

    def test_first_tier_only(self):
        # Between thresholds: only the 20% tier applies.
        # 0.20 × (7000 − 5818) = 236.40
        self.assertAlmostEqual(ontario_surtax(7000, 2026), 236.40, places=2)

    def test_both_tiers_stack(self):
        # 0.20 × (8000 − 5818) + 0.36 × (8000 − 7446)
        expected = 0.20 * (8000 - 5818) + 0.36 * (8000 - 7446)
        self.assertAlmostEqual(ontario_surtax(8000, 2026), expected, places=2)

    def test_year_versioned_thresholds(self):
        # 2025 thresholds are lower, so the same tax yields more surtax.
        self.assertGreater(ontario_surtax(8000, 2025), ontario_surtax(8000, 2026))
        # 2025: 0.20×(8000−5710) + 0.36×(8000−7307)
        expected_2025 = 0.20 * (8000 - 5710) + 0.36 * (8000 - 7307)
        self.assertAlmostEqual(ontario_surtax(8000, 2025), expected_2025, places=2)


class TestOntarioHealthPremiumExact(unittest.TestCase):
    """Health premium tiers based on taxable income (not a surtax)."""

    def test_zero_below_floor(self):
        self.assertEqual(ontario_health_premium(20000, 2026), 0.0)
        self.assertEqual(ontario_health_premium(0, 2026), 0.0)
        self.assertEqual(ontario_health_premium(-5000, 2026), 0.0)

    def test_first_tier_marginal(self):
        # >$20k–$36k: 6% over $20k, capped at $300.
        # 25000 → 0.06 × 5000 = 300 (hits cap exactly)
        self.assertAlmostEqual(ontario_health_premium(25000, 2026), 300.0, places=2)
        # 22000 → 0.06 × 2000 = 120
        self.assertAlmostEqual(ontario_health_premium(22000, 2026), 120.0, places=2)

    def test_second_tier(self):
        # >$36k–$48k: $300 + 6% over $36k, capped at $450.
        # 40000 → 300 + 0.06×4000 = 540 → cap 450
        self.assertAlmostEqual(ontario_health_premium(40000, 2026), 450.0, places=2)
        # 37000 → 300 + 0.06×1000 = 360
        self.assertAlmostEqual(ontario_health_premium(37000, 2026), 360.0, places=2)

    def test_third_tier(self):
        # >$48k–$72k: $450 + 25% over $48k, capped at $600.
        # 48400 → 450 + 0.25×400 = 550
        self.assertAlmostEqual(ontario_health_premium(48400, 2026), 550.0, places=2)
        # 50000 → 450 + 0.25×2000 = 950 → cap 600
        self.assertAlmostEqual(ontario_health_premium(50000, 2026), 600.0, places=2)

    def test_fourth_tier(self):
        # >$72k–$200k: $600 + 25% over $72k, capped at $750.
        # 73000 → 600 + 0.25×1000 = 850 → cap 750
        self.assertAlmostEqual(ontario_health_premium(73000, 2026), 750.0, places=2)
        self.assertAlmostEqual(ontario_health_premium(150000, 2026), 750.0, places=2)

    def test_top_tier_cap(self):
        # >$200k: never exceeds $900.
        self.assertAlmostEqual(ontario_health_premium(300000, 2026), 900.0, places=2)
        self.assertAlmostEqual(ontario_health_premium(10_000_000, 2026), 900.0, places=2)

    def test_monotonic_nondecreasing(self):
        prev = -1.0
        for income in range(0, 260000, 1000):
            cur = ontario_health_premium(income, 2026)
            self.assertGreaterEqual(cur + 1e-9, prev)
            prev = cur


class TestOntarioSalesTaxCreditExact(unittest.TestCase):
    """OSTC — the refundable Ontario Trillium component."""

    def test_full_credit_below_threshold_single(self):
        # Single adult, income below threshold → full $378.
        self.assertAlmostEqual(
            ontario_sales_tax_credit(20000, 1, 0, 2026), 378.0, places=2)

    def test_per_person_scaling(self):
        # 2 adults + 2 children below family threshold → 378 × 4.
        self.assertAlmostEqual(
            ontario_sales_tax_credit(30000, 2, 2, 2026), 378.0 * 4, places=2)

    def test_single_phaseout(self):
        # Single over $29,047: reduced 4% of excess.
        # 40000 → 378 − 0.04×(40000−29047) = 378 − 438.12 → floors at 0
        self.assertAlmostEqual(
            ontario_sales_tax_credit(40000, 1, 0, 2026), 0.0, places=2)
        # 35000 → 378 − 0.04×(35000−29047) = 378 − 238.12 = 139.88
        self.assertAlmostEqual(
            ontario_sales_tax_credit(35000, 1, 0, 2026), 139.88, places=2)

    def test_family_uses_family_threshold(self):
        # Family with children uses the higher family threshold.
        # 40000, 2a+2c → 1512 − 0.04×(40000−36309) = 1512 − 147.64 = 1364.36
        self.assertAlmostEqual(
            ontario_sales_tax_credit(40000, 2, 2, 2026), 1364.36, places=2)

    def test_floors_at_zero(self):
        self.assertEqual(ontario_sales_tax_credit(1_000_000, 1, 0, 2026), 0.0)

    def test_negative_income_treated_as_zero(self):
        self.assertAlmostEqual(
            ontario_sales_tax_credit(-100, 1, 0, 2026), 378.0, places=2)

    def test_year_versioned_amount(self):
        # 2025 (2024 tax year) per-person amount is lower than 2026.
        self.assertAlmostEqual(
            ontario_sales_tax_credit(20000, 1, 0, 2025), 371.0, places=2)


class TestOntarioTrilliumBenefit(unittest.TestCase):
    """OTB total = OSTC + caller-supplied OEPTC + NOEC."""

    def test_sums_components(self):
        # OSTC full ($378) + OEPTC 1238 + NOEC 189
        total = ontario_trillium_benefit(
            20000, 1, 0, oeptc=1238, noec=189, year=2026)
        self.assertAlmostEqual(total, 378.0 + 1238 + 189, places=2)

    def test_ostc_only_when_no_other_components(self):
        self.assertAlmostEqual(
            ontario_trillium_benefit(20000, 1, 0, year=2026), 378.0, places=2)

    def test_negative_components_treated_as_zero(self):
        self.assertAlmostEqual(
            ontario_trillium_benefit(20000, 1, 0, oeptc=-50, noec=-50, year=2026),
            378.0, places=2)


class TestOntarioLIFTCredit(unittest.TestCase):
    """LIFT — non-refundable low-income workers credit."""

    def test_no_credit_without_employment_income(self):
        self.assertEqual(ontario_lift_credit(0, 0, 0, 2026), 0.0)

    def test_capped_at_max(self):
        # 5.05% of $30k = $1,515 → capped at $875, no phase-out below thresholds.
        self.assertAlmostEqual(
            ontario_lift_credit(30000, 30000, 30000, 2026), 875.0, places=2)

    def test_rate_applies_below_cap(self):
        # 5.05% of $10k = $505 (below the $875 cap).
        self.assertAlmostEqual(
            ontario_lift_credit(10000, 10000, 10000, 2026), 505.0, places=2)

    def test_individual_phaseout(self):
        # Individual net income $40k → reduce by 5%×(40000−32500)=375.
        # base 875 − 375 = 500
        self.assertAlmostEqual(
            ontario_lift_credit(30000, 40000, 40000, 2026), 500.0, places=2)

    def test_family_phaseout_can_dominate(self):
        # Individual below its threshold, but family income high → family
        # reduction is the greater term.
        # indiv 30k (excess 0), family 70k (excess 5000) → reduce 5%×5000=250
        self.assertAlmostEqual(
            ontario_lift_credit(30000, 30000, 70000, 2026), 875.0 - 250.0, places=2)

    def test_greater_of_two_reductions(self):
        # indiv excess: 45000−32500 = 12500 → 625
        # family excess: 66000−65000 = 1000 → 50
        # greater = 625 → 875 − 625 = 250
        self.assertAlmostEqual(
            ontario_lift_credit(30000, 45000, 66000, 2026), 250.0, places=2)

    def test_floors_at_zero(self):
        self.assertEqual(ontario_lift_credit(30000, 100000, 100000, 2026), 0.0)

    def test_family_defaults_to_individual(self):
        # When family_net_income is None, uses individual for both terms.
        with_default = ontario_lift_credit(30000, 40000, None, 2026)
        explicit = ontario_lift_credit(30000, 40000, 40000, 2026)
        self.assertAlmostEqual(with_default, explicit, places=2)


if __name__ == '__main__':
    unittest.main()