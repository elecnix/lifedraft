"""Issue #352 -- the principal-residence exemption's C is counted PER PROPERTY.

ITA paragraph 40(2)(b) taxes ``A - A*B/C``, where C is "the number of taxation
years that end after the acquisition date during which the taxpayer owned the
property". C is a fact about ONE property.

``countries/canada/pre_designation.py`` computed instead a shared
``family_window_years`` -- the span from the earliest to the latest designated
year across ALL of the couple's properties -- and used it as the denominator for
every property. That is exact only when every property is held for the whole
window, which is false in the ordinary home-plus-cottage household: a cottage
bought years after the home has a shorter C, and pricing it against the wider
family window taxes a gain the ``1 + designated`` year exempts in full.

The root cause was a missing fact, not a wrong formula: the ``property`` record
had no acquisition date. This PR adds an optional ``acquired`` (an ISO date or a
bare year). When present, C is that property's own ownership span, in both the
estate deemed-disposition path and the voluntary-sale rule. When absent the
family window is used as before, and the run discloses that it is an
approximation (``model_fidelity`` id ``pre_family_window_denominator``).

Fabricated round numbers and role-based ids only (DP#4/DP#15).
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
    acquisition_year, ownership_years, taxable_gain_fraction,
    family_window_years, family_year_conflict)

from test_input_contract import _load_example, _two_generation_subset
import contract_schema


# ──────────────────────────────────────────────────────────────────────────
# The issue's own case, on the pure functions. The home is held 15 years and
# designated 5 (2010-2014); the cottage is held 10 years and designated for
# all 10 (2015-2024). The issue states the statutory answers outright:
# home taxable 150,000 * (1 - 6/15) = 90,000, cottage taxable 0.
# ──────────────────────────────────────────────────────────────────────────

def _period(from_year, to_year):
    return {"from": f"{from_year}-01-01",
            "to": None if to_year is None else f"{to_year}-12-31"}


def _periods(years):
    return [_period(a, b) for a, b in years]


class TestTheIssueCase(unittest.TestCase):
    """The exact figures the issue asserts, with C counted per property."""

    def test_home_taxable_gain_is_90_000(self):
        c = ownership_years(2010, 2024)
        self.assertEqual(c, 15, "the home was owned through 15 taxation years")
        self.assertAlmostEqual(150_000 * taxable_gain_fraction(5, c), 90_000)

    def test_cottage_taxable_gain_is_zero(self):
        c = ownership_years(2015, 2024)
        self.assertEqual(c, 10, "the cottage was owned through 10")
        self.assertAlmostEqual(250_000 * taxable_gain_fraction(10, c), 0.0)

    def test_the_alternate_designation_gives_the_same_answers(self):
        """Designating the cottage 2015-2023 (9 years) must leave BOTH gains
        unchanged: the home's C stays 15 and the cottage's stays 10. Under the
        family window this second designation *shrank* the denominator to 14
        and changed both -- it made the home's gain 85,714 and the cottage's
        71,429 instead of 90,000 and 0."""
        self.assertAlmostEqual(
            150_000 * taxable_gain_fraction(5, ownership_years(2010, 2024)),
            90_000)
        self.assertAlmostEqual(
            250_000 * taxable_gain_fraction(9, ownership_years(2015, 2024)),
            0.0)

    def test_the_family_window_is_what_produced_the_phantom_tax(self):
        """Today's behaviour, pinned so the regression is visible: the
        cottage's gain priced against the 15-year family window is 26.67%
        taxable -- about 17,000 of tax on a gain s.40(2)(b) exempts in full
        (at a 50% inclusion rate and a marginal rate near 50%)."""
        self.assertAlmostEqual(250_000 * taxable_gain_fraction(10, 15),
                               66_666.67, places=1)
        self.assertNotAlmostEqual(250_000 * taxable_gain_fraction(10, 15), 0.0)

    def test_a_bare_year_and_a_full_date_give_the_same_acquisition_year(self):
        """The schema accepts both (a household may know only the year)."""
        self.assertEqual(acquisition_year("2010-06-30"), 2010)
        self.assertEqual(acquisition_year(2010), 2010)

    def test_disposing_before_acquiring_is_refused(self):
        """A loud refusal, not a negative count folded into an exemption."""
        with self.assertRaises(ValueError):
            ownership_years(2020, 2010)


# ──────────────────────────────────────────────────────────────────────────
# Through the real engine. The shipped example's horizon ends in 2075, so the
# acquisitions are dated inside it: the home in 2056 (C = 20 years to 2075),
# the cottage in 2061 (C = 15). The cottage is designated for 14 of its 15
# years, which the ``1 + designated`` year exempts in full -- but which the
# 19-year family window (2056..2074) taxes at 21%.
# ──────────────────────────────────────────────────────────────────────────

_HOME_ACQUIRED, _COTTAGE_ACQUIRED = 2056, 2061
_TERMINAL_YEAR = 2075
# The home's value clears the OSFI B-20 LTV limit on the base document's
# $340k mortgage + $150k HELOC secured against it (an unrelated guard, but one
# that refuses a document rather than silently repricing the debt).
_HOME_VALUE, _HOME_ACB = 900_000, 600_000      # gain 300,000
_COTTAGE_VALUE, _COTTAGE_ACB = 650_000, 400_000  # gain 250,000
_HOME_YEARS = [(2056, 2060)]                   # 5 designated years
_COTTAGE_YEARS = [(2061, 2074)]                # 14 of its 15 owned years


def _doc(base, acquired=True, cottage_sale=False):
    doc = copy.deepcopy(base)
    principal = next(p for p in doc["properties"] if p["kind"] == "principal")
    principal["value"]["amount"] = _HOME_VALUE
    principal["acb"] = _HOME_ACB
    principal["designated_principal_residence_years"] = _periods(_HOME_YEARS)
    if acquired:
        principal["acquired"] = _HOME_ACQUIRED
    cottage = {
        "id": "couple_cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": _COTTAGE_VALUE, "as_of": "2026-06-30"},
        "acb": _COTTAGE_ACB,
        "designated_principal_residence_years": _periods(_COTTAGE_YEARS),
    }
    if acquired:
        cottage["acquired"] = _COTTAGE_ACQUIRED
    if cottage_sale:
        cottage["sale"] = {"date": "2070-06-30", "selling_costs": 0}
    doc["properties"].append(cottage)
    return doc


def _run(doc):
    """Validate -> map to internal config -> run the real engine."""
    contract_schema.validate_contract(doc)
    legacy = ic.to_internal_config(doc)
    return FamilySimulation(SimulationConfig.from_dict(legacy)).run(), legacy


class TestTheEstatePath(unittest.TestCase):
    """``contract_estate._map_pre_property_gains`` -- the deemed disposition."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def _gains(self, doc):
        legacy = ic.to_internal_config(doc)
        return {g["id"]: g for g in legacy["estate"]["property_gains"]}

    def test_c_is_each_property_s_own_ownership_span(self):
        gains = self._gains(_doc(self.base))
        # Home: C = 20 (2056..2075), 5 designated -> 1 - 6/20 = 0.70.
        self.assertAlmostEqual(gains["principal_residence"]["taxable_fraction"],
                               0.70)
        # Cottage: C = 15 (2061..2075), 14 designated -> 1 - 15/15 = 0.
        self.assertAlmostEqual(gains["couple_cottage"]["taxable_fraction"], 0.0)
        # The home's taxable gain, the figure the issue is about.
        self.assertAlmostEqual(
            300_000 * gains["principal_residence"]["taxable_fraction"], 210_000)

    def test_without_an_acquisition_the_family_window_is_still_used(self):
        """DP#32: the fallback stays, unchanged, for a document that does not
        state the fact -- 19 years of family window, so the home is 1 - 6/19
        and the cottage is taxed on a gain that is statutorily exempt."""
        gains = self._gains(_doc(self.base, acquired=False))
        self.assertAlmostEqual(
            family_window_years({
                "principal_residence": set(range(2056, 2061)),
                "couple_cottage": set(range(2061, 2075))}), 19)
        self.assertAlmostEqual(gains["principal_residence"]["taxable_fraction"],
                               1 - 6 / 19)
        self.assertAlmostEqual(gains["couple_cottage"]["taxable_fraction"],
                               1 - 15 / 19)
        # The two paths agree on what the family window is, so the fallback is
        # one spelling (DP#9), not two drifting approximations.
        legacy = ic.to_internal_config(_doc(self.base, acquired=False))
        self.assertEqual(legacy["estate"]["property_gains"][0]["taxable_fraction"],
                         1 - 6 / 19)

    def test_the_acquisition_changes_the_estate_gain_and_nothing_else(self):
        """The fix is scoped to the fraction: the fmv and acb the estate
        carries are identical with and without the acquisition date."""
        with_acq = self._gains(_doc(self.base))
        without = self._gains(_doc(self.base, acquired=False))
        for pid in with_acq:
            self.assertEqual(with_acq[pid]["fmv"], without[pid]["fmv"])
            self.assertEqual(with_acq[pid]["acb"], without[pid]["acb"])
        self.assertNotEqual(with_acq["couple_cottage"]["taxable_fraction"],
                            without["couple_cottage"]["taxable_fraction"])


class TestTheVoluntarySalePath(unittest.TestCase):
    """``rules_disposition._disposition_gain_tax`` -- a mid-horizon sale."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_the_sale_carries_the_raw_acquisition_to_the_rule(self):
        legacy = ic.to_internal_config(_doc(self.base, cottage_sale=True))
        sale = next(p["sale"] for p in legacy["properties"]
                    if p["id"] == "couple_cottage")
        self.assertEqual(sale["acquired"], _COTTAGE_ACQUIRED)

    def test_the_rule_counts_c_per_property_when_the_sale_carries_it(self):
        """The cottage sells in 2070 having been owned 10 years (2061..2070)
        and designated all 10 -- exempt in full -- while the family window is
        15, which would tax it at 1 - 11/15."""
        own = ownership_years(_COTTAGE_ACQUIRED, 2070)
        self.assertEqual(own, 10)
        self.assertAlmostEqual(taxable_gain_fraction(10, own), 0.0)
        self.assertNotAlmostEqual(taxable_gain_fraction(10, 15), 0.0)

    def test_the_principal_s_own_sale_carries_the_acquisition_too(self):
        """The principal residence's OWN sale (a downsize) is a separate mapper
        from the non-principal property sale. Both must carry the acquisition,
        or the same household is priced two ways depending on which property
        moved (DP#9 -- one spelling of the carried fact)."""
        doc = _doc(self.base)
        principal = next(p for p in doc["properties"]
                         if p["kind"] == "principal")
        principal["sale"] = {"date": "2070-06-30", "selling_costs": 0}
        legacy = ic.to_internal_config(doc)
        sale = legacy["property"]["principal_sale"]
        self.assertEqual(sale["acquired"], _HOME_ACQUIRED)

    def test_the_principal_s_sale_without_an_acquisition_carries_none(self):
        """Absence stays absent -- no invented acquisition year (DP#32). The
        key is simply not carried, so the rule falls back to the family
        window."""
        doc = _doc(self.base, acquired=False)
        principal = next(p for p in doc["properties"]
                         if p["kind"] == "principal")
        principal["sale"] = {"date": "2070-06-30", "selling_costs": 0}
        sale = ic.to_internal_config(doc)["property"]["principal_sale"]
        self.assertNotIn("acquired", sale)

    def test_a_sale_without_an_acquisition_keeps_the_family_window(self):
        """The byte-identical fallback, on the rule's own input."""
        sale = {"year": 2070, "owner_roles": {"p1": 1.0},
                "designated_principal_residence_years":
                    _periods([(2061, 2070)]),
                "selling_costs": 0.0,
                "family_pre_window": 15}
        brackets = [{"min": 0, "max": None, "rate": 0.5}]
        tax_family = _disposition_gain_tax(
            50_000, sale, 2070, brackets, 0.0, 0.0)
        sale_with_acq = dict(sale, acquired=_COTTAGE_ACQUIRED)
        tax_own = _disposition_gain_tax(
            50_000, sale_with_acq, 2070, brackets, 0.0, 0.0)
        self.assertGreater(tax_family, 0.0)
        self.assertAlmostEqual(tax_own, 0.0)


class TestTheDocumentsOwnGainIsRepriced(unittest.TestCase):
    """End to end, through ``FamilySimulation.run()``: a document that states
    the acquisition gets the statutory apportionment, and the estate's
    disclosed gain actually moves."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def test_the_run_completes_with_the_acquisition_declared(self):
        (results, legacy) = _run(_doc(self.base))
        self.assertTrue(results)
        gains = {g["id"]: g for g in legacy["estate"]["property_gains"]}
        self.assertAlmostEqual(gains["couple_cottage"]["taxable_fraction"], 0.0,
                               msg="a cottage designated for all but one of "
                                   "the years it was owned is exempt")

    def test_no_family_year_is_claimed_twice(self):
        """The per-property change must not disturb the one-per-year rule."""
        legacy = ic.to_internal_config(_doc(self.base))
        designations = {
            "principal_residence": set(range(2056, 2061)),
            "couple_cottage": set(range(2061, 2075)),
        }
        self.assertIsNone(family_year_conflict(designations))


class TestTheApproximationIsDisclosed(unittest.TestCase):
    """#352: when the acquisition is absent the figure is approximate, and the
    run says so. DP#32 -- absence is disclosed, not silently substituted."""

    def setUp(self):
        self.base = _two_generation_subset(_load_example())

    def _active_ids(self, doc):
        import model_fidelity as mf
        legacy = ic.to_internal_config(doc)
        return {a.id for a in mf.active_approximations(legacy)}

    def test_it_fires_while_the_family_window_is_the_denominator(self):
        self.assertIn('pre_family_window_denominator',
                      self._active_ids(_doc(self.base, acquired=False)))

    def test_it_does_not_fire_once_every_property_states_its_acquisition(self):
        """Statutory C is used, so there is nothing to caveat -- and a caveat
        for an approximation that no longer exists is its own defect."""
        self.assertNotIn('pre_family_window_denominator',
                         self._active_ids(_doc(self.base)))

    def test_it_does_not_fire_for_a_single_property_household(self):
        """One property's own span IS the family window, so the denominator is
        correct and no approximation exists to disclose."""
        doc = _doc(self.base, acquired=False)
        doc["properties"] = [p for p in doc["properties"]
                             if p["kind"] == "principal"]
        self.assertNotIn('pre_family_window_denominator',
                         self._active_ids(doc))


if __name__ == "__main__":
    unittest.main()
