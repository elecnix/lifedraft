#!/usr/bin/env python3
"""Cash-flow solvency: the identity, the forced-liquidation waterfall, and a
golden ruin scenario (issue #679).

## The bug this closes

The simulator applied ``savings_rate`` to GROSS income and booked the
resulting contributions unconditionally -- it never asked whether the money
existed after debt service and living costs. A household could be cash-flow
insolvent for a decade while the engine kept recording new investment
contributions, and reported the resulting terminal wealth as though it were
achievable. Measured on a real household: a job-loss scenario reported a
multi-million-dollar terminal net benefit for a household that would have
had roughly $46/month left after the mortgage, property tax, insurance,
utilities and groceries -- before cars, fuel, phones, clothing, or
children -- with EI running out after 45 weeks. Hard deficit from month
one; the engine never noticed (there was no solvency model anywhere in the
codebase -- grep for insolven/bankrupt/ruin/forced_liquidat/shortfall
returned nothing outside the retirement drawdown).

## What's tested here (DP#4/DP#15: fabricated round numbers, role-based
## names -- no real household's figures appear anywhere in this file)

1. ``liquidation_waterfall.py`` in isolation (DP#11: unit tests verify each
   module) -- the pure fold, and each cost function's arithmetic.
2. DP#17 (both sides of every threshold) applied to the solvency identity
   itself: just-affordable (no liquidation), just-not-affordable (the
   waterfall closes a small gap), and full ruin (the waterfall is
   exhausted and money is still short).
3. A golden scenario whose income collapses in year 2 (an EI-only job-loss
   shock, held constant thereafter) and which must report ruin via
   ``YearResult.ruined`` for every year the shortfall survives the
   waterfall -- not a terminal figure presented as though it were
   achievable.
"""

import unittest

import countries.canada  # noqa: F401 -- registers the Canada jurisdiction providers

from liquidation_waterfall import (
    LiquidationSource, WaterfallResult, capital_gains_cost, identity_cost,
    months_covered, ordinary_income_cost, reserve_target, run_waterfall,
    summarize_solvency,
)
from simulation_config import SimulationConfig
from simulation_state import SimState, simulate_year_pure, adult_tfsa_slot  # #700
from trajectory_invariants import assert_invariant, run_invariant


def _mort_data(balance, payment=18_000, principal=6_000):
    """Fabricated fixed-payment mortgage data (DP#4/DP#15)."""
    return {
        'end_balance': max(0.0, balance - principal),
        'total_payment': payment,
        'total_interest': payment - principal,
        'total_principal': principal,
    }


def _base_config(**overrides):
    defaults = dict(
        projection_years=6,
        investment_return=0.05,
        mortgage_balance=300_000,
        mortgage_rate=0.05,
        margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 95_000, 'birth_year': 1985,
             'rrsp_room_accumulated': 40_000, 'tfsa_room_accumulated': 20_000},
        ],
        children=[],
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def _reserve_config(**overrides):
    """A config carrying a declared emergency-reserve POLICY (#688).

    ``held_in=None`` means the reserve is held outside every declared account
    (an ordinary savings balance) -- the isolation case used by most tests
    here, so the reserve's behaviour is not entangled with any host account's.
    Tests that care specifically about the carve-out set ``held_in``.
    """
    defaults = dict(
        emergency_reserve_target_months=6,
        emergency_reserve_rate=0.03,
        emergency_reserve_instrument='cash',
        emergency_reserve_held_in=None,
    )
    defaults.update(overrides)
    return _base_config(**defaults)


# ============================================================================
# 1. liquidation_waterfall.py in isolation (DP#11).
# ============================================================================

class TestLiquidationWaterfallPure(unittest.TestCase):

    def test_zero_shortfall_is_a_no_op(self):
        result = run_waterfall(0.0, [LiquidationSource('reserve', 10_000, identity_cost)])
        self.assertEqual(result.steps, ())
        self.assertEqual(result.covered, 0.0)
        self.assertFalse(result.ruined)

    def test_identity_cost_is_dollar_for_dollar(self):
        net, tax, gain = identity_cost(1_000.0)
        self.assertEqual((net, tax, gain), (1_000.0, 0.0, 0.0))

    def test_single_source_fully_covers_shortfall(self):
        result = run_waterfall(4_000.0, [LiquidationSource('reserve', 10_000, identity_cost)])
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].gross_drawn, 4_000.0)
        self.assertEqual(result.covered, 4_000.0)
        self.assertEqual(result.remaining_shortfall, 0.0)
        self.assertFalse(result.ruined)

    def test_order_is_the_caller_s_order_not_sorted_by_size(self):
        """The waterfall draws sources in the order supplied, not by size or
        cost -- the ORDER is the household's forced-liquidation order,
        decided by the caller (issue #679's whole point)."""
        result = run_waterfall(
            15_000.0,
            [
                LiquidationSource('small_first', 5_000, identity_cost),
                LiquidationSource('big_second', 100_000, identity_cost),
            ],
        )
        self.assertEqual([s.source for s in result.steps], ['small_first', 'big_second'])
        self.assertEqual(result.steps[0].gross_drawn, 5_000.0)
        self.assertEqual(result.steps[1].gross_drawn, 10_000.0)

    def test_exhausted_sources_report_ruin(self):
        result = run_waterfall(50_000.0, [LiquidationSource('reserve', 5_000, identity_cost)])
        self.assertTrue(result.ruined)
        self.assertAlmostEqual(result.covered, 5_000.0)
        self.assertAlmostEqual(result.remaining_shortfall, 45_000.0)

    def test_ordinary_income_cost_grosses_up_for_tax(self):
        """A fully-taxable source must draw MORE gross than the net amount
        needed, to cover the tax on the way out."""
        result = run_waterfall(
            7_000.0, [LiquidationSource('rrsp', 100_000, ordinary_income_cost(0.30))])
        step = result.steps[0]
        self.assertAlmostEqual(step.net_proceeds, 7_000.0, places=2)
        self.assertGreater(step.gross_drawn, 7_000.0)
        self.assertAlmostEqual(step.tax, step.gross_drawn - step.net_proceeds, places=2)
        self.assertAlmostEqual(step.tax, step.gross_drawn * 0.30, places=2)

    def test_capital_gains_cost_taxes_only_the_gain_fraction(self):
        """A non-reg account with a 50% unrealized gain, 50% inclusion,
        30% marginal rate: only 0.5 * 0.5 * 0.30 = 7.5% of the GROSS
        withdrawal goes to tax."""
        cost_fn = capital_gains_cost(gain_frac=0.5, inclusion_rate=0.5, marginal_rate=0.30)
        net, tax, gain = cost_fn(10_000.0)
        self.assertAlmostEqual(gain, 5_000.0)
        self.assertAlmostEqual(tax, 10_000.0 * 0.5 * 0.5 * 0.30)
        self.assertAlmostEqual(net, 10_000.0 - tax)

    def test_capital_gains_cost_reports_a_loss_honestly_and_charges_no_tax(self):
        """DP#679: a forced sale while the account sits BELOW cost basis
        must report a genuine negative realized_gain, not a floor at zero
        -- and a loss costs nothing in tax (this engine does not carry a
        capital-loss forward to shelter a future gain, see the module
        docstring)."""
        cost_fn = capital_gains_cost(gain_frac=-0.20, inclusion_rate=0.5, marginal_rate=0.30)
        net, tax, gain = cost_fn(10_000.0)
        self.assertAlmostEqual(gain, -2_000.0)
        self.assertEqual(tax, 0.0)
        self.assertEqual(net, 10_000.0)

    def test_multi_source_waterfall_matches_issue_679_order(self):
        """The exact order issue #679 specifies: emergency reserve ->
        revolving credit -> non-reg (capital gains) -> TFSA -> registered
        (fully taxable)."""
        sources = [
            LiquidationSource('emergency_reserve', 2_000, identity_cost),
            LiquidationSource('revolving_credit', 3_000, identity_cost),
            LiquidationSource('non_reg', 4_000, capital_gains_cost(0.25, 0.5, 0.30)),
            LiquidationSource('tfsa', 1_000, identity_cost),
            LiquidationSource('registered', 50_000, ordinary_income_cost(0.30)),
        ]
        result = run_waterfall(9_900.0, sources)
        touched = [s.source for s in result.steps]
        self.assertEqual(touched, ['emergency_reserve', 'revolving_credit', 'non_reg', 'tfsa', 'registered'])
        self.assertFalse(result.ruined)
        self.assertAlmostEqual(result.covered, 9_900.0, places=2)

    def test_negligible_source_is_skipped_not_recorded_as_a_zero_draw(self):
        """A source whose balance has been drawn down to a sub-cent DUST
        residual -- e.g. an emergency reserve all but exhausted across several
        shortfall years -- must be skipped by the negligible-draw guard, not
        appended as a phantom ~$0 liquidation step, and the waterfall must
        keep going (``continue``, not ``break``) to the next real source. A
        zero-dollar 'forced sale' in the report is exactly the kind of noise
        #679 exists to keep out of the household's forced-liquidation list."""
        sources = [
            LiquidationSource('emergency_reserve', 5e-10, identity_cost),  # dust
            LiquidationSource('non_reg', 100_000, identity_cost),
        ]
        result = run_waterfall(4_000.0, sources)
        # the dust reserve produced no step; the real source covered it all
        self.assertEqual([s.source for s in result.steps], ['non_reg'])
        self.assertAlmostEqual(result.covered, 4_000.0, places=2)
        self.assertFalse(result.ruined)


# ============================================================================
# 2. DP#17: both sides of the solvency threshold, via simulate_year_pure.
# ============================================================================

class TestSolvencyThresholdBothSides(unittest.TestCase):
    """Same household shape throughout; only living_costs/after_tax_income
    move across the threshold, isolating the identity itself."""

    def _run(self, *, living_costs, after_tax_income, reserve=0.0, margin_available=0,
              non_reg=0.0, non_reg_acb=0.0, tfsa=0.0, rrsp=0.0):
        # reserve_rate=0.0 throughout this class: an explicit, representable
        # zero (a chequing account that pays nothing), chosen so the reserve's
        # OWN growth does not move the threshold arithmetic these tests are
        # isolating. #688's growth behaviour is tested on its own, below.
        cfg = (_reserve_config(margin_available=margin_available,
                                emergency_reserve_rate=0.0)
               if reserve else _base_config(margin_available=margin_available))
        canada = {}
        if rrsp:
            # #700: per-adult RRSP store — one synthetic primary adult
            canada['adult_rrsp'] = {'primary': {'own': rrsp, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}
        if tfsa:
            # #700: per-adult TFSA store — one synthetic primary adult
            canada['adult_tfsa'] = {'primary': {'balance': tfsa, 'room': 0.0}}
        state = SimState(
            emergency_reserve_balance=reserve,
            non_reg_balance=non_reg,
            non_reg_acb=non_reg_acb,
            jurisdiction_state=({'canada': canada} if canada else {}),
        )
        return simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=0.0, primary_marginal_rate=0.30,
            mortgage_data=_mort_data(cfg.mortgage_balance),
            living_costs=living_costs, after_tax_income=after_tax_income,
        )

    def test_module_off_when_living_costs_not_supplied(self):
        """DP#16: living_costs<=0 means the household's own budget was
        never supplied -- the check does not run at all, regardless of how
        large a gap there would otherwise be."""
        result, _ = self._run(living_costs=0.0, after_tax_income=1.0)
        self.assertEqual(result.solvency_shortfall, 0.0)
        self.assertEqual(result.forced_liquidation_events, [])
        self.assertFalse(result.ruined)

    def test_just_affordable_no_liquidation_fires(self):
        """available == required + $1 of slack: solvent, nothing drawn."""
        result, new_state = self._run(
            living_costs=50_000, after_tax_income=50_000 + 18_000 + 1, reserve=5_000)
        self.assertEqual(result.solvency_shortfall, 0.0)
        self.assertEqual(result.forced_liquidation_events, [])
        self.assertFalse(result.ruined)
        self.assertEqual(new_state.emergency_reserve_balance, 5_000)

    def test_just_not_affordable_waterfall_covers_it(self):
        """available == required - $1: a one-dollar shortfall, covered
        entirely (and cheaply) by the emergency reserve -- not ruin."""
        result, new_state = self._run(
            living_costs=50_000, after_tax_income=50_000 + 18_000 - 1, reserve=5_000)
        self.assertAlmostEqual(result.solvency_shortfall, 1.0, places=2)
        self.assertAlmostEqual(result.solvency_covered, 1.0, places=2)
        self.assertFalse(result.ruined)
        self.assertEqual(len(result.forced_liquidation_events), 1)
        self.assertEqual(result.forced_liquidation_events[0]['source'], 'emergency_reserve')
        self.assertAlmostEqual(new_state.emergency_reserve_balance, 4_999.0, places=2)

    def test_full_ruin_when_every_source_is_exhausted(self):
        """A large, sustained shortfall with only a small reserve and no
        other assets: the waterfall cannot close the gap -- ruin."""
        result, new_state = self._run(
            living_costs=54_000, after_tax_income=28_000, reserve=2_000)
        required = 54_000 + 18_000  # living costs + mortgage payment, zero contributions
        expected_shortfall = required - 28_000
        self.assertAlmostEqual(result.solvency_shortfall, expected_shortfall, places=2)
        self.assertAlmostEqual(result.solvency_covered, 2_000.0, places=2)
        self.assertTrue(result.ruined)
        self.assertEqual(new_state.emergency_reserve_balance, 0.0)

    def test_waterfall_order_touches_every_pool_in_the_issue_679_order(self):
        result, _ = self._run(
            living_costs=54_000, after_tax_income=28_000,
            reserve=2_000, margin_available=3_000,
            non_reg=4_000, non_reg_acb=4_000, tfsa=1_000, rrsp=50_000)
        touched = [e['source'] for e in result.forced_liquidation_events]
        # `revolving_credit` is ABSENT, not out of order: the step exists and
        # is drawn in the right place, but it can only ever find $0 until a
        # revolving credit facility can be declared at all (#689 -- see
        # TestCreditFacilityGapIsDisclosed). Every other pool is drawn in
        # exactly #679's order.
        self.assertEqual(touched, ['emergency_reserve', 'non_reg', 'tfsa', 'registered'])
        self.assertTrue(result.credit_facility_unrepresentable)
        # Non-reg was drawn at cost basis (no gain) -- no tax on that leg.
        non_reg_event = next(e for e in result.forced_liquidation_events if e['source'] == 'non_reg')
        self.assertAlmostEqual(non_reg_event['tax'], 0.0, places=2)
        # Registered (RRSP) IS fully taxable -- some tax on that leg.
        reg_event = next(e for e in result.forced_liquidation_events if e['source'] == 'registered')
        self.assertGreater(reg_event['tax'], 0.0)

    def test_realized_loss_reported_honestly(self):
        """A forced sale of a non-reg position sitting BELOW cost basis
        (a market-crash-correlated scenario) must show up as a genuine
        loss on the report, not a floored zero."""
        result, _ = self._run(
            living_costs=54_000, after_tax_income=28_000,
            reserve=0.0, non_reg=10_000, non_reg_acb=15_000)
        self.assertLess(result.forced_liquidation_realized_loss, 0.0)
        non_reg_event = next(e for e in result.forced_liquidation_events if e['source'] == 'non_reg')
        self.assertLess(non_reg_event['realized_gain'], 0.0)
        self.assertEqual(non_reg_event['tax'], 0.0)  # a loss triggers no tax


# ============================================================================
# 3. The golden ruin scenario: income collapses in year 2 (job loss, held
# at EI level thereafter), and the household must report ruin.
# ============================================================================

RUIN_LIVING_COSTS = 54_000
RUIN_MORTGAGE_PAYMENT = 18_000
RUIN_AFTER_TAX_WORKING = 90_000    # comfortable, pre-collapse
RUIN_AFTER_TAX_EI = 28_000         # after job loss -- EI-level, held flat
RUIN_RRSP_CONTRIBUTION = 8_000     # the "committed savings" the pre-#679
                                    # engine kept booking regardless of
                                    # affordability


def _run_income_collapse_trajectory(collapse_year: int, n_years: int = 4,
                                     reserve: float = 10_000):
    """Fold ``simulate_year_pure`` by hand across ``n_years``: comfortable
    income through ``collapse_year - 1``, EI-level income from
    ``collapse_year`` onward. A small starting cushion (fabricated, round
    numbers -- DP#4/DP#15), same shape as a real household's: a modest cash
    reserve and a little non-reg/TFSA/RRSP.

    ``margin_available`` is deliberately 0. The waterfall's revolving-credit
    step is structurally empty until #689 (see apply_solvency's docstring);
    a HELOC is a *secured* product and is not a substitute for the unsecured
    line of credit that step is about.
    """
    cfg = _reserve_config(emergency_reserve_rate=0.0)
    state = SimState(
        emergency_reserve_balance=reserve,
        mortgage_balance=cfg.mortgage_balance,
        non_reg_balance=5_000, non_reg_acb=5_000,
        jurisdiction_state={'canada': {  # #700: per-adult stores
            'adult_tfsa': {'primary': {'balance': 3_000, 'room': 0.0}},
            'adult_rrsp': {'primary': {'own': 2_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}}},
    )
    results = []
    for year in range(n_years):
        working = year < collapse_year
        after_tax_income = RUIN_AFTER_TAX_WORKING if working else RUIN_AFTER_TAX_EI
        allocations = {
            '_primary_income': 95_000, '_annual_savings': RUIN_RRSP_CONTRIBUTION,
            'primary_rrsp': RUIN_RRSP_CONTRIBUTION,
        }
        mort = _mort_data(state.mortgage_balance, payment=RUIN_MORTGAGE_PAYMENT)
        result, state = simulate_year_pure(
            state=state, year=year,
            allocations=allocations, config=cfg, investment_return=0.05,
            primary_marginal_rate=0.30, mortgage_data=mort,
            living_costs=RUIN_LIVING_COSTS, after_tax_income=after_tax_income,
        )
        results.append(result)
    return results


class TestGoldenRuinScenario(unittest.TestCase):
    """The scenario decisions.income[] exists to answer: 'I am taking on a
    large mortgage on the strength of a new job. What happens if I lose
    it?' -- a question about ruin in year one or two, not terminal net
    worth decades out."""

    def test_pre_collapse_years_are_solvent(self):
        results = _run_income_collapse_trajectory(collapse_year=2)
        self.assertFalse(results[0].ruined)
        self.assertFalse(results[1].ruined)
        self.assertEqual(results[0].solvency_shortfall, 0.0)
        self.assertEqual(results[1].solvency_shortfall, 0.0)

    def test_income_collapse_triggers_ruin(self):
        """Job loss in year 2 (0-based index 2): required outflows
        (mortgage + living costs + the still-being-booked RRSP
        contribution) blow straight through EI-level after-tax income, and
        the household's cushion (reserve + undrawn HELOC + small non-reg/
        TFSA/RRSP) is nowhere near enough to cover it. Ruin -- not a large
        reassuring terminal number reached as though this were survivable."""
        results = _run_income_collapse_trajectory(collapse_year=2, n_years=4)
        self.assertTrue(results[2].ruined,
                         "the collapse year itself must report ruin")
        self.assertTrue(results[3].ruined,
                         "ruin persists into the following year -- EI-level "
                         "income does not recover, and the accounts the "
                         "waterfall already drained in year 2 are not there "
                         "again in year 3")
        # The engine must not pretend the pre-existing $8,000/year RRSP
        # commitment was still affordable once income collapsed.
        for r in results[2:]:
            self.assertGreater(r.solvency_shortfall, 0.0)

    def test_terminal_total_assets_is_not_presented_as_achievable_without_checking_ruined(self):
        """This is the reporting failure issue #679 is actually about: a
        caller that reads ONLY the terminal total_assets (or a net-benefit
        figure derived from it) without checking `.ruined` gets a number
        that looks like ordinary investment growth. `.ruined` is what
        makes that number refusable -- any downstream reporting/objective
        layer MUST check it before quoting a terminal figure as achievable
        (see this PR's notes on scope: wiring objective.py's own terminal
        report to this flag is out of this file's territory, but the flag
        it must check is proven correct and present, here)."""
        results = _run_income_collapse_trajectory(collapse_year=2, n_years=4)
        self.assertTrue(any(r.ruined for r in results))
        # A caller CAN discover this from the trajectory alone (the whole
        # point) -- proven directly against the mechanism from #581/#679:
        run_invariant('ruin_reported_when_shortfall_survives_waterfall', results,
                       {'expect_ruin': True})  # must not raise / must find it
        assert_invariant('ruin_reported_when_shortfall_survives_waterfall', results,
                          {'expect_ruin': True})

    def test_cash_flow_identity_holds_every_year(self):
        """The trajectory invariant (#581-style, checked every year): the
        waterfall's own reported numbers are internally consistent for the
        whole ruin trajectory, both before and after the collapse."""
        results = _run_income_collapse_trajectory(collapse_year=2, n_years=4)
        assert_invariant('cash_flow_solvency_identity', results, {})

    def test_no_ruin_if_the_job_never_collapses(self):
        """DP#17's other side at the trajectory level: the identical
        household, identical costs, identical starting cushion -- but the
        job never disappears. Never ruined."""
        results = _run_income_collapse_trajectory(collapse_year=99, n_years=4)
        self.assertFalse(any(r.ruined for r in results))
        assert_invariant('cash_flow_solvency_identity', results, {})


# ============================================================================
# 4. The emergency reserve (issue #688): its size, its LOCATION, and that it
# is held in CASH -- the single most important risk decision a leveraged
# household makes, and one the contract could not express at all.
# ============================================================================

class TestReserveSizingPure(unittest.TestCase):
    """The pure arithmetic, in isolation (DP#11)."""

    def test_target_months_times_essential_outflows(self):
        # 6 months of ($54,000 living + $18,000 debt service) = half of $72,000
        self.assertAlmostEqual(
            reserve_target(6, annual_living_costs=54_000, annual_debt_service=18_000),
            36_000.0)

    def test_no_declared_reserve_is_a_hard_zero(self):
        """DP#32: a household that declared no reserve holds ZERO -- never a
        comfortable cushion assumed into existence."""
        self.assertEqual(
            reserve_target(None, annual_living_costs=54_000, annual_debt_service=18_000),
            0.0)

    def test_declared_zero_target_is_also_zero(self):
        """`target_months: 0` is a real, declarable decision ('I hold no
        reserve'). It yields the same $0 as absence -- but the CONFIG keeps
        the two distinguishable (None vs 0.0), which is what lets the report
        say which one this household actually did."""
        self.assertEqual(
            reserve_target(0, annual_living_costs=54_000, annual_debt_service=18_000),
            0.0)

    def test_debt_service_counts_toward_essential_outflows(self):
        """The mortgage does not stop when the job does -- so a reserve sized
        against living costs ALONE is systematically too small, in exactly the
        scenario it exists for."""
        with_debt = reserve_target(12, annual_living_costs=54_000, annual_debt_service=18_000)
        without_debt = reserve_target(12, annual_living_costs=54_000, annual_debt_service=0)
        self.assertGreater(with_debt, without_debt)

    def test_months_covered_is_the_number_the_household_needs_to_hear(self):
        # $18,000 against $72,000/yr of essential outflows = 3 months.
        self.assertAlmostEqual(
            months_covered(18_000, annual_living_costs=54_000, annual_debt_service=18_000),
            3.0)


class TestReserveGrowsAtItsOwnRate(unittest.TestCase):
    """#688's first enforcement requirement: 'A reserve declared but modelled
    at the portfolio return is a bug: assert the reserve's growth uses its
    declared instrument.'"""

    def _grow_one_year(self, *, reserve_rate, investment_return):
        cfg = _reserve_config(emergency_reserve_rate=reserve_rate)
        state = SimState(emergency_reserve_balance=10_000)
        _, new_state = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=investment_return,
            primary_marginal_rate=0.30,
            mortgage_data=_mort_data(cfg.mortgage_balance),
            # Comfortably solvent -- nothing is drawn, so the ONLY thing that
            # moves the reserve this year is its own growth.
            living_costs=20_000, after_tax_income=200_000,
        )
        return new_state.emergency_reserve_balance

    def test_reserve_compounds_at_its_declared_cash_rate(self):
        self.assertAlmostEqual(
            self._grow_one_year(reserve_rate=0.03, investment_return=0.07),
            10_000 * 1.03, places=2)

    def test_reserve_does_not_compound_at_the_portfolio_return(self):
        """A reserve compounding at 7% is not a reserve. Same portfolio
        return, two different declared cash rates -> two different reserves,
        and NEITHER equals the portfolio's."""
        at_3pct = self._grow_one_year(reserve_rate=0.03, investment_return=0.07)
        at_1pct = self._grow_one_year(reserve_rate=0.01, investment_return=0.07)
        self.assertNotAlmostEqual(at_3pct, 10_000 * 1.07, places=2)
        self.assertNotAlmostEqual(at_1pct, 10_000 * 1.07, places=2)
        self.assertGreater(at_3pct, at_1pct)

    def test_a_zero_rate_is_honoured_not_treated_as_unset(self):
        """DP#32: a chequing account paying nothing is a real, representable
        answer -- 0 must not be quietly upgraded to some assumed cash yield."""
        self.assertAlmostEqual(
            self._grow_one_year(reserve_rate=0.0, investment_return=0.07),
            10_000.0, places=2)

    def test_a_reserve_with_no_declared_rate_fails_loudly(self):
        """DP#32: no rate, no guess. A balance the engine cannot honestly
        compound must crash, not compound at a number nobody supplied."""
        cfg = _base_config()  # no reserve policy at all
        state = SimState(emergency_reserve_balance=10_000)
        with self.assertRaises(ValueError) as ctx:
            simulate_year_pure(
                state=state, year=0,
                allocations={'_primary_income': 95_000, '_annual_savings': 0},
                config=cfg, investment_return=0.07, primary_marginal_rate=0.30,
                mortgage_data=_mort_data(cfg.mortgage_balance))
        self.assertIn('emergency_reserve_rate', str(ctx.exception))


class TestReserveIsCarvedOutNotAddedOn(unittest.TestCase):
    """Money conservation (DP#18): the reserve is a cash SLEEVE inside the
    account it is held in, not new money layered on top of it. Declaring a
    reserve must not make the household richer."""

    def _config(self, **kw):
        return _reserve_config(
            mortgage_balance=0,   # isolate: essential outflows = living costs only
            living_costs=60_000,
            emergency_reserve_target_months=6,   # -> $30,000 target
            **kw)

    def test_reserve_held_in_tfsa_is_carved_out_of_the_tfsa(self):
        cfg = self._config(emergency_reserve_held_in='tfsa')
        cfg.family_members[0]['tfsa_balance'] = 50_000
        state = SimState.initial(cfg)
        self.assertAlmostEqual(state.emergency_reserve_balance, 30_000.0)
        canada = state.jurisdiction_state['canada']
        self.assertAlmostEqual(adult_tfsa_slot(canada, 0)[0], 20_000.0)  # #700

    def test_total_assets_is_unchanged_by_declaring_a_reserve(self):
        """The whole point: a reserve does not create money. It moves money
        from 'invested at the portfolio return' to 'parked in cash' -- and
        THAT is the trade the sweep prices."""
        with_reserve = self._config(emergency_reserve_held_in='tfsa')
        with_reserve.family_members[0]['tfsa_balance'] = 50_000
        without_reserve = _base_config(mortgage_balance=0, living_costs=60_000)
        without_reserve.family_members[0]['tfsa_balance'] = 50_000

        self.assertAlmostEqual(
            SimState.initial(with_reserve).total_assets(),
            SimState.initial(without_reserve).total_assets(),
            places=2)

    def test_reserve_cannot_exceed_the_account_it_is_held_in(self):
        """A household cannot hold more cash in an account than the account
        contains. Falling short of the declared target is a real fact to
        REPORT ('you are N months short'), not an error."""
        cfg = self._config(emergency_reserve_held_in='tfsa')
        cfg.family_members[0]['tfsa_balance'] = 12_000   # target is 30,000
        state = SimState.initial(cfg)
        self.assertAlmostEqual(state.emergency_reserve_balance, 12_000.0)
        self.assertAlmostEqual(adult_tfsa_slot(state.jurisdiction_state['canada'], 0)[0], 0.0)  # #700

    def test_an_empty_host_account_means_a_zero_reserve_not_a_crash(self):
        """DP#32: zero is a VALUE, not an error.

        An empty host account is not a different kind of thing from a
        half-full one -- it is the extreme of the same fact: the household
        holds a $0 reserve and is short by its entire declared target. An
        earlier draft raised here, which was self-contradictory (a $1 balance
        clamped and reported; a $0 balance crashed) and actively dangerous:
        inside the optimizer, #657's `except Exception: score = -inf` swallows
        the crash and ranks the strategy last -- indistinguishable from a
        merely bad one. It also crashed the VOI sweep, which legitimately
        perturbs a balance to zero.
        """
        cfg = self._config(emergency_reserve_held_in='tfsa')
        cfg.family_members[0]['tfsa_balance'] = 0   # nothing there to hold it in
        state = SimState.initial(cfg)
        self.assertEqual(state.emergency_reserve_balance, 0.0)

    def test_reserve_held_in_an_account_the_engine_cannot_resolve_fails_loudly(self):
        """The other side: a held_in naming an account kind the engine has no
        balance for at all (a typo, an unsupported kind) is an UNANSWERABLE
        question, not a zero -- and it must still crash."""
        cfg = self._config(emergency_reserve_held_in='not_a_real_account_kind')
        with self.assertRaises(ValueError) as ctx:
            SimState.initial(cfg)
        self.assertIn('held_in', str(ctx.exception))

    def test_held_in_null_means_held_outside_every_declared_account(self):
        """An ordinary savings balance the contract does not otherwise model:
        nothing to carve out of, so nothing is."""
        cfg = self._config(emergency_reserve_held_in=None)
        cfg.family_members[0]['tfsa_balance'] = 50_000
        state = SimState.initial(cfg)
        self.assertAlmostEqual(state.emergency_reserve_balance, 30_000.0)
        self.assertAlmostEqual(
            adult_tfsa_slot(state.jurisdiction_state['canada'], 0)[0], 50_000.0)  # #700


class TestReserveIsDrawnFirst(unittest.TestCase):
    """#688's second enforcement requirement: 'A contract declaring
    emergency_reserve must have it drawn FIRST in the #679 waterfall --
    assert the ordering behaviourally, not by inspection.'"""

    def test_reserve_is_the_first_source_drawn(self):
        cfg = _reserve_config(emergency_reserve_rate=0.0)
        state = SimState(
            emergency_reserve_balance=5_000,
            non_reg_balance=100_000, non_reg_acb=100_000,
            jurisdiction_state={'canada': {  # #700: per-adult stores
                'adult_tfsa': {'primary': {'balance': 50_000, 'room': 0.0}},
                'adult_rrsp': {'primary': {'own': 50_000, 'own_room': 0.0, 'spousal_as_annuitant': 0.0}}}},
        )
        result, _ = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=0.0, primary_marginal_rate=0.30,
            mortgage_data=_mort_data(cfg.mortgage_balance),
            living_costs=54_000, after_tax_income=28_000,
        )
        sources = [e['source'] for e in result.forced_liquidation_events]
        self.assertEqual(sources[0], 'emergency_reserve')
        # Drawn to nothing before anything taxable was touched.
        self.assertAlmostEqual(result.forced_liquidation_events[0]['gross_drawn'], 5_000.0)

    def test_a_bigger_reserve_prevents_a_forced_sale_that_a_smaller_one_does_not(self):
        """The behavioural proof that the reserve is doing its job: same
        household, same shock -- a reserve large enough to absorb the year's
        shortfall means NOTHING taxable is sold; a reserve too small means the
        non-reg account is liquidated. That difference is the product."""
        big, _ = self._shock(reserve=60_000)
        small, _ = self._shock(reserve=1_000)
        self.assertEqual([e['source'] for e in big.forced_liquidation_events],
                          ['emergency_reserve'])
        self.assertIn('non_reg', [e['source'] for e in small.forced_liquidation_events])

    def _shock(self, *, reserve):
        cfg = _reserve_config(emergency_reserve_rate=0.0)
        state = SimState(
            emergency_reserve_balance=reserve,
            non_reg_balance=100_000, non_reg_acb=60_000,   # 40% unrealized gain
            jurisdiction_state={'canada': {}},
        )
        return simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=0.0, primary_marginal_rate=0.30,
            mortgage_data=_mort_data(cfg.mortgage_balance),
            living_costs=54_000, after_tax_income=28_000,
        )

    def test_reserve_reports_months_covered_and_its_target(self):
        result, _ = self._shock(reserve=60_000)
        # Essential outflows: $54,000 living + $18,000 debt service =
        # $72,000/yr = $6,000/mo. The declared 6-month target is $36,000.
        self.assertAlmostEqual(result.emergency_reserve_target, 36_000.0, places=2)
        # This year's shortfall: $72,000 required - $28,000 after-tax income
        # (no contributions) = $44,000, absorbed entirely by the reserve.
        self.assertAlmostEqual(result.solvency_shortfall, 44_000.0, places=2)
        self.assertFalse(result.ruined)
        # $60,000 - $44,000 = $16,000 left = 2.67 months of runway. That
        # number -- "you have 2.7 months left, and you said you wanted 6" --
        # is the whole reason the field exists.
        self.assertAlmostEqual(result.emergency_reserve_months_covered,
                                (60_000 - 44_000) / 6_000, places=2)
        self.assertLess(result.emergency_reserve_months_covered, 6.0)


class TestReserveTargetIsSweepable(unittest.TestCase):
    """#688's third requirement: 'The optimizer must be able to SWEEP the
    target (0/3/6/12/24 months) so a household can see the real trade --
    expected terminal wealth against the probability and cost of a forced
    sale. That trade IS the product.'

    DP#18: proven on the ENGINE's OUTPUT, never on the merged config -- a
    sweep that writes to a key the engine does not read is a no-op sweep that
    reports the same number N times and calls it a sensitivity analysis
    (#591's exact failure)."""

    def _base_dict(self):
        cfg = _reserve_config(
            mortgage_balance=0, living_costs=60_000,
            emergency_reserve_held_in='tfsa',
            emergency_reserve_rate=0.005,   # cash: near-nothing
            emergency_reserve_target_months=0)
        cfg.family_members[0]['tfsa_balance'] = 120_000
        return cfg.to_dict()

    def _terminal_tfsa_plus_reserve(self, months):
        from scenario_overlay import ScenarioOverlay, apply_overlay
        cfg_dict = apply_overlay(
            self._base_dict(),
            ScenarioOverlay(label=f'{months}mo', emergency_reserve_months=months))
        cfg = SimulationConfig.from_dict(cfg_dict)
        state = SimState.initial(cfg)
        _, new_state = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=0.08, primary_marginal_rate=0.30,
            living_costs=60_000, after_tax_income=200_000)   # solvent: nothing drawn
        return new_state.total_assets()

    def test_sweeping_the_target_moves_the_engine_s_output(self):
        """More months parked in cash -> less compounding at the portfolio
        return -> strictly less terminal wealth. If the sweep landed on a dead
        key, all three of these would be identical."""
        wealth = {m: self._terminal_tfsa_plus_reserve(m) for m in (0, 6, 24)}
        self.assertGreater(wealth[0], wealth[6])
        self.assertGreater(wealth[6], wealth[24])

    def test_sweeping_a_household_with_no_reserve_policy_is_refused(self):
        """DP#32: a reserve swept against an INVENTED rate and location is not
        a sweep of the household's real decision. Refuse rather than fabricate
        the fields the sweep would need."""
        from scenario_overlay import ScenarioOverlay, apply_overlay
        base = _base_config(living_costs=60_000).to_dict()
        with self.assertRaises(ValueError) as ctx:
            apply_overlay(base, ScenarioOverlay(label='12mo', emergency_reserve_months=12))
        self.assertIn('emergency_reserve', str(ctx.exception))

    def test_the_overlay_round_trips(self):
        """DP#24."""
        from scenario_overlay import ScenarioOverlay
        overlay = ScenarioOverlay(label='12mo', emergency_reserve_months=12)
        self.assertEqual(
            ScenarioOverlay.from_dict(overlay.to_dict()).emergency_reserve_months, 12)


# ============================================================================
# 5. Issue #689: a revolving credit facility is UNREPRESENTABLE in the
# contract, so the waterfall's second step is structurally empty -- and the
# report must SAY SO rather than quietly reporting a pessimistic number as
# if it were the truth.
# ============================================================================

class TestCreditFacilityGapIsDisclosed(unittest.TestCase):

    def test_a_shortfall_year_discloses_that_the_credit_step_is_unrepresentable(self):
        results = _run_income_collapse_trajectory(collapse_year=2, n_years=4)
        shortfall_years = [r for r in results if r.solvency_shortfall > 0]
        self.assertTrue(shortfall_years)
        for r in shortfall_years:
            self.assertTrue(
                r.credit_facility_unrepresentable,
                "a household's real resilience is being UNDERSTATED (it may hold "
                "a line of credit the contract cannot express, #689) -- the report "
                "must say so, not present the pessimistic number as the truth")

    def test_a_solvent_year_makes_no_such_claim(self):
        """DP#17's other side: the disclosure is about a shortfall the
        waterfall had to fund. A solvent year had nothing to fund, so nothing
        was understated, and it must not cry wolf."""
        results = _run_income_collapse_trajectory(collapse_year=99, n_years=4)
        self.assertFalse(any(r.credit_facility_unrepresentable for r in results))

    def test_the_credit_step_finds_nothing_and_is_not_faked_with_the_heloc(self):
        """A HELOC is a SECURED product sharing the mortgage's registered
        charge (#664/#681). Quietly spending a household's investment-loan
        room as if it were an emergency line would model a different product
        than the one being asked about -- so the step draws $0, and the
        household's HELOC room is left untouched."""
        cfg = _reserve_config(margin_available=100_000, emergency_reserve_rate=0.0)
        state = SimState(emergency_reserve_balance=1_000, heloc_balance=0.0)
        result, new_state = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 0},
            config=cfg, investment_return=0.0, primary_marginal_rate=0.30,
            mortgage_data=_mort_data(cfg.mortgage_balance),
            living_costs=54_000, after_tax_income=28_000,
        )
        drawn = {e['source']: e['gross_drawn'] for e in result.forced_liquidation_events}
        self.assertNotIn('revolving_credit', drawn)
        self.assertEqual(new_state.heloc_balance, 0.0)
        self.assertTrue(result.ruined)   # nothing else to sell -- and it says so
        self.assertTrue(result.credit_facility_unrepresentable)


class TestBorrowedMoneyIsAnInflowNotJustAnOutflow(unittest.TestCase):
    """**A false ruin is the same defect as a false solvency, pointed the
    other way.**

    The year-0 leveraged lump sum (a HELOC margin draw + a mortgage cash-out
    -- the Smith Manoeuvre's whole mechanic) is allocated into RRSP/TFSA/
    non-reg exactly like a cash contribution, so it lands in the solvency
    identity's ``contributions`` term. But not one dollar of it came out of
    the household's pay: it came from the lender, and its financing is booked
    as DEBT and serviced with interest in every later year.

    Counting that outflow while ignoring the inflow that funded it invents a
    cash shortfall the household never had, forces a liquidation to cover it,
    and reports RUIN for precisely the leveraged strategies this repo exists
    to evaluate. Left unfixed it produces the mirror image of the original
    bug: a comfortably solvent, high-earning household told it is insolvent
    in year one and ruined decades later -- because the engine booked its
    borrowed investment as if the household had paid for it out of salary.
    """

    def _year_zero(self, *, borrowed_investment):
        cfg = _reserve_config(emergency_reserve_rate=0.0)
        state = SimState(
            emergency_reserve_balance=20_000,
            mortgage_balance=cfg.mortgage_balance,
            non_reg_balance=50_000, non_reg_acb=50_000,
        )
        # A big year-0 lump, invested -- exactly what fill_room() books when a
        # leveraged strategy deploys borrowed money.
        return simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 150_000, '_annual_savings': 200_000,
                          'non_reg': 200_000},
            config=cfg, investment_return=0.0, primary_marginal_rate=0.40,
            mortgage_data=_mort_data(cfg.mortgage_balance),
            living_costs=60_000, after_tax_income=100_000,
            borrowed_investment=borrowed_investment,
        )[0]

    def test_investing_borrowed_money_does_not_manufacture_a_shortfall(self):
        """The $200k contribution is funded by the $200k draw. Cash in = cash
        out. There is no shortfall, so there must be no forced liquidation."""
        result = self._year_zero(borrowed_investment=200_000)
        self.assertEqual(result.solvency_shortfall, 0.0)
        self.assertFalse(result.ruined)
        self.assertEqual(result.forced_liquidation_events, [])

    def test_the_same_contribution_funded_by_NOTHING_is_still_a_shortfall(self):
        """DP#17's other side, and the guard against 'fixing' the false ruin
        by simply switching the check off: an unfunded $200k contribution is a
        REAL shortfall and must still be caught. Only genuinely borrowed money
        counts as an inflow."""
        result = self._year_zero(borrowed_investment=0.0)
        self.assertGreater(result.solvency_shortfall, 0.0)
        self.assertTrue(result.forced_liquidation_events,
                        "a contribution nothing funded must still force a "
                        "liquidation -- that is the original #679 bug")

    def test_borrowed_money_does_not_pay_the_mortgage_or_the_groceries(self):
        """The inflow is bounded: it offsets the invested contribution, not the
        household's living costs and debt service. A household that borrows to
        invest has NOT thereby fed itself -- if income cannot cover the
        essentials, that is still a shortfall."""
        cfg = _reserve_config(emergency_reserve_rate=0.0)
        state = SimState(mortgage_balance=cfg.mortgage_balance,
                          non_reg_balance=50_000, non_reg_acb=50_000)
        result, _ = simulate_year_pure(
            state=state, year=0,
            allocations={'_primary_income': 95_000, '_annual_savings': 100_000,
                          'non_reg': 100_000},
            config=cfg, investment_return=0.0, primary_marginal_rate=0.30,
            mortgage_data=_mort_data(cfg.mortgage_balance),
            # EI-level income: cannot cover living costs + the mortgage, even
            # though the whole $100k contribution is borrowed.
            living_costs=54_000, after_tax_income=28_000,
            borrowed_investment=100_000,
        )
        self.assertGreater(
            result.solvency_shortfall, 0.0,
            "borrowing to INVEST does not feed the household -- the essentials "
            "still have to be paid out of income, and when they cannot be, that "
            "is a real shortfall")


# ═══════════════════════════════════════════════════════════════════════════
# THE REPORTING LAYER -- issue #679's actual complaint.
#
# A solvency model that never reaches the number the human reads fixes
# NOTHING. The original bug was not a miscomputation: it was that
# optimize.py printed "$4,431,353 of terminal net benefit" for a household
# that could not make its mortgage payment. Building the machinery without
# changing THAT number leaves the defect exactly where it was.
#
# These tests assert on the DATA the printer consumes, not on stdout, so
# they cannot be satisfied by a cosmetic string and cannot be broken by
# someone reformatting a table.
# ═══════════════════════════════════════════════════════════════════════════

class TestSolvencySummaryFold(unittest.TestCase):
    """``summarize_solvency`` -- the pure fold that turns a trajectory into
    the three facts the household actually asked for (DP#3)."""

    def test_an_unengaged_run_is_reported_as_UNCHECKED_not_as_safe(self):
        """DP#32, and the most dangerous falsehood this report could tell.
        A household that never declared its living costs has not been found
        solvent -- it has not been CHECKED. `engaged=False` is what forces
        the caller to say so instead of printing a reassuring '0 shortfalls'.
        """
        results = _run_income_collapse_trajectory(collapse_year=99, n_years=3)
        for r in results:
            r.living_costs = 0.0     # the module never ran: no budget was supplied
        summary = summarize_solvency(results)
        self.assertFalse(summary['engaged'])

    def test_a_ruined_trajectory_reports_ruin_and_the_year_it_runs_out(self):
        results = _run_income_collapse_trajectory(collapse_year=2, n_years=5)
        summary = summarize_solvency(results)
        self.assertTrue(summary['engaged'])
        self.assertTrue(summary['ruined'],
                        "the trajectory goes insolvent -- the summary the report "
                        "prints from MUST say so")
        self.assertIsNotNone(summary['first_shortfall_year'])
        self.assertIsNotNone(summary['first_ruin_year'])

    def test_it_reports_what_the_household_was_forced_to_sell(self):
        """'What it was forced to sell, and what that cost' -- issue #679's
        third required output. A ruin report that omits it is a verdict
        without evidence."""
        results = _run_income_collapse_trajectory(collapse_year=2, n_years=5)
        summary = summarize_solvency(results)
        self.assertTrue(summary['forced_liquidation_gross_by_source'],
                        "a household forced into a liquidation must be told WHICH "
                        "accounts were drained")
        self.assertTrue(summary['credit_facility_unrepresentable'],
                        "#689: every shortfall priced with an empty revolving-credit "
                        "step understates resilience -- the report must disclose it")

    def test_a_solvent_trajectory_reports_no_ruin(self):
        """DP#17's other side -- the summary must not cry wolf."""
        summary = summarize_solvency(
            _run_income_collapse_trajectory(collapse_year=99, n_years=4))
        self.assertFalse(summary['ruined'])
        self.assertIsNone(summary['first_ruin_year'])
        self.assertEqual(summary['forced_liquidation_gross_by_source'], {})


class TestRuinedScenarioCannotReportAnUnqualifiedTerminalFigure(unittest.TestCase):
    """**The regression guard for the original defect.**

    ``winners_by_income_scenario`` is the exact function behind the table
    that printed ``Job loss -- EI only | balanced | $4,431,353``. A ruined
    scenario appearing there with nothing but a big number is the bug. This
    asserts the row carries the verdict, so the printer CANNOT render it as
    an ordinary, achievable outcome.
    """

    def _row(self, sid, label, net_benefit, ruined, first_ruin_year=None):
        return {
            'income_scenario_id': sid, 'income_scenario_label': label,
            'strategy': 'balanced', 'net_benefit': net_benefit,
            'deduct_later': False,
            'solvency': {
                'engaged': True, 'ruined': ruined,
                'first_ruin_year': first_ruin_year,
                'first_shortfall_year': first_ruin_year,
                'shortfall_years': 3 if ruined else 0,
                'runway_months_at_start': 1.2 if ruined else 12.0,
                'forced_liquidation_gross_by_source': (
                    {'non_reg': 40_000} if ruined else {}),
                'forced_liquidation_tax': 6_000.0 if ruined else 0.0,
                'forced_liquidation_realized_loss': -9_000.0 if ruined else 0.0,
                'uncovered_shortfall': 25_000.0 if ruined else 0.0,
                'credit_facility_unrepresentable': ruined,
            },
        }

    def test_a_ruined_scenario_row_carries_the_ruin_verdict(self):
        import optimize
        winners = optimize.winners_by_income_scenario([
            self._row('stay', 'Stay at current job', 9_700_000, ruined=False),
            # A large, reassuring terminal figure for a household that is
            # cash-flow insolvent -- exactly the shape of the original bug.
            self._row('job_loss', 'Job loss -- EI only', 4_431_353,
                      ruined=True, first_ruin_year=2),
        ])
        stay, job_loss = winners
        self.assertFalse(stay['ruined'])
        self.assertTrue(
            job_loss['ruined'],
            "issue #679: a household that cannot make its mortgage payment must "
            "NOT be representable in the recommendation table as merely a "
            "smaller terminal number. The row must carry the ruin verdict, so "
            "no caller can print the figure as achievable.")
        self.assertEqual(job_loss['solvency']['first_ruin_year'], 2)

    def test_the_printed_report_never_shows_a_ruined_figure_as_a_plain_dollar_amount(self):
        """Stdout is not the contract -- but the ONE string that must never
        appear is a ruined scenario's terminal figure formatted as an
        ordinary dollar amount with no verdict attached. That is what the
        household misreads as 'job loss is survivable and merely expensive'.
        """
        import io, contextlib, optimize
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_income_scenario_report([
                self._row('stay', 'Stay at current job', 9_700_000, ruined=False),
                self._row('job_loss', 'Job loss -- EI only', 4_431_353,
                          ruined=True, first_ruin_year=2),
            ])
        out = buf.getvalue()

        self.assertIn('RUIN', out)
        self.assertIn('NOT ACHIEVABLE', out)
        # The SOLVENT scenario still reports its figure normally (the column is
        # right-aligned, hence the substring rather than an exact '$9,700,000').
        self.assertIn('9,700,000', out)

        ruin_line = next(l for l in out.splitlines()
                         if 'Job loss -- EI only' in l)
        self.assertNotIn('$4,431,353', ruin_line,
                         "the ruined scenario's terminal figure must never be "
                         "printed as a bare dollar amount on the recommendation "
                         "line -- that is the exact number issue #679 exists to kill")

        # And the three facts that ARE the answer to the job-loss question:
        self.assertIn('Runway', out)              # months of runway
        self.assertIn('1st shortfall', out)       # the year it runs out
        self.assertIn('FORCED TO SELL', out)      # what it had to liquidate
        self.assertIn('UNDERSTATED', out)         # #689 disclosed, not faked

    def test_an_unengaged_contract_is_reported_as_unchecked(self):
        """DP#32 at the reporting layer: no living-costs budget means the
        household was never CHECKED, and the report must not imply safety."""
        import io, contextlib, optimize
        rows = [self._row('stay', 'Stay', 9_700_000, ruined=False),
                self._row('job_loss', 'Job loss', 4_400_000, ruined=False)]
        for r in rows:
            r['solvency']['engaged'] = False
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_income_scenario_report(rows)
        out = buf.getvalue()
        self.assertIn('SOLVENCY NOT CHECKED', out)
        self.assertIn('not been checked, not cleared', out)

    def test_an_unchecked_row_is_marked_inline_not_only_in_the_footer(self):
        """Issue #733: 'SOLVENCY NOT CHECKED' used to print 74 lines below
        the ranking row it qualifies, under a different heading, well after
        a household has already read the figure and drawn a conclusion. A
        row whose scenario was never solvency-checked must be unmissable
        AT THE ROW -- asserted on the RENDERED TEXT (the row line itself),
        never on the result object (which was already correct; only the
        rendering lost the information, per #733's own enforcement ask)."""
        import io, contextlib, optimize
        rows = [self._row('stay', 'Stay', 9_700_000, ruined=False),
                self._row('job_loss', 'Job loss -- EI only', 11_084_000, ruined=False)]
        for r in rows:
            r['solvency']['engaged'] = False
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_income_scenario_report(rows)
        out = buf.getvalue()

        unchecked_line = next(l for l in out.splitlines()
                               if 'Job loss -- EI only' in l)
        self.assertIn(
            'UNCHECKED', unchecked_line,
            "a scenario whose cash-flow identity was never checked (no "
            "household_budget.annual_living_costs declared) must say so "
            "ON THE RANKING ROW ITSELF, not only in a footer 74 lines "
            "below it (#733).")
        self.assertNotRegex(
            unchecked_line, r'\$11,084,000\s*$',
            "the unchecked figure must never render as a BARE dollar "
            "amount indistinguishable from a checked, achievable one -- "
            "that is exactly the defect #733 exists to close.")

    def test_a_checked_solvent_row_still_renders_a_plain_figure(self):
        """The flip side of the #733 fix (DP#17): a row whose scenario WAS
        actually solvency-checked and found solvent must NOT be marked
        UNCHECKED -- the marker is reserved for genuine absence of a check,
        never applied indiscriminately."""
        import io, contextlib, optimize
        rows = [self._row('stay', 'Stay', 9_700_000, ruined=False),
                self._row('salary_cut', 'Salary cut', 8_100_000, ruined=False)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            optimize._print_income_scenario_report(rows)
        out = buf.getvalue()
        self.assertNotIn('UNCHECKED', out)
        stay_line = next(l for l in out.splitlines() if 'Stay' in l and 'Salary' not in l)
        self.assertIn('9,700,000', stay_line)


if __name__ == '__main__':
    unittest.main()
