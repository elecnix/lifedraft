#!/usr/bin/env python3
"""Enforcement tests for issue #1069 -- the reader ``heloc_interest_unfunded``
never had.

``apply_heloc_interest_servicing`` (#681) writes
``heloc_interest_unfunded`` when the non-reg/SM pots cannot cover the
cash-serviced HELOC interest, and ``YearResult`` surfaces it -- and before
this issue NOTHING read it: no trajectory invariant, no objective, no report.
Since #1036 wires ``liabilities[kind=heloc].capitalize_interest`` (false =
service ALL drawn-margin interest in cash, the declared default in
``schema/example.json``), any household that draws the margin and cannot fully
service it loses that interest silently, every year (~$4,306/yr for ~35 years
on the reviewer's repro).

Three parts, mirroring the #681 module's shape:

  1. a run through ``FamilySimulation.run()`` on a household whose pots run
     dry asserts the unfunded amount is REPORTED, not evaporated;
  2. the conservation invariant runs IN THE FOLD (both ``FamilySimulation``
     and ``GridOptimizer._run_simulation`` refuse an evaporating bookkeeping);
  3. the invariant's mechanics, as raw-trajectory checks (DP#17: both sides
     of every threshold).

Fabricated round numbers, role-based names only (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from countries.canada.strategies import STRATEGY_BALANCED
from optimizer import GridOptimizer
from simulation import FamilySimulation
from simulation_config import SimulationConfig
from year_result import YearResult
from trajectory_invariants import (
    InvariantBreachedError, RUN_PATH_INVARIANTS, assert_run_invariants,
    run_invariant,
)

HOUSE_VALUE = 800_000       # -> 80% charge = $640,000
MORTGAGE = 400_000
MARGIN_DRAW = 240_000       # mortgage + draw = $640k: the charge opens FULL


def _household(projection_years=35):
    """A retired household that drew its margin to the charge limit.

    Retired (no employment income), zero declared savings, zero investment
    return: the drawn lump sum is the only pot, the cash-serviced interest
    drains it within a few years, and from then on EVERY year's serviced
    interest is unpayable. On this base the engine capitalizes up to the
    charge and services only the over-room slice -- the same servicing rule
    #1036's ``capitalize_interest: false`` default routes ALL margin interest
    through -- so this is the path that produces ``heloc_interest_unfunded``,
    at production intensity.
    """
    cfg = SimulationConfig(
        projection_years=projection_years,
        house_value=HOUSE_VALUE,
        mortgage_balance=MORTGAGE,
        margin_available=MARGIN_DRAW,
        mortgage_rate=0.05,
        heloc_rate=0.055,
        amortization_years=25,
        refinance_amortization_years=25,
        heloc_readvance=True,
        family_members=[
            {'role': 'primary', 'birth_year': 1958, 'gross_income': 150_000,
             'retirement_age': 60, 'rrsp_room_accumulated': 0,
             'tfsa_room_accumulated': 20_000},
        ],
        start_year=2026,
        investment_return=0.0,
        savings_rate=0.0,
    )
    sim = FamilySimulation(cfg, adapter=CanadaAdapter(cfg),
                           use_readvanceable=True, lump_sum=cfg.margin_available)
    return cfg, sim.run()


def _evaporate_unfunded():
    """Restore, for one test, the exact defect #1069 closes: a bookkeeping
    where the unpayable slice silently vanishes. Wraps the real servicing
    rule and zeroes its report AFTER the fact -- the pots are still drained
    (or never touched), but nothing is reported."""
    import rule_registry
    import simulation_rules  # noqa: F401 -- the import POPULATES RULES
    original = rule_registry.RULES['heloc_interest_servicing']

    def _evaporating(ws, ctx):
        fired = bool(original(ws, ctx))
        ws.heloc_interest_unfunded = 0.0
        return fired

    rule_registry.RULES['heloc_interest_servicing'] = _evaporating
    return original


# ============================================================================
# 1. The capitalize_interest=false path reports the shortfall it cannot pay.
# ============================================================================

class TestUnfundedInterestIsReportedNotEvaporated:
    def test_shortfall_is_reported_once_the_pots_run_dry(self):
        _, results = _household()
        serviced_years = [r for r in results if r.heloc_interest_serviced > 0]
        assert len(serviced_years) == len(results), (
            "fixture broken: this household must service margin interest in "
            "cash every year (the charge opens full)")
        # The drawn lump sum covers the first few years; once it is gone,
        # EVERY serviced dollar is unpayable -- and must be REPORTED.
        first_dry = next((i for i, r in enumerate(results)
                          if r.heloc_interest_unfunded > 0), None)
        assert first_dry is not None, (
            "no year reported unfunded interest -- the shortfall evaporated")
        assert first_dry <= len(results) - 20, (
            f"fixture broken: pots lasted {first_dry} of {len(results)} years; "
            "the unpayable tail is too short to bite")
        for r in results[first_dry:]:
            assert r.heloc_interest_unfunded == pytest.approx(
                r.heloc_interest_serviced), (
                f"year {r.year}: pots are empty but only "
                f"${r.heloc_interest_unfunded:,.2f} of "
                f"${r.heloc_interest_serviced:,.2f} serviced interest was "
                f"reported as unfunded -- the rest evaporated")

    def test_the_leak_is_material_not_a_rounding_artifact(self):
        """The reviewer's repro loses ~$4,306/yr for ~35 years. This fixture's
        shape is the same: six figures silently leave the balance sheet over
        the horizon while the run stays green."""
        _, results = _household()
        total = sum(r.heloc_interest_unfunded for r in results)
        assert total > 100_000, (
            f"total unfunded ${total:,.2f} -- expected a material silent leak")

    def test_no_year_reports_more_unfunded_than_it_owed(self):
        _, results = _household()
        for r in results:
            assert -0.5 <= r.heloc_interest_unfunded <= r.heloc_interest_serviced + 0.5, (
                f"year {r.year}: unfunded ${r.heloc_interest_unfunded:,.2f} "
                f"outside [0, serviced ${r.heloc_interest_serviced:,.2f}]")


# ============================================================================
# 2. The conservation invariant runs in the fold, on every production run.
# ============================================================================

class TestInvariantRunsInTheFold:
    def test_invariant_is_in_the_run_path_set(self):
        assert 'heloc_interest_fully_accounted' in RUN_PATH_INVARIANTS

    def test_family_simulation_refuses_an_evaporating_bookkeeping(self):
        """Behavioural proof, not narration: restore the #1069 defect by hand
        (zero the unfunded report after the real rule runs) and confirm
        ``FamilySimulation.run()`` itself REFUSES the trajectory -- the
        funded+unfunded split no longer conserves against the serviced slice.
        Pre-#1069 this trajectory completed green."""
        original = _evaporate_unfunded()
        try:
            with pytest.raises(InvariantBreachedError, match='unfunded'):
                _household(projection_years=10)
        finally:
            import rule_registry
            rule_registry.RULES['heloc_interest_servicing'] = original

    def test_optimizer_fold_refuses_it_too(self):
        """The OPTIMIZER's fold ranks scenarios; an invariant wired into only
        one of the two folds is the half-enforcement #681 is about."""
        import rule_registry
        original = _evaporate_unfunded()
        try:
            cfg = SimulationConfig(
                projection_years=10, house_value=HOUSE_VALUE,
                mortgage_balance=MORTGAGE, margin_available=MARGIN_DRAW,
                mortgage_rate=0.05, heloc_rate=0.055, amortization_years=25,
                refinance_amortization_years=25, heloc_readvance=True,
                start_year=2026, investment_return=0.0, savings_rate=0.0,
                family_members=[{'role': 'primary', 'birth_year': 1958,
                                  'gross_income': 150_000, 'retirement_age': 60,
                                  'rrsp_room_accumulated': 0,
                                  'tfsa_room_accumulated': 20_000}],
            )
            opt = GridOptimizer(cfg)
            with pytest.raises(InvariantBreachedError, match='unfunded'):
                opt._run_simulation(cfg, strategy=STRATEGY_BALANCED, use_readvanceable=True,
                                    lump_sum=cfg.margin_available)
        finally:
            rule_registry.RULES['heloc_interest_servicing'] = original


# ============================================================================
# 3. The invariant's mechanics, as raw-trajectory checks (DP#17).
# ============================================================================

class TestInvariantMechanics:
    def test_clean_split_passes(self):
        ok = [YearResult(year=0, heloc_interest_charged=1_000.0,
                          heloc_interest_capitalized=600.0,
                          heloc_interest_serviced=400.0,
                          heloc_servicing_funded=250.0,
                          heloc_interest_unfunded=150.0)]
        assert run_invariant('heloc_interest_fully_accounted', ok, {}) == []

    def test_absorbed_charged_slice_is_flagged(self):
        """$200 of charged interest is neither capitalized nor serviced."""
        absorbed = [YearResult(year=3, heloc_interest_charged=1_000.0,
                                heloc_interest_capitalized=600.0,
                                heloc_interest_serviced=200.0,
                                heloc_servicing_funded=200.0,
                                heloc_interest_unfunded=0.0)]
        violations = run_invariant('heloc_interest_fully_accounted', absorbed, {})
        assert len(violations) == 1
        assert 'absorbed' in violations[0].message
        assert 'neither capitalized nor serviced' in violations[0].message

    def test_absorbed_serviced_slice_is_flagged(self):
        """The #1069 evaporation shape exactly: $150 of SERVICED interest is
        neither funded from the pots nor reported as unfunded."""
        evaporated = [YearResult(year=7, heloc_interest_charged=1_000.0,
                                  heloc_interest_capitalized=600.0,
                                  heloc_interest_serviced=400.0,
                                  heloc_servicing_funded=250.0,
                                  heloc_interest_unfunded=0.0)]
        violations = run_invariant('heloc_interest_fully_accounted', evaporated, {})
        assert len(violations) == 1
        assert 'neither funded nor reported' in violations[0].message

    def test_negative_unfunded_is_flagged(self):
        negative = [YearResult(year=1, heloc_interest_charged=500.0,
                                heloc_interest_capitalized=0.0,
                                heloc_interest_serviced=500.0,
                                heloc_servicing_funded=700.0,
                                heloc_interest_unfunded=-200.0)]
        violations = run_invariant('heloc_interest_fully_accounted', negative, {})
        assert any('negative' in v.message for v in violations)

    def test_unfunded_exceeding_the_serviced_slice_is_flagged(self):
        fabricated = [YearResult(year=2, heloc_interest_charged=500.0,
                                  heloc_interest_capitalized=0.0,
                                  heloc_interest_serviced=500.0,
                                  heloc_servicing_funded=0.0,
                                  heloc_interest_unfunded=900.0)]
        violations = run_invariant('heloc_interest_fully_accounted', fabricated, {})
        assert any('never charged' in v.message for v in violations)

    def test_household_without_a_heloc_is_trivially_clean(self):
        """All-zero defaults (no facility, or a year the rule never fires)
        conserve trivially -- the guard costs nothing where it does not apply
        (mirrors #681's no-property case)."""
        plain = [YearResult(year=0), YearResult(year=1)]
        assert run_invariant('heloc_interest_fully_accounted', plain, {}) == []

    def test_assert_run_invariants_raises_on_the_evaporation_repro(self):
        bad = [YearResult(year=0, heloc_interest_charged=1_000.0,
                           heloc_interest_capitalized=600.0,
                           heloc_interest_serviced=400.0,
                           heloc_servicing_funded=250.0,
                           heloc_interest_unfunded=0.0)]
        with pytest.raises(InvariantBreachedError) as exc:
            assert_run_invariants(bad, SimulationConfig(start_year=2026))
        assert 'heloc_interest_fully_accounted' in str(exc.value)
