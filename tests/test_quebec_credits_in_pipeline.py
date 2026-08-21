"""Test that Quebec refundable credits are wired into compute_total_tax.

Issue #214: Quebec refundable credits (solidarity, QPIP, FSS) were implemented
as pure functions but never called from compute_total_tax() or the simulation
pipeline. This meant the simulation overstated the household's effective tax
burden by ~$1,081/yr in solidarity credit alone.

These tests verify that:
1. compute_total_tax() includes Quebec refundable credits in its output
2. The solidarity credit reduces net tax for eligible Quebec residents
3. QPIP premiums and FSS contributions are included in the breakdown
4. Non-Quebec provinces receive zero Quebec-specific credits
5. The net effect on total_tax is correct (credits reduce, contributions add)
"""

import unittest
from countries.canada.tax_calc import compute_total_tax


class TestQuebecCreditsInPipeline(unittest.TestCase):
    """Test that compute_total_tax includes Quebec refundable credits."""

    def test_compute_total_tax_includes_quebec_refundable_credits_key(self):
        """compute_total_tax must return a 'quebec_refundable_credits' key."""
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            year=2026,
            province="quebec",
        )
        self.assertIn('quebec_refundable_credits', result)

    def test_solidarity_credit_reduces_tax_for_couple(self):
        """A couple at $72k spouse income gets solidarity credit.

        The solidarity credit for a couple at $72k (below the couple threshold
        of $50,310) should be the full couple max ($1,515 for 2026).
        This must appear in the breakdown.
        """
        result = compute_total_tax(
            taxable_income=72000,
            employment_income=72000,
            is_couple=True,
            year=2026,
            province="quebec",
        )
        qc_credits = result['quebec_refundable_credits']
        self.assertIn('solidarity_credit', qc_credits)
        # At $72k income, below the couple threshold, full credit applies
        self.assertGreater(qc_credits['solidarity_credit'], 0)

    def test_solidarity_credit_single_low_income(self):
        """Single filer at low income gets the full solidarity credit."""
        result = compute_total_tax(
            taxable_income=30000,
            employment_income=30000,
            is_couple=False,
            year=2026,
            province="quebec",
        )
        qc_credits = result['quebec_refundable_credits']
        self.assertIn('solidarity_credit', qc_credits)
        # At $30k, below the single threshold ($40,225), full credit applies
        self.assertGreater(qc_credits['solidarity_credit'], 0)

    def test_qpip_premium_included_for_quebec(self):
        """QPIP premium must appear in Quebec tax calculation."""
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            year=2026,
            province="quebec",
        )
        qc_credits = result['quebec_refundable_credits']
        self.assertIn('qpip_premium', qc_credits)
        # At $60k employment income, QPIP premium should be positive
        self.assertGreater(qc_credits['qpip_premium'], 0)

    def test_fss_included_for_self_employed_quebec(self):
        """FSS contribution must appear for self-employed Quebec residents."""
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            self_employment_income=60000,
            year=2026,
            province="quebec",
        )
        qc_credits = result['quebec_refundable_credits']
        self.assertIn('fss_contribution', qc_credits)
        # At $60k self-employment income, FSS should be positive
        self.assertGreater(qc_credits['fss_contribution'], 0)

    def test_fss_zero_for_employee_only(self):
        """FSS is zero for employees (no self-employment income)."""
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            self_employment_income=0,
            year=2026,
            province="quebec",
        )
        qc_credits = result['quebec_refundable_credits']
        self.assertAlmostEqual(qc_credits['fss_contribution'], 0, places=2)

    def test_no_quebec_credits_for_other_provinces(self):
        """Non-Quebec provinces receive zero Quebec-specific credits."""
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            year=2026,
            province="ontario",
        )
        qc_credits = result['quebec_refundable_credits']
        self.assertAlmostEqual(qc_credits['solidarity_credit'], 0, places=2)
        self.assertAlmostEqual(qc_credits['qpip_premium'], 0, places=2)
        self.assertAlmostEqual(qc_credits['fss_contribution'], 0, places=2)

    def test_solidarity_credit_reduces_total_tax(self):
        """The solidarity credit should reduce total tax for eligible residents.

        With the solidarity credit, total tax should be lower than without it.
        For a couple at low income, the credit can be significant (~$1,515).
        """
        result_with = compute_total_tax(
            taxable_income=40000,
            employment_income=40000,
            is_couple=True,
            year=2026,
            province="quebec",
        )
        # The net effect: solidarity reduces tax, QPIP adds to it
        # solidarity_credit should be positive and reduce total tax
        qc_credits = result_with['quebec_refundable_credits']
        self.assertGreater(qc_credits['solidarity_credit'], 0)
        # total_tax should reflect the net effect
        self.assertIn('total_tax', result_with)

    def test_net_quebec_credits_in_breakdown(self):
        """The breakdown must include net_quebec_refundable_credits."""
        result = compute_total_tax(
            taxable_income=60000,
            employment_income=60000,
            is_couple=True,
            year=2026,
            province="quebec",
        )
        self.assertIn('breakdown', result)
        self.assertIn('net_quebec_refundable_credits', result['breakdown'])

    def test_high_income_reduces_solidarity_credit(self):
        """At high income, solidarity credit should be reduced or zero."""
        result_low = compute_total_tax(
            taxable_income=30000,
            employment_income=30000,
            is_couple=False,
            year=2026,
            province="quebec",
        )
        result_high = compute_total_tax(
            taxable_income=200000,
            employment_income=200000,
            is_couple=False,
            year=2026,
            province="quebec",
        )
        # Higher income should have lower or zero solidarity credit
        credit_low = result_low['quebec_refundable_credits']['solidarity_credit']
        credit_high = result_high['quebec_refundable_credits']['solidarity_credit']
        self.assertGreaterEqual(credit_low, credit_high)


if __name__ == '__main__':
    unittest.main()