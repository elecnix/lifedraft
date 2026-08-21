#!/usr/bin/env python3
"""Unit tests for attribution.py module.

Run with: python3 -m pytest countries/canada/tests/ -v
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

import unittest

from countries.canada.attribution import (
    check_attribution, check_tosi, attribution_planning_summary,
    check_below_market_loan_attribution,
    check_separation_transfer_exception,
    TransferType, RecipientRole, IncomeType, TOSIExclusion,
)


class TestSpousalPropertyAttribution(unittest.TestCase):
    """Test ITA s.74.1: spousal property transfer/loan."""

    def test_property_transfer_attributed(self):
        """Property transfer to spouse: attribution applies indefinitely."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
        )
        self.assertTrue(result.attributed)
        self.assertIn(IncomeType.ALL, result.income_types_attributed)

    def test_prescribed_rate_loan_no_attribution(self):
        """Prescribed-rate loan with interest paid: NO attribution."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            prescribed_rate_used=True,
            interest_paid_by_jan30=True,
        )
        self.assertFalse(result.attributed)
        self.assertTrue(result.escape_available)

    def test_prescribed_rate_loan_interest_not_paid(self):
        """Prescribed-rate loan with interest NOT paid: attribution applies."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            prescribed_rate_used=True,
            interest_paid_by_jan30=False,
        )
        self.assertTrue(result.attributed)

    def test_non_prescribed_loan_attribution(self):
        """Non-prescribed-rate loan: attribution applies regardless."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            prescribed_rate_used=False,
            interest_paid_by_jan30=True,  # Doesn't matter
        )
        self.assertTrue(result.attributed)


class TestMinorChildAttribution(unittest.TestCase):
    """Test ITA s.74.2: minor child attribution."""

    def test_minor_child_income_attributed(self):
        """Minor child (age 10): income attributes back."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=10,
        )
        self.assertTrue(result.attributed)
        self.assertIn(IncomeType.INTEREST, result.income_types_attributed)
        self.assertIn(IncomeType.DIVIDEND, result.income_types_attributed)

    def test_minor_child_capital_gains_not_attributed(self):
        """Capital gains do NOT attribute back for minor children."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=10,
        )
        # Capital gains should NOT be in the attributed types
        self.assertNotIn(IncomeType.CAPITAL_GAIN, result.income_types_attributed)

    def test_adult_child_no_attribution(self):
        """Adult child (age 20): no attribution."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=20,
        )
        self.assertFalse(result.attributed)

    def test_exactly_17_attributed(self):
        """Age 17: still a minor, attribution applies."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=17,
        )
        self.assertTrue(result.attributed)

    def test_18_not_attributed(self):
        """Age 18: not a minor, attribution does NOT apply."""
        result = check_attribution(
            TransferType.MINOR_CHILD,
            donor_role="primary",
            recipient_role="child",
            recipient_age=18,
        )
        self.assertFalse(result.attributed)


class TestTOSI(unittest.TestCase):
    """Test TOSI (Tax On Split Income) checks."""

    def test_tosi_applies_to_minor(self):
        """TOSI applies to split income received by a minor."""
        result = check_tosi(recipient_age=15, source_type="dividend",
                           income_amount=10000)
        self.assertTrue(result['tosi_applies'])

    def test_tosi_excluded_share(self):
        """Excluded share (10%+ ownership, 20+ hrs/week): TOSI doesn't apply."""
        result = check_tosi(
            recipient_age=20,
            source_type="business",
            recipient_hours_per_week=25,
            recipient_ownership_pct=0.15,
            income_amount=50000,
        )
        self.assertFalse(result['tosi_applies'])

    def test_tosi_age_25_exclusion(self):
        """Age 25+ exclusion for business/dividend income."""
        result = check_tosi(
            recipient_age=25,
            source_type="business",
            income_amount=30000,
        )
        self.assertFalse(result['tosi_applies'])

    def test_tosi_reasonable_return(self):
        """Reasonable return: TOSI doesn't apply up to that amount."""
        result = check_tosi(
            recipient_age=22,
            source_type="dividend",
            income_amount=20000,
            reasonable_return_amount=25000,
        )
        self.assertFalse(result['tosi_applies'])

    def test_tosi_tax_calculation(self):
        """TOSI tax = income × top marginal rate."""
        result = check_tosi(
            recipient_age=15,
            source_type="dividend",
            income_amount=10000,
        )
        self.assertTrue(result['tosi_applies'])
        # Default combined rate for Quebec (53.25%)
        expected_tax = 10000 * 0.5325
        self.assertAlmostEqual(result['tosi_tax'], expected_tax, places=0)

    def test_tosi_tax_with_custom_rate(self):
        """TOSI tax uses the province-specific rate when provided (DP#12)."""
        # Ontario combined top rate ~53.4%
        result = check_tosi(
            recipient_age=15,
            source_type="dividend",
            income_amount=10000,
            top_marginal_rate=0.5341,
        )
        expected_tax = 10000 * 0.5341
        self.assertAlmostEqual(result['tosi_tax'], expected_tax, places=0)
        self.assertAlmostEqual(result['top_marginal_rate'], 0.5341, places=4)

    def test_tosi_federal_only_rate(self):
        """TOSI with federal-only rate (33%) should produce lower tax."""
        result = check_tosi(
            recipient_age=15,
            source_type="dividend",
            income_amount=10000,
            top_marginal_rate=0.33,
        )
        expected_tax = 10000 * 0.33
        self.assertAlmostEqual(result['tosi_tax'], expected_tax, places=0)

    def test_tosi_default_rate_is_combined(self):
        """Default TOSI rate should be combined federal+provincial, not federal-only."""
        result = check_tosi(
            recipient_age=15,
            source_type="dividend",
            income_amount=10000,
        )
        # Default should be combined rate (~53.25% for Quebec), NOT federal-only (33%)
        self.assertGreater(result['top_marginal_rate'], 0.40,
                         "TOSI rate should be combined federal+provincial, not federal-only")


class TestAttributionPlanningSummary(unittest.TestCase):
    """Test the comprehensive planning summary."""

    def test_summary_with_minor_child(self):
        """Summary with a minor child includes attribution note."""
        result = attribution_planning_summary(
            spouse_age=46,
            child_ages=[10, 15],
            has_prescribed_rate_loan=True,
        )
        self.assertIn('spousal_attribution', result)
        self.assertIn('minor_child_attribution', result)
        self.assertIsInstance(result['planning_notes'], list)

    def test_summary_no_attribution(self):
        """Summary with prescribed-rate loan: no spousal attribution."""
        result = attribution_planning_summary(
            spouse_age=46,
            has_prescribed_rate_loan=True,
            interest_paid_on_time=True,
        )
        self.assertFalse(result['spousal_attribution']['attributed'])

    def test_summary_child_at_18(self):
        """Child at 18: no minor child attribution."""
        result = attribution_planning_summary(
            child_ages=[18, 21],
        )
        for child in result['minor_child_attribution']:
            self.assertFalse(child['attributed'])


class TestJointElection(unittest.TestCase):
    """Test ITA s.74.1(2): joint election to avoid attribution on spousal transfers."""

    def test_joint_election_avoids_attribution(self):
        """Joint election (ITA s.74.1(2)): spouses can elect to avoid attribution."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            joint_election=True,
        )
        self.assertFalse(result.attributed)
        self.assertTrue(result.escape_available)
        self.assertIn("Joint election", result.reason)

    def test_joint_election_overrides_prescribed_rate(self):
        """Joint election takes precedence even without prescribed-rate loan."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            joint_election=True,
            prescribed_rate_used=False,
            interest_paid_by_jan30=False,
        )
        self.assertFalse(result.attributed)
        self.assertTrue(result.escape_available)

    def test_no_joint_election_attribution_applies(self):
        """Without joint election, attribution still applies normally."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            joint_election=False,
        )
        self.assertTrue(result.attributed)

    def test_joint_election_escape_description(self):
        """Joint election escape description mentions s.74.1(2)."""
        result = check_attribution(
            TransferType.PROPERTY_TRANSFER,
            donor_role="primary",
            recipient_role="spouse",
            joint_election=True,
        )
        self.assertIn("74.1(2)", result.escape_description)


class TestBelowMarketLoanAttribution(unittest.TestCase):
    """Test ITA s.74.5(1): below-market interest loan attribution (issue #311)."""

    def test_below_prescribed_rate_attributes(self):
        """Loan below the prescribed rate fails s.74.5(2): income attributes back."""
        result = check_below_market_loan_attribution(
            loan_principal=100000,
            loan_rate=0.01,        # 1%
            prescribed_rate=0.04,  # 4%
            income_earned=7000,
        )
        self.assertTrue(result.attributed)
        self.assertEqual(result.attributed_income, 7000)
        self.assertEqual(result.required_rate, 0.04)

    def test_at_required_rate_with_timely_interest_no_attribution(self):
        """Loan at the required rate + interest paid by Jan 30: exception applies."""
        result = check_below_market_loan_attribution(
            loan_principal=100000,
            loan_rate=0.04,
            prescribed_rate=0.04,
            interest_paid_by_jan30=True,
            income_earned=7000,
        )
        self.assertFalse(result.attributed)
        self.assertEqual(result.attributed_income, 0.0)

    def test_required_rate_is_lesser_of_prescribed_and_commercial(self):
        """s.74.5(2)(a): benchmark is the LESSER of prescribed and commercial rate."""
        # Prescribed 4%, commercial 3% -> required 3%. Loan at 3% clears it.
        result = check_below_market_loan_attribution(
            loan_principal=100000,
            loan_rate=0.03,
            prescribed_rate=0.04,
            commercial_rate=0.03,
            income_earned=5000,
        )
        self.assertEqual(result.required_rate, 0.03)
        self.assertFalse(result.attributed)

    def test_rate_met_but_interest_not_paid_attributes(self):
        """Edge: rate is adequate but interest not paid by Jan 30 -> attribution."""
        result = check_below_market_loan_attribution(
            loan_principal=100000,
            loan_rate=0.04,
            prescribed_rate=0.04,
            interest_paid_by_jan30=False,
            income_earned=6000,
        )
        self.assertTrue(result.attributed)
        self.assertEqual(result.attributed_income, 6000)


class TestSeparationTransferException(unittest.TestCase):
    """Test ITA s.74.5(3): separation/breakdown attribution exception (issue #312)."""

    def test_income_attribution_off_on_breakdown(self):
        """s.74.5(3)(a): income attribution is off while separated due to breakdown."""
        result = check_separation_transfer_exception(
            living_separate_and_apart=True,
            due_to_breakdown=True,
            is_capital_gain=False,
        )
        self.assertFalse(result.attributed)
        self.assertTrue(result.escape_available)

    def test_capital_gain_needs_joint_election(self):
        """s.74.5(3)(b): capital-gain relief needs a joint election."""
        no_election = check_separation_transfer_exception(
            living_separate_and_apart=True,
            due_to_breakdown=True,
            is_capital_gain=True,
            joint_election_74_2=False,
        )
        self.assertTrue(no_election.attributed)

        with_election = check_separation_transfer_exception(
            living_separate_and_apart=True,
            due_to_breakdown=True,
            is_capital_gain=True,
            joint_election_74_2=True,
        )
        self.assertFalse(with_election.attributed)

    def test_not_separated_no_relief(self):
        """Edge: not living separate and apart -> normal attribution stands."""
        result = check_separation_transfer_exception(
            living_separate_and_apart=False,
            due_to_breakdown=False,
        )
        self.assertTrue(result.attributed)
        self.assertFalse(result.escape_available)

    # -- #726: the `is_capital_gain or True` bug ---------------------------
    # The not-qualifies branch used to read `attributed=is_capital_gain or True`,
    # which is always True and left the `is_capital_gain` flag dead: a capital
    # gain was never marked CAPITAL_GAIN and was indistinguishable from
    # ordinary income (the capital-gains attribution path was unreachable).
    # These tests fail on the `or True` form and pass after the fix.

    def test_not_qualified_capital_gain_is_marked_capital_gain(self):
        """When the exception does NOT qualify and the income is a capital gain,
        the result must record s.74.2 capital-gain attribution distinctly
        (income_types_attributed=[CAPITAL_GAIN]), not the empty list the dead
        `is_capital_gain or True` produced."""
        result = check_separation_transfer_exception(
            living_separate_and_apart=False,
            due_to_breakdown=False,
            is_capital_gain=True,
        )
        self.assertTrue(result.attributed)  # attribution stands
        self.assertEqual(result.income_types_attributed, [IncomeType.CAPITAL_GAIN])

    def test_not_qualified_ordinary_income_is_not_marked_capital_gain(self):
        """A non-capital-gain amount must NOT receive capital-gain treatment.
        Under the dead `is_capital_gain or True` the flag had no effect, so a
        capital gain and ordinary income were indistinguishable. After the fix,
        ordinary income is marked s.74.1 ([ALL]), distinct from [CAPITAL_GAIN]."""
        result = check_separation_transfer_exception(
            living_separate_and_apart=False,
            due_to_breakdown=False,
            is_capital_gain=False,
        )
        self.assertTrue(result.attributed)
        self.assertEqual(result.income_types_attributed, [IncomeType.ALL])
        self.assertNotIn(IncomeType.CAPITAL_GAIN, result.income_types_attributed)

    def test_is_capital_gain_flag_changes_the_result_in_not_qualified_branch(self):
        """The defining regression test for #726: the `is_capital_gain` flag
        must actually branch. With the old `is_capital_gain or True` both values
        produced identical results; after the fix they must differ in
        income_types_attributed."""
        cg = check_separation_transfer_exception(
            living_separate_and_apart=False, due_to_breakdown=False,
            is_capital_gain=True,
        )
        ordinary = check_separation_transfer_exception(
            living_separate_and_apart=False, due_to_breakdown=False,
            is_capital_gain=False,
        )
        self.assertEqual(cg.income_types_attributed, [IncomeType.CAPITAL_GAIN])
        self.assertEqual(ordinary.income_types_attributed, [IncomeType.ALL])
        self.assertNotEqual(cg.income_types_attributed,
                           ordinary.income_types_attributed)


if __name__ == '__main__':
    unittest.main()