#!/usr/bin/env python3
"""Issue #956 bite C: sweepable numeric ``year`` leaves for purchase & sale.

The purchase/sale TIMING ("when is the optimal year to buy/sell?") needs the
calendar YEAR to be a numeric leaf ``sensitivity.sweeps`` can range. Before
this bite the year was derived by string-slicing a date (``int(date[:4])``):
``sweep.resolve_leaf`` requires a PRE-EXISTING numeric leaf, and
``sensitivity.sweeps`` values are number-only -- so timing was not sweepable.

This bite adds an OPTIONAL numeric ``year`` to ``property_purchase`` and
``property_sale`` as a first-class sweepable leaf, with a ``date``-xor-``year``
``oneOf`` (exactly one of the two must be given). When ``year`` is present the
mapper uses it directly; when absent it falls back to ``int(date[:4])`` exactly
as before (byte-identical round-trip, DP#32 -- never ``x or DEFAULT``).

These tests lock:
  - the mapper uses ``year`` when present (both purchase and sale), and still
    derives from ``date`` when ``year`` is absent (byte-identical);
  - a purchase declared with ``year`` and one with the equivalent ``date``
    produce the SAME mapped entry (equivalence);
  - ``sweep.resolve_leaf`` finds ``properties.N.purchase.year`` and
    ``properties.N.sale.year`` when declared, and a sweep can set them;
  - the schema validates a property declaring ``purchase: {year, closing_costs}``
    and REJECTS one giving BOTH ``date`` and ``year``, or NEITHER (the oneOf);
    the symmetric REJECTs hold for ``sale``.

Mirrors ``tests/test_issue_956_property_appreciation.py`` and
``tests/test_issue_696_dated_purchase.py`` for style. Round numbers per DP#15.
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
import sweep

from test_input_contract import _load_example, _two_generation_subset


# ──────────────────────────────────────────────────────────────────────────
# Minimal doc helpers (mirror test_issue_956_property_appreciation.py)
# ──────────────────────────────────────────────────────────────────────────

def _doc_with_cottage(purchase=None, sale=None):
    """Minimal contract doc: the couple (p1/p2) jointly owns a cottage worth
    500k with a 300k mortgage secured against it (net_equity 200k at the
    couple's 100% share). Optional ``purchase``/``sale`` are attached when
    given. The cottage is the ONLY property -> it lives at index 0, so
    ``sweep.resolve_leaf(doc, "properties.0.purchase.year")`` resolves."""
    prop = {
        "id": "cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": 500000, "as_of": "2026-06-30"},
        "acb": 500000,
        "designated_principal_residence_years": [],
    }
    if purchase is not None:
        prop["purchase"] = purchase
    if sale is not None:
        prop["sale"] = sale
    return {
        "properties": [prop],
        "liabilities": [{
            "id": "cottage_mortgage", "kind": "mortgage", "collateral": "cottage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "balance": {"amount": 300000, "as_of": "2026-06-30"},
            "rate": 0.05, "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 1500},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
        }],
    }


def _full_doc_with_cottage(purchase=None, sale=None):
    """The full 4-generation example with a cottage appended, for the schema
    oneOf validation cases (the minimal doc lacks the people/accounts the
    composed schema requires)."""
    doc = _two_generation_subset(_load_example())
    doc = copy.deepcopy(doc)
    prop = {
        "id": "cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": 500000, "as_of": "2026-06-30"},
        "acb": 500000,
        "designated_principal_residence_years": [],
    }
    if purchase is not None:
        prop["purchase"] = purchase
    if sale is not None:
        prop["sale"] = sale
    doc["properties"].append(prop)
    return doc


# ──────────────────────────────────────────────────────────────────────────
# Piece 1 -- the mapper
# ──────────────────────────────────────────────────────────────────────────

class MapperUsesYearWhenPresent(unittest.TestCase):
    """A declared numeric ``year`` leaf is read directly (never derived from a
    date), for BOTH purchase and sale."""

    def test_purchase_year_used_directly(self):
        entry = ic._map_owned_properties(
            _doc_with_cottage(purchase={"year": 2030, "closing_costs": 10000}),
            "p1", "p2")[0]
        self.assertEqual(entry["purchase"],
                         {"year": 2030, "closing_costs": 10000.0})

    def test_sale_year_used_directly(self):
        entry = ic._map_owned_properties(
            _doc_with_cottage(sale={"year": 2031, "selling_costs": 25000}),
            "p1", "p2")[0]
        self.assertEqual(entry["sale"]["year"], 2031)
        self.assertEqual(entry["sale"]["selling_costs"], 25000.0)


class MapperDerivesFromDateWhenYearAbsent(unittest.TestCase):
    """DP#32 byte-identical round-trip: when ``year`` is absent the year is
    derived from ``date`` exactly as before this bite (``int(date[:4])``),
    never ``x or DEFAULT``."""

    def test_purchase_derives_year_from_date(self):
        entry = ic._map_owned_properties(
            _doc_with_cottage(purchase={"date": "2030-06-30",
                                        "closing_costs": 10000}),
            "p1", "p2")[0]
        self.assertEqual(entry["purchase"],
                         {"year": 2030, "closing_costs": 10000.0})

    def test_sale_derives_year_from_date(self):
        entry = ic._map_owned_properties(
            _doc_with_cottage(sale={"date": "2031-06-30", "selling_costs": 25000}),
            "p1", "p2")[0]
        self.assertEqual(entry["sale"]["year"], 2031)


class YearAndEquivalentDateProduceSameEntry(unittest.TestCase):
    """Equivalence: a purchase/sale declared with ``year: N`` and one with the
    equivalent ``date: "N-..."`` map to the SAME internal entry -- so a sweep
    over ``year`` is a faithful stand-in for the dated declaration."""

    def test_purchase_equivalence(self):
        by_year = ic._map_owned_properties(
            _doc_with_cottage(purchase={"year": 2030, "closing_costs": 10000}),
            "p1", "p2")[0]["purchase"]
        by_date = ic._map_owned_properties(
            _doc_with_cottage(purchase={"date": "2030-06-30",
                                        "closing_costs": 10000}),
            "p1", "p2")[0]["purchase"]
        self.assertEqual(by_year, by_date)

    def test_sale_equivalence(self):
        by_year = ic._map_owned_properties(
            _doc_with_cottage(sale={"year": 2031, "selling_costs": 25000}),
            "p1", "p2")[0]["sale"]
        by_date = ic._map_owned_properties(
            _doc_with_cottage(sale={"date": "2031-06-30", "selling_costs": 25000}),
            "p1", "p2")[0]["sale"]
        self.assertEqual(by_year, by_date)


# ──────────────────────────────────────────────────────────────────────────
# Piece 2 -- sweepability
# ──────────────────────────────────────────────────────────────────────────

class YearIsASweepableLeaf(unittest.TestCase):
    """``sweep.resolve_leaf`` finds the numeric ``year`` leaf when declared, and
    a sweep can set it -- the whole point of the bite."""

    def test_purchase_year_resolves(self):
        doc = _doc_with_cottage(purchase={"year": 2030, "closing_costs": 10000})
        container, key = sweep.resolve_leaf(doc, "properties.0.purchase.year")
        self.assertEqual(container[key], 2030)
        container[key] = 2032                      # a sweep can set it
        self.assertEqual(doc["properties"][0]["purchase"]["year"], 2032)

    def test_sale_year_resolves(self):
        doc = _doc_with_cottage(sale={"year": 2031, "selling_costs": 25000})
        container, key = sweep.resolve_leaf(doc, "properties.0.sale.year")
        self.assertEqual(container[key], 2031)
        container[key] = 2036                      # a sweep can set it
        self.assertEqual(doc["properties"][0]["sale"]["year"], 2036)

    def test_purchase_year_absent_does_not_resolve(self):
        """A purchase declared with ``date`` (no ``year``): ``resolve_leaf``
        fails loudly on the missing leaf -- a sweep may only vary a value the
        household actually declared (DP#18/DP#32)."""
        doc = _doc_with_cottage(purchase={"date": "2030-06-30",
                                          "closing_costs": 10000})
        with self.assertRaises(sweep.SweepPathError):
            sweep.resolve_leaf(doc, "properties.0.purchase.year")


# ──────────────────────────────────────────────────────────────────────────
# Piece 3 -- the schema (oneOf: exactly one of date/year)
# ──────────────────────────────────────────────────────────────────────────

class SchemaOneOfDateXorYear(unittest.TestCase):
    """The schema validates a property declaring ``purchase``/``sale`` with
    ``year`` (and the required sibling), and REJECTS one giving BOTH ``date``
    and ``year``, or NEITHER -- the ``oneOf`` enforces exactly one."""

    def test_purchase_year_only_is_valid(self):
        ic.validate_contract(
            _full_doc_with_cottage(purchase={"year": 2030,
                                              "closing_costs": 10000}))

    def test_sale_year_only_is_valid(self):
        ic.validate_contract(_full_doc_with_cottage(sale={"year": 2030}))

    def test_purchase_both_date_and_year_is_rejected(self):
        doc = _full_doc_with_cottage(
            purchase={"date": "2030-06-30", "year": 2030, "closing_costs": 10000})
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_purchase_neither_date_nor_year_is_rejected(self):
        doc = _full_doc_with_cottage(purchase={"closing_costs": 10000})
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_sale_both_date_and_year_is_rejected(self):
        doc = _full_doc_with_cottage(sale={"date": "2030-06-30", "year": 2030})
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_sale_neither_date_nor_year_is_rejected(self):
        doc = _full_doc_with_cottage(sale={})
        with self.assertRaises(ic.ContractValidationError):
            ic.validate_contract(doc)

    def test_purchase_date_only_still_valid(self):
        """Round-trip: the existing dated declaration still validates."""
        ic.validate_contract(
            _full_doc_with_cottage(purchase={"date": "2030-06-30",
                                              "closing_costs": 10000}))

    def test_sale_date_only_still_valid(self):
        ic.validate_contract(_full_doc_with_cottage(sale={"date": "2030-06-30"}))


class SchemaCarriesYearLeaf(unittest.TestCase):
    """Both ``property_purchase`` and ``property_sale`` carry the new numeric
    ``year`` property (integer | null), and the ``oneOf`` date-xor-year."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "schema", "input_schema.json")) as f:
            self.schema = json.load(f)

    def _check_year_leaf(self, def_name):
        d = self.schema["$defs"][def_name]
        # `year` is an integer | null leaf.
        yr = d["properties"]["year"]
        self.assertEqual(len(yr["anyOf"]), 2)
        self.assertTrue(any(t.get("type") == "integer" for t in yr["anyOf"]))
        self.assertTrue(any(t.get("type") == "null" for t in yr["anyOf"]))
        # oneOf enforces exactly one of date/year.
        self.assertEqual([sorted(o["required"]) for o in d["oneOf"]],
                         [["date"], ["year"]])

    def test_property_purchase_has_year_leaf(self):
        self._check_year_leaf("property_purchase")
        # closing_costs remains top-level required for a purchase.
        self.assertEqual(self.schema["$defs"]["property_purchase"]["required"],
                         ["closing_costs"])

    def test_property_sale_has_year_leaf(self):
        self._check_year_leaf("property_sale")
        # sale has nothing else top-level required (selling_costs is optional).
        self.assertNotIn("required", self.schema["$defs"]["property_sale"])


if __name__ == "__main__":
    unittest.main()