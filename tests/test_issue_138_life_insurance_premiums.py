#!/usr/bin/env python3
"""Issue #138 (slice 1): life-insurance PREMIUMS as dated cash-flow legs.

Before this slice, the entire insurance model was one number at death
(``life_insurance_death_benefit`` into the terminal estate): a policy
costing real money every year was modelled as FREE. This module tests the
premium side -- every declared policy's ``premium_annual`` becomes one
NEGATIVE dated cash-flow leg per calendar year the policy charges it, folded
into the engine's EXISTING dated cash-flow channel (the same channel #139's
transaction costs ride), so every objective that folds the balance sheet
sees the true cost of coverage.

Cliff semantics (what makes keep/replace/lapse priceable):
  * a TERM policy charges through the last calendar year whose FIRST DAY it
    is in force, then stops (a Jun-30 2036 expiry still charges its 2036
    premium; a Jan-1 2036 expiry does not charge January -- DP#1);
  * a PERMANENT policy (``term_end_date`` null) charges through the whole
    projection window;
  * a declared ``renewal_end_date`` keeps the DEATH BENEFIT alive past the
    cliff but does NOT invent a renewal premium -- the insurer sets the
    renewal rate at underwriting, never the engine (DP#32: price what is
    declared, log the gap, never fill it with a guess);
  * an incoherent renewal (on a permanent policy, or ending on/before its
    own term) is refused loudly at the contract boundary.

Golden no-op (DP#32): a household declaring no policies produces no legs
and a byte-identical run.

Fabricated round numbers, role-based names (DP#4/DP#15).
"""
import copy
import unittest

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
import contract_errors
from _example_doc import minimal_example
from contract_estate import map_insurance_premiums
from contract_schema import validate_contract


def _policy(*, pol_id="term_p1", kind="term", face=500_000, premium=1_200,
            term_end="2036-06-30", renewal_end=None, insured="p1", owner="p1"):
    """One synthetic policy: $500,000 face, $1,200/yr, round dates only."""
    entry = {
        "id": pol_id, "owner": owner, "insured": insured,
        "beneficiary": "p2", "kind": kind, "face_amount": face,
        "premium_annual": premium, "as_of": "2026-01-01",
        "term_end_date": term_end,
    }
    if renewal_end is not None:
        entry["renewal_end_date"] = renewal_end
    return entry


def _minimal_doc(policies):
    """The smallest document ``map_insurance_premiums`` can date against:
    horizon person p1 born 1980-01-01 projected to age 95 -> last simulated
    year 2075."""
    return {
        "people": [{"id": "p1", "birth_date": "1980-01-01"}],
        "decisions": {"horizon": {"person": "p1", "until_age": 95}},
        "estate": {"life_insurance": policies},
    }


# ============================================================================
# 1. The pure mapper: one leg per charged-premium year, cliff-aware
# ============================================================================

class TestMapInsurancePremiums(unittest.TestCase):

    def test_term_policy_charges_through_a_mid_year_cliff(self):
        """A Jun-30 2036 expiry means the policy covers part of 2036: the
        $1,200 premium fires 2026..2036 INCLUSIVE, then stops."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-06-30")]),
            primary_id="p1", start_year=2026)
        self.assertEqual([leg["year"] for leg in legs],
                         list(range(2026, 2037)))
        self.assertEqual({leg["amount"] for leg in legs}, {-1200.0})

    def test_jan_first_cliff_charges_no_january_premium(self):
        """A Jan-1 2036 expiry: the policy is in force on ZERO days of 2036,
        so the last leg is 2035 (DP#1: dates, not years)."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual([leg["year"] for leg in legs], list(range(2026, 2036)))

    def test_permanent_policy_charges_through_the_horizon(self):
        """term_end_date=null (permanent): the premium runs to the LAST
        simulated year (1980 + 95 = 2075), not to some invented cap."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(kind="permanent", term_end=None)]),
            primary_id="p1", start_year=2026)
        self.assertEqual(len(legs), 2075 - 2026 + 1)
        self.assertEqual(legs[-1]["year"], 2075)

    def test_already_lapsed_policy_charges_nothing(self):
        """A term policy that expired before the projection starts charges
        nothing -- a lapsed policy is not a cost."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2020-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(legs, [])

    def test_renewal_does_not_invent_a_post_cliff_premium(self):
        """A declared renewal keeps the coverage alive past the cliff, but
        the RENEWAL premium is the insurer's to quote: the priced legs stop
        at term_end_date exactly as if the policy had lapsed."""
        lapsed = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-06-30")]),
            primary_id="p1", start_year=2026)
        renewed = map_insurance_premiums(
            _minimal_doc([_policy(term_end="2036-06-30",
                                  renewal_end="2046-06-30")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(renewed, lapsed)

    def test_zero_premium_is_a_real_zero_not_a_drop(self):
        """DP#32: a declared $0 premium produces legs carrying 0 -- absence
        and zero stay distinguishable shapes, neither coerced."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(premium=0, term_end="2028-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(len(legs), 2)
        self.assertTrue(all(leg["amount"] == 0.0 for leg in legs))

    def test_legs_are_post_tax_costs_named_by_policy(self):
        """Premiums are NOT deductible -- each leg is after-tax cash out,
        signed negative, identified by its policy id."""
        legs = map_insurance_premiums(
            _minimal_doc([_policy(pol_id="term_a", term_end="2027-01-01")]),
            primary_id="p1", start_year=2026)
        self.assertEqual(legs[0]["tax_treatment"], "post-tax")
        self.assertEqual(legs[0]["kind"], "cost")
        self.assertEqual(legs[0]["id"], "term_a")

    def test_no_policies_produce_no_legs(self):
        """The golden no-op seam: no declared policies -> no legs at all."""
        self.assertEqual(map_insurance_premiums(_minimal_doc([]),
                                                primary_id="p1",
                                                start_year=2026), [])

    def test_renewal_without_a_term_is_refused_loudly(self):
        """A permanent policy does not renew: renewal_end_date on a policy
        with term_end_date=null is contradictory input, refused (DP#32)."""
        with self.assertRaises(contract_errors.ContractAdaptationError):
            map_insurance_premiums(
                _minimal_doc([_policy(kind="permanent", term_end=None,
                                      renewal_end="2046-06-30")]),
                primary_id="p1", start_year=2026)

    def test_renewal_ending_before_the_term_is_refused_loudly(self):
        """Coverage cannot end twice: a renewal_end_date on/before the very
        term_end_date it claims to extend is refused, never truncated."""
        with self.assertRaises(contract_errors.ContractAdaptationError):
            map_insurance_premiums(
                _minimal_doc([_policy(term_end="2036-06-30",
                                      renewal_end="2036-06-30")]),
                primary_id="p1", start_year=2026)


if __name__ == "__main__":
    unittest.main()
