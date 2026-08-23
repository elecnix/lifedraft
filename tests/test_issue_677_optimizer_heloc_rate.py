#!/usr/bin/env python3
"""Tests for issue #677: the HELOC-rate-from-mortgage-rate bug (#654) was
still live in the optimizer's OWN paths.

#654 fixed the core ``simulation.py`` pricing path (a ``kind=heloc``
liability's own declared ``rate`` reaches ``SimulationConfig.heloc_rate``
and is honoured outright by ``HELOCPath.get_heloc_rate``). #677 found the
identical mortgage-rate-aliasing pattern still live in:

- ``optimizer.py``'s ``Optimizer.__init__`` -- built ``HELOCPath`` with no
  ``fixed_rate`` at all, so it ALWAYS derived the HELOC rate from the
  mortgage's rate path, for every household, declared HELOC rate or not.
  This is the engine behind every ``Optimizer.optimize()`` ranking
  (``GridOptimizer``/``ScipyOptimizer``).
- ``optimize.py``'s ``run_optimization``/``simulated_deduct_timing`` (the
  strategy-discovery screening that decides whether Smith-Manoeuvre is even
  offered) and its CLI ``main()`` INPUTS header (the headline after-tax SM
  cost every user reads) -- all read ``property.mortgage_rate`` as a
  stand-in for the HELOC's own rate.
- ``scenario_discovery.py``'s ``_discover_strategy_scenarios``/
  ``_discover_readvanceable_options`` -- the same screening heuristic,
  reached via ``discover_anchors``/``simulate.py``'s pipeline.

Root assertion this file exists to pin down (mirroring #654's own
``tests/test_issue_654_heloc_rate.py``, but through the OPTIMIZER rather
than the simulator, since that is where the surviving sites lived): a
household whose HELOC rate differs from its mortgage rate MUST produce a
DIFFERENT after-tax Smith-Manoeuvre borrowing decision than one where they
are equal -- asserted through ``optimizer.Optimizer`` and
``optimize.run_optimization``, not just ``simulation.FamilySimulation``.

DP#15: all data below is fabricated, round, role-based (DP#4).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from optimizer import Optimizer
from optimize import run_optimization
from config_access import resolve_heloc_rate
from simulation_config import SimulationConfig
from countries.canada.rate_model import build_rate_path
from countries.canada.adapter import CanadaAdapter


# ── optimizer.py: Optimizer.__init__'s HELOCPath construction ──────────────

def _make_optimizer_config(mortgage_rate, heloc_rate):
    """Fabricated round-number config (DP#4/DP#15), mirroring
    tests/test_issue_654_heloc_rate.py's _make_config."""
    return SimulationConfig(
        projection_years=3,
        investment_return=0.06,
        salary_growth=0.0,
        savings_rate=0.10,
        house_value=600_000,
        mortgage_balance=200_000,
        mortgage_rate=mortgage_rate,
        ltv_max=0.80,
        amortization_years=20,
        margin_available=100_000,
        heloc_readvance=False,
        heloc_rate=heloc_rate,
        family_members=[
            {'role': 'primary', 'gross_income': 130_000,
             'rrsp_room_accumulated': 100_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1985},
            {'role': 'spouse', 'gross_income': 70_000,
             'rrsp_room_accumulated': 50_000, 'tfsa_room_accumulated': 30_000,
             'birth_year': 1987},
        ],
        children=[],
    )


class OptimizerHelocPathTest(unittest.TestCase):
    """optimizer.py:237-238 -- Optimizer.__init__ built HELOCPath with no
    fixed_rate at all, so it ALWAYS derived the rate from the mortgage's
    rate path, regardless of any declared config.heloc_rate."""

    def test_declared_heloc_rate_reaches_the_optimizer_not_the_mortgage_rate(self):
        config = _make_optimizer_config(mortgage_rate=0.02, heloc_rate=0.08)
        opt = Optimizer(config)
        self.assertEqual(opt.heloc_path.get_heloc_rate(0), 0.08)
        self.assertNotEqual(opt.heloc_path.get_heloc_rate(0), config.mortgage_rate)

    def test_equal_vs_differing_heloc_rate_produce_different_optimizer_heloc_paths(self):
        equal = Optimizer(_make_optimizer_config(mortgage_rate=0.03, heloc_rate=0.03))
        differs = Optimizer(_make_optimizer_config(mortgage_rate=0.03, heloc_rate=0.09))
        self.assertNotEqual(
            equal.heloc_path.get_heloc_rate(0),
            differs.heloc_path.get_heloc_rate(0),
            "The Optimizer's own HELOC rate path must reflect the declared "
            "heloc_rate, not silently collapse to the mortgage rate for "
            "every household (#677).",
        )
        self.assertEqual(differs.heloc_path.get_heloc_rate(0), 0.09)

    def test_optimizer_mirrors_adapter_build_heloc_path(self):
        """Optimizer must delegate to the SAME already-fixed (#654) code
        path FamilySimulation uses, not a second, independently-broken
        construction (DP#9: no duplicate logic)."""
        config = _make_optimizer_config(mortgage_rate=0.04, heloc_rate=0.07)
        opt = Optimizer(config)
        adapter = CanadaAdapter(config)
        expected = adapter.build_heloc_path(opt.rate_path, heloc_rate=config.heloc_rate)
        self.assertEqual(
            opt.heloc_path.get_heloc_rate(0),
            expected.get_heloc_rate(0),
        )


# ── optimize.py: run_optimization/simulated_deduct_timing screening ────────

def _make_run_optimization_cfg(heloc_rate, mortgage_rate=0.03):
    """A household with a cheap legacy fixed mortgage (3%) and a
    readvanceable facility -- the ordinary shape #677 describes. Round,
    fabricated numbers (DP#4/DP#15)."""
    return {
        'assumptions': {
            'projection_years': 5,
            'investment_return': 0.07,
            'salary_growth': 0.02,
        },
        'savings': {'rate': 0.20},
        'property': {
            'house_value': 500000,
            'mortgage_balance': 100000,
            'mortgage_rate': mortgage_rate,
            'heloc_rate': heloc_rate,
            'margin_available': 100000,
            'heloc_readvance': True,
            'ltv_max': 0.80,
            'amortization_years': 25,
        },
        'family': {
            'members': [
                {'role': 'primary', 'gross_income': 150000,
                 'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000,
                 'pension_adjustment': 0},
                {'role': 'spouse', 'gross_income': 70000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 30000,
                 'pension_adjustment': 0},
            ],
            'children': [],
        },
        'accounts': {
            'rrsp_annual_percent': 0.18,
            'rrsp_annual_max': 33000,
            'tfsa_annual_room_per_person': 7000,
            'resp_current_balance': 0,
        },
    }


class RunOptimizationHelocRateTest(unittest.TestCase):
    """optimize.py:~606/~775 (run_optimization/simulated_deduct_timing) --
    used to read property.mortgage_rate as opt_heloc_rate outright,
    understating the after-tax SM cost for the common shape of a cheap
    legacy fixed mortgage alongside a pricier floating HELOC.

    This household is constructed so the after-tax Smith-Manoeuvre
    profitability gate (DP#6, countries.canada.strategies.
    discover_strategies) FLIPS depending only on the declared heloc_rate,
    with mortgage_rate and every other input held fixed: at a 3% HELOC rate
    (equal to the mortgage) the strategy clears the after-tax hurdle; at an
    11% HELOC rate it does not. Verified empirically against this exact
    fixture, not asserted from a hand-derived formula.

    Before #677's fix, BOTH cases read mortgage_rate (3%) regardless of the
    declared heloc_rate, so 'readvance_priority' was discovered in both --
    the optimizer offered Smith-Manoeuvre as viable even in the 11% HELOC
    case where it genuinely is not profitable, always in the household's
    favour.
    """

    def _strategy_names(self, heloc_rate):
        cfg = _make_run_optimization_cfg(heloc_rate)
        results = run_optimization(cfg)
        return {r['strategy'] for r in results}

    def test_cheap_heloc_equal_to_mortgage_offers_readvance_priority(self):
        names = self._strategy_names(heloc_rate=0.03)
        self.assertIn('readvance_priority', names)

    def test_expensive_heloc_above_mortgage_does_not_offer_readvance_priority(self):
        names = self._strategy_names(heloc_rate=0.11)
        self.assertNotIn('readvance_priority', names)

    def test_differing_heloc_rate_changes_the_optimizer_decision(self):
        """The required behavioural assertion (#656/#677's enforcement
        ask): a document whose HELOC rate differs from its mortgage rate
        MUST produce a different after-tax SM borrowing decision than one
        where they are equal -- asserted through run_optimization()
        itself, not a value computed alongside it."""
        equal_names = self._strategy_names(heloc_rate=0.03)  # == mortgage_rate
        differs_names = self._strategy_names(heloc_rate=0.11)  # != mortgage_rate
        self.assertNotEqual(
            equal_names, differs_names,
            "A HELOC rate that differs from the mortgage rate must change "
            "which strategies the optimizer offers -- identical results "
            "mean the declared HELOC rate never reached run_optimization() "
            "(#677).",
        )


class ResolveHelocRateTest(unittest.TestCase):
    """config_access.resolve_heloc_rate() -- the one canonical resolver
    every #677 call site now uses."""

    def test_declared_property_heloc_rate_wins_over_mortgage_rate(self):
        cfg = {'property': {'mortgage_rate': 0.02, 'heloc_rate': 0.08,
                             'margin_available': 50000}}
        self.assertEqual(resolve_heloc_rate(cfg), 0.08)

    def test_assumptions_override_wins_over_property_heloc_rate(self):
        """DP#5: an anchor decision (optimize.py's apply_anchor_preset)
        overriding the HELOC rate for a hypothetical scenario must win over
        the household's base declared rate, not be silently shadowed by
        it."""
        cfg = {
            'property': {'mortgage_rate': 0.02, 'heloc_rate': 0.08,
                         'margin_available': 50000},
            'assumptions': {'heloc_rate': 0.05},
        }
        self.assertEqual(resolve_heloc_rate(cfg), 0.05)

    def test_no_facility_returns_the_caller_supplied_default_not_mortgage_rate(self):
        """A household with no readvanceable facility at all must not have
        the mortgage rate silently smuggled in as its HELOC rate, even as
        an inert placeholder -- the caller's own explicit default wins."""
        cfg = {'property': {'mortgage_rate': 0.02}}
        self.assertEqual(resolve_heloc_rate(cfg, default=0.0), 0.0)
        self.assertIsNone(resolve_heloc_rate(cfg))

    def test_declared_facility_without_a_rate_derives_from_mortgage_and_warns(self):
        """A hand-built config that predates the heloc_rate field (declares
        margin_available but never heloc_rate) gets the DP#13
        mortgage-derived placeholder -- logged, not silent -- matching
        #654's own precedent for exactly this legacy-fixture case. A real
        contract can never reach this branch (input_contract.py always
        declares both together)."""
        cfg = {'property': {'mortgage_rate': 0.045, 'margin_available': 50000}}
        with self.assertLogs('simulation_config', level='WARNING') as caught:
            rate = resolve_heloc_rate(cfg)
        self.assertEqual(rate, 0.045)
        self.assertTrue(
            any('heloc_rate' in m for m in caught.output),
            f"Expected a loud warning about the undeclared HELOC rate; got: {caught.output}",
        )


if __name__ == "__main__":
    unittest.main()
