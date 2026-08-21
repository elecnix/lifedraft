#!/usr/bin/env python3
"""Tests for issue #61: AMT integration, Quebec abatement separation,
Canada Employment Amount, and Basic Personal Amount optimization.

All test data uses round numbers. No personal information.

Run with: python3 -m pytest tests/test_issue_61.py -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from countries.canada.tax_calc import (
    QC_ABATEMENT,
    basic_personal_amount_credit,
    canada_employment_credit,
    combined_tax_separate,
    compute_non_refundable_credits,
    compute_total_tax,
    federal_tax,
    federal_tax_before_abatement,
    optimize_bpa_transfer,
    quebec_abatement_amount,
    quebec_tax,
)
from tax_data import TaxDataProvider


def _get_fed_rates(year=2026):
    """Helper to get federal lowest_rate and bpa from provider."""
    provider = TaxDataProvider()
    fed_data = provider._load_year(year, 'canada', 'federal')
    lowest_rate = fed_data.federal_brackets[0].rate if fed_data.federal_brackets else 0.15
    return lowest_rate, fed_data.basic_personal_amount


class TestQuebecAbatementSeparate(unittest.TestCase):
    """Test that Quebec abatement is modeled as a separate step, not baked in."""

    def test_federal_tax_before_abatement_exists(self):
        tax = federal_tax_before_abatement(100000, 2026, "quebec")
        self.assertGreater(tax, 0)

    def test_federal_tax_before_abatement_no_abatement(self):
        before = federal_tax_before_abatement(100000, 2026, "quebec")
        after = federal_tax(100000, 2026, "quebec")
        self.assertGreater(before, after)
        self.assertAlmostEqual(before - after, before * QC_ABATEMENT, places=2)

    def test_quebec_abatement_amount(self):
        before = federal_tax_before_abatement(100000, 2026, "quebec")
        abatement = quebec_abatement_amount(100000, 2026, "quebec")
        self.assertAlmostEqual(abatement, before * QC_ABATEMENT, places=2)

    def test_quebec_abatement_zero_for_ontario(self):
        abatement = quebec_abatement_amount(100000, 2026, "ontario")
        self.assertAlmostEqual(abatement, 0)

    def test_combined_tax_separate_equals_combined(self):
        income = 150000
        result = combined_tax_separate(income, 2026, "quebec")
        before = federal_tax_before_abatement(income, 2026, "quebec")
        abatement = quebec_abatement_amount(income, 2026, "quebec")
        qc_tax = quebec_tax(income, 2026)
        expected = before - abatement + qc_tax
        self.assertAlmostEqual(result, expected, places=2)

    def test_combined_tax_separate_positive(self):
        result = combined_tax_separate(100000, 2026, "quebec")
        self.assertGreater(result, 0)

    def test_federal_tax_before_abatement_same_for_all_provinces(self):
        qc = federal_tax_before_abatement(100000, 2026, "quebec")
        on = federal_tax_before_abatement(100000, 2026, "ontario")
        self.assertAlmostEqual(qc, on, places=2)


class TestCanadaEmploymentAmount(unittest.TestCase):
    """Test Canada Employment Amount (line 31260)."""

    def test_canada_employment_credit_2026(self):
        """CEA for 2026 = $1,501 × lowest federal rate (from provider)."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2026, 'canada', 'federal')
        lowest_rate = fed_data.federal_brackets[0].rate
        credit = canada_employment_credit(50000, 2026, provider)
        self.assertAlmostEqual(credit, 1501 * lowest_rate, places=2)

    def test_canada_employment_credit_zero_income(self):
        credit = canada_employment_credit(0, 2026)
        self.assertAlmostEqual(credit, 0)

    def test_canada_employment_credit_capped(self):
        provider = TaxDataProvider()
        fed_data = provider._load_year(2026, 'canada', 'federal')
        lowest_rate = fed_data.federal_brackets[0].rate
        credit = canada_employment_credit(200000, 2026, provider)
        self.assertAlmostEqual(credit, 1501 * lowest_rate, places=2)

    def test_canada_employment_credit_uses_provider_rate(self):
        """Credit uses lowest_rate from provider, not hardcoded 0.15."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2026, 'canada', 'federal')
        lowest_rate = fed_data.federal_brackets[0].rate
        credit = canada_employment_credit(50000, 2026, provider)
        self.assertAlmostEqual(credit, 1501 * lowest_rate, places=2)
        # Verify it's not 0.15 × 1501
        self.assertNotAlmostEqual(credit, 1501 * 0.15, places=2)

    def test_canada_employment_amount_2025_is_1471(self):
        """2025 Canada Employment Amount is $1,471 (CRA T4127 Table 8.2)."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2025, 'canada', 'federal')
        self.assertAlmostEqual(fed_data.canada_employment_amount, 1471, places=0)

    def test_canada_employment_amount_2023_is_1368(self):
        """2023 Canada Employment Amount is $1,368 (CRA T4127 Table 8.2)."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2023, 'canada', 'federal')
        self.assertAlmostEqual(fed_data.canada_employment_amount, 1368, places=0)

    def test_canada_employment_amount_2024_is_1433(self):
        """2024 Canada Employment Amount is $1,433 (CRA T4127 Table 8.2)."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2024, 'canada', 'federal')
        self.assertAlmostEqual(fed_data.canada_employment_amount, 1433, places=0)

    def test_bpa_minimum_2023_is_13520(self):
        """2023 BPA minimum is $13,520 (CRA Federal Worksheet Line 30000)."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2023, 'canada', 'federal')
        self.assertAlmostEqual(fed_data.bpa_minimum, 13520, places=0)

    def test_non_refundable_credits_include_employment(self):
        nr_credits = compute_non_refundable_credits(
            employment_income=80000,
            taxable_income=80000,
            year=2026,
            province="quebec",
        )
        self.assertIn('canada_employment', nr_credits)
        self.assertGreater(nr_credits['canada_employment'], 0)


class TestBasicPersonalAmount(unittest.TestCase):
    """Test Basic Personal Amount credit and spousal optimization."""

    def test_bpa_credit_basic(self):
        lowest_rate, bpa = _get_fed_rates()
        credit = basic_personal_amount_credit(bpa, lowest_rate)
        self.assertAlmostEqual(credit, bpa * lowest_rate, places=2)

    def test_bpa_credit_zero_bpa(self):
        credit = basic_personal_amount_credit(0, 0.15)
        self.assertAlmostEqual(credit, 0)

    def test_bpa_credit_low_income_reduced(self):
        credit = basic_personal_amount_credit(15780, 0.15, income=10000)
        self.assertAlmostEqual(credit, 10000 * 0.15, places=2)

    def test_optimize_bpa_transfer(self):
        result = optimize_bpa_transfer(150000, 5000, 15780, 0.15)
        transferable = (15780 - 5000) * 0.15
        self.assertAlmostEqual(result['spouse_transferable'], transferable, places=2)
        self.assertGreater(result['total_credits'], 0)

    def test_optimize_bpa_no_transfer_when_spouse_high_income(self):
        result = optimize_bpa_transfer(150000, 50000, 15780, 0.15)
        self.assertAlmostEqual(result['spouse_transferable'], 0, places=2)

    def test_optimize_bpa_both_high_income(self):
        result = optimize_bpa_transfer(100000, 80000, 15780, 0.15)
        expected_primary = 15780 * 0.15
        expected_spouse = 15780 * 0.15
        self.assertAlmostEqual(result['primary_credit'], expected_primary, places=2)
        self.assertAlmostEqual(result['spouse_credit'], expected_spouse, places=2)
        self.assertAlmostEqual(result['spouse_transferable'], 0, places=2)

    def test_optimize_bpa_both_low_income(self):
        """Both spouses below BPA — transfer capped by primary's tax room.

        With primary=5000, spouse=3000, BPA=15780, rate=0.15:
        - primary_credit = 5000 × 0.15 = 750
        - spouse_direct_credit = 3000 × 0.15 = 450
        - spouse_unused = 15780 - 3000 = 12780
        - raw_spouse_transferable = 12780 × 0.15 = 1917
        - primary_tax_room ≈ max(0, 5000×0.15 - 750) = 0 (fallback)
        - spouse_transferable = min(1917, 0) = 0
        """
        result = optimize_bpa_transfer(5000, 3000, 15780, 0.15)
        self.assertAlmostEqual(result['spouse_transferable'], 0, places=2)
        self.assertAlmostEqual(result['primary_credit'], 5000 * 0.15, places=2)
        self.assertAlmostEqual(result['spouse_credit'], 3000 * 0.15, places=2)
        self.assertIn('total_credits', result)
        self.assertNotIn('total_savings', result)

    def test_optimize_bpa_with_primary_federal_tax(self):
        """C2: Passing actual federal tax caps transfer correctly.

        For QC resident with primary_income=30000, spouse=0:
        - Federal tax on $30k ≈ 4200 (14% bracket, minus 16.5% abatement)
        - primary_credit = 15780 × 0.14 = 2209.20
        - primary_tax_room = max(0, 4200 - 2209.20) = 1990.80
        - spouse_unused = 15780, raw_transferable = 15780 × 0.14 = 2209.20
        - spouse_transferable = min(2209.20, 1990.80) = 1990.80
        """
        primary_fed_tax = federal_tax(30000, 2026, "quebec")
        result = optimize_bpa_transfer(
            30000, 0, 15780, 0.14,
            primary_federal_tax=primary_fed_tax,
        )
        # spouse_transferable should be capped by federal tax room
        self.assertGreater(result['spouse_transferable'], 0)
        self.assertLessEqual(result['spouse_transferable'], primary_fed_tax)

    def test_optimize_bpa_moderate_income_qc(self):
        """Moderate-income QC: spouse_transferable doesn't exceed fed tax room.

        primary=30000, spouse=0, BPA=15780, using actual federal tax.
        """
        provider = TaxDataProvider()
        fed_data = provider._load_year(2026, 'canada', 'federal')
        lowest_rate = fed_data.federal_brackets[0].rate
        bpa = fed_data.basic_personal_amount

        primary_fed_tax = federal_tax(30000, 2026, "quebec")
        result = optimize_bpa_transfer(
            30000, 0, bpa, lowest_rate,
            primary_federal_tax=primary_fed_tax,
        )
        primary_credit = basic_personal_amount_credit(bpa, lowest_rate, 30000)
        expected_room = max(0, primary_fed_tax - primary_credit)
        self.assertAlmostEqual(result['spouse_transferable'], expected_room, places=2)

    def test_optimize_bpa_transfer_with_phaseout(self):
        """Primary in phaseout range gets reduced BPA; spouse transferable adjusts.

        primary=220000, spouse=0, BPA=16452, rate=0.14,
        threshold=181440, end=258482, min=14829.
        """
        result = optimize_bpa_transfer(
            220000, 0, 16452, 0.14,
            bpa_phaseout_threshold=181440,
            bpa_phaseout_end=258482,
            bpa_minimum=14829,
        )
        # Primary's own BPA credit (before transfer) should reflect partial phaseout
        primary_own_bpa = basic_personal_amount_credit(
            16452, 0.14, 220000,
            bpa_phaseout_threshold=181440,
            bpa_phaseout_end=258482,
            bpa_minimum=14829,
        )
        # result['primary_credit'] = primary_own_bpa + spouse_transferable
        self.assertAlmostEqual(
            result['primary_credit'],
            primary_own_bpa + result['spouse_transferable'],
            places=2,
        )
        # Primary own BPA credit should be less than full BPA
        self.assertLess(primary_own_bpa, 16452 * 0.14)
        # Primary own BPA credit should be above minimum BPA
        self.assertGreater(primary_own_bpa, 14829 * 0.14)

    def test_non_refundable_credits_include_bpa(self):
        nr_credits = compute_non_refundable_credits(
            employment_income=80000,
            taxable_income=80000,
            year=2026,
            province="quebec",
        )
        self.assertIn('basic_personal_amount', nr_credits)
        self.assertGreater(nr_credits['basic_personal_amount'], 0)

    def test_bpa_reduced_for_low_taxable_income(self):
        nr_credits = compute_non_refundable_credits(
            employment_income=5000,
            taxable_income=5000,
            year=2026,
            province="quebec",
        )
        lowest_rate, bpa = _get_fed_rates()
        self.assertAlmostEqual(
            nr_credits['basic_personal_amount'],
            5000 * lowest_rate,
            places=2,
        )
        self.assertLess(nr_credits['basic_personal_amount'], bpa * lowest_rate)


class TestAMTIntegrationInTaxCalc(unittest.TestCase):
    """Test that AMT is properly integrated into the tax calculation flow."""

    def test_amt_importable_from_tax_calc(self):
        self.assertTrue(callable(compute_total_tax))

    def test_compute_total_tax_includes_amt(self):
        result = compute_total_tax(
            taxable_income=200000,
            employment_income=200000,
            year=2026,
            province="quebec",
        )
        self.assertIn('regular_tax', result)
        self.assertIn('amt_surcharge', result)
        self.assertIn('total_tax', result)
        self.assertIn('non_refundable_credits', result)

    def test_compute_total_tax_no_amt_for_low_income(self):
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            year=2026,
            province="quebec",
        )
        self.assertAlmostEqual(result['amt_surcharge'], 0, places=2)

    def test_compute_total_tax_includes_credits(self):
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            year=2026,
            province="quebec",
        )
        self.assertGreater(result['non_refundable_credits']['total'], 0)

    def test_amt_uses_federal_only_regular_tax(self):
        """AMT comparison uses federal-only tax, not combined."""
        result = compute_total_tax(
            taxable_income=300000,
            employment_income=300000,
            year=2026,
            province="quebec",
        )
        self.assertIn('federal_after_credits', result['breakdown'])
        self.assertIn('combined_after_credits', result['breakdown'])
        self.assertLess(
            result['breakdown']['federal_after_credits'],
            result['breakdown']['combined_after_credits'],
        )

    def test_a_large_rrsp_deduction_triggers_NO_amt(self):
        """Was `test_amt_triggered_by_large_deduction`, asserting the opposite.

        An RRSP deduction is not an AMT preference item — it appears nowhere in
        ITA s.127.52(1)'s closed list of add-backs, so it reduces regular taxable
        income and adjusted taxable income alike. A taxpayer who earned $250k and
        deducted $200k has an AMT base of $50k: nowhere near the exemption.

        The old expectation only passed because `amt_adjusted_income` invented an
        RRSP add-back (#710/#754). Wired into the engine, that fabrication cut a
        real household's projected refund by 21% for a tax that does not exist.
        """
        result = compute_total_tax(
            taxable_income=50000,
            employment_income=250000,
            year=2026,
            province="quebec",
        )
        self.assertAlmostEqual(result['amt_surcharge'], 0, places=2)


class TestFederalCreditsDontReduceProvincialTax(unittest.TestCase):
    """C1: Federal non-refundable credits reduce federal tax only."""

    def test_low_income_credits_dont_reduce_provincial(self):
        """For taxable_income=5000, combined_after_credits >= provincial_tax.

        Federal credits (BPA + CEA) will exceed federal tax at low income,
        but should not reduce provincial tax.
        """
        result = compute_total_tax(
            taxable_income=5000,
            employment_income=5000,
            year=2026,
            province="quebec",
        )
        prov_tax = result['breakdown']['provincial_tax']
        combined = result['breakdown']['combined_after_credits']
        net_qc = result['breakdown']['net_quebec_refundable_credits']
        # Provincial tax is unaffected by federal credits;
        # combined includes the net Quebec refundable credit effect
        self.assertAlmostEqual(
            combined, prov_tax + result['breakdown']['federal_after_credits'] - net_qc,
            places=2)
        # Federal credits should not reduce provincial tax
        self.assertAlmostEqual(
            result['breakdown']['provincial_tax'], prov_tax, places=2)

    def test_credits_dont_exceed_federal_tax_at_bpa_income(self):
        """At income = BPA ($15,780 for 2026), verify credit arithmetic.

        Federal tax on $15,780 at 14% bracket, minus 16.5% QC abatement.
        Credits (BPA + CEA) should not reduce provincial tax.
        Quebec refundable credits (solidarity) further reduce combined tax.
        """
        result = compute_total_tax(
            taxable_income=15780,
            employment_income=15780,
            year=2026,
            province="quebec",
        )
        # combined_after_credits = fed_after_credits + prov_tax - net_quebec_credits
        fed_after = result['breakdown']['federal_after_credits']
        prov_tax = result['breakdown']['provincial_tax']
        net_qc = result['breakdown']['net_quebec_refundable_credits']
        combined = result['breakdown']['combined_after_credits']
        expected = fed_after + prov_tax - net_qc
        self.assertAlmostEqual(combined, expected, places=2)
        # Provincial tax should equal the gross provincial tax
        gross_prov = quebec_tax(15780, 2026)
        self.assertAlmostEqual(prov_tax, gross_prov, places=2)


class TestNonQuebecProvince(unittest.TestCase):
    """Test tax calculation paths for non-Quebec provinces."""

    def test_combined_tax_separate_ontario(self):
        result = combined_tax_separate(100000, 2026, "ontario")
        self.assertGreater(result, 0)

    def test_compute_total_tax_ontario(self):
        result = compute_total_tax(
            taxable_income=80000,
            employment_income=80000,
            year=2026,
            province="ontario",
        )
        self.assertGreater(result['total_tax'], 0)
        self.assertIn('quebec_abatement', result['breakdown'])
        self.assertAlmostEqual(result['breakdown']['quebec_abatement'], 0)

    def test_ontario_combined_equals_fed_plus_provincial(self):
        fed_before = federal_tax_before_abatement(100000, 2026, "ontario")
        combined = combined_tax_separate(100000, 2026, "ontario")
        self.assertGreater(combined, fed_before)


class TestRateConsistency(unittest.TestCase):
    """Test that CEA and BPA credits use the same rate from provider."""

    def test_cea_and_bpa_use_same_rate(self):
        """Both credits should use the same lowest federal bracket rate."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2026, 'canada', 'federal')
        lowest_rate = fed_data.federal_brackets[0].rate

        nr_credits = compute_non_refundable_credits(
            employment_income=50000,
            taxable_income=50000,
            year=2026,
            province="quebec",
            provider=provider,
        )
        cea_expected = 1501 * lowest_rate
        bpa_expected = fed_data.basic_personal_amount * lowest_rate

        self.assertAlmostEqual(nr_credits['canada_employment'], cea_expected, places=2)
        self.assertAlmostEqual(nr_credits['basic_personal_amount'], bpa_expected, places=2)

    def test_2026_lowest_rate_from_provider(self):
        """Verify 2026 lowest federal rate from provider (0.14, not 0.15)."""
        provider = TaxDataProvider()
        fed_data = provider._load_year(2026, 'canada', 'federal')
        lowest_rate = fed_data.federal_brackets[0].rate
        self.assertAlmostEqual(lowest_rate, 0.14, places=2)


class TestAMTWhenFederalCreditsZeroOut(unittest.TestCase):
    """N3/N7 (DP#17): AMT applies when credits zero out federal tax."""

    def test_amt_with_capital_gains_low_employment(self):
        """High capital gains, low employment → credits zero out fed tax, AMT kicks in.

        Scenario: taxable_income=10000, of which 10000 is the taxable half of a
        $20,000 capital gain. AMTI = 10000 + 10000 = 20000 (100% inclusion).
        Federal tax on $10k is near zero after BPA+CEA credits, and AMTI is far
        below the $181,440 exemption, so no AMT. This exercises the path where
        credits zero out federal tax.
        """
        result = compute_total_tax(
            taxable_income=10000,
            employment_income=10000,
            taxable_capital_gains=10000,
            capital_gains_inclusion=0.50,
            year=2026,
            province="quebec",
        )
        # Federal credits (BPA+CEA) should zero out federal tax
        self.assertAlmostEqual(result['breakdown']['federal_after_credits'], 0, places=2)
        # AMTI is only $35k, well below exemption, so no AMT surcharge
        self.assertAlmostEqual(result['amt_surcharge'], 0, places=2)
        # Total tax should be provincial tax minus net Quebec refundable credits
        # (at low income, solidarity credit exceeds QPIP)
        net_qc = result['breakdown']['net_quebec_refundable_credits']
        expected_total = result['breakdown']['provincial_tax'] - net_qc
        self.assertAlmostEqual(
            result['total_tax'],
            max(0, expected_total),
            places=2,
        )

    def test_amt_triggers_on_a_large_capital_gain_despite_credits(self):
        """Was `test_amt_triggers_despite_credits`, which built its AMTI out of a
        $180k RRSP add-back that the statute does not provide. Rebuilt on the
        add-back that IS real: 100% capital-gains inclusion, s.127.52(1)(d).

        A taxpayer with a $1,000,000 gain and no other income has $500,000 of
        taxable income (regular 50% inclusion) but $1,000,000 of AMTI. Regular
        federal tax on $500k is well below 20.5% of (1,000,000 - 181,440), so
        the minimum tax binds — and credits cannot rescue them, which is the
        point this test was always trying to make.
        """
        result = compute_total_tax(
            taxable_income=500000,
            employment_income=0,
            taxable_capital_gains=500000,   # = $1M gain x 50% regular inclusion
            capital_gains_inclusion=0.50,
            year=2026,
            province="quebec",
        )
        self.assertGreater(result['amt_surcharge'], 0)
        self.assertGreater(result['total_tax'], result['breakdown']['provincial_tax'])


class TestCapitalGainsInComputeTotalTax(unittest.TestCase):
    """N4: capital gains are 100% included in AMTI (ITA s.127.52(1)(d)) — the one
    add-back big enough to make AMT bite.

    Was `TestCapitalGainsDeductionInComputeTotalTax`, whose fixture carried a
    $200k RRSP deduction it believed was driving the AMT. It was not.
    """

    def test_a_bigger_gain_means_a_bigger_amt(self):
        """Monotonicity: more gain, more minimum tax. Same taxable income in both
        arms, so the ONLY thing moving is the AMT add-back."""
        small = compute_total_tax(
            taxable_income=500000,
            employment_income=500000,
            taxable_capital_gains=0,
            year=2026,
            province="quebec",
        )
        large = compute_total_tax(
            taxable_income=500000,
            employment_income=0,
            taxable_capital_gains=500000,   # the whole $500k is a taxable gain
            capital_gains_inclusion=0.50,
            year=2026,
            province="quebec",
        )
        self.assertAlmostEqual(small['amt_surcharge'], 0, places=2)
        self.assertGreater(large['amt_surcharge'], 0)


class TestBPAPhaseout(unittest.TestCase):
    """Test BPA high-income phaseout (ITA s.118(1.1))."""

    def test_bpa_credit_no_phaseout_with_default_income(self):
        """Default income (None) should give full BPA, not minimum."""
        credit = basic_personal_amount_credit(16452, 0.14,
            bpa_phaseout_threshold=181440, bpa_phaseout_end=258482, bpa_minimum=14829)
        self.assertAlmostEqual(credit, 16452 * 0.14, places=2)

    def test_bpa_no_phaseout_below_threshold(self):
        """Below threshold, full BPA applies."""
        credit = basic_personal_amount_credit(16452, 0.14, income=100000,
                                               bpa_phaseout_threshold=181440,
                                               bpa_phaseout_end=258482,
                                               bpa_minimum=14829)
        self.assertAlmostEqual(credit, 16452 * 0.14, places=2)

    def test_bpa_full_phaseout_above_end(self):
        """Above phaseout end, BPA = minimum."""
        credit = basic_personal_amount_credit(16452, 0.14, income=300000,
                                               bpa_phaseout_threshold=181440,
                                               bpa_phaseout_end=258482,
                                               bpa_minimum=14829)
        self.assertAlmostEqual(credit, 14829 * 0.14, places=2)

    def test_bpa_partial_phaseout(self):
        """In phaseout range, BPA reduces linearly (CRA folio formula).

        For 2026: max=$16,452, min=$14,829, threshold=$181,440, end=$258,482
        At income=$220,000:
          enhanced = 16452 - 14829 = 1623
          fraction = (220000 - 181440) / (258482 - 181440) = 38560 / 77042 ≈ 0.5006
          reduction = 1623 × 0.5006 ≈ 812.5
          effective_bpa = 16452 - 812.5 ≈ 15639.5
          credit ≈ 15639.5 × 0.14 ≈ 2189.53
        """
        credit = basic_personal_amount_credit(16452, 0.14, income=220000,
                                               bpa_phaseout_threshold=181440,
                                               bpa_phaseout_end=258482,
                                               bpa_minimum=14829)
        enhanced = 16452 - 14829
        fraction = (220000 - 181440) / (258482 - 181440)
        effective_bpa = 16452 - enhanced * fraction
        expected = effective_bpa * 0.14
        self.assertAlmostEqual(credit, expected, places=2)
        # Should be less than full BPA credit but more than minimum
        self.assertLess(credit, 16452 * 0.14)
        self.assertGreater(credit, 14829 * 0.14)

    def test_bpa_phaseout_via_compute_non_refundable_credits(self):
        """High-income taxpayer gets reduced BPA through compute_total_tax."""
        result = compute_total_tax(
            taxable_income=300000,
            employment_income=300000,
            year=2026,
            province="quebec",
        )
        nr = result['non_refundable_credits']
        # At $300k (above phaseout end), BPA should be at minimum
        provider = TaxDataProvider()
        fed_data = provider._load_year(2026, 'canada', 'federal')
        expected_bpa_credit = fed_data.bpa_minimum * fed_data.federal_brackets[0].rate
        self.assertAlmostEqual(
            nr['basic_personal_amount'], expected_bpa_credit, places=2,
        )
        # BPA credit should be less than full BPA × rate
        full_bpa_credit = fed_data.basic_personal_amount * fed_data.federal_brackets[0].rate
        self.assertLess(nr['basic_personal_amount'], full_bpa_credit)

    def test_bpa_no_phaseout_when_threshold_zero(self):
        """When threshold=0, no phaseout (backward compatible)."""
        credit = basic_personal_amount_credit(16452, 0.14, income=300000,
                                               bpa_phaseout_threshold=0,
                                               bpa_phaseout_end=0,
                                               bpa_minimum=0)
        self.assertAlmostEqual(credit, 16452 * 0.14, places=2)

    def test_cra_2024_example(self):
        """CRA folio example: $225,000 income in 2024.

        min_bpa = $14,156, enhanced = $1,549, threshold = $173,205, end = $246,752
        fraction = (225000 - 173205) / (246752 - 173205) = 51795 / 73547 ≈ 0.7042
        reduction = 1549 × 0.7042 ≈ 1091
        effective_bpa = 15705 - 1091 = 14614
        credit = 14614 × 0.15 = 2192.10
        """
        credit = basic_personal_amount_credit(15705, 0.15, income=225000,
                                               bpa_phaseout_threshold=173205,
                                               bpa_phaseout_end=246752,
                                               bpa_minimum=14156)
        self.assertAlmostEqual(credit, 2192.10, places=0)


if __name__ == '__main__':
    unittest.main()
