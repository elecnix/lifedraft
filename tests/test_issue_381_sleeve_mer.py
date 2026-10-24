"""Issue #381: the Smith-Manoeuvre sleeve needs its own DECLARABLE fee.

#316 applied a declared ``mer`` / ``expected_return`` to the **non-registered
account** and deliberately stopped the sleeve (``ws.new_sm_investment``) from
inheriting that account's fee -- correct, since the sleeve is a separate pot and
another account's fee is not its fee. But the sleeve then compounded at the
**unshifted** shared taxable rate with **no fee of its own**, and the contract
had no input to declare one. A borrowed-to-invest sleeve held in funds or ETFs
pays a MER like any other holding; compounding it fee-free overstates terminal
assets and the SM's ranked benefit, and the error GROWS with the horizon. That
is the optimistic-bias class.

The fix follows #1036's precedent for this exact liability: a heloc
declaration is now READ (mapped to ``SimulationConfig.sm_investment_mer`` and
netted off the sleeve's growth at its full rate, the #291 per-account
convention) or REFUSED LOUDLY when nonsensical -- never silently dropped, and
never a silent zero. Absent declaration leaves the sleeve growing exactly as
before, so the golden invariant cannot move (DP#32).

The two properties the issue asks for are pinned here:

* a DECLARED sleeve fee reduces the sleeve's terminal value by the compounded
  fee -- proven on two REAL trajectories, not by re-deriving the growth;
* the non-registered account's own MER still does NOT leak into the sleeve
  (#316's sabotage stays green): the two fees are separate facts, and charging
  one pot the other's fee is the bug this issue is about.

Engine-driven throughout (``FamilySimulation.run()`` on the real contract), no
hand-built engine state (DP#11). Fabricated round figures, role-based ids
(DP#4/DP#15).
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
from countries.canada.adapter import CanadaAdapter

_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "lifedraft", "minimal-two-adult", "input.json")


def _doc(*, sleeve_mer=None, non_reg_mer=None) -> dict:
    """The shipped two-adult example, with its HELOC declared readvanceable so
    the Smith-Manoeuvre sleeve actually opens -- money BORROWED TO INVEST, the
    pot #381 is about. ``sleeve_mer`` sets the line's own ``investment_mer``;
    ``non_reg_mer`` sets the non-reg ACCOUNT's declared fee, which must NOT
    reach the sleeve (#316)."""
    with open(_EXAMPLE) as fh:   # this repo runs filterwarnings=error (an unclosed file is an error)
        doc = json.load(fh)
    for liability in doc["liabilities"]:
        if liability["kind"] == "heloc":
            liability["readvanceable"] = True
            if sleeve_mer is not None:
                liability["investment_mer"] = sleeve_mer
    if non_reg_mer is not None:
        for product in doc["assumptions"]["products"].values():
            if product.get("category") == "global_equity_index":
                product["mer"] = non_reg_mer
    return doc


def _map_heloc_without_validation(doc: dict):
    """Call the heloc -> property mapping DIRECTLY, bypassing schema
    validation, so the mapper's own refusal is observable -- validate_contract
    rejects the same document first, which is the correct first gate (see
    test_a_fee_above_one_is_refused_by_the_schema)."""
    from contract_principal import map_property_config
    doc = copy.deepcopy(doc)
    mortgage = next((x for x in doc["liabilities"] if x["kind"] == "mortgage"), None)
    heloc = next((x for x in doc["liabilities"] if x["kind"] == "heloc"), None)
    family = {m["id"]: m for m in doc["people"]}
    primary_id = doc["decisions"]["horizon"]["person"]
    spouse_id = next((pid for pid, m in family.items()
                      if any(r["type"] == "spouse_of" and r["person"] == primary_id
                             for r in m.get("relationships", []))), None)
    return map_property_config(doc["properties"][0], mortgage, heloc, None,
                               primary_id, spouse_id, None)


def _run(doc: dict):
    cfg = input_contract.to_internal_config(copy.deepcopy(doc))
    config = SimulationConfig.from_dict(cfg)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return FamilySimulation(config, adapter=CanadaAdapter(config)).run()


def _sleeve(results):
    """The sleeve's terminal balance -- the pot #381 is about.

    NOT ``non_reg_balance``: the SM sleeve is tracked as its OWN pot
    (``sm_investment_balance``) and reaches net worth through ``total_assets``,
    so reading non_reg would have measured the wrong thing entirely (my first
    attempt did, and every assertion passed for the wrong reason)."""
    return results[-1].sm_investment_balance


class TheFeeIsMappedAndBounded(unittest.TestCase):
    """A declared fee reaches the engine; an absent one leaves no key (DP#32)."""

    def test_a_declared_sleeve_fee_reaches_the_config(self):
        cfg = input_contract.to_internal_config(_doc(sleeve_mer=0.0055))
        self.assertEqual(
            SimulationConfig.from_dict(cfg).sm_investment_mer, 0.0055)

    def test_no_declared_fee_leaves_the_key_absent(self):
        cfg = input_contract.to_internal_config(_doc())
        self.assertIsNone(SimulationConfig.from_dict(cfg).sm_investment_mer)

    def test_a_fee_above_one_is_refused_by_the_schema(self):
        """The SCHEMA is the first gate, and it refuses the impossible value by
        name -- before any mapper runs. A fee above 100% describes a fund that
        owes more than it holds, not a fee."""
        from contract_errors import ContractValidationError
        with self.assertRaises(ContractValidationError) as ctx:
            input_contract.to_internal_config(_doc(sleeve_mer=1.5))
        self.assertIn("investment_mer", str(ctx.exception))

    def test_a_negative_fee_is_refused_by_the_schema(self):
        from contract_errors import ContractValidationError
        with self.assertRaises(ContractValidationError):
            input_contract.to_internal_config(_doc(sleeve_mer=-0.001))

    def test_a_fee_above_one_is_refused_again_at_the_mapper(self):
        """Defence in depth for a document that skipped schema validation: the
        mapper refuses the same value itself, naming the liability (DP#32 --
        absence must fail loudly at every layer it can be caught)."""
        doc = _doc(sleeve_mer=1.5)
        with self.assertRaises(ContractAdaptationError) as ctx:
            _map_heloc_without_validation(doc)
        self.assertIn("investment_mer", str(ctx.exception))


class ADeclaredFeeCostsTheSleeveMoney(unittest.TestCase):
    """(1) A declared fee reduces the sleeve's terminal value, by the compounded
    fee. Relational: fee-free is the maximum, a bigger fee is lower."""

    def test_the_fee_free_sleeve_is_the_best_case(self):
        free = _sleeve(_run(_doc()))
        charged = _sleeve(_run(_doc(sleeve_mer=0.02)))
        self.assertGreater(
            free, charged,
            "a sleeve paying a MER must end BELOW one compounding fee-free "
            "(issue #381: the fee-free sleeve overstated terminal assets)")

    def test_a_bigger_fee_ends_lower(self):
        small = _sleeve(_run(_doc(sleeve_mer=0.005)))
        large = _sleeve(_run(_doc(sleeve_mer=0.03)))
        self.assertGreater(small, large)

    def test_the_fee_reaches_net_worth_not_only_the_sleeve_row(self):
        """The sleeve's drag must reach the household's assets, not just the
        sleeve's own line -- that is the point: the fee-free sleeve overstated
        terminal assets (issue #381)."""
        self.assertLess(_run(_doc(sleeve_mer=0.02))[-1].total_assets,
                        _run(_doc())[-1].total_assets)


class TheTwoFeesStaySeparate(unittest.TestCase):
    """(#316's sabotage stays green) The non-reg account's MER must NOT leak into
    the sleeve: the sleeve has its own fee, or none -- never the account's."""

    def test_a_non_reg_account_fee_does_not_move_the_sleeve(self):
        baseline = _sleeve(_run(_doc()))
        with_account_fee = _sleeve(_run(_doc(non_reg_mer=0.05)))
        self.assertEqual(
            baseline, with_account_fee,
            "the non-registered account's declared mer leaked into the "
            "Smith-Manoeuvre sleeve (issue #381; #316 stopped this leak and "
            "the sleeve's OWN fee must not reintroduce it)")

    def test_the_sleeve_fee_does_move_the_sleeve(self):
        """... and the contrast: the sleeve's own fee IS priced. Without this,
        the no-leak test above would pass for the wrong reason (a fee that is
        simply never applied anywhere)."""
        self.assertNotEqual(_sleeve(_run(_doc())),
                            _sleeve(_run(_doc(sleeve_mer=0.02))))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()