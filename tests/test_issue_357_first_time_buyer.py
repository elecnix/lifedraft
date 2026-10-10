"""Issue #357: a first-home instrument must be gated on ACTUAL first-time-buyer status.

A household declaring ``first_home_purchases: [{buyer, year}]`` always got BOTH
instruments for that buyer: the FHSA drained tax-free and up to $60,000 taken
out of the buyer's RRSP non-taxably. Nobody checked whether the buyer is a
first-time home buyer. Under ITA s.146.01(1) the HBP requires that the buyer did
not live, in the 4-calendar-year lookback, in a qualifying home they OWNED --
and that test counts a home owned by their spouse/common-law partner and
occupied during the relationship. Under ITA s.146.6(1) the FHSA qualifying
withdrawal applies the buyer's OWN history (a partner's home does not count for
a *withdrawal*, only for opening).

So a buyer who fails the test was silently granted a non-taxable RRSP withdrawal
and a tax-free FHSA drain that CRA and Revenu Québec would refuse: the down
payment is overstated and that year's tax understated.

The rules already existed and were never consulted --
``FHSAAccount.is_first_home_buyer`` reached only from tests, because
``_apply_first_home_to_account`` built the account with no
``principal_residence_years``, and ``HBPAccount.is_first_home`` defaulted to
True. This wires them to a DECLARED fact, and refuses loudly rather than
granting an instrument nobody is entitled to.

Acceptance cases are the issue's own (1)-(4). Eligibility is pure arithmetic on
declared years, so the gates are unit-tested against the rule functions; the
fold-level refusal is proven through the real loading boundary, because the
refusal happens at ingestion.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract
from contract_errors import ContractAdaptationError
from simulation_state import apply_child_first_home_purchases
from countries.canada.fhsa import FHSAAccount
from countries.canada.hbp_rules import is_first_time_home_buyer

_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "lifedraft", "minimal-two-adult", "input.json")


def _doc(buyer="primary") -> dict:
    with open(_EXAMPLE) as fh:   # filterwarnings=error: an unclosed file IS a failure
        doc = json.load(fh)
    doc["first_home_purchases"] = [{"buyer": "p1", "year": 2024}]
    return doc


def _declare_history(doc: dict, person_id: str, years) -> dict:
    """Declare a person's owned-principal-residence history as RANGES, which is
    what a household can actually state ("I owned the condo 2015-2022")."""
    person = next(p for p in doc["people"] if p["id"] == person_id)
    person["owned_principal_residence_history"] = [
        {"from": f"{a}-01-01", "to": (None if b is None else f"{b}-12-31")}
        for a, b in years
    ]
    return doc


class ADeclaredSpanIsExpandedNotTrusted(unittest.TestCase):
    """The residence history is expanded from declared spans into calendar years
    for the rules. A span the schema permits but the engine must survive: an
    ``open`` span is bounded by ``as_of`` (the engine knows nothing about years
    the document has not reached), and a REVERSED span (``to`` before
    ``from``) states one year rather than none -- silently yielding zero years
    would make a buyer with a garbled history look eligible."""

    def test_an_open_span_runs_to_as_of_and_therefore_disqualifies(self):
        """An OPEN span ("still owned") is bounded by ``as_of``, so an open span
        starting inside the lookback disqualifies the buyer -- which is right:
        still owning the home is exactly the disqualifying fact."""
        doc = _declare_history(_doc(), "p1", [(2023, None)])
        with self.assertRaises(ContractAdaptationError) as ctx:
            input_contract.to_internal_config(copy.deepcopy(doc))
        # The refusal names the expanded years, so the bound is visible.
        self.assertIn("2026", str(ctx.exception))

    def test_a_reversed_span_still_states_a_year(self):
        doc = _doc()
        person = next(p for p in doc["people"] if p["id"] == "p1")
        person["owned_principal_residence_history"] = [
            {"from": "2022-01-01", "to": "2019-12-31"},   # reversed
        ]
        # 2022 is inside the 2024 lookback, so this buyer is refused -- NOT
        # waved through by an empty year list.
        with self.assertRaises(ContractAdaptationError):
            input_contract.to_internal_config(copy.deepcopy(doc))


class TheGateAppliesToWhicheverMemberBuys(unittest.TestCase):
    """The eligibility test is about the BUYER, so it must reach a CHILD buyer
    as well as an adult -- the first-home instruments are the same two for
    either, and the mapper gates every entry, not just adult ones."""

    @staticmethod
    def _doc_with_buyer(buyer: str, years):
        with open(_EXAMPLE) as fh:
            doc = json.load(fh)
        person = next(p for p in doc["people"] if p["id"] == buyer)
        person["owned_principal_residence_history"] = [
            {"from": "2024-01-01", "to": "2026-06-30"},
        ]
        doc["first_home_purchases"] = [{"buyer": buyer, "year": 2028}]
        return doc

    def test_a_child_buyer_inside_the_lookback_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            input_contract.to_internal_config(self._doc_with_buyer("ca", None))

    def test_an_adult_buyer_inside_the_lookback_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            input_contract.to_internal_config(self._doc_with_buyer("p1", None))

    def test_history_OUTSIDE_the_lookback_still_loads(self):
        """The complement, and the case my first probe got wrong: 2020-2023 is
        outside a 2028 purchase's 2024-2028 window, so that household IS a
        first-time buyer and must load. A test that only ever checks the refusal
        would pass while the gate rejected everyone."""
        with open(_EXAMPLE) as fh:
            doc = json.load(fh)
        person = next(p for p in doc["people"] if p["id"] == "ca")
        person["owned_principal_residence_history"] = [
            {"from": "2020-01-01", "to": "2023-12-31"},
        ]
        doc["first_home_purchases"] = [{"buyer": "ca", "year": 2028}]
        cfg = input_contract.to_internal_config(doc)
        self.assertEqual(cfg["family"]["first_home_purchases"][0]["buyer"], "ca")


class ANoneYearListIsAbsenceNotACrash(unittest.TestCase):
    """``prior_residence_years`` is only WRITTEN when non-empty, so its absence
    is "nothing declared" and must stay a no-op. A hand-built internal config can
    carry an explicit ``None`` there, and ``tuple(None)`` raised a bare TypeError
    out of the fold -- a crash rather than the absence it is."""

    @staticmethod
    def _accounts():
        return [{"fhsa_balance": 0.0, "fhsa_lifetime_remaining": 0.0,
                 "rrsp_balance": 0.0, "non_reg_balance": 0.0, "non_reg_acb": 0.0}]

    def test_an_explicit_none_does_not_crash_the_child_fold(self):
        out = apply_child_first_home_purchases(
            self._accounts(), [{"id": "ca"}],
            [{"buyer": "ca", "year": 2028, "prior_residence_years": None}], 2028)
        self.assertEqual(out[0]["rrsp_balance"], 0.0)

    def test_a_declared_list_is_still_used(self):
        out = apply_child_first_home_purchases(
            self._accounts(), [{"id": "ca"}],
            [{"buyer": "ca", "year": 2028, "prior_residence_years": [2024]}], 2028)
        self.assertEqual(out[0]["rrsp_balance"], 0.0)   # no crash, no purchase funding


class TheEligibilityArithmetic(unittest.TestCase):
    """s.146.01(1): 4-calendar-year lookback over the buyer's OWN owned principal
    residence years, plus the partner's when they lived in one during the
    relationship. Unit-tested against the pure rule, because it is pure
    arithmetic over declared years (DP#11)."""

    def test_own_recent_ownership_disqualifies(self):
        self.assertFalse(is_first_time_home_buyer(2024, own_years=[2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022]))

    def test_no_history_is_eligible(self):
        self.assertTrue(is_first_time_home_buyer(2024, own_years=[]))

    def test_ownership_outside_the_lookback_is_eligible_again(self):
        """Acceptance (4): occupancy ending 2019, buying 2024, is outside the
        4-year window (2020-2024) -- eligible again."""
        self.assertTrue(is_first_time_home_buyer(2024, own_years=[2015, 2016, 2017, 2018, 2019]))

    def test_a_partner_owned_home_during_the_relationship_disqualifies(self):
        """Acceptance (3): the HBP test counts the partner's home too, when they
        lived in it during the relationship."""
        self.assertFalse(is_first_time_home_buyer(
            2024, own_years=[], partner_years=[2022, 2023], relationship_start_year=2021))

    def test_a_partner_home_OWNED_BEFORE_the_relationship_does_not_disqualify(self):
        """... but a home owned BEFORE they met was never occupied during the
        relationship, so it must not disqualify the buyer. The relationship
        starts in 2024, so no partner year inside the lookback overlaps it.

        (A partner year that DOES overlap the relationship disqualifies even
        where it started before it -- they may have lived there together in the
        overlap year, which is the CRA "in-home" condition; that is the case
        above.)"""
        self.assertTrue(is_first_time_home_buyer(
            2024, own_years=[], partner_years=[2022, 2023], relationship_start_year=2024))

    def test_an_overlapping_partner_year_disqualifies_even_if_it_started_earlier(self):
        self.assertFalse(is_first_time_home_buyer(
            2024, own_years=[], partner_years=[2022, 2023], relationship_start_year=2023))


class TheFhsaWithdrawalRuleIsConsulted(unittest.TestCase):
    """s.146.6(1): a QUALIFYING withdrawal tests the ACCOUNT HOLDER's own history
    only -- a partner's home does not affect it. The rule already existed; this
    pins that it is the one production must consult."""

    def test_own_history_blocks_the_qualifying_withdrawal(self):
        acct = FHSAAccount(balance=20_000, open_year=2015, principal_residence_years=[2021, 2022])
        self.assertFalse(acct.is_first_home_buyer(2024))
        self.assertFalse(acct.qualifying_withdrawal(2024)["eligible"])

    def test_no_history_leaves_it_eligible(self):
        acct = FHSAAccount(balance=20_000, open_year=2015)
        self.assertTrue(acct.qualifying_withdrawal(2024)["eligible"])


class AnIneligibleBuyerIsRefused(unittest.TestCase):
    """(1) The refusal, at the ingestion boundary where the fold reads it."""

    def test_an_ineligible_primary_buyer_is_refused(self):
        doc = _declare_history(_doc(), "p1", [(2015, 2022)])
        with self.assertRaises(ContractAdaptationError) as ctx:
            input_contract.to_internal_config(copy.deepcopy(doc))
        self.assertIn("first-time home buyer", str(ctx.exception).lower())

    def test_the_refusal_names_the_buyer_and_the_years(self):
        doc = _declare_history(_doc(), "p1", [(2015, 2022)])
        with self.assertRaises(ContractAdaptationError) as ctx:
            input_contract.to_internal_config(copy.deepcopy(doc))
        message = str(ctx.exception)
        self.assertIn("2021", message)   # a lookback year it disqualifies
        self.assertIn("p1", message)

    def test_an_eligible_buyer_still_loads(self):
        """Control: the refusal must not fire on a household that qualifies."""
        doc = _declare_history(_doc(), "p1", [(2015, 2019)])
        cfg = input_contract.to_internal_config(copy.deepcopy(doc))
        self.assertEqual(cfg["family"]["first_home_purchases"][0]["buyer"], "p1")

    def test_a_buyer_with_no_declared_history_still_loads(self):
        """(DP#32) Absent history is an ABSENCE, not a disqualification: the
        household has told us nothing, and refusing every undeclared purchase
        would break every ordinary first-time buyer."""
        cfg = input_contract.to_internal_config(copy.deepcopy(_doc()))
        self.assertEqual(cfg["family"]["first_home_purchases"][0]["year"], 2024)


class TheDeclaredHistoryReachesThePurchaseEntry(unittest.TestCase):
    """The declared years must travel with the purchase (they are the buyer's
    eligibility evidence), and an absent declaration must leave no key."""

    def test_history_is_carried_onto_the_mapped_purchase(self):
        doc = _declare_history(_doc(), "p1", [(2015, 2019)])
        cfg = input_contract.to_internal_config(copy.deepcopy(doc))
        entry = cfg["family"]["first_home_purchases"][0]
        self.assertEqual(sorted(entry["prior_residence_years"]), [2015, 2016, 2017, 2018, 2019])

    def test_no_declared_history_leaves_no_key(self):
        cfg = input_contract.to_internal_config(copy.deepcopy(_doc()))
        self.assertNotIn("prior_residence_years", cfg["family"]["first_home_purchases"][0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()