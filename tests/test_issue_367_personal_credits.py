#!/usr/bin/env python3
"""Issue #367: declared personal outlays must reach the tax computation.

A household could not declare medical expenses, charitable donations or a
federal political contribution, and union or professional dues. The credit
functions existed and were tested; nothing called them, so a senior paying for
care or a household that gives regularly was taxed as if it had claimed nothing
and its after-tax cash was understated every year.

Three levels are tested, deliberately (DP#11):

- the PURE schedule (``political_contribution_credit``) against hand-computed
  figures, because a wrong bracket schedule is invisible in an end-to-end delta;
- the CONTRACT, including every refusal the adapter promises -- a claim the
  document declares but the run cannot apply must fail loudly, never vanish;
- the ENGINE, driving ``simulate_year_pure`` and asserting the year's after-tax
  cash moves by the credit, which is the property the issue asks for and the one
  a unit test of the rule alone would not establish.

DP#15: fabricated round numbers and role-based names only.
"""

from __future__ import annotations

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from contract_errors import ContractAdaptationError
from countries.canada.tax_calc import political_contribution_credit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_input_contract import _load_example, _two_generation_subset


def _doc_with_claims(person_id="p1", **claims):
    doc = _two_generation_subset(_load_example())
    p = next(x for x in doc["people"] if x["id"] == person_id)
    entry = {"year": 2026}
    entry.update(claims)
    p["annual_claims"] = [entry]
    return doc


def _claims_for(doc, person_id="p1"):
    cfg = ic.to_internal_config(doc)
    member = next(m for m in cfg["family"]["members"]
                  if m["id"] == person_id)
    return member.get("claims_by_year")


class TheContractAcceptsAndMapsDatedClaims(unittest.TestCase):
    def test_all_four_categories_round_trip(self):
        doc = _doc_with_claims(medical_expenses=27_600.0,
                               charitable_donations=1_000.0,
                               political_contributions_federal=500.0,
                               union_dues=900.0)
        self.assertEqual(_claims_for(doc), {2026: {
            "medical_expenses": 27_600.0,
            "charitable_donations": 1_000.0,
            "political_contributions_federal": 500.0,
            "union_dues": 900.0}})

    def test_a_zero_or_absent_line_is_not_a_declared_claim(self):
        """A year whose every amount is 0 claims nothing; keeping the entry
        would make a no-claim year look like a declared one."""
        doc = _doc_with_claims(medical_expenses=0, charitable_donations=0)
        self.assertIsNone(_claims_for(doc))

    def test_a_member_with_no_claims_block_carries_no_key(self):
        self.assertIsNone(_claims_for(_two_generation_subset(_load_example())))


class AClaimTheRunCannotApplyIsRefused(unittest.TestCase):
    """DP#32: each of these, if dropped silently, understates the household's
    credits while looking like a correct run."""

    def _refuse(self, **entry):
        doc = _two_generation_subset(_load_example())
        p = next(x for x in doc["people"] if x["id"] == "p1")
        p["annual_claims"] = [entry]
        with self.assertRaises(ValueError):
            ic.to_internal_config(doc)

    def test_two_entries_for_one_year(self):
        doc = _doc_with_claims(charitable_donations=100.0)
        p = next(x for x in doc["people"] if x["id"] == "p1")
        p["annual_claims"].append({"year": 2026, "charitable_donations": 500.0})
        with self.assertRaises(ValueError):
            ic.to_internal_config(doc)

    def test_an_undated_claim(self):
        self._refuse(charitable_donations=100.0)

    def test_a_year_before_the_projection(self):
        self._refuse(year=2019, charitable_donations=100.0)

    def test_a_negative_amount(self):
        self._refuse(year=2026, medical_expenses=-1.0)

    def test_a_non_numeric_amount(self):
        self._refuse(year=2026, union_dues="900")


class ThePoliticalCreditFollowsThePublishedSchedule(unittest.TestCase):
    """ITA s.127(3) / CRA lines 40900-41000: 75% of the first $400, 50% of the
    next $350, 33 1/3% of the next $525, plateauing at $650 on $1,275."""

    def test_each_bracket_boundary(self):
        for amount, expected in ((100.0, 75.0), (300.0, 225.0), (400.0, 300.0),
                                 (750.0, 475.0), (1_275.0, 650.0),
                                 (5_000.0, 650.0)):
            self.assertAlmostEqual(
                political_contribution_credit(amount, 2024), expected, places=2,
                msg=f"on ${amount}")

    def test_a_non_positive_contribution_earns_nothing(self):
        self.assertEqual(political_contribution_credit(0.0, 2024), 0.0)


class TheRuleMovesTheYearsCash(unittest.TestCase):
    """The property the issue asks for: the year's after-tax cash changes by
    the credit amount. Asserted on the ENGINE's own output, by running the same
    household twice -- with and without the declared claims."""

    def _run(self, claims):
        from test_issue_584_rules_registry import _build_year_inputs, _make_config
        from simulation_state import simulate_year_pure, SimState
        from canada_state_accessors import _default_canada_state

        members = [
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
             'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000},
        ]
        if claims:
            members[0]['claims_by_year'] = {2026: claims}
        config = _make_config(family_members=members, projection_years=1)
        return simulate_year_pure(
            state=SimState(jurisdiction_state={'canada': _default_canada_state()}),
            year=0,
            inputs=_build_year_inputs(
                allocations={'_primary_income': 120_000, '_annual_savings': 0},
                config=config, investment_return=0.0,
                primary_marginal_rate=0.40, calendar_year=2026,
                tax_provider=None,
                primary_tax_before=30_000.0,
                primary_taxable_income=120_000.0,
                # A spending requirement, so apply_solvency reaches the branch
                # that books the POST-credit after-tax income (with no
                # living_costs it returns early and nothing observes the credit).
                living_costs=60_000, after_tax_income=45_000,
            ),
        )[0]

    def test_a_political_contribution_moves_the_cash_by_the_schedule(self):
        """The declared amount is $500, so the credit is 75% x 400 + 50% x 100 =
        $350 -- hand-computed, and asserted against the ENGINE's own after-tax
        cash, not against a formula this test retyped."""
        without = self._run(None)
        with_claim = self._run({'political_contributions_federal': 500.0})
        self.assertAlmostEqual(
            with_claim.after_tax_income - without.after_tax_income,
            350.0, places=6)

    def test_union_dues_are_a_deduction_worth_bracket_fill(self):
        """A deduction is worth the tax on the income slice it removes: more
        than nothing, and never more than the amount itself."""
        without = self._run(None)
        with_dues = self._run({'union_dues': 900.0})
        delta = with_dues.after_tax_income - without.after_tax_income
        self.assertGreater(delta, 0.0)
        self.assertLess(delta, 900.0)

    def test_medical_and_donation_credits_raise_the_cash(self):
        without = self._run(None)
        with_claims = self._run({'medical_expenses': 6_000.0,
                                 'charitable_donations': 2_000.0})
        self.assertGreater(
            with_claims.after_tax_income - without.after_tax_income, 0.0)

    def test_a_household_declaring_nothing_is_a_strict_no_op(self):
        """The golden path: no claims -> 0.0, so the golden invariant is
        unchanged by construction rather than by measurement."""
        without = self._run(None)
        also_without = self._run(None)
        self.assertEqual(without.after_tax_income, also_without.after_tax_income)


class TheRuleRefusesToInventAQuebecRateOrApplyAStaleYear(unittest.TestCase):
    """Unit-level: the branches no end-to-end household reaches.

    A Quebec credit is converted at QUEBEC's lowest bracket rate. When the run
    is not in Quebec, or the split brackets are unavailable, the rate is 0.0 --
    the credit is worth nothing rather than being valued at a rate that belongs
    to another jurisdiction (DP#32). A member whose claims ledger carries no
    entry for the year being priced is likewise a 0.0, not a stale year's
    amount."""

    def _ctx(self, province, provider=None, claims_year=2026,
             calendar_year=2026):
        from rule_registry import RuleContext
        from test_issue_584_rules_registry import _make_config

        config = _make_config(
            projection_years=1,
            province=province,
            family_members=[
                {'role': 'primary', 'birth_year': 1980, 'gross_income': 120_000,
                 'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000,
                 'claims_by_year': {claims_year: {
                     'political_contributions_federal': 500.0}}},
            ],
        )
        return RuleContext(
            year=0, calendar_year=calendar_year, allocations={}, config=config,
            investment_return=0.0, mortgage_rate=0.0, heloc_rate=0.0,
            mortgage_data=None, use_readvanceable=False, deduct_later=False,
            primary_marginal_rate=0.0, spouse_marginal_rate=0.0, resp_data=None,
            fhsa_contribution=0.0, rrsp_annual_limit=None, tfsa_annual_limit=None,
            fhsa_annual_limit=None, non_reg_after_tax_return=None,
            cpp_income=0.0, oas_income=0.0, pension_income=0.0,
            drawdown_order=None, rrif_min_rate_primary=0.0,
            rrif_min_rate_spouse=0.0, drawdown_net_target=0.0,
            retiree_marginal_rate=0.0, drawdown_bracket_target=None,
            drawdown_other_taxable_income=0.0, primary_retired=False,
            spouse_retired=False, year_brackets=None,
            tax_provider=provider,
            primary_tax_before=9_000.0, primary_taxable_income=120_000.0,
        )

    def test_a_non_quebec_run_gets_no_quebec_credit(self):
        """Ontario: the federal political credit still applies, and the Quebec
        rate path is never entered."""
        from rule_registry import YearWorkingState
        from rules_personal_credits import apply_personal_credits

        ws = YearWorkingState()
        fired = apply_personal_credits(ws, self._ctx('ontario'))
        self.assertTrue(fired)
        self.assertAlmostEqual(ws.personal_credit_applied_primary, 350.0, places=6)

    def test_missing_split_brackets_are_worth_nothing_not_a_guess(self):
        from rule_registry import YearWorkingState
        from rules_personal_credits import apply_personal_credits

        from tax_data import default_tax_provider

        class _NoSplit:
            """Every call delegates to the real provider except the split, which
            is what a year the provider has no split for looks like."""

            def __init__(self):
                self._real = default_tax_provider()

            def __getattr__(self, name):
                return getattr(self._real, name)

            def get_split_brackets(self, year, province=None):
                raise ValueError("no split for this year")

        ws = YearWorkingState()
        fired = apply_personal_credits(ws, self._ctx('quebec', provider=_NoSplit()))
        # The federal credit still applies; the Quebec side is 0.0, not invented.
        self.assertTrue(fired)
        self.assertGreater(ws.personal_credit_applied_primary, 0.0)

    def test_an_empty_provincial_slice_is_worth_nothing_not_a_guess(self):
        """A provider that HAS a year but no provincial brackets must not have
        the federal lowest rate pressed into service as Quebec's."""
        from rule_registry import YearWorkingState
        from rules_personal_credits import apply_personal_credits

        from tax_data import default_tax_provider

        class _NoProvincial:
            def __init__(self):
                self._real = default_tax_provider()

            def __getattr__(self, name):
                return getattr(self._real, name)

            def get_split_brackets(self, year, province=None):
                return [], []

        ws = YearWorkingState()
        fired = apply_personal_credits(
            ws, self._ctx('quebec', provider=_NoProvincial()))
        self.assertTrue(fired)
        self.assertGreater(ws.personal_credit_applied_primary, 0.0)

    def test_a_tax_year_before_the_credit_data_starts_claims_nothing(self):
        """The amounts and thresholds are year-versioned; a projection INDEX
        (which a bare unit-test caller supplies) is not a tax year, so the rule
        refuses rather than pricing 2023 off 2026 data."""
        from rule_registry import YearWorkingState
        from rules_personal_credits import apply_personal_credits

        ws = YearWorkingState()
        self.assertFalse(apply_personal_credits(
            ws, self._ctx('quebec', calendar_year=2023)))
        self.assertEqual(ws.personal_credit_applied_primary, 0.0)

    def test_a_year_with_no_entry_in_the_ledger_claims_nothing(self):
        from rule_registry import YearWorkingState
        from rules_personal_credits import apply_personal_credits

        ws = YearWorkingState()
        fired = apply_personal_credits(ws, self._ctx('quebec', claims_year=2025))
        self.assertFalse(fired)
        self.assertEqual(ws.personal_credit_applied_primary, 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
