"""Issue #362, first half: stop accepting an expense field the engine ignores.

#980 added `expenses_annual` for a **self-employment** income (T2125 -> net
business income). The schema, however, permits the field on an income of ANY
kind, and the engine reads it only on `self_employment`. So a household that
declares expenses on an `employment` segment passes validation and has the
figure silently dropped:

    simulation._income_components_for_year(
        63000, [{"kind": "employment", "amount": 63000,
                 "expenses_annual": 8700, ...}], 2024, 0.0, 0)
    -> (63000.0, 63000.0)      # the 8,700 vanishes, with no error

That is DP#32's silent dead read: a declared fact accepted by the contract and
then ignored by the engine, so a commission salesperson or an employee who
pays their own vehicle costs is taxed on gross while believing otherwise. The
schema's own description admits it — *"Has NO effect for any other kind"* — which
is precisely the tell.

This is the **first half** of #362. The honest fix today is to REFUSE the
declaration rather than keep accepting it and ignoring it. The deduction itself
(ITA s.8(1)(f)/(h), a `basis` of commission vs salaried, the commission-income
cap, the RRSP earned-income effect, and the AMT add-back) is the second half and
is NOT here: it is a modelling change across both returns, and refusing the
field is what makes room for it to arrive as a fact that is actually used.

Scope is deliberately narrow:

* a `self_employment` income keeps `expenses_annual` — byte-identical to today;
* an income of any OTHER kind that declares it now fails schema validation,
  naming the kind;
* an income that declares nothing is untouched (DP#32: absence stays absence).

The existing ENGINE-level test for the ignored field
(`test_issue_980.test_expenses_on_an_employment_segment_are_ignored`) builds an
internal `SimulationConfig` directly, bypassing the contract, so it still holds:
the engine still ignores the field there, and the contract no longer lets anyone
get into that state by accident.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

import contract_schema
import input_contract
from contract_errors import ContractValidationError

_EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "examples", "lifedraft", "minimal-two-adult", "input.json")


def _doc() -> dict:
    with open(_EXAMPLE) as fh:      # filterwarnings=error: an unclosed file IS a failure
        return json.load(fh)


def _set_first_income(doc: dict, **fields) -> None:
    person = doc["people"][0]
    person["incomes"] = [{**person["incomes"][0], **fields}]


class ExpensesAreScopedToSelfEmployment(unittest.TestCase):
    """The contract may no longer state an expense the engine would ignore."""

    def test_expenses_on_an_employment_income_fail_validation(self):
        """Acceptance case 4 of #362: a document with `expenses_annual` on a
        kind=employment segment must fail schema validation rather than load and
        drop the figure."""
        doc = _doc()
        _set_first_income(doc, expenses_annual=1000)
        with self.assertRaises(ContractValidationError) as ctx:
            input_contract.to_internal_config(doc)
        message = str(ctx.exception)
        self.assertIn("expenses_annual", message)
        self.assertIn("employment", message,
                      "the refusal must name the kind it was declared on")

    def test_expenses_on_a_self_employment_income_still_validate(self):
        """The control: this is the field's ONLY legitimate home, and it must
        keep working byte-identically (#980)."""
        doc = _doc()
        _set_first_income(doc, kind="self_employment", expenses_annual=1000)
        cfg = input_contract.to_internal_config(doc)
        self.assertEqual(cfg["family"]["members"][0]["role"], "primary",
                         "the document must still LOAD with a self-employment "
                         "expense -- this change refuses the other kinds, it does "
                         "not touch #980's own path")

    def test_an_income_declaring_no_expenses_is_untouched(self):
        """DP#32: absence stays absence. A run must not start failing because a
        household said nothing."""
        cfg = input_contract.to_internal_config(_doc())
        self.assertTrue(cfg)   # the shipped example declares none, and loads


class OtherKindsAreRefusedToo(unittest.TestCase):
    """The rule is "self_employment only", not "employment is special"."""

    def test_every_other_kind_is_refused(self):
        for kind in ("rental", "investment", "ei", "other"):
            with self.subTest(kind=kind):
                doc = _doc()
                _set_first_income(doc, kind=kind, expenses_annual=1000)
                with self.assertRaises(ContractValidationError):
                    input_contract.to_internal_config(doc)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()