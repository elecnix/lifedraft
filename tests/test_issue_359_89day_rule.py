"""Issue #359 acceptance test (2), the contained subset: carry the fact, disclose the gap.

ITA s.146.01(2)(a) denies the deduction on an RRSP contribution made less than
90 days before an HBP withdrawal, to the extent the post-withdrawal balance
falls below that contribution. `countries/canada.hbp_rules
.deductible_contribution_before_hbp` implements the rule correctly and is still
re-exported from `countries/canada/__init__.py` with **no production caller** --
so a household that contributes and then withdraws under the HBP is modelled as
fully deductible whatever the timing, and pays less tax than CRA assesses.

This ships the part that can be shipped honestly TODAY:

* a dated, DECLARED RRSP contribution input the contract cannot express today
  (`people[].incomes[]` carries income kinds only, and a contribution is not
  income), carried through to the internal config and to the fold;
* a `model_fidelity` caveat disclosing that a declared contribution inside the
  90-day window is **not yet re-priced** -- absence of the input is not absence
  of the rule, and silence would be the worst outcome.

The re-pricing itself is deliberately NOT here. An independent review traced the
fold ordering and it is unfavourable: `simulation_state.py:2453` runs
`run_rules` (contributions -> rrsp_ledger -> rrsp_deduction, and computes the
year's tax) and the HBP withdrawal happens at `:2508`, 55 lines LATER -- so
`rrsp_balance_after_withdrawal`, the argument the rule needs, does not exist
when the deduction is claimed. Closing that needs either reordering the
first-home step ahead of `run_rules` (blocked: the step reads `ws.new_rrsp_bal`,
which the `contributions` rule produces) or a second pass that re-claims the
deduction and recomputes the year. That wants the whole PR to itself, starting
from a red test that demonstrates the ordering problem -- not a leaf and a call
in the wrong place. (Recorded on issue #359.)

The caveat is pinned in BOTH directions: it fires for a declared in-window
contribution and stays silent otherwise, because a caveat that fires
unconditionally is noise and one that never fires is worse than none.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contract_people
import input_contract
import model_fidelity
from contract_errors import ContractAdaptationError, ContractValidationError
from model_fidelity import Direction, FidelityContext

_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "lifedraft", "minimal-two-adult", "input.json")

CAVEAT_ID = 'rrsp_contribution_in_hbp_window_not_repriced'


def _doc() -> dict:
    with open(_EXAMPLE) as fh:      # filterwarnings=error: an unclosed file IS a failure
        return json.load(fh)


def _declare_contribution(doc: dict, person_id: str, date: str, amount: float) -> dict:
    person = next(p for p in doc["people"] if p["id"] == person_id)
    person.setdefault("rrsp_contributions", []).append(
        {"date": date, "amount": amount})
    return doc


class TheContributionIsDeclaredAndCarried(unittest.TestCase):
    """A dated RRSP contribution is a NEW, separate fact: it must not be folded
    into `ws.p_rrsp`, which is the engine's own model-generated annual allocation
    (min(income, room)) and is not an event with a date."""

    def test_a_declared_contribution_reaches_the_internal_config(self):
        doc = _declare_contribution(_doc(), "p1", "2024-11-15", 8000.0)
        cfg = input_contract.to_internal_config(doc)
        member = next(m for m in cfg["family"]["members"] if m.get("id") == "p1")
        self.assertEqual(member["rrsp_contributions"],
                         [{"date": "2024-11-15", "amount": 8000.0}])

    def test_no_declared_contribution_leaves_no_key(self):
        """DP#32: absence stays absence -- no key, not an empty list standing in
        for a household that said nothing."""
        cfg = input_contract.to_internal_config(_doc())
        member = next(m for m in cfg["family"]["members"] if m.get("id") == "p1")
        self.assertNotIn("rrsp_contributions", member)

    def test_a_negative_amount_is_refused(self):
        with self.assertRaises((ContractValidationError,
                                ContractAdaptationError)):
            input_contract.to_internal_config(
                _declare_contribution(_doc(), "p1", "2024-11-15", -1.0))


def _ctx(*, contributions, first_home_purchases, province="quebec"):
    """A FidelityContext shaped like the surfaces build: the declared
    contributions and the first-home purchase the engine is actually reading."""
    heloc = {"readvanceable": True, "capitalize_interest": True,
              "limit": 150000.0, "rate": 0.0545, "rate_type": "variable",
              "deductibility": {"investment_portion": 0.0, "personal_portion": 1.0},
              "collateral": "principal_residence"}
    cfg = {
        "property": {"heloc": heloc, "heloc_rate": 0.0545, "has_heloc": True},
        "first_home_purchases": first_home_purchases,
        "family": {"members": [{"role": "primary", "rrsp_contributions": contributions}]},
    }
    return FidelityContext(cfg=cfg, objective_name="min_shortfall")


def _active(caveat_id, ctx):
    approx = next((a for a in model_fidelity.all_approximations() if a.id == caveat_id),
                  None)
    return False if approx is None else approx.is_active(ctx)


class MalformedInputIsSurvivable(unittest.TestCase):
    """The defensive branches are reachable, not dead.

    A hand-built internal config -- or a future mapper -- can put anything in
    these fields, and the caveat is read on every surface. Every branch here
    returns a DEFINED answer rather than raising, because a caveat that crashes
    the report is worse than one that stays quiet.
    """

    def test_an_unparseable_date_is_not_reported(self):
        """A contribution with no usable date cannot be placed in the window, so
        the engine cannot support the claim -- staying silent is the honest
        answer, and it must not raise."""
        ctx = _ctx(contributions=[{"amount": 8000.0}],            # no date at all
                   first_home_purchases=[{"buyer": "p1", "year": 2025}])
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_a_malformed_date_is_not_reported(self):
        ctx = _ctx(contributions=[{"date": "not-a-date", "amount": 8000.0}],
                   first_home_purchases=[{"buyer": "p1", "year": 2025}])
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_a_non_numeric_purchase_year_is_skipped(self):
        ctx = _ctx(contributions=[{"date": "2024-11-15", "amount": 8000.0}],
                   first_home_purchases=[{"buyer": "p1", "year": "soon"}])
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_a_purchase_with_no_year_is_skipped(self):
        ctx = _ctx(contributions=[{"date": "2024-11-15", "amount": 8000.0}],
                   first_home_purchases=[{"buyer": "p1"}])
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_junk_entries_do_not_raise(self):
        """Junk must never raise -- and a junk entry must not MASK a real one:
        the valid dated contribution beside it still puts us in the window."""
        self.assertFalse(_active(CAVEAT_ID, _ctx(
            contributions=["not a dict"], first_home_purchases=[{"buyer": "p1", "year": 2025}])))
        self.assertFalse(_active(CAVEAT_ID, _ctx(contributions=None,
                                                   first_home_purchases=None)))
        self.assertTrue(_active(CAVEAT_ID, _ctx(
            contributions=[{"date": "2024-11-15", "amount": 1}, "not a dict"],
            first_home_purchases=[{"buyer": "p1", "year": 2025}])),)

    def test_a_non_dict_config_does_not_raise(self):
        self.assertFalse(_active(CAVEAT_ID, FidelityContext(cfg="not a dict")))

    def test_a_config_with_no_family_block_does_not_raise(self):
        ctx = FidelityContext(cfg={"property": {"heloc": {"readvanceable": True},
                                              "has_heloc": True},
                                     "first_home_purchases": [{"buyer": "p1", "year": 2025}]})
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_a_family_block_without_a_members_list_does_not_raise(self):
        ctx = FidelityContext(cfg={"property": {"heloc": {"readvanceable": True},
                                              "has_heloc": True},
                                     "first_home_purchases": [{"buyer": "p1", "year": 2025}],
                                     "family": {"members": "not a list"}})
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_a_non_dict_member_does_not_raise(self):
        ctx = FidelityContext(cfg={"property": {"heloc": {"readvanceable": True},
                                              "has_heloc": True},
                                     "first_home_purchases": [{"buyer": "p1", "year": 2025}],
                                     "family": {"members": ["not a dict"]}})
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_one_unusable_date_does_not_hide_a_usable_one(self):
        """Two contributions in the same window, one of them undated: the dated
        one is still placed, so the caveat still fires. An unusable entry must
        skip, not abort the whole scan."""
        ctx = _ctx(contributions=[{"amount": 8000.0},                      # no date
                                  {"date": "2024-11-15", "amount": 1}],  # usable
                   first_home_purchases=[{"buyer": "p1", "year": 2025}])
        self.assertTrue(_active(CAVEAT_ID, ctx))

    def test_a_junk_purchase_entry_does_not_raise(self):
        ctx = _ctx(contributions=[{"date": "2024-11-15", "amount": 8000.0}],
                   first_home_purchases=["not a dict"])
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_a_context_with_no_family_block_does_not_raise(self):
        self.assertFalse(_active(CAVEAT_ID, FidelityContext()))


class ANegativeContributionIsRefusedAtTheBoundary(unittest.TestCase):
    """Defence in depth, and the schema is the first gate.

    `money` rejects a negative amount, so the CONTRACT path never reaches the
    mapper's own check -- which is the right order (the first gate validates).
    The mapper guard exists for the path that can bypass validation, so this
    drives the mapper directly: a hand-built internal config carrying a negative
    contribution must still be refused, not carried into the fold.
    """

    def test_the_schema_refuses_a_negative_amount_first(self):
        with self.assertRaises(ContractValidationError):
            input_contract.to_internal_config(
                _declare_contribution(_doc(), "p1", "2024-11-15", -500.0))

    def test_the_mapper_refuses_it_too_when_validation_is_bypassed(self):
        from contract_people import _map_member
        doc = _declare_contribution(_doc(), "p1", "2024-11-15", -500.0)
        person = next(p for p in doc["people"] if p["id"] == "p1")
        with self.assertRaises(ContractAdaptationError) as ctx:
            _map_member(doc, "p1", "primary", person)
        self.assertIn("negative", str(ctx.exception).lower())


class TheUnrepricedWindowIsDisclosed(unittest.TestCase):
    """The caveat fires exactly when a declared contribution sits inside the
    90-day window before a modelled HBP withdrawal."""

    def test_it_fires_for_a_contribution_inside_the_window(self):
        # The purchase must come AFTER the contribution: a 2024-11-15
        # contribution with a purchase in 2024 would place the withdrawal
        # before it. The realistic shape is a contribution late in the year and a
        # purchase early the next.
        ctx = _ctx(contributions=[{"date": "2024-11-15", "amount": 8000.0}],
                   first_home_purchases=[{"buyer": "p1", "year": 2025}])
        self.assertTrue(_active(CAVEAT_ID, ctx),
                        "a declared contribution inside the HBP window is not "
                        "re-priced; silence would be the worst outcome")

    def test_it_stays_silent_with_no_declared_contribution(self):
        ctx = _ctx(contributions=None, first_home_purchases=[{"buyer": "p1", "year": 2024}])
        self.assertFalse(_active(CAVEAT_ID, ctx),
                         "no declared contribution means nothing to disclose")

    def test_it_stays_silent_for_a_contribution_well_before_the_window(self):
        """The complement: a contribution long before the withdrawal is outside
        s.146.01(2)(a) and is fully deductible, so there is nothing to disclose."""
        ctx = _ctx(contributions=[{"date": "2024-01-15", "amount": 8000.0}],
                   first_home_purchases=[{"buyer": "p1", "year": 2026}])
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_it_stays_silent_without_an_hbp_purchase(self):
        """No HBP, no 90-day rule, no caveat."""
        ctx = _ctx(contributions=[{"date": "2024-11-15", "amount": 8000.0}],
                   first_home_purchases=None)
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_it_says_which_way_it_biases(self):
        approx = next(a for a in model_fidelity.all_approximations()
                      if a.id == CAVEAT_ID)
        # The deduction is NOT applied, so the taxable income feeding it is too
        # LOW and the tax they PAY is too HIGH: OVERSTATES, naming the tax.
        self.assertEqual(approx.direction, Direction.OVERSTATES)
        self.assertIn("tax PAID", approx.biased_figure)
        self.assertNotIn("UNDERSTATED --", approx.biased_figure,
                         "one figure, one direction: the taxable-income "
                         "consequence belongs in the detail, not as a second "
                         "figure with the opposite sign in the headline")
        self.assertEqual(approx.issue, '#359')
        self.assertTrue(approx.biased_figure)


class AnImpossibleDateIsNotAUsableDate(unittest.TestCase):
    """A date that is int-parsable but not a real calendar date.

    `_declared_date` split "2024-13-01" and called `int()` on all three parts.
    Month 13 parses fine, so the tuple `(2024, 13, 1)` escaped the handler, and
    the `date(*made)` call in the predicate raised ValueError on it.

    That raise never reached a report -- `is_active` fails OPEN on purpose
    (DP#32) -- so the observed defect was not a crash. It was that the caveat
    returned True for ANY malformed date: a targeted disclosure turned into a
    false alarm on every household carrying one, and it reported a gap that
    was not there. These tests pin the honest reading -- no calendar day is
    named, so it cannot be placed in a window, so the entry is unusable.
    """

    def test_the_helper_rejects_an_impossible_month(self):
        self.assertIsNone(model_fidelity._declared_date("2024-13-01"))

    def test_the_helper_rejects_an_impossible_day(self):
        self.assertIsNone(model_fidelity._declared_date("2024-02-31"))

    def test_the_caveat_does_not_raise_on_an_impossible_date(self):
        ctx = _ctx(contributions=[{"date": "2024-13-01", "amount": 8000.0}],
                   first_home_purchases=[{"buyer": "p1", "year": 2025}])
        self.assertFalse(_active(CAVEAT_ID, ctx))

    def test_an_impossible_date_does_not_hide_a_usable_one(self):
        """The unusable entry is skipped; the usable one still gets reported."""
        ctx = _ctx(contributions=[{"date": "2024-13-01", "amount": 8000.0},
                                  {"date": "2024-12-20", "amount": 8000.0}],
                   first_home_purchases=[{"buyer": "p1", "year": 2025}])
        self.assertTrue(_active(CAVEAT_ID, ctx))

    def test_a_real_date_still_parses(self):
        self.assertEqual(model_fidelity._declared_date("2024-12-20"),
                         (2024, 12, 20))


class ANonNumericContributionAmountIsRefused(unittest.TestCase):
    """DP#32 at the boundary: a mistyped amount is a loud refusal, not a crash.

    Measured before the fix, at the MAPPER (which is the layer that can reach
    these -- the schema types `amount` as money and requires `date`, so the
    contract path never gets there):

        amount="eight thousand" -> ValueError: could not convert string to float
        amount=None            -> TypeError: float() argument must be ...
        an entry with no 'date' -> KeyError: 'date'

    The tests call `contract_people._map_member` directly for exactly that
    reason. Driving `to_internal_config` instead would have passed on the
    schema's refusal and proved nothing about this guard -- which is the trap
    AGENTS.md warns about in "reimplementing the engine in the test", in its
    mirror image: passing on someone else's guard.
    """

    def _map(self, amount, *, date="2024-12-20"):
        doc = _doc()
        _declare_contribution(doc, "p1", date, amount)
        person = next(p for p in doc["people"] if p["id"] == "p1")
        return contract_people._map_member(doc, "p1", "primary", person)

    def _map_raw(self, entry):
        """An entry that never became schema-valid in the first place."""
        doc = _doc()
        person = next(p for p in doc["people"] if p["id"] == "p1")
        person["rrsp_contributions"] = [entry]
        return contract_people._map_member(doc, "p1", "primary", person)

    def test_a_non_numeric_amount_is_refused_not_crashed(self):
        with self.assertRaises(ContractAdaptationError) as caught:
            self._map("eight thousand")
        message = str(caught.exception)
        self.assertIn("p1", message)
        self.assertIn("rrsp_contributions[0]", message)

    def test_a_none_amount_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            self._map(None)

    def test_a_missing_date_is_refused_not_a_keyerror(self):
        """The refusal message quotes the date. A missing one must be refused,
        not raise KeyError while building the message that would explain it."""
        with self.assertRaises(ContractAdaptationError) as caught:
            self._map_raw({"amount": 8000.0})
        self.assertIn("date", str(caught.exception))

    def test_a_missing_amount_is_refused_not_a_keyerror(self):
        """The mirror of the missing-date case, and the one gap left when the
        other five shapes were closed: `float(contribution["amount"])` indexed
        the key, and `except (TypeError, ValueError)` does not catch KeyError.
        Caught by Cite on the commit that added the guard."""
        with self.assertRaises(ContractAdaptationError) as caught:
            self._map_raw({"date": "2024-12-20"})
        self.assertIn("amount", str(caught.exception))

    def test_a_non_object_entry_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            self._map_raw("8000")

    def test_the_refusal_names_the_offending_entry(self):
        """A list of contributions is a list of separate assertions: the second
        bad one must not be blamed on the first."""
        doc = _doc()
        person = next(p for p in doc["people"] if p["id"] == "p1")
        person["rrsp_contributions"] = [
            {"date": "2024-12-20", "amount": 8000.0},
            {"date": "2024-12-21", "amount": "eight thousand"},
        ]
        with self.assertRaises(ContractAdaptationError) as caught:
            contract_people._map_member(doc, "p1", "primary", person)
        self.assertIn("rrsp_contributions[1]", str(caught.exception))

    def test_a_valid_amount_still_maps(self):
        member = self._map(8000.0)
        self.assertEqual(member["rrsp_contributions"],
                         [{"date": "2024-12-20", "amount": 8000.0}])


class TheContractPathIsAlreadyRefusedByTheSchema(unittest.TestCase):
    """Why the mapper guard above is defence in depth, not the primary guard.

    Recorded so the mapper tests are not mistaken for the only coverage, and so
    nobody 'simplifies' the mapper on the belief that it carries the check.
    """

    def test_the_schema_refuses_each_of_them_first(self):
        for amount in ("eight thousand", None, -1.0):
            with self.subTest(amount=amount):
                doc = _doc()
                _declare_contribution(doc, "p1", "2024-12-20", amount)
                with self.assertRaises(ContractValidationError):
                    input_contract.to_internal_config(doc)


class TheCaveatNeedsAModelledWithdrawal(unittest.TestCase):
    """It must not fire for a HELOC that funds no HBP withdrawal.

    Only a READVANCEABLE line funds the sleeve, so only a readvanceable line
    makes the engine perform a withdrawal whose 90-day rule goes unpriced. A
    bare `has_heloc` is not that: with one, the caveat claimed a gap for a
    household that never had an HBP withdrawal at all.
    """

    def _ctx_heloc(self, readvanceable):
        heloc = {"readvanceable": readvanceable, "capitalize_interest": True,
                 "limit": 150000.0, "rate": 0.0545, "rate_type": "variable",
                 "deductibility": {"investment_portion": 0.0,
                                   "personal_portion": 1.0},
                 "collateral": "principal_residence"}
        cfg = {
            "property": {"heloc": heloc, "heloc_rate": 0.0545,
                         "has_heloc": True},
            "first_home_purchases": [{"buyer": "p1", "year": 2025}],
            "family": {"members": [{"role": "primary",
                                    "rrsp_contributions": [
                                        {"date": "2024-12-20",
                                         "amount": 8000.0}]}]},
        }
        return FidelityContext(cfg=cfg, objective_name="min_shortfall")

    def test_a_heloc_that_cannot_fund_the_sleeve_does_not_fire(self):
        self.assertFalse(_active(CAVEAT_ID, self._ctx_heloc(False)))

    def test_a_readvanceable_line_still_fires(self):
        self.assertTrue(_active(CAVEAT_ID, self._ctx_heloc(True)))


class TheNegativeGuardIsExercisedAtTheMapper(unittest.TestCase):
    """Cite's question, and it had teeth: the negative refusal was only
    reachable through the schema, so the mapper's own `amount < 0.0` branch had
    no direct test. Driven at `_map_member`, like its neighbours, so relaxing
    the schema's money type later cannot quietly unrefuse a negative."""

    def test_a_negative_amount_is_refused_by_the_mapper_itself(self):
        doc = _doc()
        _declare_contribution(doc, "p1", "2024-12-20", -1.0)
        person = next(p for p in doc["people"] if p["id"] == "p1")
        with self.assertRaises(ContractAdaptationError) as caught:
            contract_people._map_member(doc, "p1", "primary", person)
        self.assertIn("cannot be negative", str(caught.exception))


class TheCaveatNeedsAModelledWithdrawal(unittest.TestCase):
    """It must not fire for a HELOC that funds no HBP withdrawal.

    Only a READVANCEABLE line funds the sleeve, so only a readvanceable line
    makes the engine perform a withdrawal whose 90-day rule goes unpriced. A
    bare `has_heloc` is not that: with one, the caveat claimed a gap for a
    household that never had an HBP withdrawal at all.
    """

    def _ctx_heloc(self, readvanceable):
        heloc = {"readvanceable": readvanceable, "capitalize_interest": True,
                 "limit": 150000.0, "rate": 0.0545, "rate_type": "variable",
                 "deductibility": {"investment_portion": 0.0,
                                   "personal_portion": 1.0},
                 "collateral": "principal_residence"}
        return _ctx(contributions=[{"date": "2024-12-20", "amount": 8000.0}],
                    first_home_purchases=[{"buyer": "p1", "year": 2025}]) \
            .__class__(cfg={
                "property": {"heloc": heloc, "heloc_rate": 0.0545,
                             "has_heloc": True},
                "first_home_purchases": [{"buyer": "p1", "year": 2025}],
                "family": {"members": [{"role": "primary",
                                        "rrsp_contributions": [
                                            {"date": "2024-12-20",
                                             "amount": 8000.0}]}]}},
                objective_name="min_shortfall")

    def test_a_heloc_that_cannot_fund_the_sleeve_does_not_fire(self):
        self.assertFalse(_active(CAVEAT_ID, self._ctx_heloc(False)))

    def test_a_readvanceable_line_still_fires(self):
        self.assertTrue(_active(CAVEAT_ID, self._ctx_heloc(True)))


class TheNegativeGuardIsExercisedAtTheMapper(unittest.TestCase):
    """Cite's question, and it had teeth: the negative refusal was only
    reachable through the schema, so the mapper's own `amount < 0.0` branch had
    no direct test. Driven at `_map_member`, like its neighbours, so relaxing
    the schema's money type later cannot quietly unrefuse a negative."""

    def test_a_negative_amount_is_refused_by_the_mapper_itself(self):
        doc = _doc()
        _declare_contribution(doc, "p1", "2024-12-20", -1.0)
        person = next(p for p in doc["people"] if p["id"] == "p1")
        with self.assertRaises(ContractAdaptationError) as caught:
            contract_people._map_member(doc, "p1", "primary", person)
        self.assertIn("cannot be negative", str(caught.exception))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
