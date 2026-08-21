#!/usr/bin/env python3
"""Issue #729 + #730 (DP#24): SimulationConfig.to_dict() re-emits every key
from_dict() ingests -- a config that loads but does not round-trip silently
loses data on any load->modify->save cycle.

Two confirmed omissions, both fixed in simulation_config.py:

  * #729 -- the top-level ``lira`` block (CRI/LIRA locked-in pension data)
    was consumed by ``from_dict`` (``cfg.get('lira')``) but never re-emitted
    by ``to_dict``. A config with a $52,837 locked-in account, saved and
    reloaded, lost it.
  * #730 -- ``property.cash_out`` (the invested-capital source a refinance
    books, DP#18) was consumed by ``from_dict`` (``prop.get('cash_out')``)
    but never re-emitted by ``to_dict``. A config that had booked a
    cash-out refinance, saved and reloaded, silently dropped that leg.

The canonical DP#24 property is: ``from_dict(to_dict(cfg))`` preserves the
values the household declared. These tests assert that for ``lira`` and
``cash_out`` specifically, and a general "the round-tripped dict agrees with
the original on these keys" check -- fabricated round numbers only (DP#15),
no personal data.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import SimulationConfig


def _base_config(**overrides):
    """A minimal SimulationConfig with a primary + spouse, round numbers."""
    defaults = dict(
        projection_years=5,
        investment_return=0.07,
        house_value=400000,
        mortgage_balance=200000,
        mortgage_rate=0.05,
        margin_available=50000,
        family_members=[
            {'role': 'primary', 'gross_income': 120000, 'birth_year': 1979,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            {'role': 'spouse', 'gross_income': 60000, 'birth_year': 1981,
             'rrsp_room_accumulated': 30000, 'tfsa_room_accumulated': 20000},
        ],
        children=[],
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


class TestToDictReEmitsLiraAndCashOut(unittest.TestCase):
    """DP#24: to_dict() must re-emit lira (#729) and cash_out (#730)."""

    def test_lira_survives_round_trip(self):
        """#729: a declared LIRA block survives to_dict -> from_dict."""
        lira = {
            'balance': 50000,          # fabricated round number (DP#15)
            'birth_year': 1979,
            'jurisdiction': 'quebec',
            'reference_rate': 0.06,
            'conversion_year': None,
        }
        cfg = _base_config(lira_data=dict(lira))
        round_tripped = SimulationConfig.from_dict(cfg.to_dict())

        # The general DP#24 assertion: the lira block the household declared
        # is what comes back, key for key -- not a hardcoded-balance pin.
        self.assertEqual(round_tripped.lira_data, lira)
        # And specifically the balance (the load-bearing figure #293 wired).
        self.assertEqual(round_tripped.lira_data['balance'], 50000)

    def test_cash_out_survives_round_trip(self):
        """#730: a booked refinance cash_out survives to_dict -> from_dict."""
        cfg = _base_config(cash_out=75000)   # fabricated round number (DP#15)
        round_tripped = SimulationConfig.from_dict(cfg.to_dict())

        self.assertEqual(round_tripped.cash_out, 75000)

    def test_lira_and_cash_out_round_trip_together(self):
        """Both omitted keys fixed in the same to_dict() method round-trip
        together -- the regression this PR lands is the combination."""
        lira = {
            'balance': 50000,
            'birth_year': 1979,
            'jurisdiction': 'quebec',
            'reference_rate': 0.06,
            'conversion_year': 2045,
        }
        cfg = _base_config(lira_data=dict(lira), cash_out=75000)
        d = cfg.to_dict()

        # The round-tripped dict must carry both keys at all, before we even
        # reload -- this is the exact emission #729/#730 lacked.
        self.assertIn('lira', d)
        self.assertIn('cash_out', d['property'])
        self.assertEqual(d['lira'], lira)
        self.assertEqual(d['property']['cash_out'], 75000)

        round_tripped = SimulationConfig.from_dict(d)
        self.assertEqual(round_tripped.lira_data, lira)
        self.assertEqual(round_tripped.cash_out, 75000)

    def test_absent_lira_and_zero_cash_out_round_trip_to_absent(self):
        """DP#32: absence must round-trip to absence, not a fabricated block.

        A household that declared no LIRA and booked no cash-out must not
        acquire either on a save/reload -- the absence-safe convention
        to_dict() uses for consumer_loans/installments/equity_grants, and
        ScenarioOverlay.to_dict() uses for its own cash_out.
        """
        cfg = _base_config()  # no lira_data, cash_out defaults to 0.0
        d = cfg.to_dict()

        self.assertNotIn('lira', d)
        self.assertNotIn('cash_out', d['property'])

        round_tripped = SimulationConfig.from_dict(d)
        self.assertEqual(round_tripped.lira_data, {})
        self.assertEqual(round_tripped.cash_out, 0.0)


if __name__ == '__main__':
    unittest.main()