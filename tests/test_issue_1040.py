#!/usr/bin/env python3
"""Issue #1040: borrow-to-invest "hold the draw flat" flag (opt out of the
RRSP-refund HELOC paydown sweep) -- #1036 follow-up.

#1036's ``decisions.borrow_to_invest`` reuses the year-0 margin-draw
machinery (``initial_state_for_run`` -> ``heloc_balance``), which participates
in the ``rrsp_refund_heloc_paydown`` rule: each year's RRSP tax refund pays
the drawn balance down dollar-for-dollar (the Smith-Manoeuvre debt sweep).
Correct for an accumulator sweeping their refund against the loan -- but an
accumulator who means "borrow $X and HOLD it invested" had no way to opt out.

This file locks the acceptance criteria:

1. An option declaring ``hold_draw: true`` holds its draw flat across the
   horizon (no RRSP-refund paydown; the refund stays in the household's cash
   and flows to the usual allocation instead), while the interest is still
   priced, deducted under s.20(1)(c), and serviced per ``capitalize_interest``.
2. The SAME config without the flag keeps the existing paydown behaviour
   (regression pin).
3. The golden household (which declares no ``borrow_to_invest``) is
   byte-identical -- default False is a strict no-op (covered by the default
   paths here plus the untouched golden fixture).

DP#15: no personal data. Fixtures use fabricated round numbers and role-based
names (DP#4).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from countries.canada.rate_model import build_rate_path
from countries.canada.strategies import STRATEGY_BALANCED
import input_contract as ic
import optimize
from objective import MAX_NET_BENEFIT
from simulation import FamilySimulation
from simulation_config import SimulationConfig


def _engine_config(hold_draw):
    """Internal config for the engine-level tests (the same shape
    tests/test_strategy_simulation.py's TestDebtTracking uses). RRSP room +
    a 20% savings rate so real refunds arise; capitalize_interest=False so
    the drawn-margin interest is serviced in CASH and the balance moves ONLY
    through the rrsp_refund_heloc_paydown rule -- making the hold-vs-sweep
    difference directly observable on heloc_balance."""
    return SimulationConfig(
        projection_years=4,
        investment_return=0.07,
        salary_growth=0.0,
        savings_rate=0.20,
        house_value=1_000_000,
        mortgage_balance=100_000,
        mortgage_rate=0.05,
        ltv_max=0.80,
        amortization_years=20,
        margin_available=200_000,
        heloc_readvance=False,
        capitalize_interest=False,
        hold_borrow_to_invest_draw=hold_draw,
        heloc_rate=0.05,
        # Route the whole year-0 draw into non-reg (the same lever
        # optimize.run_borrow_to_invest_exploration sets per cell, D3/#1036)
        # so the s.20(1)(c) trace is 100% investment and the deduction is
        # observable -- the registered-first waterfall would otherwise land
        # the draw in RRSP/TFSA and the deductible proportion would be 0.
        family_members=[
            {'role': 'primary', 'gross_income': 120_000,
             'rrsp_room_accumulated': 100_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1990},
            {'role': 'spouse', 'gross_income': 50_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1992},
        ],
        children=[],
        refinance_advance_deductible_non_reg=100_000,
    )


def _run(config, lump_sum=100_000):
    rp = build_rate_path("test", 0.05, config.projection_years,
                         'variable', [0.05])
    sim = FamilySimulation(config, STRATEGY_BALANCED, rp,
                           use_readvanceable=False, lump_sum=lump_sum)
    return sim, sim.run()


class TestHoldDrawEngineBehaviour(unittest.TestCase):
    """Acceptance #1 and #2, driven through FamilySimulation.run()."""

    def test_hold_draw_holds_the_balance_flat(self):
        """hold_draw=true: no RRSP-refund paydown touches the draw -- the
        balance is held flat across the horizon while the interest is still
        priced, serviced in cash, and deducted under s.20(1)(c)."""
        sim, results = _run(_engine_config(hold_draw=True))
        self.assertGreater(sim._state.heloc_balance, 0,
                           "the draw must be booked (lump_sum via "
                           "initial_state_for_run)")
        draw = sim._state.heloc_balance

        # Held FLAT: with capitalize_interest=False the balance moves only
        # through the paydown rule, so every year-end equals the draw.
        for r in results:
            self.assertAlmostEqual(
                r.heloc_balance, draw, places=6,
                msg=f"hold_draw must hold the balance flat; year {r.year} "
                    f"read {r.heloc_balance:,.2f} vs draw {draw:,.2f}")

        # The cumulative refund-paydown tracker never moved.
        self.assertEqual(
            sim._state.jurisdiction_state['canada']['heloc_rrsp_paydown'], 0.0,
            "hold_draw must opt the draw out of the RRSP-refund paydown sweep")

        # Refunds were still GENERATED (they flow to the usual allocation
        # instead of the sweep) ...
        refunds = sum(r.rrsp_tax_savings for r in results)
        self.assertGreater(refunds, 0,
            "the RRSP refund must still be generated under hold_draw")
        # ... the interest was still PRICED and serviced in cash ...
        serviced = sum(r.heloc_interest_serviced for r in results)
        self.assertGreater(serviced, 0,
            "the drawn-margin interest must still be priced and serviced")
        # ... and still DEDUCTED under s.20(1)(c).
        self.assertGreater(results[0].margin_deductible_interest, 0,
            "the borrowed-to-invest interest must stay deductible")

    def test_default_sweeps_refund_against_the_draw(self):
        """Regression pin: the SAME config without hold_draw keeps the
        pre-#1040 behaviour -- each year's refund pays the draw down."""
        sim, results = _run(_engine_config(hold_draw=False))
        refunds = sum(r.rrsp_tax_savings for r in results)
        self.assertGreater(refunds, 0,
            "fixture must generate refunds for the sweep to be observable")
        self.assertGreater(
            sim._state.jurisdiction_state['canada']['heloc_rrsp_paydown'], 0,
            "default (no hold_draw) must keep the RRSP-refund paydown sweep")
        draw = 100_000
        self.assertLess(results[-1].heloc_balance, draw,
            "with the sweep active the balance must fall below the draw")

    def test_hold_draw_refund_flows_to_the_usual_allocation(self):
        """The refund itself is identical under both flags (only its
        ALLOCATION differs): under hold_draw the refund is not consumed by
        the paydown, so it stays in the household's cash and reaches the
        usual pots -- the two trajectories genuinely diverge."""
        _, results_hold = _run(_engine_config(hold_draw=True))
        _, results_sweep = _run(_engine_config(hold_draw=False))
        refunds_hold = sum(r.rrsp_tax_savings for r in results_hold)
        refunds_sweep = sum(r.rrsp_tax_savings for r in results_sweep)
        self.assertAlmostEqual(
            refunds_hold, refunds_sweep, places=6,
            msg="the refund generated must not depend on where it is allocated")
        self.assertGreater(
            abs(results_hold[-1].total_assets
                - results_sweep[-1].total_assets), 1.0,
            msg="holding the draw must reallocate the refund -- the "
                "trajectories must differ")


class TestHoldDrawContractMapping(unittest.TestCase):
    """Schema -> input_contract -> internal option dict (DP#18: the decision
    must reach something the engine reads; DP#32: absence stays absent)."""

    def _doc_with_btv(self, **extra):
        from test_input_contract import _load_example, _two_generation_subset
        doc = _two_generation_subset(_load_example())
        doc["liabilities"] = [
            dict(l, readvanceable=False) if l["kind"] == "heloc" else l
            for l in doc["liabilities"] if l["kind"] == "heloc"
        ]
        option = {"id": "btv_hold", "label": "Draw $50k and hold",
                  "source": doc["liabilities"][0]["id"],
                  "amount": 50_000, "target_account": "non_reg"}
        option.update(extra)
        doc["decisions"]["borrow_to_invest"] = [option]
        return doc

    def test_schema_accepts_and_maps_hold_draw_true(self):
        doc = self._doc_with_btv(hold_draw=True)
        ic.validate_contract(doc)  # must not raise
        cfg = ic.to_internal_config(doc)
        options = cfg["borrow_to_invest_options"]
        self.assertEqual(len(options), 1)
        self.assertTrue(options[0].get("hold_draw"),
            "a declared hold_draw=true must reach the internal option dict")

    def test_absent_hold_draw_stays_absent(self):
        """A config that does not declare hold_draw maps to the exact
        pre-#1040 internal shape -- no synthesized key (DP#24/DP#32)."""
        doc = self._doc_with_btv()
        ic.validate_contract(doc)  # must not raise
        cfg = ic.to_internal_config(doc)
        self.assertNotIn("hold_draw", cfg["borrow_to_invest_options"][0])

    def test_simulation_config_round_trips_the_flag(self):
        sc = SimulationConfig.from_dict({
            'assumptions': {'projection_years': 1},
            'property': {'borrow_to_invest_hold_draw': True}})
        self.assertTrue(sc.hold_borrow_to_invest_draw)
        reloaded = SimulationConfig.from_dict(sc.to_dict())
        self.assertTrue(reloaded.hold_borrow_to_invest_draw,
            "a declared hold_draw must survive a load->modify->save cycle")

    def test_simulation_config_defaults_false_when_absent(self):
        sc = SimulationConfig.from_dict({
            'assumptions': {'projection_years': 1}})
        self.assertFalse(sc.hold_borrow_to_invest_draw,
            "absent flag defaults to the pre-#1040 sweep behaviour (DP#32)")
        d = SimulationConfig.from_dict(sc.to_dict()).to_dict()
        self.assertNotIn('borrow_to_invest_hold_draw',
                         d['property'],
            "False round-trips to 'absent', byte-identical to pre-#1040")


class TestHoldDrawExplorationWiring(unittest.TestCase):
    """optimize.run_borrow_to_invest_exploration must set the engine-facing
    property.borrow_to_invest_hold_draw key on exactly the hold-draw cells'
    configs (DP#18: the decision modifies a key the engine reads)."""

    def test_only_hold_draw_cells_set_the_engine_flag(self):
        from test_input_contract import _load_example, _two_generation_subset
        doc = _two_generation_subset(_load_example())
        doc["liabilities"] = [
            dict(l, readvanceable=False) if l["kind"] == "heloc" else l
            for l in doc["liabilities"] if l["kind"] == "heloc"
        ]
        source_id = doc["liabilities"][0]["id"]
        doc["decisions"]["borrow_to_invest"] = [
            {"id": "btv_sweep", "label": "Draw $50k (sweep)",
             "source": source_id, "amount": 50_000,
             "target_account": "non_reg"},
            {"id": "btv_hold", "label": "Draw $50k and hold",
             "source": source_id, "amount": 50_000,
             "target_account": "non_reg", "hold_draw": True},
        ]
        cfg = ic.to_internal_config(doc)

        captured = []

        def fake_map_scenarios(payloads):
            captured.extend(p['kwargs']['cfg'] for p in payloads)
            return []

        orig = optimize._map_scenarios
        optimize._map_scenarios = fake_map_scenarios
        try:
            results = optimize.run_borrow_to_invest_exploration(
                cfg, "input.json", objective=MAX_NET_BENEFIT)
        finally:
            optimize._map_scenarios = orig
        self.assertEqual(results, [])

        # One cfg per (cell x income scenario); the hold-draw cell's cfgs all
        # carry the flag, and NO other cell's does.
        hold_cfgs = [c for c in captured
                     if c['property'].get('borrow_to_invest_hold_draw')]
        sweep_cfgs = [c for c in captured
                      if not c['property'].get('borrow_to_invest_hold_draw')]
        self.assertTrue(hold_cfgs, "the hold-draw cell must set the flag")
        self.assertTrue(sweep_cfgs,
            "the baseline and sweep cells must not set the flag")
        # Every captured cfg that sets the flag declares the same draw amount
        # as the hold option (the refinance_advance split marks the cell's
        # amount); the flag rides only on those.
        for c in hold_cfgs:
            self.assertGreaterEqual(
                c['property']['refinance_advance_deductible_non_reg'], 50_000)


if __name__ == '__main__':
    unittest.main()
