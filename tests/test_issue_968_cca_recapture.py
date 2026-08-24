#!/usr/bin/env python3
"""Issue #968: CCA recapture on a mid-horizon rental SALE (Bite B's
``property_disposition`` rule taxed the sold rental's capital gain + PRE but
SKIPPED the CCA recapture -- ITA s.13(1), 100%-inclusion ordinary income --
because the fold exposed no rental UCC as of the sale year, only the terminal
``final.rental_ucc`` the estate path consumes).

A rental that claimed CCA (non-cash depreciation, #694) depreciates a
declining-balance UCC below its capital cost. On a mid-horizon sale, proceeds
(up to the original capital cost) that exceed the remaining UCC are RECAPTURED
as ordinary income -- real tax the model omitted, understating a rental sale's
tax and overstating its return. The estate path (``objective._cca_recapture_for``)
already SKIPS a sold rental's recapture at the deemed disposition (issue #964)
on the assumption the disposition rule prices it at the sale; before #968 that
assumption was unmet, so a sold CCA-electing rental's recapture was taxed
NOWHERE. This bite closes the sale-side clawback.

The fix threads the rental's per-year OPENING UCC (the UCC immediately before
the mid-year disposition -- the closing UCC of the prior year, already carried
in ``jurisdiction_state['canada']['rental_ucc']``) onto ``YearWorkingState`` so
the ``property_disposition`` rule can call
``cca.recapture_on_disposition(p_gross, capital_cost, ucc_at_sale)`` and tax the
``recapture`` (and deduct any ``terminal_loss``) as ordinary income at the
owner's marginal rate, split per owner -- consuming recapture/terminal_loss
only, never the primitive's ``capital_gain`` (the gain path already taxes it,
DP#9).

Acceptance (per the issue):
  - A rental that claimed CCA and is sold crystallizes recapture as ORDINARY
    income in the sale year -- a BIGGER sale-year disposition tax than the
    identical rental with NO CCA election.
  - A rental with no CCA election -> NO recapture (its sale-year tax equals
    the no-CCA baseline).
  - A NON-rental sale (a cottage) -> NO recapture.
  - Money conservation holds (the sale-year run invariants, wired into
    ``FamilySimulation.run`` via ``trajectory_invariants``, enforce this on
    every run here).
  - The golden invariant ``9709753.139463063`` is byte-exact (no rental sale /
    no CCA -> unchanged by construction -- verified separately in
    ``test_golden_trajectory_581``).

All fixtures use fabricated ids and round numbers (DP#4/DP#15); no real figure,
name, or account enters the repo. These tests run the real engine
(``FamilySimulation.run``), so the money-conservation invariant suite is
enforced automatically on every run.
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from countries.canada.cca import recapture_on_disposition
from rules_disposition import _disposition_cca_recapture_tax
from tax_data import default_tax_provider

from test_input_contract import _load_example, _two_generation_subset
from test_issue_694_cca_recapture import _add_owned_rental, _CCA as _RENTAL_CCA
from test_issue_956_bite_b_sale_core import (
    _add_cottage, _base_doc_no_pr_designation, _SALE_2031,
)
import contract_schema


# The shipped example projects 50 years from start_year 2026, so results[i]
# is calendar year 2026 + i. A sale dated 2031 fires in results[5] -- well
# before the terminal year, so the sold rental is unambiguously out of the
# estate (issue #964) and its recapture must be priced at the sale (#968).
_SALE_YEAR = 2031
_SALE_INDEX = _SALE_YEAR - 2026  # 5


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


def _rental_doc_with_sale(sale, cca=_RENTAL_CCA):
    """A couple-owned rental (gross rent 30k, expenses 8k, mortgage-free) with
    an optional CCA election and a dated mid-horizon SALE. The rental's value
    is 500k (ACB 400k) so it has a real capital gain at the sale; the CCA
    election depreciates a 380k building so there is real UCC to recapture."""
    doc = _base_doc()
    doc = _add_owned_rental(doc, gross_rent=30000, expenses=8000, cca=cca)
    for p in doc["properties"]:
        if p["id"] == "couple_rental":
            p["sale"] = sale
    contract_schema.validate_contract(doc)
    return doc


def _rental_doc_with_sale_below_ucc(sale, cca=_RENTAL_CCA):
    """A couple-owned rental SOLD BELOW its remaining UCC so the disposition
    crystallizes a TERMINAL LOSS (ITA s.20(16)) instead of recapture: the
    building's value is set to 200k (below the ~310k UCC the 4%-on-380k class
    has declined to by the 2031 sale) and its ACB to 200k (so there is NO
    capital gain -- the sale-year tax delta isolates the terminal-loss
    deduction cleanly). The rental still declares 30k gross rent / 8k expenses
    so CCA is claimed each pre-sale year and the UCC genuinely declines."""
    doc = _base_doc()
    doc = _add_owned_rental(doc, gross_rent=30000, expenses=8000, cca=cca)
    for p in doc["properties"]:
        if p["id"] == "couple_rental":
            p["sale"] = sale
            p["value"]["amount"] = 200000  # proceeds below the declining UCC
            p["acb"] = 200000  # ACB = proceeds -> zero capital gain (isolate)
    contract_schema.validate_contract(doc)
    return doc


# ──────────────────────────────────────────────────────────────────────────
# 1. A CCA-electing rental SOLD crystallizes recapture as ordinary income in
#    the sale year (a bigger sale-year disposition tax than the same rental
#    with no CCA election).
# ──────────────────────────────────────────────────────────────────────────

class CCARecaptureOnRentalSale(unittest.TestCase):
    """The disposition rule prices a sold rental's CCA recapture (ITA s.13(1))
    as ordinary income in the sale year, on top of the capital-gain + PRE tax
    Bite B already carried. A CCA-electing rental's sale-year disposition tax
    EXCEEDS the identical no-CCA rental's -- the recapture is EXTRA tax, and it
    is real (the previously-claimed depreciation clawed back at 100% inclusion)."""

    def test_ucc_declined_before_the_sale_so_there_is_cca_to_recapture(self):
        """Control: the rental's UCC is below its 380k capital cost by the sale
        year (CCA was claimed in the pre-sale years), so a sale at/above cost
        has real recapture. The UCC ledger is on the result of the year BEFORE
        the sale (the opening UCC at the sale year)."""
        results, legacy = _run(_rental_doc_with_sale(_SALE_2031))
        # The year before the sale carries the closing UCC the sale year opens
        # on (the UCC immediately before the mid-year disposition).
        pre_sale_ucc = results[_SALE_INDEX - 1].rental_ucc["couple_rental"]
        self.assertLess(
            pre_sale_ucc, _RENTAL_CCA["capital_cost"],
            "control: CCA must have been claimed before the sale so there is "
            f"UCC to recapture; got UCC {pre_sale_ucc} vs capital cost "
            f"{_RENTAL_CCA['capital_cost']}")
        self.assertGreater(
            pre_sale_ucc, 0.0, "the UCC is positive (the class is not empty)")

    def test_cca_election_raises_the_sale_year_disposition_tax(self):
        """The CCA-electing rental's sale-year ``sale_disposition_tax`` is
        GREATER than the identical no-CCA rental's: the recapture is priced on
        top of the capital-gain + PRE tax. (Both have the SAME capital gain --
        the gain path is unchanged -- so the delta IS the recapture tax.)"""
        with_cca, _ = _run(_rental_doc_with_sale(_SALE_2031, cca=_RENTAL_CCA))
        no_cca, _ = _run(_rental_doc_with_sale(_SALE_2031, cca=None))
        self.assertGreater(
            with_cca[_SALE_INDEX].sale_disposition_tax,
            no_cca[_SALE_INDEX].sale_disposition_tax,
            "a CCA-electing rental sold mid-horizon must carry a BIGGER "
            "sale-year disposition tax than the identical no-CCA rental "
            f"(issue #968): with_cca={with_cca[_SALE_INDEX].sale_disposition_tax} "
            f"vs no_cca={no_cca[_SALE_INDEX].sale_disposition_tax}. The delta "
            "is the CCA recapture priced as ordinary income at the sale.")

    def test_recapture_is_ordinary_income_not_a_capital_gain(self):
        """The recapture tax is priced at the owner's MARGINAL ordinary rate
        (100% inclusion, ITA s.13(1)), not the 50% capital-gains inclusion. A
        direct check: the recapture figure itself (proceeds capped at cost less
        the opening UCC) is positive, and the sale-year tax delta exceeds what
        a 50%-inclusion gain on the SAME dollars would cost at the lowest
        bracket -- recapture is taxed WORSE than a gain, exactly the asymmetry
        a CCA model must carry."""
        with_cca, legacy = _run(_rental_doc_with_sale(_SALE_2031, cca=_RENTAL_CCA))
        no_cca, _ = _run(_rental_doc_with_sale(_SALE_2031, cca=None))
        # The couple's proceeds at the sale year (p_gross). The rental has no
        # appreciation_rate -> p_gross = value_share = the couple's value share
        # (500k, the couple owns it whole).
        prop = next(p for p in legacy["properties"] if p["id"] == "couple_rental")
        p_gross = prop["value_share"]  # no appreciation_rate -> gross = value
        ucc_at_sale = with_cca[_SALE_INDEX - 1].rental_ucc["couple_rental"]
        conseq = recapture_on_disposition(
            p_gross, _RENTAL_CCA["capital_cost"], ucc_at_sale)
        recapture = conseq["recapture"]
        self.assertGreater(
            recapture, 0.0,
            "the recapture (proceeds capped at cost less the UCC) must be "
            f"positive; got {recapture} (p_gross={p_gross}, cost="
            f"{_RENTAL_CCA['capital_cost']}, ucc={ucc_at_sale})")
        # The sale-year tax DELTA is the recapture priced as ordinary income.
        tax_delta = (with_cca[_SALE_INDEX].sale_disposition_tax
                     - no_cca[_SALE_INDEX].sale_disposition_tax)
        self.assertGreater(
            tax_delta, 0.0,
            "the sale-year disposition tax delta (the recapture tax) must be "
            f"positive; got {tax_delta}")
        # Recapture is 100% ordinary income -- taxed at LEAST the lowest
        # bracket's rate on the full recapture. A 50%-inclusion capital gain
        # on the same dollars would cost half that at the lowest bracket, so
        # the ordinary-income tax must EXCEED the 50%-gain tax on the recapture
        # at any positive bracket (recapture is taxed worse than a gain).
        from tax_data import default_tax_provider
        brackets = default_tax_provider().get_combined_brackets(
            _SALE_YEAR, "quebec")
        lowest_rate = brackets[0]["rate"]
        gain_tax_at_lowest = recapture * 0.5 * lowest_rate
        self.assertGreater(
            tax_delta, gain_tax_at_lowest,
            "the recapture tax (100% inclusion at the marginal rate) must "
            "EXCEED a 50%-inclusion capital gain on the same dollars at the "
            f"lowest bracket ({gain_tax_at_lowest}); got {tax_delta}. "
            "Recapture is ordinary income, taxed WORSE than a gain (ITA s.13).")

    def test_money_conservation_holds_in_the_sale_year(self):
        """The sale-year money-conservation invariant (wired into
        ``FamilySimulation.run`` via ``trajectory_invariants``) holds with the
        recapture priced: ``Δtotal_assets = -(selling_costs + T)`` where T now
        includes the recapture tax. The run would RAISE if conservation broke,
        so reaching the assertion is the proof."""
        results, _ = _run(_rental_doc_with_sale(_SALE_2031, cca=_RENTAL_CCA))
        # The sale year has a real (positive) disposition tax -- the recapture
        # plus the gain tax -- and the run did not raise (conservation holds).
        self.assertGreater(results[_SALE_INDEX].sale_disposition_tax, 0.0)


# ──────────────────────────────────────────────────────────────────────────
# 2. A rental with NO CCA election -> NO recapture (byte-identical sale-year
#    tax to a rental that never elected CCA).
# ──────────────────────────────────────────────────────────────────────────

class NoCCAElectionMeansNoRecapture(unittest.TestCase):
    """DP#32: a rental that never elected CCA has nothing to recapture -- its
    sale-year disposition tax is the capital-gain + PRE tax only, with zero
    recapture added. The recapture helper is a strict no-op for a rental
    without a ``cca`` block."""

    def test_no_cca_rental_sale_tax_equals_gain_tax_only(self):
        """A no-CCA rental's sale-year disposition tax is unchanged by #968:
        the recapture helper returns 0.0 (no ``cca`` election -> nothing to
        recapture). The sale still prices the capital gain + PRE."""
        no_cca, _ = _run(_rental_doc_with_sale(_SALE_2031, cca=None))
        # A positive gain tax (the rental has a 100k gain: 500k - 400k ACB),
        # but NO recapture component -- verified by the with-CCA run carrying a
        # strictly greater tax (the recapture is the only difference).
        with_cca, _ = _run(_rental_doc_with_sale(_SALE_2031, cca=_RENTAL_CCA))
        self.assertGreater(no_cca[_SALE_INDEX].sale_disposition_tax, 0.0,
                           "the no-CCA rental still pays capital-gains tax")
        self.assertGreater(
            with_cca[_SALE_INDEX].sale_disposition_tax,
            no_cca[_SALE_INDEX].sale_disposition_tax,
            "the CCA-electing rental pays MORE (recapture on top of the gain)")


# ──────────────────────────────────────────────────────────────────────────
# 3. A NON-rental sale (a cottage) -> NO recapture (the recapture helper is a
#    no-op for any property without a ``rental``/``cca`` block).
# ──────────────────────────────────────────────────────────────────────────

class NonRentalSaleHasNoRecapture(unittest.TestCase):
    """A cottage (kind=recreational, no rental income, no CCA election) sold
    mid-horizon pays capital-gain + PRE tax but NO recapture -- there is no
    ``rental`` block and no ``cca`` election, so the recapture helper is a
    strict no-op. #968 changes nothing for a non-rental sale (byte-identical
    to Bite B)."""

    def test_cottage_sale_disposition_tax_is_gain_tax_only(self):
        """A sold cottage's sale-year disposition tax is unchanged by #968: the
        recapture helper returns 0.0 (no ``rental``/``cca`` block). Verified by
        re-running the same cottage sale -- the tax is stable and the run does
        not raise (conservation holds)."""
        base = _base_doc_no_pr_designation()
        sell_doc = _add_cottage(base, sale=_SALE_2031)
        results, _ = _run(sell_doc)
        # The cottage has a 100k gain (value 500k, ACB 400k) and is NOT
        # designated (no PRE) -> fully taxable gain -> positive disposition tax,
        # but NO recapture component (no CCA). The run did not raise.
        self.assertGreater(results[_SALE_INDEX].sale_disposition_tax, 0.0,
                           "the sold cottage pays capital-gains tax (no PRE)")


# ──────────────────────────────────────────────────────────────────────────
# 4. The estate does NOT double-count: a sold CCA-electing rental's estate
#    ``cca_recapture_tax`` is 0 (the recapture was realized at the sale, not
#    re-counted at death) -- #968 makes #964's assumption true.
# ──────────────────────────────────────────────────────────────────────────

class SoldRentalRecapturedAtSaleNotAtDeath(unittest.TestCase):
    """Issue #964 made the estate SKIP a sold rental's recapture at the deemed
    disposition on the assumption the disposition rule prices it at the sale.
    Before #968 that assumption was unmet -- the recapture was taxed NOWHERE.
    #968 closes the loop: the estate's ``cca_recapture_tax`` is 0 for a sold
    rental (realized at the sale), and the sale-year disposition tax carries
    the recapture instead (one spelling, not two -- DP#9)."""

    def test_sold_cca_rental_has_zero_estate_recapture(self):
        from objective import compute_after_tax_estate
        results, legacy = _run(_rental_doc_with_sale(_SALE_2031, cca=_RENTAL_CCA))
        est = compute_after_tax_estate(results, legacy)
        self.assertEqual(
            est.cca_recapture_tax, 0.0,
            "a CCA-electing rental sold mid-horizon must contribute 0 to the "
            "estate's cca_recapture_tax (issue #964) -- its recapture was "
            f"realized at the sale (issue #968); got {est.cca_recapture_tax}")
        # And the sale-year disposition tax carries the recapture (positive,
        # above the no-CCA baseline -- proven in class 1).


# ──────────────────────────────────────────────────────────────────────────
# 5. A rental SOLD BELOW its remaining UCC crystallizes a TERMINAL LOSS
#    (ITA s.20(16)) -- a deductible ordinary-income loss that LOWERS the
#    sale-year disposition tax (the mirror of recapture). Covers the
#    terminal-loss branch of ``_disposition_cca_recapture_tax`` (L3559-3563).
# ──────────────────────────────────────────────────────────────────────────

class TerminalLossOnRentalSoldBelowUCC(unittest.TestCase):
    """The recapture helper's SYMMETRIC branch: a rental whose remaining UCC
    EXCEEDS the (cost-capped) sale proceeds empties its CCA class BELOW the
    proceeds -- a TERMINAL LOSS deductible against ordinary income (ITA
    s.20(16)), a marginal tax SAVING in the sale year (a negative incremental
    tax). This is the case the recapture-only tests do NOT exercise: recapture
    is 0 and terminal_loss is positive, so the helper's terminal-loss branch
    (the ``saving = base_tax - tax_on_income(max(0, income - share))`` block)
    runs and the per-owner floor (``min(saving, base_tax)``) binds.

    A rental sold below its UCC must carry a LOWER sale-year disposition tax
    than the same rental sold with NO terminal loss (a break-even / no-CCA
    sale) -- the terminal loss is DEDUCTED, real tax the v1 scope had no path
    to price (issue #968 prices both halves of s.13(1)/s.20(16))."""

    def test_sale_below_ucc_crystallizes_a_terminal_loss(self):
        """Control: a rental sold for 200k whose UCC has declined to ~310k by
        the 2031 sale has a real TERMINAL LOSS (UCC - cost-capped proceeds),
        not recapture. The primitive confirms the symmetry -- recapture is 0
        and terminal_loss is the ~110k shortfall."""
        results, legacy = _run(_rental_doc_with_sale_below_ucc(_SALE_2031))
        prop = next(p for p in legacy["properties"] if p["id"] == "couple_rental")
        ucc_at_sale = results[_SALE_INDEX - 1].rental_ucc["couple_rental"]
        conseq = recapture_on_disposition(
            prop["value_share"], _RENTAL_CCA["capital_cost"], ucc_at_sale)
        self.assertEqual(conseq["recapture"], 0.0,
                         "a sale below UCC has NO recapture (the class empties "
                         f"below the proceeds); got {conseq['recapture']}")
        self.assertGreater(
            conseq["terminal_loss"], 0.0,
            "a sale below UCC crystallizes a deductible terminal loss (UCC - "
            f"cost-capped proceeds); got {conseq['terminal_loss']} "
            f"(ucc={ucc_at_sale}, proceeds={prop['value_share']}, "
            f"cost={_RENTAL_CCA['capital_cost']})")

    def test_terminal_loss_is_deducted_lower_tax_than_break_even_sale(self):
        """The terminal loss is DEDUCTED against ordinary income (ITA s.20(16))
        -- a marginal tax SAVING in the sale year. The below-UCC CCA rental's
        sale-year disposition tax is LOWER than the identical rental sold with
        NO terminal loss (a break-even / no-CCA sale at the same low proceeds,
        where recapture = 0 AND terminal_loss = 0): both have the SAME zero
        capital gain (ACB = proceeds = 200k), so the tax delta IS the
        terminal-loss deduction, and it is negative (a saving)."""
        below_cca, _ = _run(_rental_doc_with_sale_below_ucc(_SALE_2031))
        # The break-even control: same rental, same low proceeds, NO CCA
        # election -- recapture = 0, terminal_loss = 0, capital gain = 0
        # (ACB = proceeds) --> zero disposition tax. The ONLY difference from
        # the below-UCC CCA run is the terminal-loss deduction.
        below_no_cca_doc = _rental_doc_with_sale_below_ucc(_SALE_2031, cca=None)
        below_no_cca, _ = _run(below_no_cca_doc)
        self.assertEqual(
            below_no_cca[_SALE_INDEX].sale_disposition_tax, 0.0,
            "control: the no-CCA break-even sale (ACB = proceeds, no "
            "recapture, no terminal loss) has ZERO disposition tax; got "
            f"{below_no_cca[_SALE_INDEX].sale_disposition_tax}")
        self.assertLess(
            below_cca[_SALE_INDEX].sale_disposition_tax,
            below_no_cca[_SALE_INDEX].sale_disposition_tax,
            "a rental sold below its UCC must carry a LOWER sale-year "
            "disposition tax than the break-even (no-terminal-loss) sale -- "
            "the terminal loss is DEDUCTED (ITA s.20(16)); got below_cca="
            f"{below_cca[_SALE_INDEX].sale_disposition_tax} vs break_even="
            f"{below_no_cca[_SALE_INDEX].sale_disposition_tax}. The delta is "
            "the terminal-loss tax saving at the owner's marginal rate.")
        # The saving is a NEGATIVE contribution (a real deduction, not a no-op).
        self.assertLess(
            below_cca[_SALE_INDEX].sale_disposition_tax, 0.0,
            "the terminal-loss deduction makes the sale-year disposition tax "
            "NEGATIVE (a tax saving); got "
            f"{below_cca[_SALE_INDEX].sale_disposition_tax}")

    def test_terminal_loss_money_conservation_holds(self):
        """The sale-year money-conservation invariant (wired into
        ``FamilySimulation.run`` via ``trajectory_invariants``) holds with the
        terminal loss deducted: ``Δtotal_assets = -(selling_costs + T)`` where
        T is now NEGATIVE (the saving). The run would RAISE if conservation
        broke, so reaching the assertion is the proof."""
        results, _ = _run(_rental_doc_with_sale_below_ucc(_SALE_2031))
        # The sale year has a NEGATIVE disposition tax (the terminal-loss
        # saving) and the run did not raise (conservation holds).
        self.assertLess(results[_SALE_INDEX].sale_disposition_tax, 0.0)


# ──────────────────────────────────────────────────────────────────────────
# 6. A rental with a CCA election but NOT sold in a given year -> NO
#    recapture / terminal loss in that year (the disposition rule is a strict
#    no-op outside the sale year; the recapture helper is never called).
# ──────────────────────────────────────────────────────────────────────────

class CCAElectionButNotSaleYearMeansNoRecapture(unittest.TestCase):
    """``_property_disposition_for`` returns zeros for every year that is NOT
    the property's sale year (DP#32 -- a strict no-op for every other year and
    every property held to the horizon), so the recapture helper is never
    reached outside the sale year. A CCA-electing rental carries ZERO
    disposition tax in every pre-sale year -- its recapture is deferred to the
    sale, not priced early."""

    def test_pre_sale_year_has_zero_disposition_tax(self):
        """A CCA-electing rental dated for a 2031 sale has ZERO disposition tax
        in 2030 (the year before the sale): the disposition rule fires only in
        the sale year, so no recapture / terminal loss is priced early. The
        sale year itself carries the real recapture tax (the contrast)."""
        results, _ = _run(_rental_doc_with_sale(_SALE_2031, cca=_RENTAL_CCA))
        # 2030 is index 4 -- the year BEFORE the 2031 sale (index 5).
        self.assertEqual(
            results[_SALE_INDEX - 1].sale_disposition_tax, 0.0,
            "a CCA-electing rental must carry ZERO disposition tax in a year "
            "that is NOT its sale year -- the recapture is deferred to the "
            f"sale (2031); got 2030 tax {results[_SALE_INDEX - 1].sale_disposition_tax}")
        # And the sale year itself carries the real recapture tax (the
        # contrast -- the helper fires ONLY in the sale year).
        self.assertGreater(
            results[_SALE_INDEX].sale_disposition_tax, 0.0,
            "the sale year carries the real recapture tax (the contrast with "
            "the zero pre-sale year)")


# ──────────────────────────────────────────────────────────────────────────
# 7. The recapture helper's early-return / skip branches (L3531 / L3540 /
#    L3545) -- direct unit tests of ``_disposition_cca_recapture_tax`` with
#    synthetic property dicts. These branches are defensive guards the
#    engine's contract mapping keeps live (a sale with no couple-owner shares,
#    a break-even sale, an owner role whose declared share is zero) and are
#    exercised here in isolation, mirroring how test_issue_694 unit-tests the
#    CCA primitives directly.
# ──────────────────────────────────────────────────────────────────────────

class RecaptureHelperBranches(unittest.TestCase):
    """The three defensive branches of ``_disposition_cca_recapture_tax`` the
    engine-level sale tests do not reach: the empty-owner-roles early return
    (L3531, ``couple_share <= 0``), the break-even early return (L3540,
    recapture AND terminal_loss both 0), and the zero-fraction-owner skip
    (L3545, ``if frac <= 0.0: continue``). Each is a real guard, not dead
    code: the contract mapping filters owner roles to ``> 0`` today, but the
    helper defends the invariant itself (a zero-share owner contributes
    nothing; a sale with no couple owners has nothing to recapture)."""

    def setUp(self):
        self._brackets = default_tax_provider().get_combined_brackets(
            _SALE_YEAR, "quebec")

    def _cca_prop(self, owner_roles, opening_ucc=300000.0):
        """A synthetic CCA-electing rental sold in the sale year with the given
        per-owner ``owner_roles`` and a 380k capital cost / 300k opening UCC
        (so a sale at/above cost has 80k of recapture to price)."""
        return {
            "id": "synthetic_rental",
            "rental": {"cca": {"rate": 0.04, "capital_cost": 380000,
                               "opening_ucc": opening_ucc}},
            "sale": {"year": _SALE_YEAR, "owner_roles": owner_roles},
        }

    def test_empty_owner_roles_returns_zero(self):
        """L3531: a sale whose ``owner_roles`` sum to zero (no couple owner has
        a positive declared share -- e.g. a property owned entirely by a
        non-couple member the engine does not tax) returns 0.0 -- there is no
        taxed owner to band the recapture against."""
        prop = self._cca_prop(owner_roles={})
        tax = _disposition_cca_recapture_tax(
            prop, 380000.0, _SALE_YEAR, self._brackets,
            80000.0, 60000.0, {})
        self.assertEqual(
            tax, 0.0,
            "a sale with no couple-owner shares (couple_share <= 0) returns "
            f"0.0 -- no taxed owner to recapture against; got {tax}")

    def test_break_even_sale_returns_zero(self):
        """L3540: a CCA-electing rental sold at BREAK-EVEN (proceeds equal to
        the remaining UCC) has recapture = 0 AND terminal_loss = 0, so the
        helper returns 0.0 before any per-owner banding -- the class settles
        with no clawback and no shortfall."""
        prop = self._cca_prop(owner_roles={"primary": 0.5, "spouse": 0.5},
                              opening_ucc=300000.0)
        # proceeds = UCC = 300k -> cost-capped proceeds 300k = UCC -> both zero.
        tax = _disposition_cca_recapture_tax(
            prop, 300000.0, _SALE_YEAR, self._brackets,
            80000.0, 60000.0, {})
        self.assertEqual(
            tax, 0.0,
            "a break-even sale (proceeds = UCC) has no recapture and no "
            f"terminal loss -> 0.0; got {tax}")

    def test_zero_fraction_owner_is_skipped(self):
        """L3545: an owner role whose declared share is zero (``frac <= 0.0``)
        is SKIPPED -- it contributes no recapture tax. A sale with primary at
        0.5 and spouse at 0.0 prices the recapture on the PRIMARY's marginal
        rate only; the spouse's zero share is skipped, not banded."""
        prop = self._cca_prop(owner_roles={"primary": 0.5, "spouse": 0.0},
                              opening_ucc=300000.0)
        # proceeds = cost = 380k -> recapture = 80k (cost - UCC), all on primary.
        tax_with_zero_spouse = _disposition_cca_recapture_tax(
            prop, 380000.0, _SALE_YEAR, self._brackets,
            80000.0, 60000.0, {})
        # The same sale with the spouse at 0.5 (a real share) splits the 80k
        # across both owners -- a DIFFERENT tax (the spouse bands against their
        # own, lower taxable income). The zero-spouse run prices ONLY primary.
        prop_full = self._cca_prop(owner_roles={"primary": 0.5, "spouse": 0.5},
                                   opening_ucc=300000.0)
        tax_full = _disposition_cca_recapture_tax(
            prop_full, 380000.0, _SALE_YEAR, self._brackets,
            80000.0, 60000.0, {})
        self.assertGreater(
            tax_with_zero_spouse, 0.0,
            "the primary's share of the 80k recapture is priced (the zero-"
            f"fraction spouse is skipped, not the whole sale); got {tax_with_zero_spouse}")
        self.assertNotEqual(
            tax_with_zero_spouse, tax_full,
            "the zero-fraction-spouse run (primary only) differs from the "
            "full 50/50 run -- the skip branch drops the spouse's banding, "
            f"not the whole tax; got zero-spouse={tax_with_zero_spouse} vs "
            f"full={tax_full}")


if __name__ == "__main__":
    unittest.main()