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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()