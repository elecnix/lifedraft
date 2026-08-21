#!/usr/bin/env python3
"""Regression tests for issue #293: top-level CRI/LIRA block must be tracked.

Root cause: the ``lira`` block lives at the TOP LEVEL of input.json (separate
from the RRSP balance carried inside the family member). Two defects dropped it
before the simulation ever saw it:

1. ``module_registry._deep_merge`` let the Canada overlay's ``lira.balance: 0``
   template default overwrite the user's real balance (scalar "overlay wins").
2. ``SimState.initial`` only looked inside ``primary['lira']`` (which never
   exists for real inputs) instead of the top-level ``config.lira_data``.

These tests exercise the full config-to-state plumbing the live optimizer path
uses, asserting the balance survives, surfaces in ``YearResult.lira_balance``,
counts in ``total_assets``, and grows year over year. They are relational (no
magic snapshots). LIF decumulation at age 71 is owned by issue #294.
"""

import warnings

from simulation_config import SimulationConfig
from simulation import FamilySimulation


def _config_with_top_level_lira(balance=52837.0, return_rate=0.07, years=6):
    """Build a config via from_dict with a TOP-LEVEL lira block, as real inputs do."""
    cfg = {
        'assumptions': {'projection_years': years, 'investment_return': return_rate,
                        'start_year': 2026, 'frozen_brackets': True},
        'property': {'house_value': 500000, 'mortgage_balance': 300000,
                     'mortgage_rate': 0.05, 'margin_available': 100000},
        'family': {'members': [
            {'name': 'P', 'role': 'primary', 'gross_income': 130000,
             'birth_year': 1979, 'rrsp_room_accumulated': 50000,
             'tfsa_room_accumulated': 70000},
        ], 'children': []},
        # The locked-in account, at the top level (not inside the member).
        'lira': {'balance': balance, 'birth_year': 1979,
                 'reference_rate': 0.06, 'jurisdiction': 'quebec'},
    }
    return SimulationConfig.from_dict(cfg)


def test_top_level_lira_balance_reaches_config():
    """from_dict must capture the top-level lira block (it was dropped before)."""
    cfg = _config_with_top_level_lira(balance=52837.0)
    assert cfg.lira_data.get('balance') == 52837.0


def test_lira_balance_nonzero_in_year_zero_and_grows():
    """Issue #293 acceptance: non-zero input ⇒ non-zero year 0, growing by year N."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        cfg = _config_with_top_level_lira(balance=52837.0, return_rate=0.07, years=6)
        results = FamilySimulation(cfg).run()

    series = [r.lira_balance for r in results]
    # Year 0: tracked and grown one year at the portfolio return (like RRSP).
    assert series[0] > 52837.0, f"year 0 lira_balance should grow above input, got {series[0]}"
    # Accumulation grows year over year.
    assert series[-1] > series[0], f"lira_balance should grow: {series[0]} -> {series[-1]}"


def test_lira_grows_at_portfolio_return_not_reference_rate():
    """During accumulation the LIRA grows at the portfolio return, matching RRSP."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        cfg = _config_with_top_level_lira(balance=52837.0, return_rate=0.07, years=2)
        results = FamilySimulation(cfg).run()
    # Grows at 7% (portfolio), not the 6% reference_rate.
    assert abs(results[0].lira_balance - 52837.0 * 1.07) < 1.0


def test_lira_counted_in_total_assets():
    """The locked-in account must appear in total_assets, not silently vanish."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        cfg = _config_with_top_level_lira(balance=52837.0, years=2)
        results = FamilySimulation(cfg).run()
    r0 = results[0]
    assert r0.lira_balance > 0
    assert r0.lira_balance <= r0.total_assets, "lira_balance must be included in total_assets"


def test_zero_lira_balance_stays_zero():
    """No lira input ⇒ lira_balance is zero (no phantom account)."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        cfg = _config_with_top_level_lira(balance=0.0, years=3)
        results = FamilySimulation(cfg).run()
    assert all(r.lira_balance == 0 for r in results)
