#!/usr/bin/env python3
"""Money-conservation regression tests for cash-out refinancing (issue #257).

A cash-out refinance is a MORTGAGE increase whose proceeds are invested.
The borrowed cash_out must be recorded as debt EXACTLY ONCE and the proceeds
must still be invested (no money created, no money lost).

The bug (#257): `build_overlay_config` added cash_out to BOTH
property['margin_available'] (→ initial heloc_balance) AND
property['mortgage_balance'] (→ initial mortgage_balance), recording one
borrowed dollar as debt twice. The fix records cash_out only on the mortgage
and sources its proceeds into the invested lump sum via property['cash_out'].

These assertions are RELATIONAL (deltas vs. a no-cash-out baseline), not magic
numbers — a money-conservation invariant that the cross-engine consistency
checks could not catch (both engines shared the doubled-debt path).

Issue #577 (later fix): `margin_available` → `heloc_balance` is no longer an
unconditional mapping. `SimState.initial()` leaves the margin undrawn
(heloc_balance=0); `FamilySimulation.__init__` books the drawn portion only
when the caller actually invests it via `lump_sum` (exactly what
simulate.py/optimize.py do). Tests below that exercise an actual draw now go
through `FamilySimulation(..., lump_sum=...)` rather than bare
`SimState.initial()`, so the conservation identities here still hold under
the corrected model — see `test_zero_cashout_is_noop` for the no-draw case.

Issue #664 (later fix): a mortgage and its HELOC are carved out of ONE
registered charge with ONE combined limit -- NOT independent borrowing
sources. `apply_overlay` now shrinks `margin_available` by exactly the
cash-out it books. The consequence, made explicit by
`test_total_debt_flat_when_cashout_within_margin` and
`test_total_debt_increases_beyond_margin` (DP#17: both sides of the
`margin_available` threshold): for any cash_out that fits inside the
pre-existing margin_available, total secured debt (mortgage + drawn HELOC)
is FLAT versus the fully-drawn-margin baseline -- refinancing only
RECLASSIFIES existing borrowing capacity from revolving to amortizing, it
does not create new capacity out of the same charge. Only a cash_out that
EXCEEDS margin_available adds genuinely new debt, and then by exactly the
excess. `test_invested_capital_equals_total_new_debt` (the #257 identity:
invested capital == new debt beyond the pre-existing mortgage) continues to
hold exactly in both regimes.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation_config import (
    SimulationConfig, ScenarioOverlay, build_overlay_config,
)
from simulation_state import SimState


def _base_cfg():
    """Fabricated round-number base config (DP#4)."""
    return {
        'assumptions': {'projection_years': 5, 'investment_return': 0.07},
        'property': {
            'house_value': 500000,
            'mortgage_balance': 100000,
            'margin_available': 200000,
            'mortgage_rate': 0.05,
            'ltv_max': 0.80,
        },
        'accounts': {},
        'family': {'members': [
            {'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000},
            {'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 30000},
        ]},
    }


# The base config's margin_available (200000) is the regime threshold this
# module tests both sides of (DP#17): a cash_out at or below it only
# reclassifies existing capacity; a cash_out above it draws genuinely new
# debt. 300000 is the largest cash_out this fixture's charge can hold
# (mortgage 100000 + 300000 = 400000 == 80% of house_value 500000).
_MARGIN_AVAILABLE = 200000.0


def _initial_state(cash_out):
    """Build the initial state for a given cash-out, ACTUALLY drawing the
    margin via lump_sum -- exactly as simulate.py / optimize.py do.

    Issue #577 moved margin-draw booking out of bare SimState.initial()
    (which has no way to know whether a draw was ever decided -- see
    tests/test_simulation_state.py) and into FamilySimulation.__init__,
    which books the drawn portion once the caller actually invests it via
    lump_sum. This conservation test is about a draw that DOES happen, so
    it must go through FamilySimulation to see that debt.

    issue #655: a nonzero cash_out is a new-loan refinance and requires a
    declared amortization; 25 years is a fabricated round placeholder (DP#13)
    since this fixture models no specific declared refinance option.
    """
    overlay = ScenarioOverlay(
        label="t", cash_out=cash_out,
        refinance_amortization_years=25 if cash_out > 0 else None,
    )
    cfg = build_overlay_config(_base_cfg(), overlay)
    config = SimulationConfig.from_dict(cfg)
    # Invested lump sum, sourced exactly as simulate.py / optimize.py do:
    # pre-existing undrawn HELOC margin draw (already reduced by the
    # cash-out it shares a charge with, #664) + cash-out proceeds.
    lump_sum = config.margin_available + config.cash_out
    from simulation import FamilySimulation
    from countries.canada.adapter import CanadaAdapter
    sim = FamilySimulation(config, adapter=CanadaAdapter(config),
                            use_readvanceable=False, lump_sum=lump_sum)
    state = sim._state
    total_debt = state.mortgage_balance + state.heloc_balance
    return total_debt, lump_sum


class TestCashOutMoneyConservation(unittest.TestCase):

    def test_total_debt_flat_when_cashout_within_margin(self):
        """issue #664: a cash_out that fits inside the pre-existing
        margin_available only RECLASSIFIES capacity already available under
        the shared charge (from revolving to amortizing) -- it does not
        create new capacity, so total_debt is unchanged from the
        fully-drawn-margin baseline."""
        for cash_out in (50000.0, 100000.0, _MARGIN_AVAILABLE):
            with self.subTest(cash_out=cash_out):
                baseline_debt, _ = _initial_state(0.0)
                refi_debt, _ = _initial_state(cash_out)
                self.assertAlmostEqual(refi_debt, baseline_debt, places=2)

    def test_total_debt_increases_beyond_margin(self):
        """issue #664: a cash_out that EXCEEDS the pre-existing
        margin_available draws genuinely new debt -- by exactly the excess
        over margin_available, once the shared HELOC room is exhausted."""
        for cash_out in (250000.0, 300000.0):
            with self.subTest(cash_out=cash_out):
                baseline_debt, _ = _initial_state(0.0)
                refi_debt, _ = _initial_state(cash_out)
                self.assertAlmostEqual(
                    refi_debt - baseline_debt, cash_out - _MARGIN_AVAILABLE, places=2)

    def test_invested_capital_equals_total_new_debt(self):
        """Conservation identity: every invested dollar maps to exactly one
        liability. Invested capital == margin draw + cash-out == the new debt
        beyond the pre-existing mortgage. Holds in BOTH the #664
        reclassification regime and the genuinely-new-debt regime."""
        for cash_out in (150000.0, 250000.0):
            with self.subTest(cash_out=cash_out):
                base = _base_cfg()
                pre_existing_mortgage = base['property']['mortgage_balance']
                total_debt, lump_sum = _initial_state(cash_out)
                new_debt = total_debt - pre_existing_mortgage
                self.assertAlmostEqual(lump_sum, new_debt, places=2)

    def test_margin_shrinks_dollar_for_dollar_by_cashout(self):
        """issue #664 (was #257's `test_margin_not_inflated_by_cashout`,
        asserting the OLD invariant that heloc_balance stayed identical
        with/without cash_out -- that was only correct because #664's bug
        left margin_available untouched by the overlay). Now: booking a
        cash-out mortgage increase consumes exactly that much of the SHARED
        charge, so the drawn HELOC balance falls by the same amount the
        mortgage rises -- total debt is conserved, not the HELOC balance."""
        cash_out = 150000.0
        from simulation import FamilySimulation
        from countries.canada.adapter import CanadaAdapter
        overlay0 = ScenarioOverlay(label="t", cash_out=0.0)
        overlayC = ScenarioOverlay(label="t", cash_out=cash_out, refinance_amortization_years=25)
        config0 = SimulationConfig.from_dict(build_overlay_config(_base_cfg(), overlay0))
        configC = SimulationConfig.from_dict(build_overlay_config(_base_cfg(), overlayC))
        s0 = FamilySimulation(config0, adapter=CanadaAdapter(config0), use_readvanceable=False,
                               lump_sum=config0.margin_available + config0.cash_out)._state
        sC = FamilySimulation(configC, adapter=CanadaAdapter(configC), use_readvanceable=False,
                               lump_sum=configC.margin_available + configC.cash_out)._state
        # All of the cash-out lands on the mortgage, once.
        self.assertAlmostEqual(
            sC.mortgage_balance - s0.mortgage_balance, cash_out, places=2)
        # The drawn HELOC balance falls by exactly the same amount (#664):
        # the mortgage advance consumed that much of the shared charge.
        self.assertAlmostEqual(s0.heloc_balance - sC.heloc_balance, cash_out, places=2)
        # Total secured debt is conserved -- pure reclassification.
        self.assertAlmostEqual(
            sC.mortgage_balance + sC.heloc_balance,
            s0.mortgage_balance + s0.heloc_balance, places=2)

    def test_zero_cashout_is_noop(self):
        """No cash-out AND no draw decided: cash_out field is 0, mortgage
        debt equals the base, and (#577) heloc_balance is 0 -- bare
        SimState.initial() books debt for NOTHING but what is actually
        drawn, and nothing has been drawn here (no lump_sum passed). Before
        the #577 fix this asserted heloc_balance == margin_available
        (200000): the bug was booking the whole undrawn limit as debt
        unconditionally."""
        overlay = ScenarioOverlay(label="t", cash_out=0.0)
        cfg = build_overlay_config(_base_cfg(), overlay)
        config = SimulationConfig.from_dict(cfg)
        self.assertEqual(config.cash_out, 0.0)
        state = SimState.initial(config)
        base = _base_cfg()['property']
        self.assertAlmostEqual(state.mortgage_balance,
                               base['mortgage_balance'], places=2)
        self.assertEqual(state.heloc_balance, 0.0)


if __name__ == '__main__':
    unittest.main()
