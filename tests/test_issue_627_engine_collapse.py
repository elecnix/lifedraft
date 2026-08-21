"""Tests for issue #627: the optimizer engine and the simulation engine must
be ONE implementation of "the year step," not two.

## The bug this closes

`Optimizer._run_simulation` (Grid/Scipy/Monte-Carlo optimizers) and
`DPOptimizer`'s own call sites used to build a thinner ``allocations`` dict
by hand and call ``simulate_year_pure`` directly, never passing retirement
transition, RESP grants, FHSA room, portfolio composition, cash-flow events,
year-versioned contribution limits, or the configured HELOC rate path --
and read the WRONG jurisdiction-state key for the spouse's RRSP room
(`spousal_rrsp_room` instead of `spouse_rrsp_room`, silently absorbed by
`.get(key, 0)`). None of it crashed. It just ranked strategies for a
household that never retires, never gets a CESG match, never opens an
FHSA, and always has $0 of spousal RRSP room -- and that ranking is the
number the user actually reads (#603/#625/#627).

The fix collapses both engines onto one constructor
(`simulation_state.initial_state_for_run`, #583) and one year-step
(`simulation.simulate_year`, over a `SimulationContext` both
`FamilySimulation._build_context()` and `Optimizer._build_context()`
build the same way). This file has two kinds of tests, deliberately kept
separate:

1. **Architecture tests** -- assert the two engines share the same
   function objects and that neither optimizer module still calls
   `simulate_year_pure` directly. These forbid a *second implementation*
   of the year step from being reintroduced.

2. **Rule-pinned tests** -- assert facts about the optimizer path's own
   output that follow directly from a rule (retirement age arithmetic,
   RRSP room mechanics, the CESG match, the configured HELOC rate), NOT
   by comparing the optimizer's output to `FamilySimulation`'s. Per the
   #619 postmortem (`test_apply_ltv_overlay_matches_grid_optimizer`
   pinned two independently-buggy copies to each other and they agreed,
   with each other and with nothing true) -- pinning two implementations
   together only proves anything once you already know one of them is
   the single source of truth. Since the collapse makes the optimizer
   call `simulate_year` directly, the "two implementations" comparison
   below (`TestCrossEngineByteIdentical`) is a regression guard on the
   collapse itself, not a correctness proof; the correctness proof is
   `TestOptimizerPathModelsTheRules`, which pins to the rules.
"""
import ast
import dataclasses
import inspect
import unittest

from simulation_config import SimulationConfig
from simulation import FamilySimulation, simulate_year as simulation_simulate_year
import optimizer as optimizer_module
import dp_optimizer as dp_optimizer_module
from optimizer import GridOptimizer
from strategy import list_strategies


_STRATEGIES = list_strategies()
STRATEGY_RRSP_MAX = _STRATEGIES['rrsp_max']
STRATEGY_BALANCED = _STRATEGIES['balanced']


def _source_calls_simulate_year_pure_directly(module) -> bool:
    """AST check: does `module` contain a call to the bare name
    `simulate_year_pure`? (Attribute calls like `simulation_state.
    simulate_year_pure(...)` would also be caught if ever written that way
    -- both engines should only reach it *through* `simulate_year`.)
    """
    source = inspect.getsource(module)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, 'id', None) or getattr(func, 'attr', None)
            if name == 'simulate_year_pure':
                return True
    return False


class TestOneYearStepImplementation(unittest.TestCase):
    """Architecture tests (#627): forbid a second implementation of the
    year step from being reintroduced on the optimizer path."""

    def test_optimizer_module_uses_the_same_simulate_year_function(self):
        self.assertIs(
            optimizer_module.simulate_year, simulation_simulate_year,
            "optimizer.py must fold simulation.simulate_year, not a copy of it")

    def test_dp_optimizer_module_uses_the_same_simulate_year_function(self):
        self.assertIs(
            dp_optimizer_module.simulate_year, simulation_simulate_year,
            "dp_optimizer.py must fold simulation.simulate_year, not a copy of it")

    def test_optimizer_source_never_calls_simulate_year_pure_directly(self):
        self.assertFalse(
            _source_calls_simulate_year_pure_directly(optimizer_module),
            "optimizer.py must reach simulate_year_pure only through "
            "simulation.simulate_year -- a direct call is exactly how a "
            "rule silently goes missing from the optimizer path again (#627)")

    def test_dp_optimizer_source_never_calls_simulate_year_pure_directly(self):
        self.assertFalse(
            _source_calls_simulate_year_pure_directly(dp_optimizer_module),
            "dp_optimizer.py must reach simulate_year_pure only through "
            "simulation.simulate_year -- a direct call is exactly how a "
            "rule silently goes missing from the optimizer path again (#627)")


# ============================================================================
# A fabricated, round-number household (DP#4/DP#15) whose horizon crosses
# both spouses' retirement ages, with RESP-eligible children, FHSA room,
# portfolio composition, and a cash-flow event -- so every one of the eight
# previously-missing rules has something to fire against.
# ============================================================================

START_YEAR = 2026
PRIMARY_BIRTH = 1970   # age 56 at start; retires 2035 (age 65)
SPOUSE_BIRTH = 1972    # age 54 at start; retires 2037 (age 65)
CHILD_BIRTH = 2015     # age 11 at start -- RESP-eligible for the whole horizon


def small_household_config(**overrides) -> dict:
    cfg = {
        'family': {
            'members': [
                {'role': 'primary', 'birth_year': PRIMARY_BIRTH, 'gross_income': 150_000,
                 'retirement_age': 65, 'rrsp_room_accumulated': 40_000,
                 'tfsa_room_accumulated': 20_000, 'fhsa_room_accumulated': 8_000},
                {'role': 'spouse', 'birth_year': SPOUSE_BIRTH, 'gross_income': 70_000,
                 'retirement_age': 65, 'rrsp_room_accumulated': 25_000,
                 'tfsa_room_accumulated': 20_000},
            ],
            'children': [{'name': 'child_a', 'birth_year': CHILD_BIRTH}],
        },
        'accounts': {'resp_current_balance': 5_000, 'rrsp_annual_max': 31_000},
        'assumptions': {
            'start_year': START_YEAR, 'horizon_age': 90,
            'investment_return': 0.06, 'salary_growth': 0.02, 'inflation': 0.02,
            'frozen_brackets': False,
        },
        'portfolio': {
            'accounts': {
                'non_reg': {
                    'balance': 0, 'cost_basis': 0,
                    'composition': {'cdn_equity_pct': 0.6, 'fixed_income_pct': 0.4},
                    'yield': {'eligible_dividends': 0.015, 'interest': 0.01},
                },
            },
        },
        'property': {
            'house_value': 800_000, 'mortgage_balance': 400_000, 'mortgage_rate': 0.05,
            'amortization_years': 25, 'margin_available': 100_000, 'ltv_max': 0.80,
            'heloc_readvance': True,
        },
        'savings': {'rate': 0.20},
        'retirement': {'spending_target': 70_000},
        'cash_flows': [{'year': START_YEAR + 3, 'amount': 60_000, 'tax_treatment': 'post-tax'}],
        'tax': {'province': 'qc'},
    }
    cfg.update(overrides)
    return SimulationConfig.from_dict(cfg)


class TestCrossEngineByteIdentical(unittest.TestCase):
    """Regression guard on the collapse itself (NOT a correctness proof --
    see module docstring). Since Optimizer._run_simulation now folds the
    exact same simulate_year function FamilySimulation.run() folds, running
    both with the same config/strategy/flags must produce byte-identical
    YearResult trajectories. If this test ever fails without a deliberate
    engine change, one of the two call sites has drifted back toward having
    its own copy of "the year step."
    """

    def _assert_identical(self, config, strategy, use_readvanceable, deduct_later, lump_sum=0.0):
        sim = FamilySimulation(config, strategy=strategy,
                                use_readvanceable=use_readvanceable,
                                deduct_later=deduct_later, lump_sum=lump_sum)
        sim_results = sim.run()

        opt = GridOptimizer(config)
        opt_results, _ = opt._run_simulation(
            config, strategy, use_readvanceable=use_readvanceable,
            deduct_later=deduct_later, lump_sum=lump_sum)

        self.assertEqual(len(sim_results), len(opt_results))
        for year, (a, b) in enumerate(zip(sim_results, opt_results)):
            self.assertEqual(
                dataclasses.asdict(a), dataclasses.asdict(b),
                f"year {year}: optimizer path diverged from simulation path")

    def test_no_lump_sum_smith_manoeuvre(self):
        config = small_household_config()
        self._assert_identical(config, STRATEGY_RRSP_MAX,
                                use_readvanceable=True, deduct_later=False)

    def test_no_lump_sum_baseline(self):
        config = small_household_config()
        self._assert_identical(config, STRATEGY_BALANCED,
                                use_readvanceable=False, deduct_later=True)

    def test_with_year0_lump_sum(self):
        config = small_household_config()
        self._assert_identical(config, STRATEGY_RRSP_MAX,
                                use_readvanceable=True, deduct_later=False,
                                lump_sum=150_000.0)


class TestOptimizerPathModelsTheRules(unittest.TestCase):
    """Rule-pinned tests (#627): assert facts about the optimizer path's OWN
    output, derived from the rule itself -- not by comparing to
    FamilySimulation. These are the tests that would have caught #625/#627
    even if FamilySimulation had never existed.
    """

    def _run(self, config, strategy=STRATEGY_RRSP_MAX, use_readvanceable=True,
              deduct_later=False, lump_sum=0.0):
        opt = GridOptimizer(config)
        results, final_state = opt._run_simulation(
            config, strategy, use_readvanceable=use_readvanceable,
            deduct_later=deduct_later, lump_sum=lump_sum)
        return results, final_state

    # ── item 1: retirement transition ──
    def test_employment_income_stops_at_retirement_age(self):
        config = small_household_config()
        results, _ = self._run(config)
        # Both spouses are retired well before the 30th projected year
        # (primary retires 2035 = year 9; spouse retires 2037 = year 11).
        last = results[-1]
        self.assertEqual(last.employment_income, 0.0,
                          "optimizer path must stop employment income once "
                          "every member has passed retirement_age")
        self.assertGreater(last.retirement_income, 0.0,
                            "optimizer path must model CPP/OAS/pension/drawdown "
                            "once the household has retired")

    def test_employment_income_nonzero_before_retirement(self):
        config = small_household_config()
        results, _ = self._run(config)
        self.assertGreater(results[0].employment_income, 0.0)
        self.assertEqual(results[0].retirement_income, 0.0)

    # ── item 4: spouse_rrsp_room (the typo) ──
    def test_spouse_personal_rrsp_receives_contributions_when_room_exists(self):
        """Before the fix, FamilyState.spouse_rrsp_room was read from the
        wrong jurisdiction-state key and was ALWAYS 0 on the optimizer path
        -- strategy.py's fill_room()/allocate() cap spouse_rrsp at
        min(..., state.spouse_rrsp_room, ...), so contributions['spouse_rrsp']
        was unconditionally 0 in every optimizer-driven run, regardless of
        strategy or configured room. Pinned to that mechanism directly."""
        config = small_household_config()
        results, _ = self._run(config, strategy=STRATEGY_RRSP_MAX)
        total_spouse_rrsp = sum(r.spouse_rrsp for r in results) - results[0].spouse_rrsp
        # spouse_rrsp is a cumulative balance; confirm it grew from
        # contributions, i.e. is not stuck at the opening balance.
        spouse_rrsp_contributed = any(
            r.contributions.get('spouse_rrsp', 0) > 0 for r in results)
        self.assertTrue(
            spouse_rrsp_contributed,
            "spouse's personal RRSP must receive contributions somewhere in "
            "the horizon when the spouse has real accumulated RRSP room")

    # ── item 5: FHSA ──
    #
    # None of the four predefined Canadian strategies (Balanced, Max RRSP +
    # Spousal, Readvance Priority, No Readvance) set fhsa_pct > 0 -- FHSA
    # funding through StrategyEngine.allocate() is opt-in strategy
    # configuration, on BOTH engines, and is not what #627 item 5 is about.
    # StrategyEngine.fill_room() (the year-0 lump-sum waterfall) DOES fund
    # FHSA unconditionally once state.fhsa_room > 0 -- exactly the "room
    # reaches the strategy engine at all" wiring #627 fixed (previously
    # FamilyState never carried fhsa_room/fhsa_lifetime_remaining on the
    # optimizer path, so even fill_room's FHSA step was permanently gated
    # off regardless of strategy).
    def test_fhsa_is_funded_from_a_lump_sum_when_room_exists(self):
        config = small_household_config()
        results, _ = self._run(config, strategy=STRATEGY_RRSP_MAX, lump_sum=500_000.0)
        self.assertGreater(
            results[0].contributions.get('fhsa', 0), 0.0,
            "FHSA must receive a contribution in year 0 when the primary has "
            "fhsa_room_accumulated and there is room left after RRSP/TFSA -- "
            "previously FamilyState never set fhsa_room/fhsa_lifetime_remaining "
            "at all on the optimizer path, so fill_room's FHSA step was "
            "unconditionally gated off no matter how large the lump sum")

    # ── item 6: RESP CESG/QESI grants ──
    def test_resp_grant_inflates_balance_beyond_contributions(self):
        config = small_household_config()
        results, _ = self._run(config, strategy=STRATEGY_RRSP_MAX, lump_sum=500_000.0)
        resp_contrib_year0 = results[0].contributions.get('resp', 0)
        self.assertGreater(resp_contrib_year0, 0.0, "fixture must actually contribute to RESP")
        opening_resp_balance = config.resp_current_balance
        balance_growth = results[0].resp_balance - opening_resp_balance
        # With CESG (20%) + Quebec QESI (~10%) matching, a same-year
        # contribution should grow the balance by MORE than the raw dollars
        # contributed -- the only way that happens is if a grant was
        # actually computed and applied. Previously Optimizer._run_simulation
        # never built a resp_calc/resp_children and always passed
        # resp_data=None to simulate_year_pure, so no grant was ever applied
        # regardless of how much was contributed.
        self.assertGreater(
            balance_growth, resp_contrib_year0,
            "RESP balance growth should exceed the raw contribution once "
            "CESG/QESI grants (previously never computed on the optimizer "
            "path) are applied in the same year")

    # ── item 7: cash_flows ──
    def test_cash_flow_event_year_shows_elevated_savings(self):
        config = small_household_config()
        results, _ = self._run(config, strategy=STRATEGY_BALANCED)
        cash_flow_year_index = 3  # cash_flows entry is at START_YEAR + 3
        elevated = results[cash_flow_year_index].annual_savings
        neighbour = results[cash_flow_year_index - 1].annual_savings
        # The $60k cash-flow event should push savings well above the
        # surrounding years' income-only savings (both members still working).
        self.assertGreater(
            elevated, neighbour + 50_000,
            "annual_savings in the cash_flows event year must reflect the "
            "$60k event -- previously CashFlow events were never applied on "
            "the optimizer path at all")

    # ── item 3: configured HELOC rate path (not hardcoded 0.05) ──
    def test_heloc_readvance_interest_uses_configured_rate_not_hardcoded_default(self):
        # mortgage_rate=0.09 here (vs. 0.05 elsewhere) specifically so the
        # hardcoded simulate_year_pure default (heloc_rate=0.05) would be
        # trivially distinguishable from "the configured rate path" if it
        # were still leaking through.
        config = small_household_config()
        config = dataclasses.replace(config, mortgage_rate=0.09, margin_available=100_000)
        results, final_state = self._run(
            config, strategy=STRATEGY_RRSP_MAX, use_readvanceable=True)
        opt = GridOptimizer(config)
        # The HELOC path the optimizer built from this config's rate.
        configured_rate_year0 = opt.heloc_path.get_heloc_rate(0, opt.rate_path.rate_type)
        self.assertNotAlmostEqual(
            configured_rate_year0, 0.05, places=3,
            msg="fixture sanity check: configured HELOC rate must actually "
                "differ from the old hardcoded default")
        # sm_readvanced: mortgage principal readvanced into the SM HELOC
        # balance this year (simulate_year_pure books
        # readvance_interest = new_sm_heloc * heloc_rate in the SAME year
        # the principal is readvanced, so year 0's readvance IS this
        # year's whole readvance_heloc_balance).
        readvanced_year0 = results[0].sm_readvanced
        readvance_interest_year0 = results[0].readvance_interest
        self.assertGreater(readvanced_year0, 0.0,
                            "fixture sanity check: readvance must have occurred")
        implied_rate = readvance_interest_year0 / readvanced_year0
        # If the old bug were present (heloc_rate always hardcoded 0.05),
        # implied_rate would be ~0.05 regardless of the configured
        # (much higher) mortgage-rate-derived HELOC rate path.
        self.assertNotAlmostEqual(
            implied_rate, 0.05, places=3,
            msg="readvance interest must reflect the configured HELOC rate "
                "path, not the hardcoded heloc_rate=0.05 default")
        self.assertAlmostEqual(implied_rate, configured_rate_year0, places=6)

    # ── item 8: year-versioned RRSP/TFSA limits (DP#20) ──
    def test_context_uses_year_versioned_tax_provider_not_a_frozen_flat_limit(self):
        config = small_household_config()
        opt = GridOptimizer(config)
        from simulation_state import initial_state_for_run
        opening_state = initial_state_for_run(config)
        ctx = opt._build_context(config, STRATEGY_BALANCED, False, False, 0.0, opening_state)
        limit_year0 = ctx.tax_provider.get_rrsp_limit(config.start_year)
        limit_year10 = ctx.tax_provider.get_rrsp_limit(config.start_year + 10)
        self.assertNotEqual(
            limit_year0, limit_year10,
            "the optimizer's SimulationContext must carry a real, "
            "year-versioned TaxDataProvider (DP#20) -- not config.rrsp_annual_max "
            "frozen for the whole horizon")
        self.assertFalse(
            ctx.frozen_brackets,
            "frozen_brackets must flow from config, not be silently forced")


if __name__ == '__main__':
    unittest.main()
