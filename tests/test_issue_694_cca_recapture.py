#!/usr/bin/env python3
"""Issue #694 (epic #690, bite 3): Capital Cost Allowance (CCA) on a rental
property, with recapture at the estate's deemed disposition.

A rental may ELECT CCA -- non-cash depreciation of the building against its
rental income (ITA s.20(1)(a), typically Class 1 at 4% declining balance). CCA:

  - lowers TAXABLE rental income (the deferral benefit) but NOT the cash the
    household keeps (depreciation is non-cash);
  - can never create or deepen a rental loss (the claim is capped at net rental
    income before CCA);
  - depreciates a declining-balance UCC, tracked year over year;
  - is RECAPTURED as ORDINARY income (100% inclusion, ITA s.13(1)) at
    disposition -- here the deemed disposition at death -- to the extent the
    proceeds (up to the original capital cost) exceed the remaining UCC. This is
    taxed WORSE than the 50%-inclusion capital gain on the same property, and is
    the asymmetry a CCA model MUST carry or it is systematically optimistic.

Scope of THIS bite: CCA deduction + estate recapture. It builds on #693 (net
rental income + s.20(1)(c)) and reuses #754/#600's estate deemed-disposition
path. The per-year PRE allocation (#695), a mid-horizon purchase (#696) and STR
(#697) are LATER bites, deliberately out of scope; the half-year rule (an
acquisition-year concern) is exercised in the pure module here but the wired
path treats a declared rental as an EXISTING property (no half-year), leaving the
acquisition-year path to #696.

All fixtures use fabricated ids and round numbers (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.cca import (
    cca_claim, ucc_after_claim, recapture_on_disposition)
import objective

from test_input_contract import _load_example, _two_generation_subset
import contract_schema


def _add_owned_rental(doc, gross_rent, expenses, mortgage_balance=0, rate=0.05,
                      cca=None):
    """A rental condo the PRIMARY COUPLE (p1/p2) jointly owns, declaring T776
    rent/expense facts, an optional mortgage, and an optional CCA election."""
    doc = copy.deepcopy(doc)
    rental = {
        "gross_rent_annual": gross_rent,
        "expenses_annual": expenses,
        "as_of": "2026-06-30",
    }
    if cca is not None:
        rental["cca"] = cca
    doc["properties"].append({
        "id": "couple_rental",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "rental",
        "value": {"amount": 500000, "as_of": "2026-06-30"},
        "acb": 400000,
        "designated_principal_residence_years": [],
        "rental": rental,
    })
    if mortgage_balance:
        doc["liabilities"].append({
            "id": "rental_mortgage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "mortgage",
            "balance": {"amount": mortgage_balance, "as_of": "2026-06-30"},
            "rate": rate,
            "rate_type": "fixed",
            "amortization": {"years": 25, "payment_monthly": 1200},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
            "collateral": "couple_rental",
        })
    return doc


# A Class-1 election on a building worth $380k, never yet depreciated.
_CCA = {"rate": 0.04, "capital_cost": 380000, "opening_ucc": 380000}


def _run(doc):
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run()


# ── the tax law, in isolation (countries/canada/cca) ────────────────────────
class CCATaxLaw(unittest.TestCase):
    def test_claim_is_rate_times_ucc_capped_at_income(self):
        # 4% of 380000 = 15200, and net income (20000) is above it -> full claim.
        self.assertAlmostEqual(cca_claim(380000, 0.04, 20000), 15200.0)

    def test_claim_cannot_create_a_loss(self):
        # Net income before CCA is only 5000 -> the claim is capped there, never
        # the 15200 the rate would allow (CCA cannot push income negative).
        self.assertAlmostEqual(cca_claim(380000, 0.04, 5000), 5000.0)

    def test_a_loss_year_claims_zero(self):
        # A rental already running at a loss claims NO CCA (not a negative one).
        self.assertAlmostEqual(cca_claim(380000, 0.04, -3000), 0.0)

    def test_half_year_rule_halves_the_acquisition_year_claim(self):
        # Acquisition year: only 50% of the class is eligible -> half the claim.
        self.assertAlmostEqual(
            cca_claim(380000, 0.04, 50000, is_acquisition_year=True), 7600.0)

    def test_empty_class_claims_zero(self):
        self.assertAlmostEqual(cca_claim(0.0, 0.04, 20000), 0.0)

    def test_ucc_declines_by_the_claim(self):
        self.assertAlmostEqual(ucc_after_claim(380000, 15200), 364800.0)

    def test_recapture_when_proceeds_exceed_ucc(self):
        # Sold for its full cost (380000) after depreciating UCC to 300000:
        # 80000 of CCA is recaptured as ordinary income, no capital gain (proceeds
        # do not exceed cost).
        r = recapture_on_disposition(380000, 380000, 300000)
        self.assertAlmostEqual(r['recapture'], 80000.0)
        self.assertAlmostEqual(r['capital_gain'], 0.0)
        self.assertAlmostEqual(r['terminal_loss'], 0.0)

    def test_recapture_is_capped_at_capital_cost_excess_is_capital_gain(self):
        # Sold for 420000 (above the 380000 cost) with UCC at 300000: recapture
        # is the FULL 80000 of CCA (cost - UCC), and the 40000 above cost is a
        # separate CAPITAL gain -- the two are not conflated.
        r = recapture_on_disposition(420000, 380000, 300000)
        self.assertAlmostEqual(r['recapture'], 80000.0)
        self.assertAlmostEqual(r['capital_gain'], 40000.0)

    def test_terminal_loss_when_ucc_exceeds_proceeds(self):
        # Sold for 250000 with UCC still 300000: a 50000 terminal loss, no
        # recapture (the class empties below its UCC).
        r = recapture_on_disposition(250000, 380000, 300000)
        self.assertAlmostEqual(r['terminal_loss'], 50000.0)
        self.assertAlmostEqual(r['recapture'], 0.0)


# ── the wired engine path (DP#17 rule-path) ─────────────────────────────────
class CCAReducesTaxableIncomeAndTracksUCC(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())  # no rental
        contract_schema.validate_contract(self.base)

    def test_cca_reduces_net_rental_income_and_declines_ucc(self):
        """A $30k-rent, $8k-expense, mortgage-free rental (net 22000 before CCA)
        electing CCA at 4% on a $380k building claims 15200 -> surfaced net
        rental income drops to 6800, and the UCC declines to 364800 in year 1."""
        doc = _add_owned_rental(self.base, gross_rent=30000, expenses=8000,
                                cca=_CCA)
        contract_schema.validate_contract(doc)
        results = _run(doc)
        yr = results[0]
        # 22000 net-before-CCA less 15200 CCA = 6800 T776 net rental income.
        self.assertAlmostEqual(yr.net_rental_income, 6800.0, places=6)
        self.assertAlmostEqual(yr.cca_claimed, 15200.0, places=6)
        # UCC declined by the claim, and is threaded to the terminal year.
        self.assertAlmostEqual(yr.rental_ucc["couple_rental"], 364800.0, places=6)
        self.assertLess(results[-1].rental_ucc["couple_rental"], 380000.0)

    def test_cca_is_non_cash_it_lowers_tax_not_cash_so_wealth_rises(self):
        """CCA lowers the tax bill without consuming cash, so a household that
        elects CCA ends the projection WEALTHIER than the identical rental that
        does not (more after-tax cash compounds). The GOLDEN estate invariant is
        the annual balance sheet, so this is where CCA's deferral benefit shows."""
        no_cca = _run(_add_owned_rental(self.base, 30000, 8000))
        with_cca = _run(_add_owned_rental(self.base, 30000, 8000, cca=_CCA))
        # Same operating cash, but the CCA run pays less tax each year.
        self.assertGreater(with_cca[-1].total_assets, no_cca[-1].total_assets)

    def test_cca_claim_is_capped_so_it_never_creates_a_loss(self):
        """A rental whose net income before CCA (10000) is below the 4% rate
        allowance (15200) claims only 10000 -- net rental income floored at 0,
        never negative."""
        doc = _add_owned_rental(self.base, gross_rent=15000, expenses=5000,
                                cca=_CCA)
        contract_schema.validate_contract(doc)
        yr = _run(doc)[0]
        self.assertAlmostEqual(yr.cca_claimed, 10000.0, places=6)
        self.assertAlmostEqual(yr.net_rental_income, 0.0, places=6)

    def test_cca_reaches_the_internal_config(self):
        doc = _add_owned_rental(self.base, 30000, 8000, cca=_CCA)
        legacy = ic.to_internal_config(doc)
        cca = next(p["rental"]["cca"] for p in legacy["properties"]
                   if p.get("rental", {}).get("cca"))
        self.assertEqual(cca["rate"], 0.04)
        self.assertEqual(cca["capital_cost"], 380000)
        self.assertEqual(cca["opening_ucc"], 380000)
        # couple owns 100% -> disposition proceeds is the whole property value.
        self.assertAlmostEqual(cca["fmv_at_disposition"], 500000.0)


# ── recapture at the estate (the delayed trigger) ───────────────────────────
class CCARecaptureAtEstate(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def _estate(self, doc):
        legacy = ic.to_internal_config(doc)
        cfg = SimulationConfig.from_dict(legacy)
        results = FamilySimulation(cfg).run()
        return objective.compute_after_tax_estate(results, legacy)

    def test_recapture_is_taxed_as_ordinary_income_at_death(self):
        """A rental that claimed CCA over the projection has that CCA recaptured
        at the deemed disposition: the CCA-electing estate carries a POSITIVE
        cca_recapture_tax, the identical no-CCA rental carries zero."""
        est_cca = self._estate(
            _add_owned_rental(self.base, 30000, 8000, cca=_CCA))
        est_no_cca = self._estate(
            _add_owned_rental(self.base, 30000, 8000))
        self.assertGreater(est_cca.cca_recapture_tax, 0.0)
        self.assertAlmostEqual(est_no_cca.cca_recapture_tax, 0.0)
        # The recapture is EXTRA tax on top of the same capital gain: the CCA
        # estate's total deemed-disposition tax exceeds the no-CCA one's.
        self.assertGreater(est_cca.total_tax, est_no_cca.total_tax)

    def test_recapture_helper_skips_non_dict_props_and_missing_ucc_ledger(self):
        """The defensive branches of objective._cca_recapture_for: a non-dict
        entry in ``properties`` is skipped, and a terminal result WITHOUT the
        fold's UCC ledger (``rental_ucc`` is None -- e.g. a hand-built
        YearResult) falls back to each rental's DECLARED opening UCC rather than
        crashing. Recapture is then computed on that opening UCC."""
        import types
        cfg = {"properties": [
            "not-a-dict",  # a non-dict entry -> skipped
            {"id": "r", "kind": "rental", "rental": {"cca": {
                "rate": 0.04, "capital_cost": 380000,
                "opening_ucc": 300000, "fmv_at_disposition": 500000}}},
        ]}
        final = types.SimpleNamespace(rental_ucc=None)  # no UCC ledger on result
        rec = objective._cca_recapture_for(final, cfg)
        # UCC falls back to the declared opening 300000; proceeds capped at the
        # 380000 capital cost -> 380000 - 300000 = 80000 recaptured.
        self.assertAlmostEqual(rec, 80000.0)


class TestAbsenceIsNoOp(unittest.TestCase):
    """DP#32: a household that declares no CCA -- including a rental WITHOUT a
    cca election -- must be byte-identical to #693 behaviour: no cca in the
    internal config, zero CCA on every YearResult, and no recapture at the
    estate."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())
        contract_schema.validate_contract(self.base)

    def test_rental_without_cca_carries_no_cca_facts(self):
        doc = _add_owned_rental(self.base, 30000, 8000)  # rental, no cca
        legacy = ic.to_internal_config(doc)
        for prop in legacy["properties"]:
            self.assertNotIn("cca", prop.get("rental", {}))

    def test_rental_without_cca_has_zero_cca_and_unchanged_net_income(self):
        doc = _add_owned_rental(self.base, 30000, 8000)
        results = _run(doc)
        self.assertTrue(all(r.cca_claimed == 0.0 for r in results))
        self.assertTrue(all(r.rental_ucc == {} for r in results))
        # #693's net rental income is unchanged by this bite (no CCA subtracted).
        self.assertAlmostEqual(results[0].net_rental_income, 22000.0, places=6)

    def test_no_rental_household_has_zero_cca_everywhere(self):
        results = _run(self.base)
        self.assertTrue(all(r.cca_claimed == 0.0 for r in results))
        self.assertTrue(all(r.rental_ucc == {} for r in results))

    def test_rental_without_cca_has_no_estate_recapture(self):
        legacy = ic.to_internal_config(_add_owned_rental(self.base, 30000, 8000))
        cfg = SimulationConfig.from_dict(legacy)
        results = FamilySimulation(cfg).run()
        est = objective.compute_after_tax_estate(results, legacy)
        self.assertAlmostEqual(est.cca_recapture_tax, 0.0)


if __name__ == "__main__":
    unittest.main()
