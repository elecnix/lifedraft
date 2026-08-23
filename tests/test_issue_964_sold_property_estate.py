#!/usr/bin/env python3
"""Issue #964: a property SOLD on/before the terminal (death) year must be
EXCLUDED from the estate -- its economics reach the heirs ONLY through the
reinvested sale proceeds (already in the portfolio via the disposition rule),
never as a second helping of its death-year deemed-disposition value.

The bug (see `gh issue view 964`): ``contract_estate._map_pre_property_gains``
and the sibling estate consumers (``_map_estate``'s ``principal_fmv`` /
``other_fmv`` / ``house_equity``, ``objective._estate_call_args``'s
``house_value`` / ``house_equity``, ``objective._cca_recapture_for``)
enumerated EVERY owned property and never checked for a ``sale``. A property
sold mid-horizon (Bite B non-principal #959, Bite E principal #962) was
DOUBLE-COUNTED at death: the disposition rule already converted it to
portfolio cash (proceeds invested), AND the estate still valued the
no-longer-owned property at its death-year deemed disposition. Measured on the
real personal contract at 7% home appreciation, ``sell_rent 2031`` after-tax
estate was ~$39.9M vs KEEP ~$28.65M -- economically impossible (a sold home
shouldn't add its full death value on top of the reinvested proceeds).

The fix: a property whose ``sale`` fires on/before the terminal year is NOT in
the estate. The terminal year is the year the horizon person reaches
``decisions.horizon.until_age`` -- the SAME terminal year the estate's
deemed-disposition already values on (``start_year + len(results) - 1``). A
``sale.year`` beyond the horizon never fires -> the property IS still owned at
death -> keep it.

This file verifies the exclusion on BOTH property classes:

  * **Non-principal (a cottage)** -- two properties + PRE designation so the
    per-property ``plan.property_gains`` path engages (issue #695). The sold
    cottage must be ABSENT from ``property_gains`` and contribute 0 to the
    estate's ``taxable_property_gross``.
  * **Principal residence** -- a sold primary home (Bite E) with appreciation
    + a mortgage. The estate's ``house_equity`` must be 0 (the home is gone,
    the mortgage discharged at the sale), and the sell estate must be LOWER
    than the hold (keep) estate when the home appreciates -- the double-count
    removed flips the ranking so keeping an appreciating home wins, exactly
    the issue's acceptance criterion.

All fixtures use fabricated ids and round numbers (DP#4/DP#15); no real
figure, name, or account enters the repo (DP#15). These tests run the real
engine (``FamilySimulation.run``), so the money-conservation invariant suite
(``trajectory_invariants.assert_run_invariants``, wired into ``run()``) is
enforced automatically on every run here.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from objective import compute_after_tax_estate, _estate_call_args

from test_input_contract import _load_example, _two_generation_subset
from test_issue_956_bite_b_sale_core import (
    _add_cottage, _base_doc_no_pr_designation,
)
from test_issue_956_bite_e_principal_sale import (
    _add_principal_sale, _SALE_2031,
)
from test_issue_694_cca_recapture import _add_owned_rental, _CCA as _RENTAL_CCA
import contract_schema


# The shipped example projects 50 years from start_year 2026, so results[i]
# is calendar year 2026 + i. A sale dated 2031 fires in results[5] -- well
# before the terminal year (95 - age_at_start + 1 years out), so a 2031 sale
# is unambiguously "sold on/before the terminal year" (#964's exclusion case).
_SALE_2031 = _SALE_2031
_PRINCIPAL_VALUE = 650_000      # the shipped example's principal residence value


def _base_doc():
    """The shipped two-generation example, validated (the sub-family the
    adapter can honestly map onto the two-adults-plus-children engine)."""
    doc = _two_generation_subset(_load_example())
    contract_schema.validate_contract(doc)
    return doc


def _run(doc):
    """Validate -> map to internal config -> run the real engine."""
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    cfg = SimulationConfig.from_dict(legacy)
    return FamilySimulation(cfg).run(), legacy


def _estate(doc):
    """Run the engine and return (EstateResult, legacy_cfg, results)."""
    results, legacy = _run(doc)
    return compute_after_tax_estate(results, legacy), legacy, results


# ──────────────────────────────────────────────────────────────────────────
# 1. Non-principal property (a cottage) SOLD mid-horizon: excluded from the
#    estate's property_gains and taxable_property_gross.
# ──────────────────────────────────────────────────────────────────────────

class SoldNonPrincipalPropertyExcludedFromEstate(unittest.TestCase):
    """A cottage sold on/before the terminal year is not owned at death -- the
    disposition rule already invested its net proceeds into the portfolio, so
    the estate must not value it AGAIN. With two properties + PRE
    designation, the per-property ``plan.property_gains`` path (issue #695)
    engages, so this also verifies the exclusion in
    ``_map_pre_property_gains``."""

    def _two_property_doc_with_designated_cottage(self, cottage_sale=None):
        """The base doc with the principal's PRE designation CLEARED and a
        cottage DESIGNATED as a principal residence (so the per-property PRE
        allocation engages -- two properties, at least one designated). The
        cottage carries an accrued gain (value 500k, ACB 400k) so it has a
        real death-value to be excluded."""
        doc = _base_doc_no_pr_designation()
        # Designate the cottage for the same years the principal used to be
        # (from 2023 onward) so there is no family-year conflict and the PRE
        # allocation has a contest to resolve -> property_gains engages.
        doc = _add_cottage(doc, sale=cottage_sale)
        for p in doc["properties"]:
            if p["id"] == "couple_cottage":
                p["designated_principal_residence_years"] = [
                    {"from": "2023-09-01", "to": None}]
        contract_schema.validate_contract(doc)
        return doc

    def test_sold_cottage_absent_from_property_gains(self):
        """The sold cottage is NOT in ``plan.property_gains`` -- the estate
        values only properties still held at the horizon. A cottage with NO
        sale IS present (the control), proving the exclusion is the sale, not
        a structural absence."""
        # Control: cottage held to the horizon -> present in property_gains.
        hold_doc = self._two_property_doc_with_designated_cottage(
            cottage_sale=None)
        hold_results, hold_legacy = _run(hold_doc)
        hold_plan = _estate_call_args(hold_results, hold_legacy)['plan']
        self.assertIsNotNone(
            hold_plan.property_gains,
            "control: two designated properties must engage property_gains")
        hold_ids = {g['id'] for g in hold_plan.property_gains}
        self.assertIn(
            "couple_cottage", hold_ids,
            "control: a held cottage must be in property_gains")

        # Exclusion: cottage sold 2031 -> ABSENT from property_gains.
        sell_doc = self._two_property_doc_with_designated_cottage(
            cottage_sale=_SALE_2031)
        sell_results, sell_legacy = _run(sell_doc)
        sell_plan = _estate_call_args(sell_results, sell_legacy)['plan']
        self.assertIsNotNone(
            sell_plan.property_gains,
            "the principal is still held, so property_gains still engages")
        sell_ids = {g['id'] for g in sell_plan.property_gains}
        self.assertNotIn(
            "couple_cottage", sell_ids,
            "a cottage sold on/before the terminal year must be EXCLUDED from "
            "the estate's property_gains (issue #964) -- its economics reach "
            "the estate only through the reinvested proceeds, not a second "
            "helping of its death-value")

    def test_sold_cottage_contributes_zero_to_estate_property_gross(self):
        """The sold cottage contributes 0 to the estate's
        ``taxable_property_gross`` -- its death-value is not in the estate.
        The cottage has a 100k accrued gain (value 500k, ACB 400k), so a
        buggy double-count would add ~its couple-share value to the gross
        estate; the exclusion holds it to the same property gross as a
        household with no cottage at all."""
        # Estate with the sold cottage vs estate with NO cottage at all:
        # the two must be equal on taxable_property_gross (the sold cottage
        # adds nothing), proving the exclusion.
        no_cottage_est, _, _ = _estate(_base_doc_no_pr_designation())

        sell_doc = self._two_property_doc_with_designated_cottage(
            cottage_sale=_SALE_2031)
        sell_est, _, _ = _estate(sell_doc)

        self.assertEqual(
            sell_est.taxable_property_gross, no_cottage_est.taxable_property_gross,
            "a cottage sold on/before the terminal year must contribute 0 to "
            "the estate's taxable_property_gross (issue #964): the sell "
            f"estate's property gross ({sell_est.taxable_property_gross}) "
            f"must equal the no-cottage estate's ({no_cottage_est.taxable_property_gross})")


# ──────────────────────────────────────────────────────────────────────────
# 2. Principal residence SOLD mid-horizon (with appreciation + a mortgage):
#    house_equity is 0 and the sell estate is LOWER than the keep estate.
# ──────────────────────────────────────────────────────────────────────────

class SoldPrincipalResidenceExcludedFromEstate(unittest.TestCase):
    """A principal residence sold on/before the terminal year (Bite E) is not
    owned at death -- the ``principal_disposition`` rule already invested its
    net proceeds into the portfolio and discharged its mortgage, so the
    estate's ``house_equity`` must be 0 (no home value, no mortgage debt) and
    the sell estate must be LOWER than the keep estate when the home
    appreciates -- the double-count removed flips the ranking so keeping an
    appreciating home wins (the issue's acceptance criterion)."""

    def _doc_with_appreciation(self, doc, rate):
        d = copy.deepcopy(doc)
        for p in d["properties"]:
            if p["kind"] == "principal":
                p["appreciation_rate"] = rate
        contract_schema.validate_contract(d)
        return d

    def test_sold_principal_house_equity_is_zero(self):
        """The sold principal contributes 0 to the estate's ``house_equity``
        -- the home is gone (proceeds invested) and the mortgage discharged at
        the sale, so there is no death-value and no death-debt for the estate
        to value. A held principal (the control) has positive house_equity."""
        # Control: principal held -> positive house_equity (it is designated,
        # tax-free, so its equity reaches the gross estate).
        hold_est, _, _ = _estate(self._doc_with_appreciation(_base_doc(), 0.07))
        self.assertGreater(
            hold_est.house_equity, 0.0,
            "control: a held, appreciating, designated principal must have "
            "positive house_equity in the estate")

        # Exclusion: principal sold 2031 -> house_equity is 0.
        sell_doc = _add_principal_sale(_base_doc(), _SALE_2031)
        sell_est, _, _ = _estate(self._doc_with_appreciation(sell_doc, 0.07))
        self.assertEqual(
            sell_est.house_equity, 0.0,
            "a principal sold on/before the terminal year must contribute 0 "
            "to the estate's house_equity (issue #964) -- the home is gone "
            f"(proceeds invested) and the mortgage discharged; got "
            f"{sell_est.house_equity}")

    def test_sell_estate_below_keep_estate_when_home_appreciates(self):
        """The issue's acceptance criterion: with the home appreciating at 7%
        (near the portfolio return), the SELL estate must be BELOW the KEEP
        estate -- the double-count removed, keeping the appreciating,
        PRE-exempt home (tax-free base) wins over selling and holding the
        freed equity as non-reg (taxed). Before #964 the sell estate was
        inflated ABOVE keep by the sold home's full appreciated death-value
        -- economically impossible."""
        keep_est, _, _ = _estate(self._doc_with_appreciation(_base_doc(), 0.07))
        sell_doc = _add_principal_sale(_base_doc(), _SALE_2031)
        sell_est, _, _ = _estate(self._doc_with_appreciation(sell_doc, 0.07))
        self.assertLess(
            sell_est.net_estate, keep_est.net_estate,
            "the sell estate must be BELOW the keep estate when the home "
            f"appreciates (issue #964): sell={sell_est.net_estate} vs "
            f"keep={keep_est.net_estate}. Before #964 the sold home's "
            f"death-value was double-counted, inflating sell above keep.")


# ──────────────────────────────────────────────────────────────────────────
# 3. Absence-safe (DP#32): a sale beyond the horizon never fires -> the
#    property IS still owned at death -> kept in the estate (the exclusion is
#    the sale firing on/before the terminal year, not the mere presence of a
#    sale block).
# ──────────────────────────────────────────────────────────────────────────

class SaleBeyondHorizonKeepsPropertyInEstate(unittest.TestCase):
    """A ``sale`` dated BEYOND the terminal year never fires inside the
    projection -> the property is still owned at death -> it stays in the
    estate. The exclusion is ``sale_year <= terminal_year``, not "a sale
    block exists" (DP#32: a declared sale that never fires is a hold)."""

    def test_sale_beyond_horizon_keeps_cottage_in_property_gains(self):
        # The shipped example projects 50 years (start 2026 -> terminal 2075).
        # A sale dated 2099 is beyond the horizon -> the cottage is held to
        # death -> it stays in property_gains.
        doc = _base_doc_no_pr_designation()
        doc = _add_cottage(
            doc, sale={"year": 2099, "selling_costs": 0})
        for p in doc["properties"]:
            if p["id"] == "couple_cottage":
                p["designated_principal_residence_years"] = [
                    {"from": "2023-09-01", "to": None}]
        contract_schema.validate_contract(doc)
        results, legacy = _run(doc)
        plan = _estate_call_args(results, legacy)['plan']
        self.assertIsNotNone(plan.property_gains)
        ids = {g['id'] for g in plan.property_gains}
        self.assertIn(
            "couple_cottage", ids,
            "a sale dated BEYOND the terminal year never fires -> the "
            "cottage is still owned at death -> it stays in the estate's "
            "property_gains (issue #964: the exclusion is sale_year <= "
            "terminal_year, not the presence of a sale block)")


# ──────────────────────────────────────────────────────────────────────────
# 4. CCA recapture: a rental SOLD mid-horizon is not recaptured AGAIN at the
#    deemed disposition (its recapture was realized at the sale).
# ──────────────────────────────────────────────────────────────────────────

class SoldRentalNotRecapturedAtDeath(unittest.TestCase):
    """A rental that elected CCA and is SOLD on/before the terminal year is
    not owned at death -- its CCA recapture was already realized at its
    mid-horizon sale (the disposition rule prices it), so
    ``objective._cca_recapture_for`` must SKIP it: the estate's
    ``cca_recapture_tax`` is 0, not a second helping of the same recapture.
    A held rental (the control) carries positive recapture."""

    def _rental_doc(self, sale=None):
        doc = _base_doc()
        doc = _add_owned_rental(doc, 30000, 8000, cca=_RENTAL_CCA)
        if sale is not None:
            for p in doc["properties"]:
                if p["id"] == "couple_rental":
                    p["sale"] = sale
        contract_schema.validate_contract(doc)
        return doc

    def test_sold_rental_has_zero_recapture_tax(self):
        """The sold rental's estate ``cca_recapture_tax`` is 0 (the recapture
        was realized at the sale, not re-counted at death); a held rental
        (the control) has positive recapture tax."""
        held_est, _, _ = _estate(self._rental_doc(sale=None))
        self.assertGreater(
            held_est.cca_recapture_tax, 0.0,
            "control: a held CCA-electing rental must have positive "
            "cca_recapture_tax at the deemed disposition")
        sold_est, _, _ = _estate(self._rental_doc(sale=_SALE_2031))
        self.assertEqual(
            sold_est.cca_recapture_tax, 0.0,
            "a rental sold on/before the terminal year must contribute 0 to "
            "the estate's cca_recapture_tax (issue #964) -- its recapture was "
            f"realized at the sale; got {sold_est.cca_recapture_tax}")


if __name__ == "__main__":
    unittest.main()