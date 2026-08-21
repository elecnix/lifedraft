#!/usr/bin/env python3
"""Issue #956 bite A: property appreciation lever.

A declared `appreciation_rate` compounds a non-principal property's GROSS value
year over year, so equity = appreciated value - the (static) secured mortgage.
This is the lever that makes a purchase-TIMING sweep meaningful — without it a
property is a static cash-drain and any "when to buy" ranking is misleading.

These tests lock: the compounding math and its ownership-year base; absence-
safety (no/zero rate ⇒ the static #692 net_equity, so the golden fixture is
unmoved — DP#32); the mapper carrying value/mortgage shares separately ONLY when
a rate is declared (byte-identical round-trip otherwise); and that the rate is a
sweepable contract leaf.

Round numbers per DP#13/DP#15.
"""

import unittest

from simulation_state import _property_equity_for_year
import input_contract as ic
import sweep


class TestAppreciationMath(unittest.TestCase):
    """The per-year equity function — where ranking correctness lives."""

    P = {'net_equity': 200000, 'appreciation_rate': 0.03,
         'value_share': 500000, 'secured_share': 300000}

    def test_no_rate_is_static_net_equity(self):
        # DP#32: absent rate ⇒ the #692 static figure, never reads value_share
        self.assertEqual(_property_equity_for_year({'net_equity': 100000}, 2030, 2026), 100000)

    def test_zero_rate_is_static_net_equity(self):
        p = {'net_equity': 100000, 'appreciation_rate': 0.0,
             'value_share': 400000, 'secured_share': 300000}
        self.assertEqual(_property_equity_for_year(p, 2030, 2026), 100000)

    def test_value_compounds_mortgage_static(self):
        # 500k value @3% for 3yr, less the static 300k mortgage
        got = _property_equity_for_year(self.P, 2029, 2026)
        self.assertAlmostEqual(got, 500000 * 1.03 ** 3 - 300000, places=6)

    def test_ownership_year_equals_net_equity(self):
        # years_held == 0 ⇒ appreciated value == gross value ⇒ equity == net_equity;
        # the purchase-year down payment (= net_equity) is therefore unaffected.
        self.assertEqual(_property_equity_for_year(self.P, 2026, 2026), 200000)

    def test_negative_rate_depreciates(self):
        p = dict(self.P, appreciation_rate=-0.05)
        self.assertAlmostEqual(_property_equity_for_year(p, 2028, 2026),
                               500000 * 0.95 ** 2 - 300000, places=6)

    def test_dated_purchase_bases_on_purchase_year(self):
        p = dict(self.P, purchase={'year': 2028})
        self.assertEqual(_property_equity_for_year(p, 2027, 2026), 0.0)          # not yet owned
        self.assertEqual(_property_equity_for_year(p, 2028, 2026), 200000)       # purchase year == net_equity
        self.assertAlmostEqual(_property_equity_for_year(p, 2030, 2026),         # 2yr after purchase
                               500000 * 1.03 ** 2 - 300000, places=6)


def _doc_with_cottage(appreciation_rate=None):
    """Minimal contract doc: the couple owns a cottage (value 500k, 300k mortgage
    secured against it)."""
    prop = {
        "id": "cottage",
        "owner": {"joint": [{"person": "p1", "pct": 0.5}, {"person": "p2", "pct": 0.5}]},
        "kind": "recreational",
        "value": {"amount": 500000, "as_of": "2026-06-30"},
        "acb": 500000,
        "designated_principal_residence_years": [],
    }
    if appreciation_rate is not None:
        prop["appreciation_rate"] = appreciation_rate
    return {
        "properties": [prop],
        "liabilities": [{
            "kind": "mortgage", "collateral": "cottage",
            "owner": {"joint": [{"person": "p1", "pct": 0.5}, {"person": "p2", "pct": 0.5}]},
            "balance": {"amount": 300000},
        }],
    }


class TestMapperCarriesShares(unittest.TestCase):
    def test_shares_emitted_only_when_rate_declared(self):
        entry = ic._map_owned_properties(_doc_with_cottage(0.03), "p1", "p2")[0]
        self.assertEqual(entry["appreciation_rate"], 0.03)
        self.assertEqual(entry["value_share"], 500000)     # couple owns 100%
        self.assertEqual(entry["secured_share"], 300000)
        self.assertEqual(entry["net_equity"], 200000)

    def test_no_rate_round_trips_byte_identically(self):
        entry = ic._map_owned_properties(_doc_with_cottage(None), "p1", "p2")[0]
        self.assertNotIn("appreciation_rate", entry)       # #692/#696 byte-identical
        self.assertNotIn("value_share", entry)
        self.assertNotIn("secured_share", entry)
        self.assertEqual(entry["net_equity"], 200000)


class TestSweepable(unittest.TestCase):
    def test_appreciation_rate_is_a_resolvable_sweep_leaf(self):
        # The sweepability claim: sensitivity.sweeps can target the leaf because
        # resolve_leaf finds it (it must pre-exist — DP#18/#32).
        doc = _doc_with_cottage(0.03)
        container, key = sweep.resolve_leaf(doc, "properties.0.appreciation_rate")
        self.assertEqual(container[key], 0.03)
        container[key] = 0.05                              # a sweep can set it
        self.assertEqual(doc["properties"][0]["appreciation_rate"], 0.05)


if __name__ == '__main__':
    unittest.main()
