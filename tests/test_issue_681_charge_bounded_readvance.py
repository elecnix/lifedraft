#!/usr/bin/env python3
"""Enforcement tests for issue #681 -- the half of #664 that was missing.

#664 defined ``charge_limit()``, refused an over-limit facility at document
load time, and wrote the ``total_secured_debt_within_charge_limit``
trajectory invariant. All correct. And then:

  - **the invariant never ran outside tests/** -- no production path called
    it, so it caught nothing a real household actually did; and
  - **the readvance rule was not bounded by the charge** -- it re-borrowed
    every year's mortgage principal with no ceiling at all.

So the charge held at t=0 and was violated by t=1: a readvanceable household
on an $800,000 home (charge $640,000) re-borrowed its way to $3,057,904 of
secured debt -- 382% LTV -- and the engine printed a confident recommendation
off it, green. Worse, the LESS it borrowed up front the MORE debt it ended
with (a smaller mortgage amortizes faster, so more principal is paid, so more
is readvanced, for longer) -- an inversion that is simply impossible.

This module is the regression lock, in three parts:

  1. the readvance is BOUNDED (``TestReadvanceBoundedByCharge``);
  2. the bound is ENFORCED FROM THE RUN PATH, not from a fixture
     (``TestInvariantRunsInProductionPath``);
  3. a breach is LOUD and, when ranked, SURFACED -- never swallowed into a
     ``-inf`` row at the bottom of a table (``TestBreachIsLoudAndSurfaced``,
     the #657 half).

Fabricated round numbers, role-based names only (DP#4/DP#15).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from countries.canada.adapter import CanadaAdapter
from countries.canada.strategies import STRATEGY_BALANCED
from objective import MAX_NET_BENEFIT
from optimizer import GridOptimizer
from simulation import FamilySimulation
from simulation_config import (
    SimulationConfig, YearResult, apply_ltv_overlay, charge_limit,
    charge_room_for_readvance, heloc_revolving_limit,
)
from trajectory_invariants import (
    InvariantBreachedError, RUN_PATH_INVARIANTS, assert_run_invariants,
    run_invariant,
)

HOUSE_VALUE = 800_000          # -> 80% charge = $640,000; 65% revolving = $520,000
CHARGE = 640_000
REVOLVING_CEILING = 520_000


def _household(ltv=0.0, margin_available=400_000, mortgage_balance=157_387,
                projection_years=25, house_value=HOUSE_VALUE, use_readvanceable=True):
    """A fabricated readvanceable household, optionally refinanced to ``ltv``."""
    cfg = SimulationConfig(
        projection_years=projection_years,
        house_value=house_value,
        mortgage_balance=mortgage_balance,
        margin_available=margin_available,
        mortgage_rate=0.05,
        heloc_rate=0.055,
        amortization_years=25,
        refinance_amortization_years=25,
        heloc_readvance=True,
        family_members=[
            {'role': 'primary', 'birth_year': 1980, 'gross_income': 150_000,
             'retirement_age': 65, 'rrsp_room_accumulated': 0,
             'tfsa_room_accumulated': 20_000},
        ],
        start_year=2026,
        investment_return=0.06,
        savings_rate=0.10,
    )
    overlaid = apply_ltv_overlay(cfg, ltv) if ltv > 0 else cfg
    lump_sum = overlaid.margin_available + overlaid.cash_out
    sim = FamilySimulation(overlaid, adapter=CanadaAdapter(overlaid),
                            use_readvanceable=use_readvanceable, lump_sum=lump_sum)
    return overlaid, sim.run()


# ============================================================================
# charge_room_for_readvance -- the mechanism, as a pure function (DP#3/DP#17:
# both sides of both ceilings).
# ============================================================================

class TestChargeRoomForReadvance:
    def test_room_is_what_the_charge_has_left(self):
        room = charge_room_for_readvance(HOUSE_VALUE, mortgage_balance=400_000,
                                          drawn_revolving=100_000)
        assert room == pytest.approx(CHARGE - 500_000)  # 140,000

    def test_room_is_zero_when_the_charge_is_full(self):
        """When the charge is full the readvance STOPS -- 0, never negative."""
        assert charge_room_for_readvance(
            HOUSE_VALUE, mortgage_balance=CHARGE, drawn_revolving=0) == 0.0

    def test_room_is_zero_when_the_charge_is_over_full(self):
        assert charge_room_for_readvance(
            HOUSE_VALUE, mortgage_balance=CHARGE + 200_000, drawn_revolving=50_000) == 0.0

    def test_paying_the_mortgage_down_is_what_creates_room(self):
        """The whole readvanceable mechanism, in one assertion: room grows
        dollar-for-dollar as mortgage principal is repaid, because the charge
        is FIXED."""
        before = charge_room_for_readvance(HOUSE_VALUE, mortgage_balance=500_000,
                                            drawn_revolving=100_000)
        after = charge_room_for_readvance(HOUSE_VALUE, mortgage_balance=490_000,
                                           drawn_revolving=100_000)
        assert after - before == pytest.approx(10_000)

    def test_revolving_ceiling_binds_before_the_combined_charge(self):
        """DP#17: the 65% revolving-only ceiling is a SEPARATE, tighter
        constraint. With a tiny mortgage the 80% charge would allow far more,
        but the revolving segment alone may not pass 65% LTV."""
        room = charge_room_for_readvance(HOUSE_VALUE, mortgage_balance=10_000,
                                          drawn_revolving=REVOLVING_CEILING - 5_000)
        # Combined room would be 640k - 515k = 125k; revolving room is only 5k.
        assert room == pytest.approx(5_000)

    def test_revolving_ceiling_is_zero_once_reached(self):
        assert charge_room_for_readvance(
            HOUSE_VALUE, mortgage_balance=0, drawn_revolving=REVOLVING_CEILING) == 0.0


# ============================================================================
# 1. The readvance is bounded by the charge (issue #681's first half).
# ============================================================================

class TestReadvanceBoundedByCharge:
    def test_secured_debt_never_exceeds_the_charge_in_any_year(self):
        """The headline: a readvanceable household run for 25 years never
        breaches its charge in ANY year. Pre-#681 this reached 382% LTV."""
        _, results = _household(ltv=0.0)
        for i, r in enumerate(results):
            total = r.mortgage_balance + r.heloc_balance
            assert total <= CHARGE + 1.0, (
                f"year {i}: secured debt ${total:,.0f} exceeds charge ${CHARGE:,.0f}")

    def test_readvance_stops_and_reports_what_it_could_not_re_borrow(self):
        """When the charge fills, the readvance stops -- and the principal it
        could NOT re-borrow is REPORTED (``readvance_blocked``), not silently
        truncated (DP#32)."""
        _, results = _household(ltv=0.0)
        assert sum(r.readvance_blocked for r in results) > 0, (
            "this household should hit its charge and be refused further "
            "readvancing; nothing was reported as blocked")
        # Every blocked year readvanced strictly less than it repaid.
        for r in results:
            if r.readvance_blocked > 0:
                assert r.sm_readvanced < r.mortgage_principal

    def test_sm_readvanced_never_exceeds_mortgage_principal_repaid(self):
        """You cannot re-borrow more than you repaid, in any year."""
        _, results = _household(ltv=0.0)
        for r in results:
            assert r.sm_readvanced <= r.mortgage_principal + 1e-6

    def test_terminal_debt_is_monotonically_non_decreasing_in_ltv(self):
        """Issue #681's sharpest tell: pre-fix, borrowing LESS produced MORE
        debt (0% LTV -> $3.06M; 80% LTV -> $640k), because a smaller mortgage
        amortized faster and the unbounded readvance drew more, for longer.
        That inversion is impossible. Terminal debt must be monotonically
        NON-DECREASING in LTV."""
        debts = []
        for ltv in (0.0, 0.30, 0.50, 0.65, 0.80):
            _, results = _household(ltv=ltv)
            debts.append((ltv, results[-1].total_debt))
        for (lo_ltv, lo_debt), (hi_ltv, hi_debt) in zip(debts, debts[1:]):
            assert hi_debt >= lo_debt - 1.0, (
                f"borrowing MORE produced LESS debt: LTV {lo_ltv:.0%} -> "
                f"${lo_debt:,.0f} but LTV {hi_ltv:.0%} -> ${hi_debt:,.0f}")

    def test_readvanceable_without_a_declared_property_is_refused(self):
        """DP#32: a readvanceable line is a claim on the charge registered
        against a property. With no property there is no charge and no
        knowable room -- refuse, rather than silently advance nothing (which
        would look like a household legitimately out of room) or silently
        advance everything (the #681 bug)."""
        with pytest.raises(ValueError, match='house_value'):
            _household(house_value=0, margin_available=50_000)


# ============================================================================
# 2. The invariant runs in the PRODUCTION path (issue #681's second half --
#    "a detector that isn't wired into the path that matters is not done").
# ============================================================================

class TestInvariantRunsInProductionPath:
    def test_trajectory_invariants_is_library_code_not_test_code(self):
        """It must be importable WITHOUT tests/ on the path -- if it only
        lives in tests/, no production run path can ever call it, which is
        precisely how #681 happened."""
        import trajectory_invariants
        module_dir = os.path.dirname(os.path.abspath(trajectory_invariants.__file__))
        assert os.path.basename(module_dir) != 'tests', (
            "trajectory_invariants.py is back inside tests/ -- an invariant "
            "that only a fixture can reach is a test, not an invariant (#681)")

    def test_charge_invariants_are_in_the_run_path_set(self):
        assert 'total_secured_debt_within_charge_limit' in RUN_PATH_INVARIANTS
        assert 'heloc_within_revolving_limit' in RUN_PATH_INVARIANTS
        # Issue #94 (DP#18): the mortgage-amortization money-conservation
        # ledger identity is also a run-path invariant -- no dollar appears or
        # vanishes in the primary mortgage as it amortizes, asserted directly
        # in the fold every run, not only in a bespoke regression test.
        assert 'mortgage_conserves_principal' in RUN_PATH_INVARIANTS

    def test_family_simulation_run_asserts_them(self):
        """Behavioural proof, not narration: monkeypatch the readvance bound
        away (restoring the #681 bug exactly -- unbounded re-borrowing) and
        confirm ``FamilySimulation.run()`` itself now REFUSES the trajectory,
        rather than returning it for ranking."""
        import simulation_config as sc
        original = sc.charge_room_for_readvance
        import simulation_rules
        try:
            # The pre-#681 rule: infinite room, i.e. no bound at all.
            simulation_rules.charge_room_for_readvance = (
                lambda *a, **kw: float('inf'))
            with pytest.raises(InvariantBreachedError, match='charge limit'):
                _household(ltv=0.0)
        finally:
            simulation_rules.charge_room_for_readvance = original

    def test_optimizer_run_simulation_asserts_them(self):
        """The same guard on the OPTIMIZER's fold -- the path that actually
        RANKS scenarios. An invariant wired into only one of the two folds is
        exactly the half-enforcement #681 is about."""
        import simulation_rules
        original = simulation_rules.charge_room_for_readvance
        cfg = SimulationConfig(
            projection_years=10, house_value=HOUSE_VALUE, mortgage_balance=157_387,
            margin_available=400_000, mortgage_rate=0.05, heloc_rate=0.055,
            amortization_years=25, refinance_amortization_years=25,
            heloc_readvance=True, start_year=2026, investment_return=0.06,
            savings_rate=0.10,
            family_members=[{'role': 'primary', 'birth_year': 1980,
                              'gross_income': 150_000, 'retirement_age': 65,
                              'rrsp_room_accumulated': 0,
                              'tfsa_room_accumulated': 20_000}],
        )
        opt = GridOptimizer(cfg)
        try:
            simulation_rules.charge_room_for_readvance = (
                lambda *a, **kw: float('inf'))
            with pytest.raises(InvariantBreachedError):
                opt._run_simulation(cfg, STRATEGY_BALANCED, use_readvanceable=True,
                                     lump_sum=cfg.margin_available)
        finally:
            simulation_rules.charge_room_for_readvance = original

    def test_assert_run_invariants_flags_the_exact_681_reproduction(self):
        """Issue #681's reported numbers, replayed as a raw trajectory."""
        cfg = SimulationConfig(projection_years=1, house_value=HOUSE_VALUE,
                                start_year=2026)
        breaching = [YearResult(year=0, mortgage_balance=157_387,
                                 heloc_balance=3_057_904 - 157_387)]
        with pytest.raises(InvariantBreachedError) as exc:
            assert_run_invariants(breaching, cfg)
        assert 'charge limit' in str(exc.value)

    def test_mortgage_conservation_is_run_path_and_discharge_aware(self):
        """Issue #94 (DP#18): the mortgage-amortization money-conservation
        identity is asserted directly in the production run path, AND it is
        discharge-aware -- a household that sells its principal residence
        mid-horizon legitimately force-zeros the secured debt from the sale
        year on, which would read as a conservation break to a naive ledger
        check. The run-path ctx must (a) supply the opening balance from the
        config, and (b) carry the principal-sale declaration so the check
        exempts the disposition years rather than false-refusing a real home
        sale."""
        from trajectory_invariants import run_path_ctx

        # The wiring: a property-holding config exposes the opening balance
        # and principal_sale in its run-path ctx.
        cfg = SimulationConfig(projection_years=10, house_value=HOUSE_VALUE,
                                mortgage_balance=400_000, amortization_years=25,
                                start_year=2026)
        ctx = run_path_ctx(cfg)
        assert ctx['opening_mortgage_balance'] == 400_000
        assert ctx['principal_sale'] is None

        # A config with NO property declares no opening (DP#32 in reverse: a
        # house_value of 0 is "no property declared", never an opening of 0
        # that the check would invent a breach from).
        no_prop = SimulationConfig(projection_years=1, house_value=0,
                                    start_year=2026)
        assert run_path_ctx(no_prop)['opening_mortgage_balance'] is None

        # Discharge-awareness: a principal disposition is the ONE legitimate
        # way the ledger identity breaks. A household that sold its home in
        # year 2 must not be refused by this invariant on the post-sale years.
        sale_years = [YearResult(year=0, mortgage_balance=300_000,
                                  mortgage_principal=0),
                       YearResult(year=1, mortgage_balance=290_000,
                                  mortgage_principal=10_000),
                       # Sale year: balance force-zeroed (it was 290,000)
                       # while the amortization schedule still books 10,000
                       # of principal -- a naive ledger check reads this as
                       # 290,000 - 10,000 != 0.
                       YearResult(year=2, mortgage_balance=0,
                                  mortgage_principal=10_000)]
        ctx_sale = {'start_year': 2026,
                    'principal_sale': {'year': 2028},
                    'opening_mortgage_balance': 300_000.0}
        # Without the discharge awareness the post-sale year 2 (balance 0
        # with 10k of schedule principal still booked) is a false breach.
        naive = run_invariant('mortgage_conserves_principal', sale_years,
                              {'start_year': 2026,
                               'opening_mortgage_balance': 300_000.0})
        assert len(naive) == 1
        # With the discharge declaration, the sale year and after are exempt.
        aware = run_invariant('mortgage_conserves_principal', sale_years,
                               ctx_sale)
        assert aware == []

    def test_no_property_means_no_charge_to_breach(self):
        """DP#32 in reverse: house_value == 0 means "no property declared",
        NOT "a $0 charge limit that every dollar violates". Inventing a
        constraint out of missing data would refuse legitimate runs."""
        cfg = SimulationConfig(projection_years=1, house_value=0, start_year=2026)
        results = [YearResult(year=0, mortgage_balance=500_000, heloc_balance=500_000)]
        assert_run_invariants(results, cfg)  # must not raise
        assert run_invariant('total_secured_debt_within_charge_limit', results,
                              {'house_value': 0}) == []


# ============================================================================
# 3. A breach is LOUD, and when ranked it is SURFACED -- not hidden at -inf
#    (the #657 half of issue #681's point 3).
# ============================================================================

class TestBreachIsLoudAndSurfaced:
    def test_infeasible_scenario_carries_a_reason_not_a_bare_minus_inf(self):
        """#657/#681: a scenario the engine REFUSED is infeasible --
        structurally impossible, not merely unattractive. Collapsing it to a
        bare ``-inf`` puts it at the bottom of the ranked table looking
        exactly like a bad-but-legal choice. It must say why."""
        cfg = SimulationConfig(
            projection_years=5, house_value=HOUSE_VALUE, mortgage_balance=100_000,
            margin_available=100_000, mortgage_rate=0.05, heloc_rate=0.055,
            amortization_years=25,
            # DP#32/#655: no refinance amortization declared -> every LTV > 0
            # candidate is refused, not silently re-amortized.
            refinance_amortization_years=None,
            start_year=2026, investment_return=0.06, savings_rate=0.10,
            family_members=[{'role': 'primary', 'birth_year': 1980,
                              'gross_income': 150_000, 'retirement_age': 65,
                              'rrsp_room_accumulated': 0,
                              'tfsa_room_accumulated': 20_000}],
        )
        ranked = GridOptimizer(cfg).optimize(
            strategies=[STRATEGY_BALANCED], objective=MAX_NET_BENEFIT,
            income_overrides=[None], ltv_levels=[0.0, 0.70],
            use_readvanceable_options=[False], deduct_later_options=[False],
        )
        refused = [r for r in ranked if r.config_overrides.get('ltv') == 0.70]
        assert refused, "expected the LTV=70% candidates to be evaluated"
        for r in refused:
            assert r.is_infeasible, (
                "an LTV the engine refused must be marked infeasible, not "
                "silently ranked")
            assert r.score == float('-inf')
            assert 'MissingRefinanceAmortizationError' in r.infeasible_reason

        feasible = [r for r in ranked if r.config_overrides.get('ltv') == 0.0]
        for r in feasible:
            assert not r.is_infeasible
            assert r.infeasible_reason is None
