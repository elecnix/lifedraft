#!/usr/bin/env python3
"""Issue #956 bite B: a dated mid-horizon voluntary property SALE.

This is the symmetric INVERSE of #696's mid-horizon purchase: a property
declaring ``sale`` (a date + optional selling costs) LEAVES the balance sheet
in the calendar year of ``sale.date``, rather than being held to the horizon.
These tests lock the MECHANICAL foundation layer only:

  - the mapper emits ``entry["sale"] = {"year", "selling_costs"}`` with the
    couple's share applied, and carries ``value_share`` / ``secured_share`` /
    ``acb_share`` so the tax + proceeds layer built on top can price the
    disposition gain (value_share - acb_share) and the debt retired
    (secured_share);
  - a property with NEITHER ``appreciation_rate`` NOR ``sale`` round-trips
    byte-identically to #692/#696 (DP#32 -- none of the share fields are
    emitted, no ``sale`` key);
  - ``_property_equity_for_year`` returns 0.0 from the sale year ONWARD
    (``cal_year >= sale['year']``) and the normal equity BEFORE it, and the
    sale gate WINS over the appreciation branch (a sold property has no equity
    regardless of appreciation);
  - the schema is valid JSON and carries the new ``property_sale`` def and
    ``sale`` slot on the ``property`` def.

The tax + proceeds layer (net proceeds invested, disposition gain taxed,
money conservation) is built ON TOP of this bite by the orchestrator; the full
suite and golden fixture are deliberately NOT run here -- money conservation
fails until that layer lands, which is expected, not a defect of this layer.

All fixtures use fabricated ids and round numbers (DP#4/DP#15).
"""

import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import input_contract as ic
from simulation_state import _property_equity_for_year

from test_input_contract import _load_example, _two_generation_subset


def _doc_with_cottage(sale=None, appreciation_rate=None, acb=500000):
    """Minimal contract doc: the couple (p1/p2) jointly owns a cottage worth
    500k with a 300k mortgage secured against it, so net_equity is 200k at the
    couple's 100% share. ``acb`` defaults to 500k (bought at value: no accrued
    gain yet); pass a different number to model a gain. Optional ``sale`` and
    ``appreciation_rate`` are attached when given."""
    prop = {
        "id": "cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5},
                            {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": 500000, "as_of": "2026-06-30"},
        "acb": acb,
        "designated_principal_residence_years": [],
    }
    if appreciation_rate is not None:
        prop["appreciation_rate"] = appreciation_rate
    if sale is not None:
        prop["sale"] = sale
    return {
        "properties": [prop],
        "liabilities": [{
            "id": "cottage_mortgage",
            "kind": "mortgage", "collateral": "cottage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5},
                                {"person": "p2", "pct": 0.5}]},
            "balance": {"amount": 300000, "as_of": "2026-06-30"},
            "rate": 0.05, "rate_type": "fixed",
            "amortization": {"years": 20, "payment_monthly": 1500},
            "renewal_date": "2029-06-01",
            "term_start_date": "2024-06-01",
        }],
    }


# ──────────────────────────────────────────────────────────────────────────
# Piece 2 -- the mapper
# ──────────────────────────────────────────────────────────────────────────

class MapperEmitsSale(unittest.TestCase):
    def test_sale_maps_year_and_selling_costs(self):
        sale = {"date": "2031-06-30", "selling_costs": 25000}
        entry = ic._map_owned_properties(_doc_with_cottage(sale=sale), "p1", "p2")[0]
        # Couple owns 100% -> selling costs pass through undivided. The per-owner
        # role split (owner_roles) is carried so the disposition rule can price
        # the gain per owner (Canada has no joint filing); the couple owns the
        # cottage 50/50 -> {'primary': 0.5, 'spouse': 0.5}. The PRE designation
        # periods are carried raw so the rule can apportion the gain (ITA
        # s.40(2)(b)); [] here = no designation -> fully taxable gain.
        self.assertEqual(entry["sale"], {"year": 2031, "selling_costs": 25000.0,
                                          "owner_roles": {"primary": 0.5,
                                                          "spouse": 0.5},
                                          "designated_principal_residence_years": []})

    def test_null_selling_costs_is_zero(self):
        """DP#32: a null selling_costs is a real $0 in disposition costs, not an
        unknown -- the mapper carries 0.0, never silently dropping the key."""
        sale = {"date": "2031-06-30", "selling_costs": None}
        entry = ic._map_owned_properties(_doc_with_cottage(sale=sale), "p1", "p2")[0]
        self.assertEqual(entry["sale"], {"year": 2031, "selling_costs": 0.0,
                                          "owner_roles": {"primary": 0.5,
                                                          "spouse": 0.5},
                                          "designated_principal_residence_years": []})

    def test_selling_costs_taken_at_the_couples_share(self):
        """A property the couple owns HALF of (a child owns the other half):
        the couple's share of selling costs is taken, the same share its
        net_equity is taken at (#692)."""
        doc = _doc_with_cottage(sale={"date": "2031-06-30", "selling_costs": 20000})
        doc["properties"][0]["owner"] = {
            "joint": [{"person": "p1", "pct": 0.25},
                      {"person": "p2", "pct": 0.25},
                      {"person": "ca", "pct": 0.5}]}
        doc["liabilities"][0]["owner"] = doc["properties"][0]["owner"]
        owned = ic._map_owned_properties(doc, "p1", "p2")
        # Couple owns 50% (p1+p2 each 25%); the other 50% is the child's. The
        # couple's share of selling costs is 10000 (half of 20000), and the
        # per-owner role split is each spouse's own 25%.
        self.assertEqual(owned[0]["sale"],
                         {"year": 2031, "selling_costs": 10000.0,
                          "owner_roles": {"primary": 0.25, "spouse": 0.25},
                          "designated_principal_residence_years": []})


class MapperCarriesSharesWhenSaleDeclared(unittest.TestCase):
    """A sale needs the SAME couple-share gross value, secured mortgage, and
    adjusted cost base the appreciation branch needs -- so the mapper carries
    value_share / secured_share / acb_share whenever appreciation_rate OR sale
    is declared. The tax + proceeds layer consumes all three."""

    def test_shares_emitted_when_sale_declared_no_rate(self):
        sale = {"date": "2031-06-30", "selling_costs": 0}
        entry = ic._map_owned_properties(_doc_with_cottage(sale=sale), "p1", "p2")[0]
        self.assertNotIn("appreciation_rate", entry)      # no rate declared
        self.assertEqual(entry["value_share"], 500000)    # couple owns 100%
        self.assertEqual(entry["secured_share"], 300000)
        self.assertEqual(entry["acb_share"], 500000)      # acb == value here
        self.assertEqual(entry["net_equity"], 200000)
        self.assertIn("sale", entry)

    def test_acb_share_reflects_a_real_cost_base(self):
        """acb 400k against a 500k value -> a 100k accrued gain the tax layer
        will price. acb_share is the couple's share of the bare-number acb."""
        sale = {"date": "2031-06-30"}
        entry = ic._map_owned_properties(
            _doc_with_cottage(sale=sale, acb=400000), "p1", "p2")[0]
        self.assertEqual(entry["value_share"], 500000)
        self.assertEqual(entry["acb_share"], 400000)

    def test_null_acb_falls_back_to_value_share(self):
        """A null ACB means 'no accrued gain yet' (bought at value), not
        unknown-to-zero (DP#32). The gain inputs collapse to
        value_share - value_share == 0, the correct disposition gain for a
        just-acquired property -- never 0.0 as the cost base."""
        sale = {"date": "2031-06-30"}
        entry = ic._map_owned_properties(
            _doc_with_cottage(sale=sale, acb=None), "p1", "p2")[0]
        self.assertEqual(entry["acb_share"], entry["value_share"])
        self.assertEqual(entry["acb_share"], 500000)

    def test_shares_taken_at_the_couples_share(self):
        """The couple owns half -> every share (value, secured, acb) is halved,
        the same share net_equity is taken at."""
        doc = _doc_with_cottage(sale={"date": "2031-06-30"}, acb=400000)
        doc["properties"][0]["owner"] = {
            "joint": [{"person": "p1", "pct": 0.25},
                      {"person": "p2", "pct": 0.25},
                      {"person": "ca", "pct": 0.5}]}
        doc["liabilities"][0]["owner"] = doc["properties"][0]["owner"]
        entry = ic._map_owned_properties(doc, "p1", "p2")[0]
        self.assertEqual(entry["value_share"], 250000)    # half of 500k
        self.assertEqual(entry["secured_share"], 150000)  # half of 300k
        self.assertEqual(entry["acb_share"], 200000)      # half of 400k


class MapperBiteAStillCarriesShares(unittest.TestCase):
    """Bite A's guarantee is preserved: appreciation_rate ALONE (no sale) still
    carries value_share / secured_share, and now acb_share too, so a property
    that appreciates but is never sold has its gain inputs available."""

    def test_rate_alone_carries_all_three_shares(self):
        entry = ic._map_owned_properties(
            _doc_with_cottage(appreciation_rate=0.03, acb=400000), "p1", "p2")[0]
        self.assertEqual(entry["appreciation_rate"], 0.03)
        self.assertEqual(entry["value_share"], 500000)
        self.assertEqual(entry["secured_share"], 300000)
        self.assertEqual(entry["acb_share"], 400000)
        self.assertNotIn("sale", entry)


class MapperByteIdenticalWhenNeitherDeclared(unittest.TestCase):
    """DP#32: a property with NEITHER appreciation_rate NOR sale carries none of
    value_share / secured_share / acb_share / sale -- byte-identical to
    #692/#696. This is the round-trip the golden fixture depends on."""

    def test_no_rate_no_sale_carries_nothing(self):
        entry = ic._map_owned_properties(_doc_with_cottage(), "p1", "p2")[0]
        self.assertNotIn("appreciation_rate", entry)
        self.assertNotIn("value_share", entry)
        self.assertNotIn("secured_share", entry)
        self.assertNotIn("acb_share", entry)
        self.assertNotIn("sale", entry)
        self.assertEqual(entry["net_equity"], 200000)


# ──────────────────────────────────────────────────────────────────────────
# Piece 3 -- the equity gate
# ──────────────────────────────────────────────────────────────────────────

class EquityGatedBySaleYear(unittest.TestCase):
    """The pure gate: a sold property is worth nothing on the balance sheet
    from its sale year onward; the normal equity stands before it. The sale
    gate WINS over the appreciation branch (a sold property has no equity
    regardless of appreciation)."""

    def test_normal_equity_before_sale_year(self):
        prop = {"net_equity": 60000.0, "sale": {"year": 2031}}
        self.assertEqual(_property_equity_for_year(prop, 2030, 2026), 60000.0)
        # And the appreciation branch computes normally before the sale year --
        # the sale gate has NOT fired. In 2030 (years_held 4 from start_year
        # 2026) the equity is the APPRECIATED value, not the static net_equity.
        prop_appr = {"net_equity": 60000.0, "appreciation_rate": 0.03,
                     "value_share": 500000, "secured_share": 300000,
                     "sale": {"year": 2031}}
        self.assertAlmostEqual(
            _property_equity_for_year(prop_appr, 2030, 2026),
            500000 * 1.03 ** 4 - 300000, places=6)

    def test_zero_from_sale_year_onward(self):
        prop = {"net_equity": 60000.0, "sale": {"year": 2031}}
        self.assertEqual(_property_equity_for_year(prop, 2031, 2026), 0.0)
        self.assertEqual(_property_equity_for_year(prop, 2040, 2026), 0.0)

    def test_sale_gate_wins_over_appreciation(self):
        """A property that appreciates AND is sold: the sale gate returns 0.0
        from the sale year, never the appreciated value (the proceeds replace
        the equity in the portfolio, handled by the tax + proceeds layer)."""
        prop = {"net_equity": 200000.0, "appreciation_rate": 0.03,
                "value_share": 500000, "secured_share": 300000,
                "sale": {"year": 2031}}
        # Before the sale year: appreciation computes normally.
        self.assertAlmostEqual(
            _property_equity_for_year(prop, 2030, 2026),
            500000 * 1.03 ** 4 - 300000, places=6)
        # From the sale year: 0.0 regardless of appreciation.
        self.assertEqual(_property_equity_for_year(prop, 2031, 2026), 0.0)
        self.assertEqual(_property_equity_for_year(prop, 2032, 2026), 0.0)

    def test_no_sale_is_unconditional(self):
        """A property with no sale is held to the horizon: the gate never fires."""
        prop = {"net_equity": 60000.0}
        self.assertEqual(_property_equity_for_year(prop, 2026, 2026), 60000.0)
        self.assertEqual(_property_equity_for_year(prop, 2076, 2026), 60000.0)


class SaleAndPurchaseGatesCompose(unittest.TestCase):
    """A property bought mid-horizon AND sold later: zero before purchase, full
    equity between, zero from the sale year onward (the purchase gate fires
    first; the sale gate fires after it)."""

    def test_purchase_then_sale_timeline(self):
        prop = {"net_equity": 60000.0,
                "purchase": {"year": 2028}, "sale": {"year": 2033}}
        self.assertEqual(_property_equity_for_year(prop, 2027, 2026), 0.0)   # not yet bought
        self.assertEqual(_property_equity_for_year(prop, 2028, 2026), 60000.0)  # purchase year
        self.assertEqual(_property_equity_for_year(prop, 2032, 2026), 60000.0)  # held
        self.assertEqual(_property_equity_for_year(prop, 2033, 2026), 0.0)   # sold
        self.assertEqual(_property_equity_for_year(prop, 2040, 2026), 0.0)   # after sale


# ──────────────────────────────────────────────────────────────────────────
# Piece 1 -- the schema
# ──────────────────────────────────────────────────────────────────────────

class SchemaCarriesPropertySaleDef(unittest.TestCase):
    def test_schema_loads_and_has_property_sale(self):
        # The universal schema is composed from schema/input_schema.json (spine)
        # plus its x-schema-parts $defs fragments; load_universal_schema()
        # returns exactly the object the single file used to be.
        schema = ic.load_universal_schema()
        self.assertIn("property_sale", schema["$defs"])
        sale_def = schema["$defs"]["property_sale"]
        # #956 bite C: `date` is no longer top-level-required -- the def now
        # requires exactly one of `date`/`year` (a sweepable numeric leaf) via
        # oneOf, and nothing else (`selling_costs` stays optional).
        self.assertNotIn("required", sale_def)
        one_of = sale_def["oneOf"]
        self.assertEqual([sorted(o["required"]) for o in one_of],
                         [["date"], ["year"]])
        self.assertFalse(sale_def.get("additionalProperties", True))
        # `year` is the sweepable numeric leaf (integer | null).
        yr_any = sale_def["properties"]["year"]["anyOf"]
        self.assertTrue(any(t.get("type") == "integer" for t in yr_any))
        self.assertTrue(any(t.get("type") == "null" for t in yr_any))
        # selling_costs is money | null (a bare number or null, matching the
        # `closing_costs` shape on `property_purchase`).
        sc_any = sale_def["properties"]["selling_costs"]["anyOf"]
        self.assertEqual(len(sc_any), 2)
        self.assertTrue(any("$ref" in t and t["$ref"].endswith("/money") for t in sc_any))
        self.assertTrue(any(t.get("type") == "null" for t in sc_any))

    def test_property_def_has_sale_slot(self):
        # The universal schema is composed from schema/input_schema.json (spine)
        # plus its x-schema-parts $defs fragments; load_universal_schema()
        # returns exactly the object the single file used to be.
        schema = ic.load_universal_schema()
        self.assertIn("sale", schema["$defs"]["property"]["properties"])
        slot = schema["$defs"]["property"]["properties"]["sale"]
        self.assertEqual(len(slot["anyOf"]), 2)
        self.assertTrue(any("$ref" in t and t["$ref"].endswith("/property_sale")
                           for t in slot["anyOf"]))
        self.assertTrue(any(t.get("type") == "null" for t in slot["anyOf"]))


if __name__ == "__main__":
    unittest.main()