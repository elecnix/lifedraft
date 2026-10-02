"""Issue #359: a household must be able to say how much to withdraw under the HBP.

``simulation_state._apply_first_home_to_account`` always withdrew
``min(rrsp_balance, HBP_MAX_WITHDRAWAL)``. $60,000 is CRA's MAXIMUM, not a fixed
amount, so any buyer whose RRSP exceeded what the down payment needed was
over-withdrawn: overstated tax-sheltered money out of the RRSP, an overstated
repayment obligation, and understated RRSP growth across the repayment years.
The contract had no way to say otherwise -- ``first_home_purchase`` was
``additionalProperties: false`` with only ``buyer`` and ``year``.

This covers acceptance test (1) of #359 -- the declared amount is honoured
exactly, and an amount the buyer's own RRSP (or the statutory maximum) cannot
cover is REFUSED rather than silently clipped (DP#32) -- plus acceptance test
(3): a document declaring NO ``hbp_amount`` leaves the fold byte-identical, so
the golden invariant cannot move. Acceptance test (2), the 89-day contribution
rule, needs a DATED RRSP contribution the schema does not yet carry and is out
of scope here (see the PR body).

Where each claim is proven, and why:

* the WITHDRAWAL ARITHMETIC is a unit test of the pure step
  (``_apply_first_home_to_account``), which is a legitimate target under DP#11
  -- it is the one place both the child and adult folds call (DP#9). The issue
  itself measured it that way;
* the MAPPING is proven through the real loading boundary
  (``input_contract.to_internal_config``);
* the EFFECT is proven relationally on the trajectory: a smaller declared
  withdrawal leaves MORE in the buyer's RRSP than the historical maximum, and a
  larger one leaves less. A snapshot would only pin whichever number happens to
  be right today.

No hand-built engine state for an engine-behaviour claim (DP#11); figures
fabricated, role-based ids (DP#4/DP#15).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract
from contract_errors import ContractAdaptationError
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from simulation_state import _apply_first_home_to_account
from countries.canada.adapter import CanadaAdapter
from countries.canada.hbp_rules import HBP_MAX_WITHDRAWAL

_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "lifedraft", "minimal-two-adult", "input.json")


def _acc(rrsp: float) -> dict:
    """A member's account dict in the flat shape the first-home step takes."""
    return {"fhsa_balance": 0.0, "fhsa_lifetime_remaining": 0.0,
            "rrsp_balance": rrsp, "non_reg_balance": 0.0, "non_reg_acb": 0.0}


def _doc() -> dict:
    with open(_EXAMPLE) as fh:
        return json.load(fh)


def _declare(doc: dict, **purchase) -> dict:
    doc["first_home_purchases"] = [{"buyer": "p1", "year": 2028, **purchase}]
    return doc


def _map(doc: dict):
    return input_contract.to_internal_config(copy.deepcopy(doc))


def _run(doc: dict):
    cfg = _map(doc)
    config = SimulationConfig.from_dict(cfg)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return FamilySimulation(config, adapter=CanadaAdapter(config)).run()


class TheStepHonoursADeclaredAmount(unittest.TestCase):
    """(1) The declared amount is withdrawn exactly, from the buyer's own RRSP."""

    def test_thirty_thousand_leaves_thirty_five_thousand_and_repays_two(self):
        """The issue's worked example: $65,000 RRSP, declare $30,000."""
        out = _apply_first_home_to_account(_acc(65_000.0), True, 2026, 30_000.0)
        self.assertEqual(out["hbp"]["withdrawal"], 30_000.0)
        self.assertEqual(out["rrsp_balance"], 35_000.0)
        self.assertEqual(out["non_reg_balance"], 30_000.0)   # down payment
        # The schedule follows the amount: ~$2,000/yr, not the maximum's ~$4,000.
        first_payment = out["hbp"]["repayment_schedule"][0]["actual_payment"]
        self.assertAlmostEqual(first_payment, 2_000.0, delta=50.0)

    def test_the_pre_five_hundred_thousand_default_when_nothing_is_declared(self):
        """(3) No declared amount == the historical min(RRSP, $60k)."""
        out = _apply_first_home_to_account(_acc(65_000.0), True, 2026)
        self.assertEqual(out["hbp"]["withdrawal"], HBP_MAX_WITHDRAWAL)
        self.assertEqual(out["rrsp_balance"], 65_000.0 - HBP_MAX_WITHDRAWAL)

    def test_a_small_rrsp_still_caps_at_the_balance_when_nothing_is_declared(self):
        """(3) The other historical branch: min() against a small balance."""
        out = _apply_first_home_to_account(_acc(15_000.0), True, 2026)
        self.assertEqual(out["hbp"]["withdrawal"], 15_000.0)


class OutOfRangeAmountIsRefused(unittest.TestCase):
    """(1, DP#32) An amount the buyer cannot cover is REFUSED, never clipped."""

    def test_above_the_statutory_maximum_is_refused(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            _apply_first_home_to_account(_acc(65_000.0), True, 2026,
                                          HBP_MAX_WITHDRAWAL + 1.0)
        self.assertIn(str(int(HBP_MAX_WITHDRAWAL)), str(ctx.exception))

    def test_above_the_buyers_own_rrsp_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            _apply_first_home_to_account(_acc(15_000.0), True, 2026, 20_000.0)

    def test_a_negative_amount_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            _apply_first_home_to_account(_acc(65_000.0), True, 2026, -1.0)

    def test_the_error_names_the_buyer_facing_numbers(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            _apply_first_home_to_account(_acc(15_000.0), True, 2026, 20_000.0)
        message = str(ctx.exception)
        self.assertIn("20000", message.replace(",", ""))


class NonNumericAmountIsRefusedNotCrashed(unittest.TestCase):
    """A review finding: ``float("abc")`` raised a bare ``ValueError`` out of the
    step, which reads as a crash rather than a refusal.

    On the CONTRACT path the schema's ``type: number`` already rejects a
    non-numeric amount, so this is the hand-built-internal-config path -- and
    there it should refuse with the same loud, naming shape as every other
    impossible declaration (DP#32), not raise a conversion error.
    """

    def test_a_non_numeric_amount_is_refused_at_the_step(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            _apply_first_home_to_account(_acc(65_000.0), True, 2026, "abc")
        self.assertIn("abc", str(ctx.exception))

    def test_the_schema_still_refuses_it_on_the_contract_path(self):
        from contract_errors import ContractValidationError
        with self.assertRaises((ContractValidationError, ContractAdaptationError)):
            _run(_declare(_doc(), hbp_amount="abc"))


class ADeclaredAmountAboveAnEmptyPrrpIsRefused(unittest.TestCase):
    """A declared amount the buyer's pot cannot cover is REFUSED, not clipped.

    This is a decision, pinned so it is not accidental: the ceiling is
    ``min(RRSP, $60,000)``, so a declared amount above an empty or drained pot is
    refused with both numbers in the message, and the household is told what they
    can actually take. Clipping instead would silently understate the down
    payment AND overstate the RRSP left sheltered -- the exact failure the leaf
    exists to prevent (DP#32). A household whose pot is genuinely empty should
    omit the leaf, which withdraws nothing by design.
    """

    def test_declaring_more_than_an_empty_pot_refuses(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            _apply_first_home_to_account(_acc(0.0), True, 2026, 60_000.0)
        message = str(ctx.exception)
        # The message names BOTH numbers the household needs: what they declared
        # and what the pot can actually carry.
        self.assertIn("60000", message.replace(",", "").replace(".0", ""))
        self.assertIn("0.00", message)

    def test_omitting_the_leaf_on_an_empty_pot_withdraws_nothing(self):
        """The contrast: the historical default on an empty pot is a NO-OP of 0,
        which is correct -- there is nothing to withdraw."""
        out = _apply_first_home_to_account(_acc(0.0), True, 2026)
        self.assertEqual(out["hbp"]["withdrawal"], 0.0)
        self.assertEqual(out["rrsp_balance"], 0.0)


class ADuplicatePurchaseIsRefused(unittest.TestCase):
    """Two entries for the SAME buyer and year are ambiguous, and the fold
    resolves them by taking the LAST declared amount -- so the earlier figure
    would silently vanish (DP#32: a dropped value must fail loudly)."""

    def test_two_entries_for_one_buyer_and_year_are_refused(self):
        doc = _doc()
        doc["first_home_purchases"] = [
            {"buyer": "p1", "year": 2028, "hbp_amount": 10_000.0},
            {"buyer": "p1", "year": 2028, "hbp_amount": 50_000.0},
        ]
        with self.assertRaises(ContractAdaptationError) as ctx:
            _map(doc)
        message = str(ctx.exception)
        self.assertIn("TWO entries", message)
        self.assertIn("2028", message)

    def test_the_same_buyer_in_different_years_is_fine(self):
        """The check is (buyer, year) -- buying again later is a different
        purchase, not a duplicate entry."""
        doc = _doc()
        doc["first_home_purchases"] = [
            {"buyer": "p1", "year": 2028, "hbp_amount": 10_000.0},
            {"buyer": "p1", "year": 2035, "hbp_amount": 20_000.0},
        ]
        cfg = _map(doc)
        self.assertEqual(len(cfg["family"]["first_home_purchases"]), 2)


class ImpossibleAmountsAreRefusedAtTheBoundary(unittest.TestCase):
    """Review findings on #397: a refusal raised inside the per-year fold can be
    SWALLOWED -- the optimizer wraps strategy evaluation in
    ``except Exception: score = -inf``, so a doomed declaration could rank a
    strategy last instead of being reported. Every statically-decidable
    impossibility is therefore refused at INGESTION, where it cannot be lost.

    (The OTHER ceiling -- the buyer's own LIVE RRSP balance -- is only knowable in
    the fold, so that check stays there, covered by
    ``OutOfRangeAmountIsRefused.test_above_the_buyers_own_rrsp_is_refused``.)
    """

    def test_a_nan_amount_is_refused(self):
        """NaN passes EVERY numeric comparison -- ``nan < 0`` and
        ``nan > ceiling`` are both False -- so it previously sailed through the
        fold's guards and landed as a NaN RRSP balance that propagated into the
        down payment and every downstream total (DP#32)."""
        with self.assertRaises(ContractAdaptationError):
            _run(_declare(_doc(), hbp_amount=float("nan")))

    def test_the_fold_also_refuses_a_non_finite_amount_called_directly(self):
        """Belt-and-braces: ingestion refuses NaN/inf, but the step is also
        public and an INTERNAL config can be built by hand (every test that does
        so bypasses the boundary). Without this guard a hand-built config would
        carry a NaN into the down payment exactly as the contract path did."""
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(ContractAdaptationError):
                _apply_first_home_to_account(_acc(65_000.0), True, 2026, bad)

    def test_an_infinite_amount_is_refused(self):
        """inf would otherwise drive the sleeve-style growth clamp to a zeroed
        pot rather than being refused."""
        with self.assertRaises(ContractAdaptationError):
            _run(_declare(_doc(), hbp_amount=float("inf")))

    def test_an_amount_above_the_statutory_maximum_is_refused_at_ingestion(self):
        """Pinned at the MAPPING boundary, not merely "somewhere": a refusal
        raised inside the per-year fold can be swallowed by the optimizer's
        ``except Exception: score = -inf``, which would rank the strategy last
        instead of reporting the bad declaration. Asserting through ``_run``
        would pass either way, because the fold refuses too -- so this calls
        ``to_internal_config`` directly, which never reaches a fold."""
        with self.assertRaises(ContractAdaptationError) as ctx:
            _map(_declare(_doc(), hbp_amount=70_000.0))
        self.assertIn(str(int(HBP_MAX_WITHDRAWAL)), str(ctx.exception).replace(",", ""))

    def test_a_declared_zero_is_honoured_not_treated_as_absent(self):
        """DP#32's zero trap, checked explicitly: ``hbp_amount: 0`` means the
        household wants NO HBP withdrawal, and must not be read as an absent leaf
        that silently becomes min(RRSP, $60,000)."""
        entry = _map(_declare(_doc(), hbp_amount=0.0))["family"]["first_home_purchases"][0]
        self.assertIn("hbp_amount", entry, "a declared 0.0 must stay a present key")
        self.assertEqual(entry["hbp_amount"], 0.0)

        out = _apply_first_home_to_account(_acc(65_000.0), True, 2026, 0.0)
        self.assertEqual(out["hbp"]["withdrawal"], 0.0)
        self.assertEqual(out["rrsp_balance"], 65_000.0,
                         "a declared 0.0 must leave the whole RRSP alone")

    def test_a_declared_zero_leaves_more_in_the_rrsp_than_the_default(self):
        """... and the observable consequence, so the test above cannot pass while
        the fold quietly substituted the maximum."""
        config = SimulationConfig.from_dict(_map(_declare(_doc())))
        # YearResult.year is the 1-indexed OFFSET from the projection start, so
        # the purchase year (2028) sits at offset 3 when start_year is 2026 --
        # i.e. index 3 - 1. Reading index (2028 - start_year) == 2 is the year
        # AFTER the purchase; the comparison still held there, which is exactly
        # why a test can pass while measuring the wrong year.
        offset = 2028 - config.start_year + 1
        zero_row = _run(_declare(_doc(), hbp_amount=0.0))[offset - 1]
        default_row = _run(_declare(_doc()))[offset - 1]
        self.assertEqual(zero_row.year, offset,
                         "the row read must BE the purchase year")
        self.assertGreater(zero_row.primary_rrsp, default_row.primary_rrsp,
                           "hbp_amount: 0 was replaced by the min(RRSP, $60k) default")


class TheContractCarriesTheAmount(unittest.TestCase):
    """The mapping: a declared amount reaches the engine, an absent one leaves
    no key behind (DP#32: absence is not a value to default)."""

    def test_a_declared_amount_is_mapped_through(self):
        cfg = _map(_declare(_doc(), hbp_amount=30000.0))
        self.assertEqual(cfg["family"]["first_home_purchases"][0]["hbp_amount"], 30000.0)

    def test_no_declared_amount_leaves_no_key(self):
        cfg = _map(_declare(_doc()))
        self.assertNotIn("hbp_amount", cfg["family"]["first_home_purchases"][0])


class TheAmountReachesTheTrajectory(unittest.TestCase):
    """Relational effect: a smaller declared withdrawal leaves MORE in the
    buyer's RRSP than the historical maximum, and a larger one leaves less."""

    #: The declared purchase year, as the engine's own index. ``YearResult.year``
    #: is the 1-indexed offset from the projection start, NOT the calendar year
    #: (same convention decumulation.summarize_drawdown_shortfall documents), so
    #: the fold's own ``start_year`` gives the index to read.
    _PURCHASE_YEAR = 2028

    def _rrsp_in_purchase_year(self, results):
        config = SimulationConfig.from_dict(_map(_declare(_doc())))
        index = self._PURCHASE_YEAR - config.start_year + 1
        return results[index - 1].primary_rrsp

    def test_a_smaller_withdrawal_leaves_more_in_the_rrsp(self):
        small = self._rrsp_in_purchase_year(_run(_declare(_doc(), hbp_amount=30_000.0)))
        default = self._rrsp_in_purchase_year(_run(_declare(_doc())))
        self.assertGreater(small, default,
                           "declaring $30,000 must leave more in the RRSP than "
                           "the historical $60,000 maximum")

    def test_the_declared_amount_is_the_one_that_moves_the_pot(self):
        small = self._rrsp_in_purchase_year(_run(_declare(_doc(), hbp_amount=30_000.0)))
        large = self._rrsp_in_purchase_year(_run(_declare(_doc(), hbp_amount=50_000.0)))
        self.assertGreater(small, large)


class ANonNumericAmountIsRefusedAtIngestion(unittest.TestCase):
    """Cite's finding, and it was real.

    `_apply_first_home_to_account` already refused a non-numeric amount, and
    `test_a_non_numeric_amount_is_refused_at_the_step` pins that. But the
    INGESTION mapper is a different function, and `math.isfinite` there raised a
    bare TypeError on a string, a null, a list or an object -- so the household
    got a traceback instead of a sentence.
    """

    def _ingest(self, value):
        # imported here, not at module scope: this mapper is not part of the
        # module's existing public surface and a sibling test imports its
        # symbols the same way
        from contract_transfers import _map_first_home_purchases
        doc = copy.deepcopy(_doc())
        doc["first_home_purchases"] = [
            {"buyer": "p1", "year": 2026, "hbp_amount": value}
        ]
        adults = {p["id"] for p in doc["people"]}
        return _map_first_home_purchases(doc, set(), adults)

    def test_a_numeric_amount_still_ingests(self):
        """The control: refusing must not break the ordinary declared amount."""
        self.assertIsInstance(self._ingest(30000.0), list)

    def test_a_declared_zero_still_ingests(self):
        """Zero is a value, not an absence -- it must survive (DP#32)."""
        self.assertIsInstance(self._ingest(0), list)

    def test_a_string_amount_is_refused(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            self._ingest("abc")
        self.assertIn("abc", str(ctx.exception))

    def test_a_null_amount_is_refused(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            self._ingest(None)
        self.assertIn("None", str(ctx.exception))

    def test_a_list_amount_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            self._ingest([])

    def test_an_object_amount_is_refused(self):
        with self.assertRaises(ContractAdaptationError):
            self._ingest({"amount": 1})

    def test_a_boolean_amount_is_refused(self):
        """bool IS an int in Python, so `true` would become a withdrawal of
        exactly $1.00 -- the quietest way to print a wrong number."""
        for value in (True, False):
            with self.assertRaises(ContractAdaptationError):
                self._ingest(value)

    def test_the_refusal_names_the_buyer_and_year(self):
        with self.assertRaises(ContractAdaptationError) as ctx:
            self._ingest("abc")
        message = str(ctx.exception)
        self.assertIn("p1", message)
        self.assertIn("2026", message)
if __name__ == "__main__":  # pragma: no cover
    unittest.main()