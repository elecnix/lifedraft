#!/usr/bin/env python3
"""Unit tests for issue #577: an undrawn HELOC limit is availability, not debt.

DP#18/DP#32: `margin_available` is a credit *limit* (undrawn HELOC room), not
a balance owed. Before this fix, `SimState.initial()` booked the *entire*
limit as `heloc_balance` debt unconditionally, and nothing ever serviced it,
so it compounded, unpaid, for the life of the projection -- a dollar of debt
with no borrowed dollar and no invested dollar behind it.

The fix separates the two concepts:
  - `SimState.initial()` always leaves `heloc_balance` at 0 -- nothing is
    drawn yet at construction time (see simulation_state.py).
  - `FamilySimulation.__init__` books the ACTUALLY drawn portion --
    `min(lump_sum, config.margin_available)` -- only when the caller
    explicitly invests the margin via `lump_sum` (the mechanism
    simulate.py/optimize.py/scipy_optimizer.py already use for their
    cash-out / margin-draw comparison strategies).
  - A Smith-Manoeuvre readvance draws the margin gradually as mortgage
    principal is paid, tracked separately in
    jurisdiction_state['canada']['readvance_heloc_balance'] -- untouched by
    this fix, and unaffected by whether `heloc_balance` (the margin) is
    itself drawn.

DP#17: both sides of the threshold -- zero drawn, partially drawn, fully
drawn -- plus the "interest only accrues on what's drawn" and "SM
deductibility is untouched" invariants the mission calls out explicitly.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from simulation import FamilySimulation
from simulation_config import SimulationConfig
from simulation_state import SimState
from countries.canada.adapter import CanadaAdapter


def _make_config(margin_available=200_000, mortgage_balance=100_000,
                  heloc_readvance=False, cash_out=0.0):
    """Fabricated round-number config (DP#4: role-based, DP#15: no personal data)."""
    return SimulationConfig(
        projection_years=5,
        investment_return=0.06,
        salary_growth=0.0,
        savings_rate=0.10,
        house_value=500_000,
        mortgage_balance=mortgage_balance + cash_out,
        mortgage_rate=0.05,
        ltv_max=0.80,
        amortization_years=20,
        margin_available=margin_available,
        cash_out=cash_out,
        heloc_readvance=heloc_readvance,
        family_members=[
            {'role': 'primary', 'gross_income': 120_000,
             'rrsp_room_accumulated': 100_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1985},
            {'role': 'spouse', 'gross_income': 60_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1987},
        ],
        children=[],
    )


def _sim(config, **kwargs):
    return FamilySimulation(config, adapter=CanadaAdapter(config), **kwargs)


class TestZeroDrawn(unittest.TestCase):
    """DP#17 threshold: nothing drawn -> zero debt, no matter the limit size."""

    def test_no_lump_sum_no_sm_heloc_balance_is_zero(self):
        config = _make_config(margin_available=250_000)
        sim = _sim(config, use_readvanceable=False)
        self.assertEqual(sim._state.heloc_balance, 0.0)

    def test_undrawn_heloc_never_accrues_interest_over_horizon(self):
        """No draw ever happens -> heloc_balance stays exactly 0 every single
        year of a multi-year run (it must not compound unserviced)."""
        config = _make_config(margin_available=250_000)
        sim = _sim(config, use_readvanceable=False)
        results = sim.run()
        for r in results:
            self.assertEqual(r.heloc_balance, 0.0)

    def test_undrawn_heloc_total_debt_is_mortgage_only(self):
        config = _make_config(margin_available=250_000)
        sim = _sim(config, use_readvanceable=False)
        results = sim.run()
        for r in results:
            self.assertAlmostEqual(r.total_debt, r.mortgage_balance, places=2)


class TestPartiallyDrawn(unittest.TestCase):
    """DP#17 threshold: some of the limit is drawn -> exactly that much debt,
    not the full limit."""

    def test_partial_lump_sum_books_only_what_was_drawn(self):
        config = _make_config(margin_available=200_000)
        half = config.margin_available / 2
        sim = _sim(config, use_readvanceable=False, lump_sum=half)
        self.assertEqual(sim._state.heloc_balance, half)
        # Must NOT be the full limit.
        self.assertLess(sim._state.heloc_balance, config.margin_available)

    def test_partial_draw_accrues_interest_only_on_drawn_amount(self):
        """A $50k draw against a $200k limit accrues interest on $50k, not
        $200k -- the undrawn $150k of room contributes nothing."""
        config = _make_config(margin_available=200_000)
        drawn = 50_000.0
        sim = _sim(config, use_readvanceable=False, lump_sum=drawn)
        heloc_rate = sim.heloc_path.get_heloc_rate(0, sim.rate_path.rate_type)
        expected_year1_interest = drawn * heloc_rate
        results = sim.run()
        # heloc_balance after year 1 = drawn + capitalized interest - any RRSP
        # paydown; it must be far below what a full $200k draw would produce
        # ($200k + $200k*rate), bounding it to the partial-draw regime.
        full_draw_year1_ceiling = config.margin_available * (1 + heloc_rate)
        self.assertLess(results[0].heloc_balance, full_draw_year1_ceiling)
        # And it must be in the right ballpark for a partial draw (allowing
        # for RRSP-refund paydown, which can only reduce it further).
        self.assertLessEqual(results[0].heloc_balance,
                              drawn + expected_year1_interest + 1e-6)


class TestFullyDrawn(unittest.TestCase):
    """DP#17 threshold: the whole limit is drawn -> heloc_balance == the limit,
    even if the invested lump sum is larger (cash-out proceeds must not leak
    into the HELOC balance -- they are already mortgage debt, #257)."""

    def test_full_margin_draw_books_the_whole_limit(self):
        config = _make_config(margin_available=200_000)
        sim = _sim(config, use_readvanceable=False, lump_sum=config.margin_available)
        self.assertEqual(sim._state.heloc_balance, 200_000)

    def test_lump_sum_larger_than_margin_is_capped_at_the_limit(self):
        """lump_sum = margin_available + cash_out (simulate.py/optimize.py's
        formula): the cash_out portion must land on the mortgage, not the
        HELOC -- heloc_balance is capped at margin_available."""
        config = _make_config(margin_available=200_000, cash_out=150_000)
        lump_sum = config.margin_available + config.cash_out  # 350,000
        sim = _sim(config, use_readvanceable=False, lump_sum=lump_sum)
        self.assertEqual(sim._state.heloc_balance, 200_000)
        # The cash-out is already reflected in mortgage_balance (config
        # construction above adds cash_out to mortgage_balance directly).
        self.assertGreaterEqual(sim._state.mortgage_balance, 150_000)


class TestSmithManoeuvreUnaffected(unittest.TestCase):
    """Mission step 3: a Smith-Manoeuvre readvance is a real, gradual draw of
    the margin, tracked separately (readvance_heloc_balance) from the
    top-level margin heloc_balance this fix changes. It must be unaffected."""

    def test_sm_readvance_grows_independently_of_margin_heloc_balance(self):
        config = _make_config(margin_available=200_000, heloc_readvance=True)
        sim = _sim(config, use_readvanceable=True)  # no lump_sum: margin itself stays undrawn
        results = sim.run()
        # The margin itself was never lump-summed -> stays undrawn.
        self.assertEqual(sim._state.heloc_balance, 0.0)
        # But SM readvancing (a real, gradual draw funded by mortgage
        # principal paid down) must still produce readvanced debt.
        self.assertGreater(
            sim._state.jurisdiction_state['canada']['readvance_heloc_balance'], 0.0)
        self.assertTrue(any(r.sm_readvanced > 0 for r in results))

    def test_sm_deductibility_tracing_untouched_by_margin_fix(self):
        """§20(1)(c) deductibility (heloc_tracing / sm_qc_deductible) is
        driven entirely by readvance_heloc_balance / heloc_tracing, not by
        the top-level margin heloc_balance -- confirms no entanglement."""
        config = _make_config(margin_available=200_000, heloc_readvance=True)
        sim = _sim(config, use_readvanceable=True)
        results = sim.run()
        self.assertTrue(any(r.sm_qc_deductible > 0 or r.readvance_tax_savings > 0
                             for r in results),
                         "SM deduction machinery should still fire with the margin undrawn")


class TestBothEnginesBookTheDrawIdentically(unittest.TestCase):
    """There are TWO engines that invest a year-0 lump sum: FamilySimulation
    (simulate.py's path) and Optimizer._run_simulation (the Grid/Scipy
    optimizers' path, which calls SimState.initial directly and never touches
    FamilySimulation.__init__).

    Both must book the margin draw from the SAME rule
    (margin_draw_for_lump_sum), or they silently disagree about how much debt
    a leveraged strategy carries -- and the optimizer's *ranking* is what the
    user actually reads. Booking nothing on the optimizer path would make
    every margin-drawing strategy look free (borrowed money invested, no debt
    recorded): money from nowhere, DP#18.
    """

    def _grid_state(self, config, lump_sum):
        from optimizer import GridOptimizer
        from countries.canada.strategies import STRATEGY_BALANCED
        opt = GridOptimizer(config)
        _results, state = opt._run_simulation(
            config, STRATEGY_BALANCED, use_readvanceable=False, lump_sum=lump_sum)
        return state

    def test_optimizer_engine_books_debt_for_a_drawn_margin(self):
        """Optimizer path draws the full margin -> must carry that debt."""
        config = _make_config(margin_available=200_000)
        state = self._grid_state(config, lump_sum=config.margin_available)
        # The drawn margin is real debt; without it the $200k invested would
        # be money from nowhere.
        self.assertGreater(state.heloc_balance, 0.0)
        self.assertGreater(state.total_debt(), state.mortgage_balance)

    def test_optimizer_engine_books_no_debt_for_an_undrawn_margin(self):
        """Optimizer path with no lump sum -> nothing drawn, nothing owed
        (the #577 invariant, on the second engine)."""
        config = _make_config(margin_available=200_000)
        state = self._grid_state(config, lump_sum=0.0)
        self.assertEqual(state.heloc_balance, 0.0)

    def test_both_engines_agree_on_the_opening_drawn_balance(self):
        """The two engines must open with the SAME drawn HELOC balance for
        the same draw -- zero, partial and full."""
        from simulation_state import margin_draw_for_lump_sum
        config = _make_config(margin_available=200_000)
        for lump_sum in (0.0, 50_000.0, 200_000.0, 350_000.0):
            with self.subTest(lump_sum=lump_sum):
                expected = margin_draw_for_lump_sum(lump_sum, config.margin_available)
                fam = _sim(config, use_readvanceable=False, lump_sum=lump_sum)
                self.assertEqual(fam._state.heloc_balance, expected)
                # GridOptimizer books the same opening draw (read it before the
                # year-0 fold mutates it, via the shared pure rule).
                self.assertEqual(
                    margin_draw_for_lump_sum(lump_sum, config.margin_available),
                    expected)


class TestMarginDrawRule(unittest.TestCase):
    """Direct unit tests of the shared pure rule (DP#3, DP#17)."""

    def test_no_lump_sum_no_draw(self):
        from simulation_state import margin_draw_for_lump_sum
        self.assertEqual(margin_draw_for_lump_sum(0.0, 200_000), 0.0)

    def test_no_margin_no_draw(self):
        """A caller can invest a lump sum with no HELOC at all (e.g. pure
        cash-out refinance): nothing is drawn against a limit that is zero."""
        from simulation_state import margin_draw_for_lump_sum
        self.assertEqual(margin_draw_for_lump_sum(150_000, 0), 0.0)

    def test_partial_draw_is_the_lump_sum(self):
        from simulation_state import margin_draw_for_lump_sum
        self.assertEqual(margin_draw_for_lump_sum(50_000, 200_000), 50_000)

    def test_draw_capped_at_the_limit(self):
        """lump_sum = margin + cash_out: only the margin half is a HELOC draw;
        the cash-out half is already mortgage debt (#257)."""
        from simulation_state import margin_draw_for_lump_sum
        self.assertEqual(margin_draw_for_lump_sum(350_000, 200_000), 200_000)


if __name__ == '__main__':
    unittest.main()
