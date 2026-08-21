#!/usr/bin/env python3
"""Tests for Alternative Minimum Tax (AMT) calculation.

All test data uses round numbers. No personal information.

CRA Reference: https://www.canada.ca/en/revenue-agency/services/tax/individuals/topics/alternative-minimum-tax.html

AMT ensures taxpayers pay a minimum amount of tax, especially those
with large deductions (RRSP, capital gains, etc.). The 2024 federal
budget increased the AMT exemption and rate significantly.

Key rules:
- Compute "adjusted taxable income" by adding back certain deductions
  and preferential-item deductions (capital gains inclusion, RRSP deductions, etc.)
- Subtract the AMT exemption (with phaseout for 2024+)
- Apply the AMT rate (15% federal for 2024+)
- Compare with regular tax; pay the higher amount
- Excess AMT paid can be carried forward 7 years as a credit

Run with: python3 -m pytest countries/canada/tests/test_amt.py -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest
from dataclasses import replace

from countries.canada.amt import (
    AMTParameters,
    compute_amt,
    amt_adjusted_income,
    total_tax_with_amt,
    stock_option_amt_addback,
    AMTCredit,
    carry_forward_amt_credit,
    QuebecIMRParameters,
    compute_quebec_imr,
)


class TestStockOptionAMTAddback(unittest.TestCase):
    """Employee stock option AMT add-back (ITA s.127.52 / s.110(1)(d), issue #314)."""

    def test_addback_is_the_reversed_deduction(self):
        """Full benefit is included for AMT: add back the 50% regular deduction."""
        # $100k benefit, 50% deducted for regular tax -> $50k added back for AMT.
        self.assertAlmostEqual(stock_option_amt_addback(100000), 50000)

    def test_zero_benefit_no_addback(self):
        self.assertAlmostEqual(stock_option_amt_addback(0), 0.0)

    def test_addback_feeds_amti(self):
        """The add-back flows through amt_adjusted_income as a preference item."""
        addback = stock_option_amt_addback(80000)  # 40000
        amti = amt_adjusted_income(taxable_income=200000,
                                   stock_option_deduction=addback)
        self.assertAlmostEqual(amti, 240000)


class TestLegislatedAMTRate(unittest.TestCase):
    """2024 verification: legislated AMT rate opt-in (issue #314)."""

    def test_default_rate_is_20_5pct_for_2024_plus(self):
        """Default for_year uses enacted 20.5% rate for 2024+ (issue #360)."""
        self.assertAlmostEqual(AMTParameters.for_year(2024).amt_rate, 0.205)

    def test_pre_2024_rate_stays_15pct(self):
        """Pre-2024 years keep the historical 15% rate."""
        self.assertAlmostEqual(AMTParameters.for_year(2023).amt_rate, 0.15)

    # `with_legislated_rate()` is gone: it existed only as a backward-compat
    # shim ("retained for backward compatibility", per its own docstring) that
    # re-applied the rate `for_year` already returns. DP#9 — no shims. Its two
    # tests asserted the shim was a no-op, which it was; there is nothing left
    # to assert. The rate itself stays pinned by the two tests above.


class TestAMTParameters(unittest.TestCase):
    """Test AMTParameters dataclass — year-versioned data (DP#2, DP#20)."""

    def test_exemption_is_derived_from_the_29pct_bracket_not_hardcoded(self):
        """ITA s.127.51: the 2024+ AMT basic exemption IS the lower limit of the
        29% federal bracket. It is derived from the bracket table the provider
        already holds (DP#2/DP#12/DP#20), not restated as an AMT constant.

        Previously `for_year` carried a hand-written table (173000/177000/180000,
        the last two commented "Approximate indexation") that could — and did —
        drift from the bracket data sitting next to it.
        """
        from tax_data import TaxDataProvider
        provider = TaxDataProvider()
        for year in (2025, 2026):
            brackets = provider._load_year(year, "canada", "federal").federal_brackets
            bracket_29 = next(b for b in brackets if abs(b.rate - 0.29) < 1e-9)
            self.assertEqual(
                AMTParameters.for_year(year, provider).exemption,
                bracket_29.min_income,
                f"{year}: AMT exemption must track the 29% bracket threshold",
            )

    def test_exemption_matches_published_cra_figures(self):
        """The derivation must land on the CRA's actual published exemptions.

        2024: $173,205. 2025: $177,882. 2026: $181,440 — which is also the
        provider's own `bpa_phaseout_threshold`, an independent second copy of
        the same statutory threshold, so the two agree.

        Before issue #748 the repo's 2024 (and 2023) federal bracket tables were
        stale, so this test deliberately did not assert 2024: the derivation
        correctly yielded a wrong number from wrong data. With #748 fixing the
        29% bracket lower bound to $173,205, the derivation now lands on the CRA
        figure and the assertion is restored.
        """
        self.assertEqual(AMTParameters.for_year(2024).exemption, 173205)
        self.assertEqual(AMTParameters.for_year(2025).exemption, 177882)
        self.assertEqual(AMTParameters.for_year(2026).exemption, 181440)

    def test_2024_parameters(self):
        """2024 budget changes: much higher exemption, 20.5% rate, no phase-out."""
        params = AMTParameters.for_year(2024)
        self.assertEqual(params.year, 2024)
        self.assertAlmostEqual(params.amt_rate, 0.205)
        # The Canadian AMT basic exemption is a flat subtraction — it is NOT
        # phased out as income rises (CRA T691; the minimum amount is simply
        # `20.5% x (ATI - exemption)`). The 25%/dollar phase-out this field used
        # to carry was the *US* AMT's design, and it got here by confusion with
        # the federal BPA phase-out (ITA s.118(1.1)), which runs over exactly the
        # same $173,205–$246,752 range because both are pegged to the 4th- and
        # top-bracket thresholds. Different rules.
        self.assertAlmostEqual(params.exemption_phaseout_rate, 0.0)

    def test_missing_bracket_data_raises_rather_than_inventing_an_exemption(self):
        """DP#32: an absent 29% bracket must fail loudly. An AMT exemption that
        silently defaulted would switch AMT off for exactly the high-income
        taxpayers the rule exists to catch."""
        class _NoBrackets:
            def _load_year(self, *a, **k):
                return type("D", (), {"federal_brackets": []})()

        with self.assertRaises(ValueError):
            AMTParameters.for_year(2026, _NoBrackets())

    def test_2023_parameters_pre_budget(self):
        """Pre-2024: lower exemption, 15% rate, no phaseout."""
        params = AMTParameters.for_year(2023)
        self.assertEqual(params.year, 2023)
        self.assertAlmostEqual(params.amt_rate, 0.15)
        self.assertEqual(params.exemption, 40000)
        self.assertEqual(params.exemption_phaseout_rate, 0.0)

    def test_2024_vs_2023_different_exemption(self):
        """2024 exemption is much higher than 2023."""
        params_2023 = AMTParameters.for_year(2023)
        params_2024 = AMTParameters.for_year(2024)
        self.assertGreater(params_2024.exemption, params_2023.exemption)

    def test_parameters_are_data(self):
        """DP#8: AMT parameters are a dataclass, not hidden in code."""
        params = AMTParameters.for_year(2024)
        # Can create modified copy (compose through data)
        modified = replace(params, exemption=200000)
        self.assertEqual(modified.exemption, 200000)
        self.assertEqual(modified.amt_rate, 0.205)

    def test_2026_parameters(self):
        """2026 parameters should be available (indexed from 2024)."""
        params = AMTParameters.for_year(2026)
        self.assertGreater(params.exemption, 0)
        self.assertGreater(params.amt_rate, 0)
        # 2026 exemption should be >= 2024 (indexed)
        params_2024 = AMTParameters.for_year(2024)
        self.assertGreaterEqual(params.exemption, params_2024.exemption)

    def test_future_year_projected(self):
        """Years beyond known data are projected with indexation."""
        params = AMTParameters.for_year(2030)
        self.assertEqual(params.year, 2030)
        self.assertGreater(params.exemption, 173000)

    def test_frozen_dataclass(self):
        """DP#8: Parameters are immutable — compose via replace()."""
        params = AMTParameters.for_year(2024)
        with self.assertRaises(AttributeError):
            params.exemption = 50000


class TestAMTAdjustedIncome(unittest.TestCase):
    """amt_adjusted_income — ITA s.127.52(1)'s CLOSED list of add-backs.

    Several tests in this class used to assert that the RRSP deduction was added
    back. It is not. s.127.52(1) enumerates its preference items exhaustively and
    the RRSP deduction (s.60(i)) is not among them, so it reduces regular taxable
    income and AMTI alike. See tests/test_issue_710_amt_base.py for the statutory
    quotation and the story of how the fabrication nearly shipped.
    """

    def test_no_adjustments_needed(self):
        """No preference items: AMTI = taxable income."""
        self.assertAlmostEqual(amt_adjusted_income(taxable_income=100000), 100000)

    def test_rrsp_deduction_is_not_an_addback_and_cannot_be_passed(self):
        """Was `test_rrsp_deduction_added_back`, asserting AMTI = 100k + 30k.

        The parameter is deleted outright rather than defaulted to 0 (DP#9), so
        the fabrication cannot creep back in via a keyword argument.
        """
        with self.assertRaises(TypeError):
            amt_adjusted_income(taxable_income=100000, rrsp_deduction=30000)

    def test_capital_gains_full_inclusion(self):
        """s.127.52(1)(d): the ss.38(a)/(b) fraction is read as '1/1'.

        A $50,000 gain puts $25,000 into taxable income at the regular 50%
        inclusion; AMT includes the whole $50,000, so $25,000 is added back.
        """
        result = amt_adjusted_income(
            taxable_income=100000,
            taxable_capital_gains=25000,   # = 50k gain x 50%
            capital_gains_inclusion=0.50,
        )
        self.assertAlmostEqual(result, 125000)

    def test_carrying_charges_half_added_back(self):
        """s.127.52(1)(j)(ii): 1/2 of s.20(1)(c)-(f)/(bb) interest — the
        leveraged-investing / Smith Manoeuvre case."""
        result = amt_adjusted_income(taxable_income=150000, carrying_charges=40000)
        self.assertAlmostEqual(result, 170000)

    def test_employment_deductions_half_added_back(self):
        """s.127.52(1)(j)(i): 1/2 of the listed s.8(1) deductions."""
        result = amt_adjusted_income(taxable_income=150000, employment_deductions=20000)
        self.assertAlmostEqual(result, 160000)

    def test_stock_option_deduction_added_back_in_full(self):
        """s.127.52(1)(h): the s.110(1)(d) deduction is DENIED — 100% of the
        benefit is included, not half of it."""
        result = amt_adjusted_income(taxable_income=100000, stock_option_deduction=40000)
        self.assertAlmostEqual(result, 140000)

    def test_non_capital_losses_half_added_back(self):
        """s.127.52(1)(i): only 1/2 of the s.111(1) carryover is deductible.

        Was `test_loss_carried_over_reduces_amti`, which SUBTRACTED the loss from
        AMTI — the wrong sign. The loss already reduced taxable income; AMT gives
        back only half of that relief, so half is ADDED to AMTI.
        """
        result = amt_adjusted_income(
            taxable_income=100000, non_capital_loss_deducted=30000)
        self.assertAlmostEqual(result, 115000)

    def test_gains_and_carrying_charges_compose(self):
        """The real leveraged-investor shape: borrow, deduct the interest under
        s.20(1)(c), and realize a gain."""
        result = amt_adjusted_income(
            taxable_income=200000,
            taxable_capital_gains=50000,     # = 100k gain x 50%
            capital_gains_inclusion=0.50,
            carrying_charges=40000,          # half added back
        )
        # 200000 + 50000 (other half of the gain) + 20000 (half the interest)
        self.assertAlmostEqual(result, 270000)

    def test_amti_never_below_zero(self):
        self.assertGreaterEqual(amt_adjusted_income(taxable_income=0), 0)

    def test_zero_inclusion_rate_raises_rather_than_erasing_the_gain(self):
        """DP#32: a nonsense inclusion rate must not silently mean 'no gain' —
        that would switch off the biggest AMT trigger there is."""
        with self.assertRaises(ValueError):
            amt_adjusted_income(
                taxable_income=100000, taxable_capital_gains=50000,
                capital_gains_inclusion=0)


class TestComputeAMT(unittest.TestCase):
    """Test compute_amt — the core AMT calculation pure function (DP#3)."""

    def test_no_amt_when_below_exemption(self):
        """Income below AMT exemption: no AMT applies."""
        params = AMTParameters.for_year(2024)
        result = compute_amt(
            regular_tax=20000,
            adjusted_income=100000,
            params=params,
        )
        # AMTI 100000 < exemption 173000 → amt_base = 0 → no AMT
        self.assertAlmostEqual(result['amt_owing'], 0)
        self.assertAlmostEqual(result['amt_surcharge'], 0)

    def test_amt_above_exemption_no_phaseout(self):
        """AMT with income above exemption but below phaseout effect.

        For 2024, the phaseout starts at $173k AMTI. At $250k AMTI,
        the phaseout reduces the exemption somewhat but doesn't
        eliminate it. We verify the phaseout arithmetic.
        """
        # A hand-built parameter set, so this tests compute_amt's ARITHMETIC and
        # not whichever exemption the bracket table happens to yield this year.
        params = AMTParameters(
            year=2024, exemption=173205, amt_rate=0.205,
            exemption_phaseout_start=173205, exemption_phaseout_rate=0.0,
        )
        result = compute_amt(
            regular_tax=5000,
            adjusted_income=250000,
            params=params,
        )
        # base = 250000 - 173205 = 76795;  AMT = 76795 * 0.205 = 15742.975
        # regular 5000 < 15742.975 -> AMT applies
        self.assertAlmostEqual(result['amt_owing'], 15742.975)
        self.assertAlmostEqual(result['amt_surcharge'], 10742.975)

    def test_exemption_is_never_phased_out_however_high_amti_goes(self):
        """The Canadian AMT basic exemption is a FLAT subtraction.

        This test previously asserted the opposite — that at $300k AMTI the
        exemption had been clawed back 25c/$ to $141,250, giving a $27,543.75
        surcharge. That phase-out does not exist in Canadian law (CRA T691: the
        minimum amount is `20.5% x (ATI - basic exemption)`, full stop; CFFP,
        U. de Sherbrooke, states the exemption "n'est pas reduite" with income).
        It is the *US* AMT's design, and it reached this module by confusion with
        the federal BPA phase-out, which spans the identical income range.

        Keeping the old expectation would have meant the AMT wired into the fold
        by #710 over-taxed every high-AMTI household. The number below is the
        law; the number above it was arithmetic against a rule that isn't real.
        """
        params = AMTParameters.for_year(2024)
        for amti in (300_000, 900_000, 5_000_000):
            result = compute_amt(regular_tax=5000, adjusted_income=amti, params=params)
            self.assertAlmostEqual(
                result['effective_exemption'], params.exemption,
                msg=f"exemption must not shrink at AMTI={amti:,}",
            )
            self.assertAlmostEqual(
                result['amt_base'], amti - params.exemption,
                msg=f"AMT base is a flat subtraction at AMTI={amti:,}",
            )

    def test_amt_below_regular_tax_no_surcharge(self):
        """When AMT ≤ regular tax, no AMT surcharge."""
        params = AMTParameters.for_year(2024)
        result = compute_amt(
            regular_tax=30000,
            adjusted_income=250000,
            params=params,
        )
        # Phaseout = (250000 - 173000) * 0.25 = 19250
        # Effective exemption = 173000 - 19250 = 153750
        # AMT base = 250000 - 153750 = 96250
        # AMT tentative = 96250 * 0.205 = 19731.25
        # Regular tax = 30000 > 19731.25 → no AMT
        self.assertAlmostEqual(result['amt_owing'], 0)
        self.assertAlmostEqual(result['amt_surcharge'], 0)

    def test_amt_carryforward(self):
        """Excess AMT paid is reported as carryforward-eligible for 7 years.

        Expectation moved because the phase-out it was computed through does not
        exist (see test_exemption_is_never_phased_out_however_high_amti_goes).
        Recomputed against the real rule, with an explicit exemption:
            base      = 300000 - 173205 = 126795
            minimum   = 126795 * 0.205  = 25992.975
            surcharge = 25992.975 - 10000 = 15992.975
        (was 22543.75, which came from a clawed-back $141,250 exemption.)

        NOTE this only *reports* the carryforward: nothing consumes it in a later
        year, so AMT paid is never actually recovered (issue #747).
        """
        params = AMTParameters(
            year=2024, exemption=173205, amt_rate=0.205,
            exemption_phaseout_start=173205, exemption_phaseout_rate=0.0,
        )
        result = compute_amt(
            regular_tax=10000,
            adjusted_income=300000,
            params=params,
        )
        self.assertAlmostEqual(result['amt_carryforward'], 15992.975)
        self.assertEqual(result['carryforward_years'], 7)

    def test_a_jurisdiction_with_a_phaseout_can_still_express_one(self):
        """Canada has no AMT exemption phase-out, but the phase-out FIELDS are
        kept so a jurisdiction that does have one (e.g. the US AMT, #438) can
        express it as data rather than by forking `compute_amt` (DP#8).

        This replaces `test_exemption_phaseout_complete`, which asserted Canada
        phased its exemption out completely above ~$865k AMTI. It does not.
        """
        params = AMTParameters(
            year=2024, exemption=173205, amt_rate=0.205,
            exemption_phaseout_start=173205, exemption_phaseout_rate=0.25,
        )
        result = compute_amt(regular_tax=5000, adjusted_income=900000, params=params)
        # phaseout = (900000 - 173205) * 0.25 = 181698.75 > 173205 -> exemption 0
        self.assertAlmostEqual(result['effective_exemption'], 0)
        self.assertAlmostEqual(result['amt_tentative'], 900000 * 0.205)

    def test_pure_function(self):
        """DP#3: Same inputs always produce same outputs."""
        params = AMTParameters.for_year(2024)
        result1 = compute_amt(regular_tax=20000, adjusted_income=300000, params=params)
        result2 = compute_amt(regular_tax=20000, adjusted_income=300000, params=params)
        self.assertEqual(result1, result2)

    def test_pre_2024_no_phaseout(self):
        """Pre-2024: no exemption phaseout."""
        params = AMTParameters.for_year(2023)
        result = compute_amt(
            regular_tax=5000,
            adjusted_income=200000,
            params=params,
        )
        # No phaseout (rate = 0)
        # Effective exemption = 40000
        # AMT base = 200000 - 40000 = 160000
        # AMT = 160000 * 0.15 = 24000
        self.assertAlmostEqual(result['effective_exemption'], 40000)
        self.assertAlmostEqual(result['amt_tentative'], 24000)
        # Regular tax 5000 < 24000 → AMT applies
        self.assertAlmostEqual(result['amt_owing'], 24000)
        self.assertAlmostEqual(result['amt_surcharge'], 19000)



class TestTotalTaxWithAMT(unittest.TestCase):
    """total_tax_with_amt — tax payable is max(regular tax, minimum amount)."""

    PARAMS = AMTParameters(
        year=2026, exemption=181440, amt_rate=0.205,
        exemption_phaseout_start=181440, exemption_phaseout_rate=0.0,
    )

    def test_no_amt_low_income(self):
        result = total_tax_with_amt(
            regular_tax=15000, taxable_income=80000, params=self.PARAMS)
        self.assertAlmostEqual(result['total_tax'], 15000)
        self.assertAlmostEqual(result['amt_surcharge'], 0)

    def test_a_large_rrsp_deduction_triggers_no_amt_at_all(self):
        """Was `test_amt_triggered_by_rrsp_and_capital_gains`, which asserted a
        $25,992.98 minimum tax arising from a $100k RRSP deduction.

        There is no such tax. Once the deduction is claimed, taxable income IS
        the AMT base — $200,000 here. The minimum amount is
        (200,000 - 181,440) x 20.5% = $3,804.80, far below regular federal tax on
        $200k, so nothing binds and the taxpayer owes exactly their regular tax.
        """
        result = total_tax_with_amt(
            regular_tax=40000, taxable_income=200000, params=self.PARAMS)
        self.assertAlmostEqual(
            result['amt_details']['amt_tentative'], 3804.80, places=2)
        self.assertAlmostEqual(result['amt_surcharge'], 0)
        self.assertAlmostEqual(result['total_tax'], 40000)

    def test_capital_gains_do_trigger_amt(self):
        """The real trigger. A $1,000,000 gain is $500k of taxable income at the
        regular 50% inclusion, but the WHOLE $1,000,000 in AMTI:

            minimum = (1,000,000 - 181,440) x 20.5% = 167,804.80
        """
        result = total_tax_with_amt(
            regular_tax=113759,
            taxable_income=500000,
            taxable_capital_gains=500000,
            capital_gains_inclusion=0.50,
            params=self.PARAMS,
        )
        self.assertAlmostEqual(result['adjusted_income'], 1000000)
        self.assertAlmostEqual(
            result['amt_details']['amt_tentative'], 167804.80, places=2)
        self.assertGreater(result['amt_surcharge'], 0)
        self.assertAlmostEqual(result['total_tax'], 167804.80, places=2)

    def test_amt_is_a_floor_not_an_addition(self):
        """max(regular, minimum) — never regular + minimum."""
        result = total_tax_with_amt(
            regular_tax=50000, taxable_income=500000,
            taxable_capital_gains=500000, capital_gains_inclusion=0.50,
            params=self.PARAMS)
        self.assertAlmostEqual(
            result['total_tax'], result['amt_details']['amt_tentative'])

    def test_adjusted_income_in_result(self):
        result = total_tax_with_amt(
            regular_tax=10000, taxable_income=120000, params=self.PARAMS)
        self.assertIn('adjusted_income', result)
        self.assertAlmostEqual(result['adjusted_income'], 120000)

    def test_zero_income_no_amt(self):
        result = total_tax_with_amt(
            regular_tax=0, taxable_income=0, params=self.PARAMS)
        self.assertAlmostEqual(result['total_tax'], 0)
        self.assertAlmostEqual(result['amt_surcharge'], 0)

    def test_amt_carryforward_for_future_credit(self):
        """AMT paid is reported as carryforward-eligible for 7 years (s.120.2).

        NOTE nothing consumes it across years — see the module header's
        NOT MODELLED list.
        """
        result = total_tax_with_amt(
            regular_tax=100000, taxable_income=500000,
            taxable_capital_gains=500000, capital_gains_inclusion=0.50,
            params=self.PARAMS)
        self.assertAlmostEqual(result['amt_carryforward'], 67804.80, places=2)
        self.assertEqual(result['carryforward_years'], 7)


class TestWhatActuallyTriggersAMT(unittest.TestCase):
    """Replaces TestAMTDeductLaterImpact, which was built entirely on the premise
    that a large RRSP deduction triggers AMT. It does not — so every test in that
    class was asserting the arithmetic of a tax that does not exist, and passing.

    These pin what actually does trigger it.
    """

    def test_no_rrsp_deduction_however_large_moves_the_amt_base(self):
        """The deduct-later strategy (#546) is NOT an AMT risk. A taxpayer with
        $400k of income deducting $150k has an AMT base of $250k — exactly their
        taxable income, unmoved by the size of the deduction."""
        self.assertAlmostEqual(amt_adjusted_income(taxable_income=250000), 250000)

    def test_capital_gains_are_the_trigger(self):
        """A realized gain nearly doubles the AMT base relative to taxable
        income — the one add-back big enough to clear the exemption."""
        taxable = 300000
        plain = amt_adjusted_income(taxable_income=taxable)
        with_gain = amt_adjusted_income(
            taxable_income=taxable, taxable_capital_gains=taxable,
            capital_gains_inclusion=0.50)
        self.assertAlmostEqual(plain, 300000)
        self.assertAlmostEqual(with_gain, 600000)

    def test_stock_options_bite_harder_than_carrying_charges(self):
        """Same dollar of deduction, different treatment: the stock option
        deduction is denied in full (h); carrying charges are only halved
        (j)(ii)."""
        opt = amt_adjusted_income(taxable_income=200000, stock_option_deduction=50000)
        interest = amt_adjusted_income(taxable_income=200000, carrying_charges=50000)
        self.assertAlmostEqual(opt, 250000)
        self.assertAlmostEqual(interest, 225000)
        self.assertGreater(opt, interest)


# ============================================================================
# Issue #747 — the three #710 deferrals
# ============================================================================

class TestFiftyPercentOfCreditsReducesTheMinimum(unittest.TestCase):
    """Part 2: the minimum amount is reduced by 50% of eligible non-refundable
    credits (ITA s.127.531; CRA T691), not compared gross against regular tax
    net of full credits."""

    _params = AMTParameters(
        year=2024, exemption=173205, amt_rate=0.205,
        exemption_phaseout_start=173205, exemption_phaseout_rate=0.0)

    def test_credits_reduce_the_surcharge_by_exactly_half_of_them(self):
        gross = compute_amt(regular_tax=5000, adjusted_income=250000,
                            params=self._params)
        net = compute_amt(regular_tax=5000, adjusted_income=250000,
                          nonrefundable_credits=10000, params=self._params)
        # base 76795 * 0.205 = 15742.975 gross; minus 50% of 10000 = 5000.
        self.assertAlmostEqual(net['minimum_amount'],
                               gross['minimum_amount'] - 5000)
        self.assertAlmostEqual(gross['amt_surcharge'] - net['amt_surcharge'], 5000)

    def test_absent_credits_is_a_no_op_gross_minimum(self):
        """Default 0.0 leaves the minimum amount gross (DP#32: no silent offset)."""
        r = compute_amt(regular_tax=5000, adjusted_income=250000, params=self._params)
        self.assertAlmostEqual(r['minimum_amount'], r['amt_tentative'])

    def test_credits_cannot_drive_the_minimum_below_zero(self):
        r = compute_amt(regular_tax=0, adjusted_income=180000,
                        nonrefundable_credits=1_000_000, params=self._params)
        self.assertGreaterEqual(r['minimum_amount'], 0.0)
        self.assertEqual(r['amt_surcharge'], 0.0)


class TestSevenYearCarryForward(unittest.TestCase):
    """Part 1: AMT paid in excess of regular tax is a credit recoverable against
    regular tax in a later year (ITA s.120.2), expiring after 7 years."""

    def test_pay_once_then_recover_over_two_later_years(self):
        # Year 1: pay a $15,000 AMT surcharge -> a credit is booked, none
        # recovered (a paying year has no recoverable room).
        rec, bal = carry_forward_amt_credit([], 2026, surcharge_paid=15000,
                                            recoverable_room=0.0)
        self.assertEqual(rec, 0.0)
        self.assertEqual([(c.year, c.amount) for c in bal], [(2026, 15000)])

        # Year 2: regular tax exceeds the minimum by only $4,000 -> recover 4000.
        rec, bal = carry_forward_amt_credit(bal, 2027, surcharge_paid=0.0,
                                            recoverable_room=4000.0)
        self.assertEqual(rec, 4000.0)
        self.assertEqual([(c.year, c.amount) for c in bal], [(2026, 11000)])

        # Year 3: ample room -> recover the remaining 11000; balance clears.
        rec, bal = carry_forward_amt_credit(bal, 2028, surcharge_paid=0.0,
                                            recoverable_room=50000.0)
        self.assertEqual(rec, 11000.0)
        self.assertEqual(bal, [])

    def test_total_recovered_never_exceeds_total_paid(self):
        rec, bal = carry_forward_amt_credit([], 2026, 15000, 0.0)
        total = 0.0
        for yr in range(2027, 2034):
            rec, bal = carry_forward_amt_credit(bal, yr, 0.0, 3000.0)
            total += rec
        self.assertAlmostEqual(total, 15000.0)  # exactly what was paid, no more
        self.assertEqual(bal, [])

    def test_credit_expires_after_seven_years(self):
        # A credit booked in 2020 is claimable 2021..2027 and gone in 2028.
        opening = [AMTCredit(2020, 5000)]
        rec, bal = carry_forward_amt_credit(opening, 2027, 0.0, 999999.0)
        self.assertEqual(rec, 5000.0)  # last claimable year

        rec, bal = carry_forward_amt_credit([AMTCredit(2020, 5000)], 2028,
                                            0.0, 999999.0)
        self.assertEqual(rec, 0.0)     # expired: not recovered
        self.assertEqual(bal, [])      # and dropped

    def test_oldest_credit_recovered_first(self):
        opening = [AMTCredit(2027, 8000), AMTCredit(2025, 3000)]
        rec, bal = carry_forward_amt_credit(opening, 2028, 0.0, 3000.0)
        self.assertEqual(rec, 3000.0)
        # the 2025 credit (oldest, nearest expiry) is consumed first
        self.assertEqual([(c.year, c.amount) for c in bal], [(2027, 8000)])


class TestQuebecIMR(unittest.TestCase):
    """Part 3: Quebec's impôt minimum de remplacement (TP-776.42) — a separate
    provincial minimum tax (19%, its own exemption)."""

    def test_sourced_exemptions(self):
        self.assertEqual(QuebecIMRParameters.exemption_for(2024), 175000.0)
        self.assertEqual(QuebecIMRParameters.exemption_for(2025), 179990.0)

    def test_rate_is_nineteen_percent(self):
        self.assertAlmostEqual(QuebecIMRParameters.for_year(2024).rate, 0.19)

    def test_2026_exemption_is_indexed_from_data_not_invented(self):
        from tax_data import default_tax_provider
        provider = default_tax_provider()
        qc_2025 = provider._load_year(2025, 'canada', 'quebec').basic_personal_amount
        qc_2026 = provider._load_year(2026, 'canada', 'quebec').basic_personal_amount
        expected = 179990.0 * (qc_2026 / qc_2025)
        self.assertAlmostEqual(
            QuebecIMRParameters.exemption_for(2026, provider), expected)

    def test_pre_reform_year_raises_rather_than_inventing(self):
        with self.assertRaises(ValueError):
            QuebecIMRParameters.exemption_for(2023)

    def test_omitted_provider_constructs_a_default_one(self):
        """An indexed year with no provider passed builds the default provider
        on demand (DP#12) -- same figure as passing one explicitly."""
        from tax_data import default_tax_provider
        explicit = QuebecIMRParameters.exemption_for(2026, default_tax_provider())
        implicit = QuebecIMRParameters.exemption_for(2026)  # provider omitted
        self.assertAlmostEqual(implicit, explicit)
        # and via the public constructor path too
        self.assertAlmostEqual(QuebecIMRParameters.for_year(2026).exemption, explicit)

    def test_indexation_factor_raises_when_a_qc_bpa_is_missing(self):
        """DP#32: a missing Quebec basic personal amount raises rather than
        silently assuming no indexation (which would freeze the exemption)."""
        from countries.canada.amt import _quebec_indexation_factor

        class _ZeroBpaData:
            basic_personal_amount = 0.0

        class _StubProvider:
            def _load_year(self, year, country, province):
                return _ZeroBpaData()

        with self.assertRaises(ValueError):
            _quebec_indexation_factor(2027, _StubProvider())

    def test_imr_surcharge_applies_the_provincial_minimum(self):
        # AMTI $600k, exemption 175000 -> base 425000, minimum 19% = 80750.
        params = QuebecIMRParameters.for_year(2024)
        imr = compute_quebec_imr(regular_qc_tax=50000, adjusted_income=600000,
                                 params=params)
        self.assertAlmostEqual(imr['imr_minimum'], 425000 * 0.19)
        self.assertAlmostEqual(imr['imr_surcharge'], 425000 * 0.19 - 50000)

    def test_no_surcharge_when_regular_qc_tax_exceeds_the_minimum(self):
        params = QuebecIMRParameters.for_year(2024)
        imr = compute_quebec_imr(regular_qc_tax=200000, adjusted_income=600000,
                                 params=params)
        self.assertEqual(imr['imr_surcharge'], 0.0)

    def test_qc_credits_reduce_the_qc_minimum_by_half(self):
        params = QuebecIMRParameters.for_year(2024)
        gross = compute_quebec_imr(50000, 600000, params)
        net = compute_quebec_imr(50000, 600000, params, nonrefundable_credits=20000)
        self.assertAlmostEqual(gross['imr_minimum'] - net['imr_minimum'], 10000)


if __name__ == '__main__':
    unittest.main()
