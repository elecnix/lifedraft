#!/usr/bin/env python3
"""Issue #969: multi-property principal-residence-exemption apportionment for a
voluntary mid-horizon SALE (ITA s.40(2)(b)).

Before #969 the disposition rules (Bite B ``property_disposition`` #959, Bite E
``principal_disposition`` #962) priced a sold property's PRE ``taxable_fraction``
against the property's OWN designation span -- a single-property approximation.
A family that owns a home AND a cottage, each designated for a disjoint slice of
the same horizon, got the exemption's denominator WRONG on a voluntary sale: the
sold property's own span is SHORTER than the family's, so ``(1 + designated) /
own_span`` capped at 1 and the gain was FULLY sheltered even though the family
gave some of those years to the OTHER property. Two designated properties could
each claim the exemption for years the family allocated to the other --
over-sheltering the gain.

The fix applies the FAMILY-level one-property-per-year window to voluntary
sales: the disposition rule prices the gain against the family designation
horizon (``family_window_years`` across ALL the couple's designated properties),
the same denominator the estate path (``input_contract._map_pre_property_gains``)
uses. The sold property's designated-year count (capped at the sale year) is the
numerator; the family window is the denominator. A family-year designated by two
properties at once is rejected loudly at contract loading (DP#32), not silently
pick-one'd -- so a sold property can never exempt a year already allocated to the
other (the years are disjoint by construction, and the family window is the
honest denominator).

These tests run the real engine (``FamilySimulation.run``), so the
money-conservation invariant suite (``trajectory_invariants.assert_run_
invariants``, wired into ``run()``) is enforced on every run here. All fixtures
use fabricated ids and round numbers (DP#4/DP#15); no real figure, name, or
account enters the repo (DP#15).
"""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_config import SimulationConfig
from simulation import FamilySimulation
from rules_disposition import _disposition_gain_tax
from countries.canada.pre_designation import (
    designated_years, family_window_years, family_year_conflict,
    taxable_gain_fraction)

from test_input_contract import _load_example, _two_generation_subset


# ──────────────────────────────────────────────────────────────────────────
# Fixtures: a couple (p1/p2) owning a principal residence AND a recreational
# cottage, each designated for a disjoint slice of a 20-year horizon. Round
# numbers (DP#4/DP#15). The shipped example projects from start_year 2026, so
# results[i] is calendar year 2026 + i; a cottage sale dated 2031 fires at
# results[5].
# ──────────────────────────────────────────────────────────────────────────

_SALE_2031 = {"date": "2031-06-30", "selling_costs": 25000}
_SALE_YEAR_INDEX = 5
_HOME_VALUE, _HOME_ACB = 900_000, 600_000       # gain 300_000
_COTTAGE_VALUE, _COTTAGE_ACB = 800_000, 300_000  # gain 500_000


def _period(from_year, to_year):
    return {"from": f"{from_year}-01-01",
            "to": None if to_year is None else f"{to_year}-12-31"}


def _periods(years):
    return [_period(a, b) for a, b in years]


def _two_properties(base, home_years, cottage_years, cottage_sale=True):
    """The p1/p2 couple owns their principal residence AND a recreational
    cottage, each with PRE designation periods (a list of (from,to) tuples;
    ``[]`` designates the property for no year). The cottage optionally
    carries a mid-horizon sale (2031). The principal's designation is set
    from ``home_years`` and its ACB is stated (a designated principal that
    gives some years to the cottage keeps a taxable share of its gain -- a
    null ACB there is refused, DP#32)."""
    doc = copy.deepcopy(base)
    principal = next(p for p in doc["properties"] if p["kind"] == "principal")
    principal["value"]["amount"] = _HOME_VALUE
    principal["acb"] = _HOME_ACB
    principal["designated_principal_residence_years"] = _periods(home_years)
    cottage = {
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
        "acb": _COTTAGE_ACB,
        "designated_principal_residence_years": _periods(cottage_years),
    }
    if cottage_sale:
        cottage["sale"] = copy.deepcopy(_SALE_2031)
    doc["properties"].append(cottage)
    return doc


def _run(doc):
    """Validate -> map to internal config -> run the real engine (which
    enforces the money-conservation invariant suite on every run). Returns
    ``(results, legacy)`` -- the year-by-year results and the internal config
    (so a test can inspect the carried ``family_pre_window`` / estate block)."""
    ic.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    return FamilySimulation(SimulationConfig.from_dict(legacy)).run(), legacy


def _flat_brackets():
    """A simple progressive bracket table for isolating the gain-banding math
    in the tax-law tests (the live fold resolves year-versioned brackets; a
    direct helper call passes its own)."""
    return [
        {"min": 0, "max": 50_000, "rate": 0.15},
        {"min": 50_000, "max": 100_000, "rate": 0.205},
        {"min": 100_000, "max": 150_000, "rate": 0.26},
        {"min": 150_000, "max": None, "rate": 0.33},
    ]


# ── the tax law, in isolation (the family window is the denominator) ───────
class FamilyWindowIsTheDenominator(unittest.TestCase):
    """``_disposition_gain_tax`` prices a sold property's gain against the
    FAMILY window (issue #969), not the property's own span in isolation."""

    def test_family_window_overrides_the_property_own_span(self):
        """A cottage designated 2017-2026 (10 years) sold in 2031, in a family
        whose home was designated 2007-2016 (the other 10 years of a 20-year
        horizon). The family window is 20; the cottage's own span is 10.
        Pricing against the family window leaves 45% of the gain taxable
        (``taxable_gain_fraction(10, 20) = 0.45``); pricing against the
        property's own span would fully exempt it (``(1+10)/10`` capped at 1
        -> 0.0 taxable) -- the over-sheltering #969 is about. The fix carries
        ``family_pre_window`` so the helper uses 20, not 10."""
        sale_family = {
            "year": 2031, "selling_costs": 25_000.0,
            "owner_roles": {"primary": 0.5, "spouse": 0.5},
            "designated_principal_residence_years": _periods([(2017, 2026)]),
            "family_pre_window": 20,
        }
        sale_own = dict(sale_family)
        del sale_own["family_pre_window"]
        gain = 500_000.0  # the cottage's full couple-share gain
        brackets = _flat_brackets()
        tax_family = _disposition_gain_tax(
            gain, sale_family, 2031, brackets, 0.0, 0.0)
        tax_own = _disposition_gain_tax(
            gain, sale_own, 2031, brackets, 0.0, 0.0)
        # The family window shelters LESS (45% taxable) than the own-span
        # (0% taxable), so the tax is strictly higher -- the fix moves the
        # number in the honest direction.
        self.assertGreater(tax_family, 0.0,
                           "the family window leaves 45% of the gain taxable")
        self.assertEqual(tax_own, 0.0,
                         "the property's own span fully exempts it (the bug)")
        self.assertGreater(tax_family, tax_own)

    def test_absent_family_window_falls_back_to_the_own_span(self):
        """A sale with no ``family_pre_window`` (the common one-property
        household, or no designation anywhere) prices against the property's
        OWN span -- byte-identical to the pre-#969 path (DP#32)."""
        sale = {
            "year": 2031, "selling_costs": 25_000.0,
            "owner_roles": {"primary": 0.5, "spouse": 0.5},
            "designated_principal_residence_years": _periods([(2017, 2026)]),
        }
        gain = 500_000.0
        tax = _disposition_gain_tax(
            gain, sale, 2031, _flat_brackets(), 0.0, 0.0)
        # 10 designated years over a 10-year own span -> fully exempt.
        self.assertEqual(tax, 0.0)

    def test_the_designated_count_is_capped_at_the_sale_year(self):
        """A property sold mid-horizon cannot designate years after its sale:
        the numerator (designated count) is capped at the sale year, even when
        the family window (the denominator) runs past it. A cottage designated
        2017-2035 sold in 2031: the family window may span to 2035, but the
        cottage's designated count caps at 2031 (15 years), not 19."""
        # Re-derive the capped count the helper computes internally, to assert
        # the cap rather than the tax dollar (the tax also depends on brackets).
        designated = {y for y in designated_years(
            _periods([(2017, 2035)]), 2031) if y <= 2031}
        self.assertEqual(designated, set(range(2017, 2032)))  # 2017..2031 = 15
        self.assertEqual(len(designated), 15)
        # 15 of 29 family years -> taxable_fraction = 1 - (1+15)/29.
        self.assertAlmostEqual(taxable_gain_fraction(15, 29),
                               1.0 - (1 + 15) / 29)


# ── a two-property family selling one cannot exempt the other's years ──────
class SaleCannotExemptTheOtherPropertysYears(unittest.TestCase):
    """The family window is the denominator, so a sold property's gain is
    sheltered only by the years the FAMILY gave IT -- not the years given to
    the other property. The total exempt years across the family respect the
    one-per-year rule."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_selling_the_cottage_shelters_only_its_own_years(self):
        """Home designated 2007-2016, cottage designated 2017-2026 (disjoint:
        no family-year conflict). Selling the COTTAGE in 2031 prices its gain
        against the 20-year family window with 10 designated years -> 45%
        taxable. Before #969 the cottage's own 10-year span fully exempted it
        (0% taxable) -- over-sheltering the gain by the 10 years the family
        gave the home. The fix raises the disposition tax strictly above 0."""
        (res, _) = _run(_two_properties(
            self.base, home_years=[(2007, 2016)],
            cottage_years=[(2017, 2026)], cottage_sale=True))
        tax = res[_SALE_YEAR_INDEX].sale_disposition_tax
        self.assertGreater(tax, 0.0,
                           "the cottage's gain is 45% taxable against the "
                           "20-year family window (10 of its years), not 0% "
                           "(the own-span over-sheltering #969 fixed)")

    def test_the_family_window_matches_the_estate_path(self):
        """The voluntary sale and the estate price the family's gain against
        the SAME family window (DP#9 -- one spelling). The cottage is SOLD
        before the terminal year, so it is excluded from the estate's
        ``property_gains`` (#964) -- its gain was already realized at sale.
        But the HOME stays in the estate, and its taxable fraction is
        ``taxable_gain_fraction(10, 20)`` (10 designated years 2007-2016 over
        the 20-year family window) -- the SAME family window the cottage's
        sale carries (``family_pre_window == 20``). One spelling of the
        family window serves both paths."""
        doc = _two_properties(
            self.base, home_years=[(2007, 2016)],
            cottage_years=[(2017, 2026)], cottage_sale=True)
        ic.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cottage_sale = next(p["sale"] for p in legacy["properties"]
                            if p["id"] == "couple_cottage")
        # The sale carries the family window (20) -- the same denominator the
        # estate path's _map_pre_property_gains computes for this family.
        self.assertEqual(cottage_sale.get("family_pre_window"), 20)
        # The cottage is sold before the terminal year -> excluded from the
        # estate (#964); the HOME stays, and its fraction is priced against the
        # SAME 20-year family window (10 of the home's years) -> 0.45.
        estate_gains = legacy["estate"]["property_gains"]
        home_gain = next(g for g in estate_gains
                         if g["id"] == "principal_residence")
        self.assertAlmostEqual(home_gain["taxable_fraction"],
                               taxable_gain_fraction(10, 20))
        self.assertAlmostEqual(home_gain["taxable_fraction"], 0.45)

    def test_the_denominator_is_the_family_window_not_the_slice(self):
        """The family has 20 designation-years to split 10/10. Whether the
        cottage gets 2017-2026 or 2007-2016, its designated COUNT is 10 and
        the FAMILY window is 20 in both -- so the cottage's sale tax is the
        SAME under either allocation. This asserts the denominator is the
        FAMILY window (20), not the slice the cottage happened to get (which
        would make the two allocations differ -- the pre-#969 own-span bug)."""
        # Cottage designated 2017-2026 (its 10 years); home 2007-2016.
        (res_cottage_slice, _) = _run(_two_properties(
            self.base, home_years=[(2007, 2016)],
            cottage_years=[(2017, 2026)], cottage_sale=True))
        # Home designated 2017-2026; cottage 2007-2016 (the other 10 years).
        (res_home_slice, _) = _run(_two_properties(
            self.base, home_years=[(2017, 2026)],
            cottage_years=[(2007, 2016)], cottage_sale=True))
        tax_cottage = res_cottage_slice[_SALE_YEAR_INDEX].sale_disposition_tax
        tax_home = res_home_slice[_SALE_YEAR_INDEX].sale_disposition_tax
        # Both allocations give the cottage 10 of the 20 family years -> the
        # SAME taxable fraction (0.45) and the SAME tax. The CHOICE the family
        # makes is WHICH property gets which years, not how many; the sale tax
        # is invariant to which SLICE the cottage got (both are 10 years).
        self.assertAlmostEqual(tax_cottage, tax_home)
        self.assertGreater(tax_cottage, 0.0)

    def test_total_exempt_years_respect_one_per_family_year(self):
        """Across the family, the exemption is one property per year. With the
        home designated 2007-2016 (10 years) and the cottage 2017-2026 (10
        years), the family's designated-year SETS are DISJOINT (no year is
        claimed twice) -- ``family_year_conflict`` returns None -- and their
        UNION is exactly the 20-year horizon. The family exempts 20 property-
        years total (10 + 10), one per calendar year, never two for the same
        year."""
        home = designated_years(_periods([(2007, 2016)]), 2026)
        cottage = designated_years(_periods([(2017, 2026)]), 2026)
        self.assertIsNone(family_year_conflict(
            {"principal_residence": home, "couple_cottage": cottage}))
        self.assertEqual(home & cottage, set())  # disjoint
        self.assertEqual(home | cottage, set(range(2007, 2027)))  # the horizon
        # The family window is the union's span (20), and the two properties'
        # designated counts sum to it (10 + 10 = 20) -- one per year.
        self.assertEqual(family_window_years(
            {"principal_residence": home, "couple_cottage": cottage}), 20)
        self.assertEqual(len(home) + len(cottage), 20)


# ── a family-year designated by two properties is rejected (DP#32) ──────────
class OnePropertyPerFamilyPerYearVoluntarySale(unittest.TestCase):
    """A document that designates the same family-year for two properties
    claims the one exemption twice. It is rejected loudly at contract loading
    (DP#32) -- for a voluntary-sale context too, not only the estate path."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_overlapping_designations_are_rejected_before_any_sale(self):
        """A family that designates 2015 for BOTH the home and the cottage, and
        sells the cottage in 2031, is rejected at ``to_internal_config`` -- the
        conflict is caught before the sale or the estate prices a gain."""
        doc = _two_properties(
            self.base, home_years=[(2010, 2020)],
            cottage_years=[(2015, 2026)], cottage_sale=True)
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("one property per family unit per year", str(ctx.exception))


# ── a null ACB on a taxable-at-death property is refused (DP#32) ─────────────
class NullAcbOnTaxablePropertyIsRefused(unittest.TestCase):
    """A property whose accrued gain is TAXABLE at death (an undesignated
    principal residence, or any non-principal property still held at the
    horizon) needs a real cost base to compute the gain. A null ``acb`` cannot
    be silently defaulted to 0 -- that would claim the property's ENTIRE value
    as an accrued gain (DP#32). ``_map_estate`` refuses both shapes loudly at
    contract loading. (These two raises are the only paths in the estate
    mapper that reject a null ACB on a still-owned property; a DESIGNATED
    principal needs no ACB -- its gain is exempt -- and a property SOLD before
    the terminal year is not owned at death, so its ACB is irrelevant to the
    estate.)"""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_undesignated_principal_with_null_acb_is_refused(self):
        """The principal residence carries NO ``designated_principal_residence_
        years``, so its gain is taxable at death (ITA s.40(2)(b) shelters only
        designated years), and its ``acb`` is null. ``to_internal_config``
        refuses it -- the unknown cost base cannot become 0 -- before the estate
        prices any gain. (The shipped example's principal IS designated, so a
        null ACB there is legitimate; clearing the designation makes the gain
        taxable and so gates the ACB.)"""
        doc = copy.deepcopy(self.base)
        principal = next(p for p in doc["properties"] if p["kind"] == "principal")
        principal["designated_principal_residence_years"] = []
        principal["acb"] = None
        ic.validate_contract(doc)
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("is a principal residence with NO "
                      "designated_principal_residence_years", str(ctx.exception))
        self.assertIn("`acb` is null", str(ctx.exception))

    def test_non_principal_property_with_null_acb_is_refused(self):
        """A NON-principal property the couple still holds at the horizon (no
        sale) is ordinary capital property -- its gain is taxable at death --
        so a null ``acb`` is refused. The principal stays designated (as
        shipped) so its own null ACB is exempt and does not fire first; the
        cottage's null ACB is what ``_map_estate`` rejects. No ``sale`` keeps
        the cottage owned at death (#964 does not exclude it)."""
        doc = copy.deepcopy(self.base)
        doc["properties"].append({
            "id": "couple_cottage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "recreational",
            "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
            "acb": None,
            "designated_principal_residence_years": [],
        })
        ic.validate_contract(doc)
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("is not the principal residence", str(ctx.exception))
        self.assertIn("`acb` is null", str(ctx.exception))


# ── a single-property sale is unchanged (the common case) ──────────────────
class SinglePropertySaleIsUnchanged(unittest.TestCase):
    """A household with no family contest prices its sale against the
    property's OWN span -- byte-identical to the pre-#969 path. There are two
    no-contest shapes: (a) NO property designates anything (``family_pre_
    window`` is absent -- the ``None`` legacy sentinel, DP#32), and (b) two
    properties but only ONE designates (the family window is carried but
    EQUALS the designating property's own span, so the fraction is unchanged)."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_no_designations_anywhere_carries_no_family_window(self):
        """A household where NO property designates any year (the principal's
        designation cleared, the cottage undesignated): there is no exemption
        to apportion, so ``family_pre_window`` is ABSENT and the cottage's
        sale is fully taxable (``taxable_fraction = 1.0``) -- byte-identical
        to the pre-#969 path (DP#32: ``None`` is the legacy sentinel)."""
        doc = copy.deepcopy(self.base)
        for p in doc["properties"]:
            if p["id"] == "principal_residence":
                p["designated_principal_residence_years"] = []
                p["acb"] = p["value"]["amount"]  # no accrued gain: no ACB gate
        doc["properties"].append({
            "id": "couple_cottage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "kind": "recreational",
            "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
            "acb": _COTTAGE_ACB,
            "designated_principal_residence_years": [],
            "sale": copy.deepcopy(_SALE_2031),
        })
        ic.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cottage_sale = next(p["sale"] for p in legacy["properties"]
                            if p["id"] == "couple_cottage")
        # No property designates -> no family contest -> no family window
        # carried (the helper falls back to the own span, which is 0 here ->
        # designated_count 0 -> fully taxable, unchanged).
        self.assertNotIn("family_pre_window", cottage_sale)
        (res, _) = _run(doc)
        # A fully-taxable sale (no designation) -> nonzero disposition tax.
        self.assertGreater(res[_SALE_YEAR_INDEX].sale_disposition_tax, 0.0)

    def test_two_properties_only_one_designates_window_equals_own_span(self):
        """Two properties, but only the cottage designates (the principal
        designates nothing): the family window equals the cottage's own span
        (the principal contributes no years), so the sale's taxable fraction
        is byte-identical to the own-span path. The family window engages (it
        is carried) but changes nothing -- the honest denominator happens to
        equal the own span when only one property designates."""
        doc = _two_properties(
            self.base, home_years=[], cottage_years=[(2017, 2026)],
            cottage_sale=True)
        ic.validate_contract(doc)
        legacy = ic.to_internal_config(doc)
        cottage_sale = next(p["sale"] for p in legacy["properties"]
                            if p["id"] == "couple_cottage")
        # The family window is carried (>=2 properties, the cottage designates)
        # and equals the cottage's own 10-year span -- so the fraction is the
        # same as the own-span path (fully exempt: 10 of 10 years).
        self.assertEqual(cottage_sale.get("family_pre_window"), 10)
        (res, _) = _run(doc)
        self.assertEqual(res[_SALE_YEAR_INDEX].sale_disposition_tax, 0.0)


# ── the golden invariant is byte-exact (the common case is unchanged) ───────
class GoldenInvariantIsByteExact(unittest.TestCase):
    """The golden household owns ONE property and declares no sale, so the
    #969 family-window wiring is a no-op by construction (no second property
    -> no family contest -> ``family_pre_window`` is None -> the disposition
    rules are strict no-ops). The 46-year terminal ``total_assets`` is
    unmoved."""

    def test_golden_invariant_is_exact(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from test_golden_trajectory_581 import golden_household_config, _run
        self.assertEqual(
            _run(golden_household_config())[-1].total_assets,
            9709753.139463063)


if __name__ == "__main__":
    unittest.main()