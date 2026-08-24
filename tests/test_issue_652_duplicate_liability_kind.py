#!/usr/bin/env python3
"""Issue #652 + #1075: the contract adapter silently dropped every liability
after the first of its kind.

``_find_liability`` returned the FIRST liability of a given kind and ignored
the rest. Its callers take exactly one mortgage / one heloc / one
line_of_credit, so a document declaring TWO ``kind=mortgage`` liabilities --
the faithful encoding of a readvanceable all-in-one's fixed sub-accounts --
validated cleanly, loaded cleanly, and SILENTLY DROPPED the second balance.
Loan-to-value came back understated in the household's favour: the exact
"engine silently substitutes zero" defect class (#603/DP#32) this adapter
exists to prevent.

Fix, phase 1 (this issue's option 1): REFUSE LOUDLY when more than one
liability of a single-facility kind matches, rather than keeping one.

Fix, phase 2 (#1075, data-model half): the refusal is LIFTED for
kind=mortgage ONLY, and only for tranches of the PRINCIPAL's OWN charge
that share ONE collateral (one registered charge). Those tranches are
summed into the single downstream facility -- balances sum into
``mortgage_balance``, the rate becomes the balance-weighted average (total
interest is preserved exactly, since rate is linear in balance), and each
tranche's new optional ``deductible`` flag / ``cash_back`` block are
carried (``deductible_mortgage_balance`` and the EXACT deductible interest
``deductible_mortgage_interest`` on the config; an
``origination_cash_back`` cash-flow in the first projection year). Every
other kind (heloc, line_of_credit, ...) still REFUSES loudly, a
multi-mortgage document whose tranches do NOT share one charge (two
genuinely separate debts) is still refused loudly, and so is any
multi-mortgage document the principal does not secure (even sharing one
non-principal collateral) -- summing unrelated charges, or folding a
rental/cottage charge into the family home's balance, would fabricate a
single mortgage out of two (DP#32).

DP#4/DP#15: fabricated round numbers, role-based ids -- no real household.
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "architecture"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

import input_contract as ic
from test_dp_income_scenario_reaches_engine import _two_generation_subset
import contract_errors
import contract_schema


def _load_doc():
    with open(contract_schema.EXAMPLE_PATH) as fh:
        return _two_generation_subset(json.load(fh))


def _liability(doc, kind):
    return next(l for l in doc["liabilities"] if l["kind"] == kind)


class TestMultiTrancheMortgageAggregates(unittest.TestCase):
    """Issue #1075: the #652 refusal is lifted for kind=mortgage tranches
    sharing one charge -- they are summed, not dropped and not refused."""

    def test_two_mortgages_on_the_same_charge_are_summed_not_dropped(self):
        """The #652 reproduction, now the multi-tranche model: two
        kind=mortgage liabilities against the same property (an all-in-one's
        fixed sub-accounts) load, and BOTH balances reach the single
        downstream ``mortgage_balance``. The pre-fix adapter kept only the
        first, dropping $20k of declared debt; the pre-#1075 adapter refused
        the pair outright. The second tranche is small enough that the sum
        stays inside the example's 80% charge (520,000): 340,000 + 20,000
        + 150,000 HELOC = 510,000."""
        doc = _load_doc()
        mortgage = _liability(doc, "mortgage")
        second = copy.deepcopy(mortgage)
        second["id"] = "mortgage_sub2"
        second["balance"] = {"amount": 20_000, "as_of": mortgage["balance"]["as_of"]}
        second["rate"] = 0.05
        doc["liabilities"] = doc["liabilities"] + [second]

        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["property"]["mortgage_balance"], 340_000 + 20_000)
        expected_rate = (340_000 * 0.045 + 20_000 * 0.05) / 360_000
        self.assertAlmostEqual(cfg["property"]["mortgage_rate"], expected_rate)

    def test_two_mortgages_on_different_charges_are_still_refused(self):
        """DP#32, the OTHER direction: the lift is for tranches of ONE
        charge. Two mortgages the principal does not secure, against two
        DIFFERENT properties, reach the unfiltered fallback lookup and must
        be refused loudly -- summing them would fabricate a single mortgage
        out of two unrelated charges. The refusal names the kind and both
        ids; it cannot be mistaken for a schema error."""
        doc = _load_doc()
        mortgage = _liability(doc, "mortgage")
        # Strip the principal's own mortgage so the fallback lookup sees
        # only the two different-charge mortgages.
        doc["liabilities"] = [l for l in doc["liabilities"] if l["kind"] != "mortgage"]
        m1 = copy.deepcopy(mortgage)
        m1["id"] = "m_cottage"
        m1["collateral"] = "cottage"
        m2 = copy.deepcopy(mortgage)
        m2["id"] = "m_rental"
        m2["collateral"] = "rental_duplex"
        doc["liabilities"] = doc["liabilities"] + [m1, m2]

        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("mortgage", msg)
        self.assertIn("m_cottage", msg)
        self.assertIn("m_rental", msg)

    def test_tranches_with_different_amortization_terms_are_refused(self):
        """The one modelling boundary the lift does not cross: the engine
        amortizes the summed balance on ONE schedule, and unlike the rate
        (linear in balance), no single years figure preserves every
        tranche's payment -- so a mix of terms is refused loudly rather
        than silently repriced (DP#32)."""
        doc = _load_doc()
        mortgage = _liability(doc, "mortgage")
        second = copy.deepcopy(mortgage)
        second["id"] = "mortgage_sub2"
        second["balance"] = {"amount": 20_000, "as_of": mortgage["balance"]["as_of"]}
        second["amortization"] = {"years": 25, "payment_monthly": 500}
        doc["liabilities"] = doc["liabilities"] + [second]

        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("amortization", str(ctx.exception))

    def test_single_mortgage_still_loads(self):
        """No-op for the common case: exactly one mortgage of a kind maps as
        before (the example document, one mortgage, still adapts)."""
        doc = _load_doc()
        self.assertEqual(
            sum(1 for l in doc["liabilities"] if l["kind"] == "mortgage"), 1)
        cfg = ic.to_internal_config(doc)
        self.assertEqual(cfg["property"]["mortgage_balance"],
                         _liability(doc, "mortgage")["balance"]["amount"])
        self.assertEqual(cfg["property"]["mortgage_rate"],
                         _liability(doc, "mortgage")["rate"])
        # A single mortgage without the new flags adds NO new keys and NO
        # origination cash-flow -- byte-identical to pre-#1075 (DP#32).
        self.assertNotIn("deductible_mortgage_balance", cfg["property"])
        self.assertFalse(any("origination" in str(cf) for cf in cfg["cash_flows"]))

    def test_two_helocs_are_still_refused(self):
        """The #652 refusal survives for every NON-mortgage kind: the #1075
        multi-tranche lift exists ONLY for kind=mortgage (N fixed sub-accounts
        of one readvanceable charge). Two kind=heloc liabilities -- two
        revolving facilities the engine consumes as ONE margin_available --
        are refused loudly, never summed and never silently dropped (DP#32)."""
        doc = _load_doc()
        heloc = _liability(doc, "heloc")
        second = copy.deepcopy(heloc)
        second["id"] = "heloc_sub2"
        second["limit"] = 100_000
        doc["liabilities"] = doc["liabilities"] + [second]

        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("heloc", msg)
        self.assertIn("heloc_sub2", msg)

    def test_two_lines_of_credit_are_still_refused(self):
        """Same #652 refusal for line_of_credit: the engine consumes ONE
        credit_facility_* scalar, and the #1075 lift exists only for
        kind=mortgage -- two lines of credit are refused loudly, never
        summed and never silently dropped (DP#32)."""
        doc = _load_doc()
        loc = _liability(doc, "line_of_credit")
        second = copy.deepcopy(loc)
        second["id"] = "loc_sub2"
        second["limit"] = 50_000
        doc["liabilities"] = doc["liabilities"] + [second]

        with self.assertRaises(contract_errors.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        msg = str(ctx.exception)
        self.assertIn("line_of_credit", msg)
        self.assertIn("loc_sub2", msg)


if __name__ == "__main__":
    unittest.main()
