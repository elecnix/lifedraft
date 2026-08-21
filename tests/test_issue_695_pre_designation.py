#!/usr/bin/env python3
"""Issue #695 (epic #690, bite 4): the principal-residence exemption is one
property per family unit per year -- and the schema field for it finally moves
the tax.

Before this bite only the designation's PRESENCE was read: the principal was
exempt iff it declared ANY year, and every other property was taxed in full. So a
family that owns a home AND a cottage could not trade the exemption between them
-- the year RANGES were parsed and never compared (the exact "read by nothing"
trap #695 names). This wires ``designated_principal_residence_years`` into the
deemed-disposition gain calc (ITA s.40(2)(b)):

  - one property per family unit per calendar year (a document that designates
    the same year for two properties is REJECTED, not silently pick-one'd);
  - the exemption is apportioned per property from the designated years, so
    designating the higher-gain property shelters more of the family's gain;
  - absent a genuine two-property contest, behaviour is byte-identical.

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
import objective
from countries.canada.estate import EstatePlan, EstateInputError
from countries.canada.pre_designation import (
    designated_years, family_window_years, taxable_gain_fraction,
    family_year_conflict)

from test_input_contract import _load_example, _two_generation_subset


# The couple's home appreciates less than their cottage: gain 300k vs 500k. The
# exemption is worth MORE on the cottage, so an optimizing family designates it.
_HOME_VALUE, _HOME_ACB = 900_000, 600_000       # gain 300_000
_COTTAGE_VALUE, _COTTAGE_ACB = 800_000, 300_000  # gain 500_000


def _period(from_year, to_year):
    return {"from": f"{from_year}-01-01",
            "to": None if to_year is None else f"{to_year}-12-31"}


def _two_properties(base, home_years, cottage_years):
    """The p1/p2 couple owns their principal residence AND a recreational
    cottage, each with an ACB and a list of (from,to) designation-year tuples.
    ``[]`` means the property is designated for no year."""
    doc = copy.deepcopy(base)
    principal = next(p for p in doc["properties"] if p["kind"] == "principal")
    principal["value"]["amount"] = _HOME_VALUE
    principal["acb"] = _HOME_ACB
    principal["designated_principal_residence_years"] = [
        _period(a, b) for a, b in home_years]
    doc["properties"].append({
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
        "acb": _COTTAGE_ACB,
        "designated_principal_residence_years": [
            _period(a, b) for a, b in cottage_years],
        "rental": None,
    })
    return doc


def _estate(doc):
    ic.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    results = FamilySimulation(cfg := SimulationConfig.from_dict(legacy)).run()
    return objective.compute_after_tax_estate(results, legacy), legacy


# ── the tax law, in isolation (countries/canada/pre_designation) ─────────────
class PREDesignationTaxLaw(unittest.TestCase):
    def test_open_period_runs_through_as_of_year(self):
        # `to: None` means still designated as of the document's as_of.
        self.assertEqual(
            designated_years([_period(2020, None)], as_of_year=2024),
            {2020, 2021, 2022, 2023, 2024})

    def test_designated_years_unions_the_periods(self):
        years = designated_years([_period(2010, 2012), _period(2015, 2016)], 2026)
        self.assertEqual(years, {2010, 2011, 2012, 2015, 2016})

    def test_window_is_the_span_across_all_properties(self):
        by_prop = {"home": {2007, 2008}, "cottage": {2024, 2025, 2026}}
        # earliest 2007 .. latest 2026 inclusive = 20 years.
        self.assertEqual(family_window_years(by_prop), 20)

    def test_full_designation_fully_exempts_the_gain(self):
        # 20 designated years out of a 20-year window: (1+20)/20 capped at 1.
        self.assertAlmostEqual(taxable_gain_fraction(20, 20), 0.0)

    def test_zero_designated_years_is_fully_taxable_no_bonus(self):
        # The "+1" bonus applies only to a property designated at least one year.
        self.assertAlmostEqual(taxable_gain_fraction(0, 20), 1.0)

    def test_partial_designation_apportions_with_the_plus_one(self):
        # 10 of 20 years: exempt (1+10)/20 = 0.55 -> taxable 0.45.
        self.assertAlmostEqual(taxable_gain_fraction(10, 20), 0.45)

    def test_conflict_is_returned_as_data_not_raised(self):
        # Properties are scanned in id order, so 'cottage' claims 2011 first and
        # 'home' is the one caught double-claiming it.
        conflict = family_year_conflict({"home": {2010, 2011}, "cottage": {2011}})
        self.assertEqual(conflict, (2011, "cottage", "home"))

    def test_no_conflict_when_years_are_disjoint(self):
        self.assertIsNone(
            family_year_conflict({"home": {2010}, "cottage": {2011}}))

    def test_a_period_that_ends_before_it_begins_is_rejected(self):
        with self.assertRaises(ValueError):
            designated_years([_period(2020, 2015)], as_of_year=2026)

    def test_window_is_zero_when_nothing_is_designated(self):
        self.assertEqual(family_window_years({"home": set(), "cottage": set()}), 0)

    def test_fraction_needs_a_positive_window_when_years_are_designated(self):
        with self.assertRaises(ValueError):
            taxable_gain_fraction(designated_count=5, window_years=0)


# ── the exemption now moves the tax (DP#17 rule-path) ────────────────────────
class DesignationMovesTheGainTax(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_designating_the_higher_gain_property_shelters_more(self):
        """The family owns a 300k-gain home and a 500k-gain cottage over the same
        20-year window. Designating the COTTAGE (the larger gain) taxes only the
        home's 300k; designating the HOME taxes the cottage's 500k. So the
        cottage designation ends with LESS deemed-disposition tax and a larger
        net estate -- the choice, and the dollars, #695 says must exist."""
        cottage_designated, _ = _estate(
            _two_properties(self.base, home_years=[], cottage_years=[(2007, None)]))
        home_designated, _ = _estate(
            _two_properties(self.base, home_years=[(2007, None)], cottage_years=[]))

        self.assertLess(cottage_designated.total_tax, home_designated.total_tax)
        self.assertGreater(
            cottage_designated.net_estate, home_designated.net_estate)

    def test_the_year_ranges_reach_the_internal_config(self):
        """#695's core: the ranges are no longer parsed-then-dropped. Two
        allocations of the SAME two properties produce different per-property
        taxable fractions in the estate block."""
        _, cfg_cottage = _estate(
            _two_properties(self.base, home_years=[], cottage_years=[(2007, None)]))
        _, cfg_home = _estate(
            _two_properties(self.base, home_years=[(2007, None)], cottage_years=[]))
        frac_cottage = {g["id"]: g["taxable_fraction"]
                        for g in cfg_cottage["estate"]["property_gains"]}
        frac_home = {g["id"]: g["taxable_fraction"]
                     for g in cfg_home["estate"]["property_gains"]}
        # Designating the cottage exempts it (0.0) and fully taxes the home (1.0);
        # designating the home flips both.
        self.assertAlmostEqual(frac_cottage["couple_cottage"], 0.0)
        self.assertAlmostEqual(frac_cottage["principal_residence"], 1.0)
        self.assertAlmostEqual(frac_home["couple_cottage"], 1.0)
        self.assertAlmostEqual(frac_home["principal_residence"], 0.0)

    def test_partial_split_lands_between_the_two_extremes(self):
        """Splitting the 20-year window 10/10 leaves BOTH properties partly
        taxed, so its total tax is strictly between designating either one
        outright -- a genuine per-year apportionment, not all-or-nothing."""
        split, _ = _estate(_two_properties(
            self.base, home_years=[(2007, 2016)], cottage_years=[(2017, None)]))
        cottage_only, _ = _estate(_two_properties(
            self.base, home_years=[], cottage_years=[(2007, None)]))
        home_only, _ = _estate(_two_properties(
            self.base, home_years=[(2007, None)], cottage_years=[]))
        self.assertLess(cottage_only.total_tax, split.total_tax)
        self.assertLess(split.total_tax, home_only.total_tax)


# ── one property per family unit per year (the enforcement) ──────────────────
class OnePropertyPerFamilyPerYear(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_overlapping_designations_are_rejected(self):
        """A document that designates the same year for BOTH properties claims
        the one exemption twice -- rejected loudly, not silently resolved."""
        doc = _two_properties(
            self.base, home_years=[(2007, 2016)], cottage_years=[(2010, 2020)])
        with self.assertRaises(ic.ContractAdaptationError) as ctx:
            ic.to_internal_config(doc)
        self.assertIn("one property per family unit per year", str(ctx.exception))

    def test_a_partly_taxable_principal_residence_needs_an_acb(self):
        """A DESIGNATED principal residence needed no ACB while it was all-or-
        nothing exempt. Once it is only PARTIALLY exempt (the family gave some
        years to the cottage), it keeps a taxable share of its gain -- so a null
        ACB can no longer be defaulted to 0 (DP#32)."""
        doc = _two_properties(
            self.base, home_years=[(2007, 2016)], cottage_years=[(2017, None)])
        principal = next(p for p in doc["properties"] if p["kind"] == "principal")
        principal["acb"] = None
        with self.assertRaises(ic.ContractAdaptationError):
            ic.to_internal_config(doc)


# ── the estate plan rejects a malformed property_gains entry ─────────────────
class EstatePlanValidatesPropertyGains(unittest.TestCase):
    """``EstatePlan.__post_init__`` guards the new per-property gains the same way
    it guards every other estate magnitude: a taxable_fraction outside [0, 1] or a
    negative fmv/acb is a malformed plan, not a number to silently clamp."""

    def _plan(self, gain):
        return EstatePlan(spousal_rollover=True, tfsa_successor_holder=True,
                          non_reg_primary_share=0.5, property_gains=(gain,))

    def test_a_taxable_fraction_outside_0_1_is_rejected(self):
        with self.assertRaises(EstateInputError):
            self._plan({"id": "x", "fmv": 100.0, "acb": 50.0,
                        "taxable_fraction": 1.5, "is_principal": False})

    def test_a_negative_fmv_or_acb_is_rejected(self):
        with self.assertRaises(EstateInputError):
            self._plan({"id": "x", "fmv": -1.0, "acb": 50.0,
                        "taxable_fraction": 0.5, "is_principal": False})


# ── DP#32: absent a two-property contest, nothing changes ────────────────────
class TestAbsenceIsNoOp(unittest.TestCase):
    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_single_property_household_gets_no_pre_allocation(self):
        """The shipped couple owns one property: the per-year allocation does not
        engage, so the estate block carries property_gains=None (the legacy
        presence-flag path)."""
        cfg = ic.to_internal_config(self.base)
        self.assertIsNone(cfg["estate"]["property_gains"])

    def test_two_properties_but_no_designation_stays_legacy(self):
        """Two properties, neither designated for any year: nothing to allocate,
        so the legacy path runs (property_gains=None) and both gains are taxed in
        full exactly as before this bite."""
        doc = _two_properties(self.base, home_years=[], cottage_years=[])
        cfg = ic.to_internal_config(doc)
        self.assertIsNone(cfg["estate"]["property_gains"])

    def test_golden_invariant_is_exact(self):
        """The golden household has no second property, so PRE wiring is a no-op
        by construction -- the 46-year terminal total_assets is unmoved."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        from test_golden_trajectory_581 import golden_household_config, _run
        self.assertEqual(
            _run(golden_household_config())[-1].total_assets, 9709753.139463063)


if __name__ == "__main__":
    unittest.main()
