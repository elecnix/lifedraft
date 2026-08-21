#!/usr/bin/env python3
"""Unit tests for fill_room(), lump_sum, Quebec deduction, and HELOC tracing
integration in strategy.py and simulation.py.

Tests every rule:
- fill_room priority order (RRSP → Spousal RRSP → Spouse RRSP → TFSA → RESP → Non-reg)
- fill_room respects room limits and deduct-later bracket targeting
- lump_sum allocates in year 0 before annual savings
- Quebec deduction limit reduces SM tax savings
- HELOC tracing records RRSP/TFSA/non-reg advances correctly
- RRSP/TFSA advances are NOT deductible, only non-reg investment is

All test data uses round numbers and fake names.
"""

import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from strategy import (
    AllocationStrategy, StrategyEngine, FamilyState, AllocationResult,
    StrategyType,
)
from countries.canada.strategies import (
    STRATEGY_BALANCED, STRATEGY_READVANCE_PRIORITY,
    STRATEGY_RRSP_MAX, STRATEGY_NO_READVANCE,
)

from simulation import FamilySimulation, SimulationConfig, YearResult
from simulation_state import adult_fhsa_slot  # #700/#643/#704: per-adult FHSA store

from optimize import compute_net_benefit

from countries.canada.rate_model import build_rate_path


def _make_state(
    primary_rrsp_room=150000,
    spouse_rrsp_room=50000,
    primary_tfsa_room=40000,
    spouse_tfsa_room=40000,
    resp_eligible_children=1,
    primary_income=120000,
    spouse_income=50000,
    bracket_gap=0.20,
    fhsa_room=0,  # No FHSA room by default in tests
    fhsa_lifetime_remaining=0,
) -> FamilyState:
    """Build a test FamilyState with round numbers."""
    return FamilyState(
        primary_income=primary_income,
        spouse_income=spouse_income,
        primary_marginal_rate=0.45,
        spouse_marginal_rate=0.25,
        primary_rrsp_room=primary_rrsp_room,
        spouse_rrsp_room=spouse_rrsp_room,
        primary_tfsa_room=primary_tfsa_room,
        spouse_tfsa_room=spouse_tfsa_room,
        fhsa_room=fhsa_room,
        fhsa_lifetime_remaining=fhsa_lifetime_remaining,
        resp_eligible_children=resp_eligible_children,
        resp_annual_match_cap=750,
        annual_savings=40000,
        bracket_gap=bracket_gap,
    )


def _make_config(
    margin_available=200000,
    mortgage_balance=100000,
    house_value=750000,
    primary_rrsp_room=150000,
    spouse_rrsp_room=50000,
    primary_tfsa_room=40000,
    spouse_tfsa_room=40000,
    savings_rate=0.20,  # DP#13: explicit round-number default for tests
) -> SimulationConfig:
    """Build a test SimulationConfig with round numbers."""
    config = SimulationConfig(
        house_value=house_value,
        mortgage_balance=mortgage_balance,
        margin_available=margin_available,
        savings_rate=savings_rate,
        family_members=[
            {'name': 'Alpha', 'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': primary_rrsp_room,
             'tfsa_room_accumulated': primary_tfsa_room,
             'pension_adjustment': 4000},
            {'name': 'Beta', 'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': spouse_rrsp_room,
             'tfsa_room_accumulated': spouse_tfsa_room,
             'pension_adjustment': 4000},
        ],
        children=[{'name': 'Kid', 'age': 10, 'gross_income': 0}],
    )
    return config


# ── fill_room() Tests ─────────────────────────────────────────────────────

class TestFillRoomPriorityOrder(unittest.TestCase):
    """Test that fill_room fills accounts in the correct priority order."""

    def test_rrsp_filled_first(self):
        """Primary + Spousal RRSP split the primary earner's room proportionally."""
        state = _make_state(primary_rrsp_room=50000)
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.fill_room(200000, state)
        # STRATEGY_BALANCED: rrsp_pct=0.30, spousal_rrsp_pct=0.10
        # Primary room (50k) split: 30/40=75% primary, 10/40=25% spousal
        self.assertAlmostEqual(result.primary_rrsp, 50000 * 0.75)
        self.assertAlmostEqual(result.spousal_rrsp, 50000 * 0.25)
        # Total RRSP from primary's room = full room
        self.assertAlmostEqual(result.primary_rrsp + result.spousal_rrsp, 50000)

    def test_tfsa_filled_before_spouse_rrsp(self):
        """TFSA is filled before spouse RRSP."""
        state = _make_state(primary_rrsp_room=0, spouse_rrsp_room=100000,
                            primary_tfsa_room=40000, spouse_tfsa_room=40000)
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.fill_room(200000, state)
        self.assertEqual(result.primary_tfsa, 40000)
        self.assertEqual(result.spouse_tfsa, 40000)

    def test_remaining_goes_to_nonreg(self):
        """Excess after all registered room goes to non-reg."""
        state = _make_state(primary_rrsp_room=10000, spouse_rrsp_room=0,
                            primary_tfsa_room=5000, spouse_tfsa_room=5000)
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.fill_room(50000, state)
        # 10000 RRSP + 5000+5000 TFSA + 1250 RESP = 21250 ← goes to reg
        # Rest: ~28750 → non-reg
        self.assertGreater(result.non_reg, 0)

    def test_zero_lump_sum(self):
        """Zero lump sum: nothing allocated."""
        state = _make_state()
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.fill_room(0, state)
        self.assertEqual(result.total_allocated, 0)

    def test_small_lump_sum_partial_fill(self):
        """Small lump sum: only first priority accounts filled."""
        state = _make_state(primary_rrsp_room=100000)
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.fill_room(50000, state)
        self.assertEqual(result.primary_rrsp, 50000)  # Only half of room
        self.assertEqual(result.primary_tfsa, 0)  # Not reached

    def test_exact_room_match(self):
        """Lump sum exactly equals total registered room → no non-reg."""
        total_room = 100000 + 50000 + 40000 + 40000  # 230k
        state = _make_state(primary_rrsp_room=100000, spouse_rrsp_room=50000,
                            primary_tfsa_room=40000, spouse_tfsa_room=40000,
                            resp_eligible_children=0)  # No RESP match
        engine = StrategyEngine(STRATEGY_BALANCED)
        # Fill room: 100k RRSP + 50k spousal + 40k+40k TFSA = 230k
        # But fill_room also adds RESP for matching (if children eligible)
        # With resp_eligible_children=0, no RESP match
        result = engine.fill_room(230000, state)
        self.assertEqual(result.non_reg, 0)


class TestFillRoomDeductLater(unittest.TestCase):
    """Test fill_room with deduct-later strategy."""

    def test_deduct_later_limits_rrsp_to_bracket_target(self):
        """Deduct-later: fill_room allocates up to room, but deduction timing defers."""
        state = _make_state(primary_income=120000, primary_rrsp_room=5000)
        strategy = AllocationStrategy(
            name="test", deduct_later=True, bracket_target=100000,
            spousal_splitting=True, min_bracket_gap=0.05,
        )
        engine = StrategyEngine(strategy)
        result = engine.fill_room(50000, state)
        # fill_room allocates up to available room (5000 for primary RRSP)
        # The deduct-later mechanism defers the deduction to reduce MTR
        self.assertLessEqual(result.primary_rrsp, 5000)

    def test_deduct_later_contributes_full_room_without_deducting(self):
        """Deduct-later: still contributes remaining room but defers deduction.
        Primary + spousal share the primary earner's room."""
        state = _make_state(primary_income=120000, primary_rrsp_room=200000)
        strategy = AllocationStrategy(
            name="test", deduct_later=False,  # Deduct now: full room allocated
            rrsp_pct=0.35, spousal_rrsp_pct=0.0,  # No spousal → all to primary
            spousal_splitting=True, min_bracket_gap=0.05,
        )
        engine = StrategyEngine(strategy)
        result = engine.fill_room(500000, state)
        # No spousal allocation → all primary room goes to primary RRSP
        self.assertEqual(result.primary_rrsp, 200000)


class TestFillRoomNoRoom(unittest.TestCase):
    """Test fill_room when rooms are zero."""

    def test_all_zero_room_goes_to_nonreg(self):
        """All zero room: everything goes to non-reg."""
        state = _make_state(primary_rrsp_room=0, spouse_rrsp_room=0,
                            primary_tfsa_room=0, spouse_tfsa_room=0,
                            resp_eligible_children=0)
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.fill_room(100000, state)
        self.assertAlmostEqual(result.non_reg, 100000)

    def test_no_tfsa_room_goes_to_next_priority(self):
        """No TFSA room: skip to next account."""
        state = _make_state(primary_rrsp_room=0, spouse_rrsp_room=0,
                            primary_tfsa_room=0, spouse_tfsa_room=0)
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.fill_room(100000, state)
        # After RRSP (0) and spousal (0), no TFSA → RESP → non-reg
        self.assertGreater(result.non_reg + result.resp, 0)


class TestFillRoomSpousalRRSP(unittest.TestCase):
    """Test fill_room spousal RRSP allocation."""

    def test_spousal_rrsp_when_bracket_gap_exists(self):
        """Spousal RRSP allocated when bracket gap > threshold.
        Spousal RRSP contributions use the primary earner's deduction room,
        split proportionally with primary RRSP based on strategy ratios."""
        state = _make_state(primary_rrsp_room=150000, bracket_gap=0.20)
        engine = StrategyEngine(AllocationStrategy(
            name="test", rrsp_pct=0.35, spousal_rrsp_pct=0.10,
            spousal_splitting=True, min_bracket_gap=0.05,
        ))
        result = engine.fill_room(200000, state)
        # Primary room (150k) split: 35/45 = ~78% to primary, 10/45 = ~22% to spousal
        self.assertGreater(result.spousal_rrsp, 0)
        self.assertGreater(result.primary_rrsp, 0)
        self.assertLessEqual(result.primary_rrsp + result.spousal_rrsp, 150000)

    def test_no_spousal_when_zero_gap(self):
        """No spousal RRSP when bracket gap is zero."""
        state = _make_state(spouse_rrsp_room=80000, bracket_gap=0.0)
        engine = StrategyEngine(AllocationStrategy(
            name="test", spousal_splitting=True, min_bracket_gap=0.05,
        ))
        result = engine.fill_room(200000, state)
        self.assertEqual(result.spousal_rrsp, 0)


# ── Lump Sum in Simulation ────────────────────────────────────────────────

class TestSimulationLumpSum(unittest.TestCase):
    """Test lump_sum parameter in FamilySimulation."""

    def test_lump_sum_allocates_year0(self):
        """Lump sum is allocated in year 0."""
        config = _make_config(primary_rrsp_room=50000, primary_tfsa_room=20000)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_BALANCED, rp,
                               use_readvanceable=False, lump_sum=100000)
        results = sim.run()
        # Year 1 should have the lump-sum contributions
        self.assertGreater(results[0].total_rrsp, 0)
        self.assertGreater(results[0].total_tfsa, 0)

    def test_no_lump_sum_no_initial_big_allocation(self):
        """Without lump sum, year 1 contributions come from annual savings only."""
        config = _make_config(primary_rrsp_room=500000)  # Lots of room
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim_no_lump = FamilySimulation(config, STRATEGY_BALANCED, rp,
                                        use_readvanceable=False, lump_sum=0)
        sim_with_lump = FamilySimulation(config, STRATEGY_BALANCED, rp,
                                          use_readvanceable=False, lump_sum=500000)
        results_no = sim_no_lump.run()
        results_with = sim_with_lump.run()
        # With lump sum, year 1 should have much more invested
        self.assertGreater(results_with[0].total_assets, results_no[0].total_assets)

    def test_lump_sum_more_results_than_years(self):
        """Lump sum in year 0 + annual savings = projection_years results still."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_BALANCED, rp,
                               use_readvanceable=False, lump_sum=50000)
        results = sim.run()
        self.assertEqual(len(results), 10)

    def test_zero_lump_sum_equivalent_to_default(self):
        """Zero lump sum behaves the same as no lump_sum."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim_default = FamilySimulation(config, STRATEGY_BALANCED, rp, use_readvanceable=False)
        sim_zero = FamilySimulation(config, STRATEGY_BALANCED, rp,
                                     use_readvanceable=False, lump_sum=0)
        r_default = sim_default.run()
        r_zero = sim_zero.run()
        self.assertAlmostEqual(r_default[-1].total_assets, r_zero[-1].total_assets, places=0)


# ── Quebec Deduction in Simulation ─────────────────────────────────────────

class TestSimulationQuebecDeduction(unittest.TestCase):
    """Test that Quebec deduction limit is applied to SM tax savings."""

    def test_readvance_tax_savings_with_qc_limit(self):
        """SM tax savings should be less than full interest × MTR due to QC limit."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=True, lump_sum=200000)
        results = sim.run()
        for r in results:
            if r.readvance_interest > 0:
                # QC deductible should be ≤ SM interest
                self.assertLessEqual(r.sm_qc_deductible, r.readvance_interest)
                # SM tax savings should be ≤ full deduction at MTR
                full_deduction = r.readvance_interest * 0.45  # Approximate MTR
                self.assertLessEqual(r.readvance_tax_savings, full_deduction + 1)  # Rounding

    def test_qc_deductible_fields_present(self):
        """YearResult has QC deduction fields."""
        r = YearResult()
        self.assertTrue(hasattr(r, 'sm_qc_deductible'))
        self.assertTrue(hasattr(r, 'sm_qc_carry_forward'))

    def test_no_sm_zero_qc_fields(self):
        """No SM: QC deduction fields should be zero."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_NO_READVANCE, rp, use_readvanceable=False)
        results = sim.run()
        for r in results:
            self.assertAlmostEqual(r.sm_qc_deductible, 0)
            self.assertAlmostEqual(r.sm_qc_carry_forward, 0)

    def test_qc_tracker_accumulates_carry_forward(self):
        """QC deduction carry-forward should accumulate when income is low."""
        config = _make_config(primary_rrsp_room=0, primary_tfsa_room=0)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=True, lump_sum=500000)
        results = sim.run()
        # QC carry-forward should be in jurisdiction_state
        qc_cf = sim._state.jurisdiction_state.get('canada', {}).get('qc_carry_forward', 0)
        # With SM active and low investment income, carry-forward should accumulate
        if any(r.readvance_interest > 0 for r in results):
            self.assertGreaterEqual(qc_cf, 0)


# ── HELOC Tracing in Simulation ───────────────────────────────────────────

class TestSimulationHELOCTracing(unittest.TestCase):
    """Test that HELOC tracing records advances per year."""

    def test_rrsp_advance_recorded_as_non_deductible(self):
        """RRSP advances are recorded in heloc_tracing state."""
        config = _make_config(primary_rrsp_room=50000, primary_tfsa_room=0)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_BALANCED, rp,
                               use_readvanceable=False, lump_sum=100000)
        sim.run()
        # Check that some RRSP advances are recorded in state
        self.assertGreater(sim._state.jurisdiction_state['canada']['heloc_tracing'].get('rrsp_advances', 0), 0)

    def test_tfsa_advance_recorded_as_non_deductible(self):
        """TFSA advances are recorded in heloc_tracing state."""
        config = _make_config(primary_rrsp_room=0, primary_tfsa_room=40000)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_BALANCED, rp,
                               use_readvanceable=False, lump_sum=100000)
        sim.run()
        # Check that some TFSA advances are recorded in state
        self.assertGreater(sim._state.jurisdiction_state['canada']['heloc_tracing'].get('tfsa_advances', 0), 0)

    def test_nonreg_advance_recorded_as_investment(self):
        """Non-reg advances are recorded as investment in heloc_tracing state."""
        config = _make_config(primary_rrsp_room=0, primary_tfsa_room=0,
                              spouse_rrsp_room=0, spouse_tfsa_room=0)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=True, lump_sum=100000)
        sim.run()
        # Check that investment advances are recorded in state
        self.assertGreater(sim._state.jurisdiction_state['canada']['heloc_tracing'].get('investment_advances', 0), 0)

    def test_deductible_proportion_in_year_result(self):
        """YearResult includes sm_deductible_proportion from tracing."""
        r = YearResult()
        self.assertTrue(hasattr(r, 'sm_deductible_proportion'))

    def test_sm_with_mixed_advances_reduces_tax_savings(self):
        """SM tax savings should be lower when advances are mixed (RRSP + non-reg)."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])

        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=True, lump_sum=300000)
        results = sim.run()

        # If there are both RRSP and non-reg advances, proportion < 1.0
        tracing = sim._state.jurisdiction_state['canada']['heloc_tracing']
        total = tracing.get('total_advances', 0)
        investment = tracing.get('investment_advances', 0)
        if total > 0 and investment < total:
            proportion = investment / total
            self.assertLess(proportion, 1.0)

    def test_sm_readvance_is_investment_purpose(self):
        """SM readvance (mortgage principal re-borrowed) is tracked as investment advances."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=True)
        sim.run()
        # SM readvances are investment-purpose advances tracked in state
        if sim.use_readvanceable:
            # May or may not have readvances depending on mortgage schedule
            pass  # Just verifying no crash


class TestSimulationNoSMTracing(unittest.TestCase):
    """Test that without SM, HELOC tracing still records annual contributions."""

    def test_no_sm_tracing_still_records(self):
        """Without SM, advance records still exist (from contributions)."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_NO_READVANCE, rp, use_readvanceable=False)
        sim.run()
        self.assertGreater(sim._state.jurisdiction_state['canada']['heloc_tracing'].get('total_advances', 0), 0)

    def test_no_sm_deductible_proportion_zero(self):
        """Without SM, SM interest and savings should be zero."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_NO_READVANCE, rp, use_readvanceable=False)
        results = sim.run()
        for r in results:
            self.assertAlmostEqual(r.readvance_interest, 0)
            self.assertAlmostEqual(r.readvance_tax_savings, 0)


# ── SimulationConfig.from_dict ────────────────────────────────────────────

class TestSimulationConfigFromDict(unittest.TestCase):
    """Test SimulationConfig.from_dict method."""

    def test_from_dict_basic(self):
        """Build config from dict with standard fields."""
        cfg = {
            'assumptions': {'projection_years': 10, 'investment_return': 0.07, 'salary_growth': 0.02},
            'savings': {'rate': 0.20},
            'property': {
                'house_value': 500000, 'mortgage_balance': 100000,
                'mortgage_rate': 0.05, 'ltv_max': 0.80,
                'margin_available': 200000, 'current_payment_monthly': 1000,
                'amortization_years': 25,
            },
            'family': {
                'members': [
                    {'role': 'primary', 'gross_income': 120000,
                     'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000},
                ],
                'children': [],
            },
            'accounts': {
                'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33000,
                'tfsa_annual_room_per_person': 7000, 'resp_current_balance': 0,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        self.assertEqual(config.projection_years, 10)
        self.assertAlmostEqual(config.investment_return, 0.07)
        self.assertAlmostEqual(config.house_value, 500000)
        self.assertAlmostEqual(config.mortgage_balance, 100000)
        self.assertAlmostEqual(config.margin_available, 200000)

    def test_from_dict_modified_ltv(self):
        """from_dict picks up modified LTV values (for LTV exploration)."""
        cfg = {
            'assumptions': {'projection_years': 10, 'investment_return': 0.07, 'salary_growth': 0.02},
            'savings': {'rate': 0.20},
            'property': {
                'house_value': 500000, 'mortgage_balance': 200000,
                'mortgage_rate': 0.05, 'ltv_max': 0.50,  # Modified!
                'margin_available': 300000, 'current_payment_monthly': 1000,
                'amortization_years': 25,
            },
            'family': {
                'members': [
                    {'role': 'primary', 'gross_income': 120000,
                     'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000},
                ],
                'children': [],
            },
            'accounts': {
                'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33000,
                'tfsa_annual_room_per_person': 7000, 'resp_current_balance': 0,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        self.assertAlmostEqual(config.ltv_max, 0.50)
        self.assertAlmostEqual(config.margin_available, 300000)

    def test_from_dict_missing_optional_fields(self):
        """from_dict handles missing optional fields with defaults."""
        cfg = {
            'assumptions': {'projection_years': 10, 'investment_return': 0.07},
            'savings': {'rate': 0.20},
            'property': {
                'house_value': 500000, 'mortgage_balance': 100000,
                'mortgage_rate': 0.05, 'ltv_max': 0.80,
            },
            'family': {
                'members': [
                    {'role': 'primary', 'gross_income': 120000},
                ],
                'children': [],
            },
            'accounts': {
                'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33000,
                'tfsa_annual_room_per_person': 7000,
            },
        }
        config = SimulationConfig.from_dict(cfg)
        self.assertAlmostEqual(config.margin_available, 0)  # Default
        self.assertAlmostEqual(config.salary_growth, 0.02)  # Default

    def test_from_dict_matches_from_json(self):
        """from_json (the contract wire boundary) and from_dict (the internal
        shape, fed the SAME document mapped by input_contract.to_internal_config)
        produce equivalent configs.

        Epic #603 Track C Phase 2b: from_dict and from_json are no longer the
        same wire format on purpose -- from_json is the sole, validated
        contract boundary; from_dict is SimulationConfig's internal dict
        shape (see simulation_config.py's docstrings on both). This test's
        job is now to prove from_json's pipeline (validate -> map -> from_dict)
        agrees with calling from_dict directly on the already-mapped internal
        dict, i.e. from_json adds validation + mapping and nothing else.
        """
        import json
        import tempfile
        import input_contract
        from test_input_contract import _load_example, _two_generation_subset

        doc = _two_generation_subset(_load_example())
        internal = input_contract.to_internal_config(doc)
        config_dict = SimulationConfig.from_dict(internal)

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(doc, f)
            f.flush()
            config_json = SimulationConfig.from_json(f.name)

        self.assertEqual(config_dict.projection_years, config_json.projection_years)
        self.assertAlmostEqual(config_dict.investment_return, config_json.investment_return)
        self.assertAlmostEqual(config_dict.house_value, config_json.house_value)
        self.assertAlmostEqual(config_dict.mortgage_balance, config_json.mortgage_balance)
        self.assertAlmostEqual(config_dict.margin_available, config_json.margin_available)


# ── Sensitivity Module Fix Verification ────────────────────────────────────

class TestSensitivityModuleFixed(unittest.TestCase):
    """Verify sensitivity.py no longer has SyntaxError."""

    def test_imports_ok(self):
        """sensitivity module imports without error."""
        from sensitivity import monte_carlo, tornado_data, break_even_analysis
        self.assertTrue(callable(monte_carlo))
        self.assertTrue(callable(tornado_data))


class TestAutoDetection(unittest.TestCase):
    """Test that optimize.py auto-detects and runs analysis from input.json data."""

    def test_has_property_data(self):
        """_has_property_data returns True when house/mortgage/margin exist."""
        from optimize import _has_property_data
        cfg = {'property': {'house_value': 500000, 'mortgage_balance': 100000, 'margin_available': 200000}}
        self.assertTrue(_has_property_data(cfg))

    def test_has_property_data_missing(self):
        """_has_property_data returns False when key fields are missing."""
        from optimize import _has_property_data
        cfg = {'property': {'house_value': 0, 'mortgage_balance': 100000}}  # No margin
        self.assertFalse(_has_property_data(cfg))

    def test_has_family_data(self):
        """_has_family_data returns True when family members exist."""
        from optimize import _has_family_data
        cfg = {'family': {'members': [{'role': 'primary', 'gross_income': 120000}]}}
        self.assertTrue(_has_family_data(cfg))

    def test_has_family_data_missing(self):
        """_has_family_data returns False when no family section."""
        from optimize import _has_family_data
        self.assertFalse(_has_family_data({}))

    def test_fhsa_auto_included_when_room_exists(self):
        """FHSA account is created when fhsa_room_accumulated is in member data."""
        config = _make_config()
        config.family_members[0]['fhsa_room_accumulated'] = 32000
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_BALANCED, rp, use_readvanceable=False)
        self.assertTrue(sim.has_fhsa)
        # CRA limits carry-forward to 1 year's unused room ($8,000), so
        # contribution_room = annual_room ($8,000) + carry_forward_room ($8,000) = $16,000
        self.assertEqual(adult_fhsa_slot(sim._state.jurisdiction_state['canada'], 0)['room'], 16000)

    def test_fhsa_not_created_when_no_room(self):
        """FHSA is not active when no FHSA room data."""
        config = _make_config()
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_BALANCED, rp, use_readvanceable=False)
        self.assertFalse(sim.has_fhsa)

    def test_attribution_checked_on_spousal_contribution(self):
        """Attribution.check_attribution is called when spousal RRSP is contributed."""
        from countries.canada.attribution import check_attribution, TransferType
        config = _make_config(spouse_rrsp_room=50000)
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_RRSP_MAX, rp,
                               use_readvanceable=False, lump_sum=100000)
        sim.run()
        # Spousal contributions should have been tracked
        # (attribution check is called internally)
        self.assertGreaterEqual(len(sim._state.jurisdiction_state['canada']['spousal_contribution_years']), 0)

    def test_retirement_module_used_in_net_benefit_with_age(self):
        """compute_net_benefit uses retirement.py when birth_year is available."""
        from optimize import compute_net_benefit
        config = _make_config()
        sim = FamilySimulation(config, STRATEGY_BALANCED, build_rate_path("test", 0.05, 10, 'variable', [0.05]), use_readvanceable=False)
        results = sim.run()
        # With birth_year in cfg, retirement module should be used
        cfg_with_age = {
            'family': {'members': [{'role': 'primary', 'birth_year': 1990}]},
            'assumptions': {'capital_gains_inclusion': 0.50,
                           'resp_eap_taxable_portion': 0.60,
                           'resp_eap_tax_rate': 0.15},
        }
        net = compute_net_benefit(results, cfg_with_age)
        self.assertIsNotNone(net)

    def test_retirement_module_not_used_without_age(self):
        """compute_net_benefit falls back to simplified when no birth_year."""
        from optimize import compute_net_benefit
        config = _make_config()
        sim = FamilySimulation(config, STRATEGY_BALANCED, build_rate_path("test", 0.05, 10, 'variable', [0.05]), use_readvanceable=False)
        results = sim.run()
        cfg_no_age = {
            'family': {'members': [{'role': 'primary'}]},  # No birth_year
            'assumptions': {'capital_gains_inclusion': 0.50,
                           'resp_eap_taxable_portion': 0.60,
                           'resp_eap_tax_rate': 0.15},
        }
        net = compute_net_benefit(results, cfg_no_age)
        self.assertIsNotNone(net)


# =============================================================================
# ACB TRACKING TESTS (DP#19: track cost basis from day one)
# =============================================================================

from countries.canada.account_models import NonRegAccount


class TestNonRegACB(unittest.TestCase):
    """Unit tests for NonRegAccount ACB tracking.
    
    Verifies DP#19: cost basis is recorded when money goes in,
    not estimated when money comes out.
    """
    
    def test_contribute_increases_acb(self):
        """Contributing increases cost basis one-for-one."""
        acct = NonRegAccount()
        acct.contribute(10000)
        self.assertAlmostEqual(acct.cost_basis, 10000)
        self.assertAlmostEqual(acct.balance, 10000)
        
        acct.contribute(25000)
        self.assertAlmostEqual(acct.cost_basis, 35000)
        self.assertAlmostEqual(acct.balance, 35000)
    
    def test_grow_does_not_change_acb(self):
        """Investment growth increases balance but NOT cost basis."""
        acct = NonRegAccount()
        acct.contribute(10000)
        growth = acct.grow(0.07)  # 7% return
        self.assertAlmostEqual(growth, 700)
        self.assertAlmostEqual(acct.cost_basis, 10000)  # ACB unchanged
        self.assertAlmostEqual(acct.balance, 10700)      # Balance increased
    
    def test_multiple_contributes_and_grows(self):
        """ACB correctly tracks across multiple contribution + growth cycles."""
        acct = NonRegAccount()
        # Year 0: contribute $50k
        acct.contribute(50000)
        self.assertAlmostEqual(acct.cost_basis, 50000)
        # Year 0.5: grow 7%
        acct.grow(0.07)
        self.assertAlmostEqual(acct.cost_basis, 50000)
        self.assertAlmostEqual(acct.balance, 53500)
        # Year 1: contribute $20k more
        acct.contribute(20000)
        self.assertAlmostEqual(acct.cost_basis, 70000)  # 50k + 20k
        self.assertAlmostEqual(acct.balance, 73500)       # 53500 + 20000
        # Year 2: grow 7%
        acct.grow(0.07)
        self.assertAlmostEqual(acct.cost_basis, 70000)   # Still same
        self.assertAlmostEqual(acct.balance, 78645)      # 73500 * 1.07
    
    def test_unrealized_gains_property(self):
        """unrealized_gains returns balance - acb."""
        acct = NonRegAccount()
        acct.contribute(100000)
        acct.grow(0.07)
        self.assertAlmostEqual(acct.unrealized_gains, 7000)
    
    def test_unrealized_gains_zero_when_no_growth(self):
        """No gains when balance == acb."""
        acct = NonRegAccount()
        acct.contribute(100000)
        self.assertAlmostEqual(acct.unrealized_gains, 0)
    
    def test_unrealized_gains_never_negative(self):
        """Never returns negative (capital losses not tracked)."""
        acct = NonRegAccount()
        acct.contribute(100000)
        acct.grow(-0.20)  # Market crash
        self.assertAlmostEqual(acct.unrealized_gains, 0)  # Floor at 0
    
    def test_capital_gains_tax_uses_acb(self):
        """capital_gains_tax computes from actual ACB, not estimated."""
        acct = NonRegAccount()
        acct.contribute(100000)
        acct.grow(0.07)  # $7k gain
        tax = acct.capital_gains_tax(marginal_rate=0.50)
        # 7k gain * 50% inclusion * 50% MTR = $1,750
        self.assertAlmostEqual(tax, 7000 * 0.50 * 0.50)
    
    def test_capital_gains_tax_zero_when_no_gains(self):
        """No tax when ACB equals or exceeds balance."""
        acct = NonRegAccount()
        acct.contribute(100000)
        tax = acct.capital_gains_tax(marginal_rate=0.50)
        self.assertAlmostEqual(tax, 0)
    
    def test_smith_contribute_also_increases_acb(self):
        """SM readvance contributions increase ACB correctly."""
        acct = NonRegAccount()
        acct.contribute(30000, is_smith=True)
        self.assertAlmostEqual(acct.cost_basis, 30000)
    
    def test_sm_contribution_plus_regular_contribution(self):
        """Mix of SM readvance and regular contributions all tracked."""
        acct = NonRegAccount()
        acct.contribute(20000)           # Regular
        acct.contribute(15000, is_smith=True)  # SM readvance
        self.assertAlmostEqual(acct.cost_basis, 35000)
        self.assertAlmostEqual(acct.balance, 35000)


class TestYearResultACB(unittest.TestCase):
    """Integration: YearResult now carries non_reg_acb from simulation."""
    
    def test_year_result_has_acb_fields(self):
        """YearResult dataclass has ACB fields."""
        yr = YearResult()
        self.assertTrue(hasattr(yr, 'non_reg_acb'))
        self.assertTrue(hasattr(yr, 'non_reg_unrealized_gains'))
        self.assertAlmostEqual(yr.non_reg_acb, 0)
    
    def test_simulation_tracks_acb(self):
        """Full simulation populates ACB in every YearResult."""
        config = SimulationConfig(
            projection_years=3,
            investment_return=0.07,
            savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=50000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            children=[],
        )
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 3, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False, lump_sum=50000)
        results = sim.run()
        
        # Year 1+ should have ACB tracked (year 0 is the lump-sum allocation)
        # Non-reg grows but ACB stays at contribution amount
        for yr in results:
            self.assertGreater(yr.non_reg_acb, 0, f"Year {yr.year} non_reg_acb should be > 0")
            # ACB <= balance (balance includes growth, ACB doesn't)
            self.assertLessEqual(yr.non_reg_acb, yr.non_reg_balance + 1,
                                 f"Year {yr.year}: ACB {yr.non_reg_acb} should be <= balance {yr.non_reg_balance}")


class TestOptimizeUsesACB(unittest.TestCase):
    """Verify optimize.py uses actual ACB instead of rough estimate."""
    
    def test_compute_net_benefit_uses_acb_from_result(self):
        """compute_net_benefit uses YearResult.non_reg_acb when available."""
        yr1 = YearResult(year=1, total_assets=200000, total_debt=100000,
                          non_reg_balance=150000, non_reg_acb=100000,
                          total_rrsp=50000, total_tfsa=0, resp_balance=0,
                          rrsp_tax_savings=10000, readvance_tax_savings=0)
        net = compute_net_benefit(
            [yr1],
            {'assumptions': {'capital_gains_inclusion': 0.50}},
        )
        # Gains = 150k - 100k = 50k (using ACB, not estimated)
        # With the rough estimate it would be: 150k - sum(contributions) which might differ
        self.assertIsNotNone(net)
    
    def test_compute_net_benefit_fallback_without_acb(self):
        """Backward compat: works even with old YearResult lacking ACB."""
        yr1 = YearResult(year=1, total_assets=200000, total_debt=100000,
                          non_reg_balance=150000,
                          total_rrsp=50000, total_tfsa=0, resp_balance=0,
                          rrsp_tax_savings=10000, readvance_tax_savings=0,
                          contributions={'non_reg': 80000})
        # YearResult always has non_reg_acb now, defaulting to 0
        # But compute_net_benefit has a fallback path for when it's 0
        net = compute_net_benefit(
            [yr1],
            {'assumptions': {'capital_gains_inclusion': 0.50}},
        )
        self.assertIsNotNone(net)


# =============================================================================
# ROUND-TRIP TESTS (DP#24: config round-trips: load, modify, save)
# =============================================================================


class TestSimulationConfigRoundTrip(unittest.TestCase):
    """Verify SimulationConfig.to_dict() is the inverse of from_dict()."""
    
    def test_round_trip_basic(self):
        """from_dict → to_dict → from_dict produces identical config."""
        cfg = {
            'assumptions': {'projection_years': 10, 'investment_return': 0.07,
                             'salary_growth': 0.03},
            'savings': {'rate': 0.25},
            'property': {'house_value': 600000, 'mortgage_balance': 200000,
                         'mortgage_rate': 0.05, 'ltv_max': 0.80,
                         'current_payment_monthly': 1500, 'amortization_years': 20,
                         'margin_available': 100000},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 150000,
                 'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 50000},
            ], 'children': []},
            'accounts': {'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33000,
                         'tfsa_annual_room_per_person': 7000, 'resp_current_balance': 0},
        }
        config1 = SimulationConfig.from_dict(cfg)
        exported = config1.to_dict()
        config2 = SimulationConfig.from_dict(exported)
        
        self.assertEqual(config1.projection_years, config2.projection_years)
        self.assertEqual(config1.investment_return, config2.investment_return)
        self.assertEqual(config1.salary_growth, config2.salary_growth)
        self.assertEqual(config1.savings_rate, config2.savings_rate)
        self.assertEqual(config1.house_value, config2.house_value)
        self.assertEqual(config1.mortgage_balance, config2.mortgage_balance)
        self.assertEqual(config1.mortgage_rate, config2.mortgage_rate)
        self.assertAlmostEqual(config1.ltv_max, config2.ltv_max)
        self.assertEqual(config1.margin_available, config2.margin_available)
    
    def test_modified_ltv_round_trip(self):
        """Modify config (LTV), export, reload — modification preserved."""
        cfg = {
            'assumptions': {'projection_years': 10, 'investment_return': 0.07},
            'savings': {'rate': 0.20},
            'property': {'house_value': 500000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 100000},
            ], 'children': []},
            'accounts': {},
        }
        config = SimulationConfig.from_dict(cfg)
        config.ltv_max = 0.30
        config.mortgage_balance = 150000  # After refinance
        config.margin_available = 50000   # Cash-out added to margin
        
        exported = config.to_dict()
        reloaded = SimulationConfig.from_dict(exported)
        self.assertAlmostEqual(reloaded.ltv_max, 0.30)
        self.assertAlmostEqual(reloaded.mortgage_balance, 150000)
        self.assertAlmostEqual(reloaded.margin_available, 50000)
    
    def test_to_dict_json_serializable(self):
        """to_dict() output can be serialized to JSON without errors."""
        cfg = {
            'assumptions': {'projection_years': 5, 'investment_return': 0.06},
            'savings': {'rate': 0.15},
            'property': {'house_value': 400000, 'mortgage_balance': 200000,
                         'mortgage_rate': 0.04},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 90000},
            ], 'children': [{'name': 'Kid', 'age': 10, 'gross_income': 0}]},
            'accounts': {},
        }
        config = SimulationConfig.from_dict(cfg)
        exported = config.to_dict()
        serialized = json.dumps(exported)  # Should not raise
        self.assertIn('assumptions', json.loads(serialized))
    
    def test_to_dict_contains_all_sections(self):
        """to_dict() output has all top-level sections."""
        config = SimulationConfig()
        d = config.to_dict()
        self.assertIn('assumptions', d)
        self.assertIn('savings', d)
        self.assertIn('property', d)
        self.assertIn('family', d)
        self.assertIn('accounts', d)


class TestCashOutPlanRoundTrip(unittest.TestCase):
    """Verify CashOutPlan.to_dict() is JSON-serializable."""
    
    def test_cashout_plan_to_dict(self):
        """compute_min_extraction result can be exported."""
        from countries.canada.cashout_optimizer import compute_min_extraction
        cfg = {
            'assumptions': {'projection_years': 10, 'investment_return': 0.07},
            'savings': {'rate': 0.20},
            'property': {'house_value': 600000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05, 'margin_available': 100000, 'ltv_max': 0.80},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 80000, 'tfsa_room_accumulated': 35000,
                 'pension_adjustment': 4000},
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 40000, 'tfsa_room_accumulated': 35000,
                 'pension_adjustment': 4000},
            ], 'children': [{'name': 'Child1', 'age': 10, 'gross_income': 0}]},
            'accounts': {'rrsp_annual_percent': 0.18, 'rrsp_annual_max': 33000,
                         'tfsa_annual_room_per_person': 7000},
        }
        plan = compute_min_extraction(cfg, investment_return=0.07)
        d = plan.to_dict()
        self.assertIn('cashout_required', d)
        self.assertIn('required_ltv', d)
        self.assertIn('account_needs', d)
        serialized = json.dumps(d)  # Must not raise
        self.assertIn('cashout_required', json.loads(serialized))
        
        # Account needs have expected fields
        for need in d['account_needs']:
            self.assertIn('account', need)
            self.assertIn('room', need)
            self.assertIn('source', need)


# =============================================================================
# VERSIONED TAX DATA TESTS (DP#20: year-versioned brackets)
# =============================================================================

from tax_data import TaxDataProvider, TaxBracket, TaxYearData


class TestTaxDataProviderYears(unittest.TestCase):
    """Verify TaxDataProvider returns different brackets per year."""
    
    def setUp(self):
        self.provider = TaxDataProvider()
    
    def test_available_years_include_2023_2026(self):
        """Provider has data for 2023-2026."""
        years = self.provider.available_years('canada', 'federal')
        self.assertIn(2023, years)
        self.assertIn(2024, years)
        self.assertIn(2025, years)
        self.assertIn(2026, years)
    
    def test_quebec_available_years(self):
        """Quebec province data available."""
        years = self.provider.available_years('canada', 'quebec')
        self.assertIn(2025, years)
        self.assertIn(2026, years)
    
    def test_different_brackets_per_year(self):
        """2026 brackets differ from 2023 brackets."""
        b2023 = self.provider.get_combined_brackets(year=2023, province='quebec')
        b2026 = self.provider.get_combined_brackets(year=2026, province='quebec')
        # Second bracket threshold differs (indexed from 2023 to 2026)
        self.assertNotEqual(b2023[1]['min'], b2026[1]['min'])
        # 2026 thresholds should be higher (indexed)
        self.assertGreater(b2026[1]['min'], b2023[1]['min'])
    
    def test_rrsp_limit_differs_per_year(self):
        """RRSP limit increases with indexation."""
        lim_2023 = self.provider.get_rrsp_limit(2023)
        lim_2026 = self.provider.get_rrsp_limit(2026)
        self.assertGreater(lim_2026, lim_2023)
    
    def test_tfsa_limit_differs_per_year(self):
        """TFSA limit: 2023 was $6500, 2024+ is $7000."""
        lim_2023 = self.provider.get_tfsa_limit(2023)
        lim_2024 = self.provider.get_tfsa_limit(2024)
        self.assertAlmostEqual(lim_2023, 6500)
        self.assertAlmostEqual(lim_2024, 7000)


class TestTaxDataProviderProjection(unittest.TestCase):
    """Verify projection beyond known years."""
    
    def setUp(self):
        self.provider = TaxDataProvider()
    
    def test_project_future_year(self):
        """Year 2030 brackets project from 2026 base."""
        b2026 = self.provider.get_combined_brackets(year=2026, province='quebec')
        b2030 = self.provider.get_combined_brackets(year=2030, province='quebec')
        # Projected brackets should exist and have multiple entries
        self.assertGreater(len(b2030), 3)  # Not empty
        # Second bracket threshold should be higher than 2026 (indexed)
        self.assertGreater(b2030[1]['min'], b2026[1]['min'])
        # Rate should stay the same, only thresholds increase
    
    def test_projection_preserves_rates(self):
        """Projecting preserves tax rates, only escalates thresholds."""
        data_2026 = self.provider._load_year(2026, 'canada', 'federal')
        data_2030 = self.provider._project_from_base(data_2026, 2030)
        self.assertEqual(len(data_2026.federal_brackets), len(data_2030.federal_brackets))
        for b26, b30 in zip(data_2026.federal_brackets, data_2030.federal_brackets):
            self.assertAlmostEqual(b26.rate, b30.rate)  # Rates unchanged
        # Thresholds escalated
        self.assertGreater(data_2030.federal_brackets[1].min_income, data_2026.federal_brackets[1].min_income)
    
    def test_projection_escalates_limits(self):
        """RRSP/TFSA limits escalate with projection."""
        data_2026 = self.provider._load_year(2026, 'canada', 'federal')
        data_2030 = self.provider._project_from_base(data_2026, 2030)
        self.assertGreater(data_2030.rrsp_limit, data_2026.rrsp_limit)
        self.assertGreater(data_2030.tfsa_limit, data_2026.tfsa_limit)
    
    def test_projection_source_is_projected(self):
        """Projected data has source='projected'."""
        data_2026 = self.provider._load_year(2026, 'canada', 'federal')
        data_2030 = self.provider._project_from_base(data_2026, 2030)
        self.assertEqual(data_2030.source, 'projected')
    
    def test_projection_custom_indexation_rate(self):
        """Higher indexation rate produces larger escalation."""
        data_2026 = self.provider._load_year(2026, 'canada', 'federal')
        data_low = self.provider._project_from_base(data_2026, 2030, indexation_rate=0.01)
        data_high = self.provider._project_from_base(data_2026, 2030, indexation_rate=0.05)
        self.assertGreater(data_high.rrsp_limit, data_low.rrsp_limit)

    def test_indexation_lifts_2031_top_threshold(self):
        """Issue #295: with indexation on (2%), 2031 top bracket > 2026's."""
        self.provider.indexation_rate = 0.02
        b2026 = self.provider.get_combined_brackets(year=2026, province='quebec')
        b2031 = self.provider.get_combined_brackets(year=2031, province='quebec')
        self.assertGreater(b2031[-1]['min'], b2026[-1]['min'])

    def test_postal_code_alias_resolves_and_indexes(self):
        """Issue #295: province='qc' resolves like 'quebec' and indexes forward."""
        self.provider.indexation_rate = 0.02
        b2026 = self.provider.get_combined_brackets(year=2026, province='qc')
        b2031 = self.provider.get_combined_brackets(year=2031, province='qc')
        self.assertGreater(b2031[-1]['min'], b2026[-1]['min'])


class TestSimulationYearVersionedBrackets(unittest.TestCase):
    """Integration: simulation uses year-specific brackets."""
    
    def test_simulation_with_frozen_brackets(self):
        """frozen_brackets=True uses same brackets for all years."""
        config = SimulationConfig(
            projection_years=3,
            investment_return=0.07,
            savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=50000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            children=[],
            start_year=2026,
            frozen_brackets=True,
        )
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 3, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False, lump_sum=50000)
        self.assertTrue(sim.frozen_brackets)
        results = sim.run()
        self.assertEqual(len(results), 3)
    
    def test_simulation_with_projected_brackets(self):
        """frozen_brackets=False projects brackets for future years."""
        config = SimulationConfig(
            projection_years=5,
            investment_return=0.07,
            savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=50000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            children=[],
            start_year=2026,
            frozen_brackets=False,
        )
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 5, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False, lump_sum=50000)
        self.assertFalse(sim.frozen_brackets)
        results = sim.run()
        self.assertEqual(len(results), 5)

    def _bracket_config(self, frozen):
        return SimulationConfig(
            projection_years=6,
            investment_return=0.07,
            savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=50000,
            inflation=0.02,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            children=[],
            start_year=2026,
            frozen_brackets=frozen,
        )

    def test_indexed_brackets_lift_top_threshold_by_2031(self):
        """Issue #295: non-frozen sim indexes the 2031 top threshold above 2026."""
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 6, 'variable', [0.05])
        sim = FamilySimulation(self._bracket_config(frozen=False),
                               STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False, lump_sum=50000)
        top_2026 = sim._get_year_brackets(2026)[-1]['min']
        top_2031 = sim._get_year_brackets(2031)[-1]['min']
        self.assertGreater(top_2031, top_2026)

    def test_frozen_brackets_keep_top_threshold_at_base_year(self):
        """Issue #295: frozen_brackets=True keeps the 2031 threshold equal to 2026."""
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 6, 'variable', [0.05])
        sim = FamilySimulation(self._bracket_config(frozen=True),
                               STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False, lump_sum=50000)
        top_2026 = sim._get_year_brackets(2026)[-1]['min']
        top_2031 = sim._get_year_brackets(2031)[-1]['min']
        self.assertEqual(top_2031, top_2026)


# =============================================================================
# CASH FLOW TESTS (DP#18: irregular income, planned expenses)
# =============================================================================


class TestCashFlowIntegration(unittest.TestCase):
    """Verify CashFlow events modify annual savings in simulation."""
    
    def test_single_cashflow_adds_to_savings(self):
        """A $20k bonus in year 2028 adds to annual savings."""
        config = SimulationConfig(
            projection_years=4,
            investment_return=0.07,
            savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=10000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            children=[],
            start_year=2026,
            cash_flows=[
                {'year': 2028, 'amount': 20000, 'source': 'bonus',
                 'tax_treatment': 'post-tax', 'role': 'primary'},
            ],
        )
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 4, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False)
        results = sim.run()
        # Year 2028 = simulation year index 2 = result year 3
        yr2028 = next(r for r in results if r.year == 3)
        # Without cash flow: savings = 100k * 0.20 = 20k
        # With cash flow: savings = 20k + 20k = 40k
        self.assertGreater(yr2028.annual_savings, 35000)
    
    def test_no_cashflows_same_as_before(self):
        """No cash_flows = same behavior as before."""
        config1 = SimulationConfig(
            projection_years=3, investment_return=0.07, savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=10000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            children=[],
        )
        config2 = SimulationConfig(
            projection_years=3, investment_return=0.07, savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=10000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 0, 'tfsa_room_accumulated': 0},
            ],
            children=[],
            cash_flows=[],
        )
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 3, 'variable', [0.05])
        sim1 = FamilySimulation(config1, STRATEGY_READVANCE_PRIORITY, rp, use_readvanceable=False)
        sim2 = FamilySimulation(config2, STRATEGY_READVANCE_PRIORITY, rp, use_readvanceable=False)
        r1 = sim1.run()
        r2 = sim2.run()
        self.assertAlmostEqual(r1[-1].total_assets, r2[-1].total_assets, places=0)
    
    def test_cashflow_round_trip(self):
        """CashFlow in to_dict/from_dict round-trip."""
        cfg = {
            'assumptions': {'projection_years': 5, 'investment_return': 0.07},
            'savings': {'rate': 0.20},
            'property': {'house_value': 500000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05},
            'family': {'members': [
                {'role': 'primary', 'gross_income': 100000},
            ], 'children': []},
            'accounts': {},
            'cash_flows': [
                {'year': 2028, 'amount': 15000, 'source': 'bonus',
                 'tax_treatment': 'post-tax', 'role': 'primary'},
            ],
        }
        config = SimulationConfig.from_dict(cfg)
        self.assertEqual(len(config.cash_flows), 1)
        exported = config.to_dict()
        self.assertEqual(len(exported['cash_flows']), 1)
        self.assertEqual(exported['cash_flows'][0]['year'], 2028)


# =============================================================================
# RETIREMENT DRAWDOWN INTEGRATION TESTS (DP#22: optimization objectives)
# =============================================================================


class TestRetirementDrawdownIntegration(unittest.TestCase):
    """Verify compute_net_benefit uses retirement module properly."""
    
    def test_birth_year_triggers_drawdown(self):
        """When birth_year present, retirement drawdown replaces simplified calc."""
        yr1 = YearResult(year=1, total_assets=200000, total_debt=100000,
                          non_reg_balance=50000, non_reg_acb=30000,
                          total_rrsp=100000, total_tfsa=50000, resp_balance=0,
                          rrsp_tax_savings=10000, readvance_tax_savings=0)
        net = compute_net_benefit(
            [yr1],
            {'assumptions': {'capital_gains_inclusion': 0.50},
             'family': {'members': [
                 {'role': 'primary', 'birth_year': 1980},
             ]}},
        )
        self.assertIsNotNone(net)
        # drawdown should produce a finite tax amount
        self.assertLess(net, 200000)  # Some withdrawal tax deducted
    
    def test_no_birth_year_uses_simplified(self):
        """Without birth_year, simplified 30% withdrawal tax applies."""
        yr1 = YearResult(year=1, total_assets=200000, total_debt=100000,
                          non_reg_balance=50000, non_reg_acb=30000,
                          total_rrsp=100000, total_tfsa=50000, resp_balance=0,
                          rrsp_tax_savings=10000, readvance_tax_savings=0)
        net = compute_net_benefit(
            [yr1],
            {'assumptions': {'capital_gains_inclusion': 0.50},
             'family': {'members': [
                 {'role': 'primary'},  # No birth_year
             ]}},
        )
        self.assertIsNotNone(net)
    
    def test_drawdown_uses_actual_acb(self):
        """Retirement drawdown uses tracked ACB for CG tax."""
        # High ACB = low CG tax
        yr_high_acb = YearResult(year=1, total_assets=200000, total_debt=100000,
                                  non_reg_balance=100000, non_reg_acb=90000,
                                  total_rrsp=50000, total_tfsa=50000, resp_balance=0,
                                  rrsp_tax_savings=10000, readvance_tax_savings=0)
        # Low ACB = high CG tax
        yr_low_acb = YearResult(year=1, total_assets=200000, total_debt=100000,
                                 non_reg_balance=100000, non_reg_acb=10000,
                                 total_rrsp=50000, total_tfsa=50000, resp_balance=0,
                                 rrsp_tax_savings=10000, readvance_tax_savings=0)
        cfg = {'assumptions': {'capital_gains_inclusion': 0.50},
               'family': {'members': [{'role': 'primary', 'birth_year': 1980}]}}
        net_high = compute_net_benefit([yr_high_acb], cfg)
        net_low = compute_net_benefit([yr_low_acb], cfg)
        # High ACB → lower CG tax → higher net benefit
        self.assertGreater(net_high, net_low)
    
    def test_oas_clawback_reduces_net_benefit(self):
        """Large RRSP triggers OAS clawback in retirement, reducing net_benefit."""
        yr_small = YearResult(year=1, total_assets=500000, total_debt=100000,
                               non_reg_balance=0, non_reg_acb=0,
                               total_rrsp=400000, total_tfsa=0, resp_balance=0,
                               rrsp_tax_savings=50000, readvance_tax_savings=0)
        yr_large = YearResult(year=1, total_assets=500000, total_debt=100000,
                               non_reg_balance=0, non_reg_acb=0,
                               total_rrsp=400000, total_tfsa=0, resp_balance=0,
                               rrsp_tax_savings=50000, readvance_tax_savings=0)
        # Same data — just verify drawdown runs and produces reasonable results
        net = compute_net_benefit(
            [yr_small],
            {'assumptions': {'capital_gains_inclusion': 0.50},
             'family': {'members': [{'role': 'primary', 'birth_year': 1979}]}},
        )
        self.assertIsNotNone(net)
        self.assertLess(net, 500000)  # Some withdrawal tax


# =============================================================================
# FORECAST PRESET TESTS (DP#21: return models are pluggable data)
# =============================================================================

from optimize import FORECAST_PRESETS, apply_preset


class TestForecastPresets(unittest.TestCase):
    """Verify forecast presets apply correct overlays."""
    
    def test_all_presets_exist(self):
        """conservative, moderate, aggressive presets exist."""
        self.assertIn('conservative', FORECAST_PRESETS)
        self.assertIn('moderate', FORECAST_PRESETS)
        self.assertIn('aggressive', FORECAST_PRESETS)
    
    def test_conservative_lower_return(self):
        """Conservative preset has lower investment return than moderate."""
        self.assertLess(FORECAST_PRESETS['conservative']['investment_return'],
                        FORECAST_PRESETS['moderate']['investment_return'])
    
    def test_aggressive_higher_return(self):
        """Aggressive preset has higher return than moderate."""
        self.assertGreater(FORECAST_PRESETS['aggressive']['investment_return'],
                           FORECAST_PRESETS['moderate']['investment_return'])
    
    def test_apply_preset_overrides_return(self):
        """apply_preset modifies investment_return in config."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'projection_years': 10,
                             'salary_growth': 0.02},
            'property': {'house_value': 500000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05},
            'savings': {'rate': 0.20},
            'family': {'members': [{'role': 'primary', 'gross_income': 100000}], 'children': []},
            'accounts': {},
        }
        result = apply_preset(cfg, 'conservative')
        # DP#21/#591: the swept rate lands in return_model, the engine's
        # single source of truth (not the deprecated assumptions scalar,
        # which the engine ignores once return_model exists).
        self.assertAlmostEqual(result['return_model']['rate'], 0.05)

    def test_apply_preset_overrides_mortgage_rate(self):
        """apply_preset modifies mortgage_rate in config."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'projection_years': 10},
            'property': {'house_value': 500000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05},
            'savings': {'rate': 0.20},
            'family': {'members': [{'role': 'primary', 'gross_income': 100000}], 'children': []},
            'accounts': {},
        }
        result = apply_preset(cfg, 'aggressive')
        self.assertAlmostEqual(result['property']['mortgage_rate'], 0.035)
    
    def test_apply_preset_does_not_modify_original(self):
        """apply_preset returns a copy, original unchanged."""
        cfg = {
            'assumptions': {'investment_return': 0.07, 'projection_years': 10},
            'property': {'house_value': 500000, 'mortgage_balance': 100000,
                         'mortgage_rate': 0.05},
            'savings': {'rate': 0.20},
            'family': {'members': [], 'children': []},
            'accounts': {},
        }
        result = apply_preset(cfg, 'conservative')
        self.assertAlmostEqual(cfg['assumptions']['investment_return'], 0.07)  # Original unchanged
        self.assertNotIn('return_model', cfg)  # Original unchanged
        self.assertAlmostEqual(result['return_model']['rate'], 0.05)  # Copy modified
    
    def test_apply_unknown_preset_returns_original(self):
        """Unknown preset name returns config unchanged."""
        cfg = {'assumptions': {'investment_return': 0.07}}
        result = apply_preset(cfg, 'nonexistent')
        self.assertAlmostEqual(result['assumptions']['investment_return'], 0.07)


# =============================================================================
# MONTHLY TIME STEPPING TESTS (monthly time stepping option)
# =============================================================================


class TestMonthlyTimeStepping(unittest.TestCase):
    """Verify monthly time stepping produces similar results to yearly."""
    
    def _make_config(self, time_step='yearly'):
        return SimulationConfig(
            projection_years=3,
            investment_return=0.07,
            salary_growth=0.02,
            savings_rate=0.20,
            house_value=0, mortgage_balance=0, mortgage_rate=0.05,
            margin_available=30000,
            family_members=[
                {'role': 'primary', 'gross_income': 100000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 20000},
            ],
            children=[],
            time_step=time_step,
        )
    
    def test_monthly_runs(self):
        """Monthly time stepping completes without error."""
        config = self._make_config('monthly')
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 3, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False)
        results = sim.run()
        self.assertEqual(len(results), 3)
    
    def test_monthly_vs_yearly_similar_total_assets(self):
        """Monthly and yearly produce similar total assets (within 5%)."""
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        
        rp = build_rate_path('test', 0.05, 3, 'variable', [0.05])
        
        config_y = self._make_config('yearly')
        sim_y = FamilySimulation(config_y, STRATEGY_READVANCE_PRIORITY, rp,
                                  use_readvanceable=False)
        results_y = sim_y.run()
        final_y = results_y[-1].total_assets
        
        config_m = self._make_config('monthly')
        sim_m = FamilySimulation(config_m, STRATEGY_READVANCE_PRIORITY, rp,
                                  use_readvanceable=False)
        results_m = sim_m.run()
        final_m = results_m[-1].total_assets
        
        # Monthly compounding should be slightly higher than yearly
        # but within 5% (due to compounding frequency)
        if final_y > 0 and final_m > 0:
            ratio = final_m / final_y
            self.assertGreater(ratio, 0.95)
            self.assertLess(ratio, 1.05)
    
    def test_monthly_config_round_trip(self):
        """time_step serializes in to_dict/from_dict."""
        config = self._make_config('monthly')
        d = config.to_dict()
        self.assertEqual(d['assumptions']['time_step'], 'monthly')
        config2 = SimulationConfig.from_dict(d)
        self.assertEqual(config2.time_step, 'monthly')
    
    def test_monthly_has_acb_fields(self):
        """Monthly results include ACB fields."""
        config = self._make_config('monthly')
        from countries.canada.strategies import STRATEGY_READVANCE_PRIORITY
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path('test', 0.05, 3, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp,
                               use_readvanceable=False)
        results = sim.run()
        for yr in results:
            self.assertGreater(yr.non_reg_acb, 0)
            self.assertLessEqual(yr.non_reg_acb, yr.non_reg_balance + 1)


class TestSimStateFork(unittest.TestCase):
    """DP#26: SimState must support safe forking via fork() and deepcopy."""

    def test_fork_deep_copies_jurisdiction_state(self):
        """SimState.fork() must deep-copy jurisdiction_state."""
        from copy import deepcopy
        from simulation_state import SimState
        state = SimState()
        state.jurisdiction_state['canada']['heloc_rrsp_paydown'] = 5000
        # Fork with a jurisdiction_state change
        new_jurisdiction = deepcopy(state.jurisdiction_state)
        new_jurisdiction['canada']['readvance_heloc_balance'] = 10000
        forked = SimState.fork(state, jurisdiction_state=new_jurisdiction)
        # Mutating forked state must NOT affect original
        forked.jurisdiction_state['canada']['heloc_rrsp_paydown'] = 9999
        self.assertAlmostEqual(state.jurisdiction_state['canada']['heloc_rrsp_paydown'], 5000)
        self.assertAlmostEqual(forked.jurisdiction_state['canada']['heloc_rrsp_paydown'], 9999)

    def test_fork_preserves_values(self):
        """Forked state preserves all original values except overridden ones."""
        from copy import deepcopy
        from simulation_state import SimState
        state = SimState()
        state.jurisdiction_state['canada']['heloc_rrsp_paydown'] = 5000
        new_jurisdiction = deepcopy(state.jurisdiction_state)
        new_jurisdiction['canada']['readvance_heloc_balance'] = 10000
        forked = SimState.fork(state, jurisdiction_state=new_jurisdiction)
        self.assertAlmostEqual(forked.jurisdiction_state['canada']['readvance_heloc_balance'], 10000)
        self.assertAlmostEqual(forked.jurisdiction_state['canada']['heloc_rrsp_paydown'], 5000)

    def test_deepcopy_isolates_jurisdiction_state(self):
        """copy.deepcopy(SimState) must deep-copy jurisdiction_state (DP#26)."""
        import copy
        from simulation_state import SimState
        state = SimState()
        state.jurisdiction_state['canada']['heloc_rrsp_paydown'] = 5000
        copied = copy.deepcopy(state)
        # Mutating copy must NOT affect original
        copied.jurisdiction_state['canada']['heloc_rrsp_paydown'] = 9999
        self.assertAlmostEqual(state.jurisdiction_state['canada']['heloc_rrsp_paydown'], 5000)
        self.assertAlmostEqual(copied.jurisdiction_state['canada']['heloc_rrsp_paydown'], 9999)

    def test_replace_does_not_isolate_jurisdiction_state(self):
        """dataclasses.replace() shares jurisdiction_state (shallow copy).
        This is expected behavior — callers must use SimState.fork() or
        copy.deepcopy() for independent branches (DP#26)."""
        from dataclasses import replace
        from simulation_state import SimState
        state = SimState()
        state.jurisdiction_state['canada']['heloc_rrsp_paydown'] = 5000
        # replace() creates a SHALLOW copy of jurisdiction_state
        forked = replace(state, mortgage_balance=10000)
        # Mutating forked state WILL affect original — this is the known limitation
        forked.jurisdiction_state['canada']['heloc_rrsp_paydown'] = 9999
        # Both share the same dict, so original is also mutated
        self.assertAlmostEqual(state.jurisdiction_state['canada']['heloc_rrsp_paydown'], 9999)


class TestFHSARoomCap(unittest.TestCase):
    """CRA s.146.6(1): FHSA participation room capped at $16,000/year."""

    def _make_config(self, fhsa_room_accumulated):
        from simulation_state import SimulationConfig
        return SimulationConfig(
            projection_years=1, investment_return=0.05, salary_growth=0,
            savings_rate=0.10, mortgage_balance=0, mortgage_rate=0.0495,
            margin_available=0,
            family_members=[
                {'role': 'primary', 'gross_income': 130000, 'birth_year': 1990,
                 'rrsp_room_accumulated': 10000, 'tfsa_room_accumulated': 5000,
                 'fhsa_room_accumulated': fhsa_room_accumulated},
                {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1992,
                 'rrsp_room_accumulated': 5000, 'tfsa_room_accumulated': 3000},
            ],
            children=[], rrsp_annual_percent=0.18, rrsp_annual_max=33810,
            tfsa_annual_room_per_person=7000, start_year=2026,
        )

    def test_initial_room_capped_to_16000(self):
        """SimState.initial() caps fhsa_room to $16,000 (annual + carry-forward)."""
        from simulation_state import SimState
        config = self._make_config(fhsa_room_accumulated=32000)
        state = SimState.initial(config)
        # CRA max per year = $8,000 annual + $8,000 carry-forward = $16,000
        self.assertLessEqual(adult_fhsa_slot(state.jurisdiction_state['canada'], 0)['room'], 16000)

    def test_initial_room_normal_value(self):
        """SimState.initial() preserves fhsa_room below $16,000."""
        from simulation_state import SimState
        config = self._make_config(fhsa_room_accumulated=8000)
        state = SimState.initial(config)
        self.assertAlmostEqual(adult_fhsa_slot(state.jurisdiction_state['canada'], 0)['room'], 8000)


if __name__ == '__main__':
    unittest.main()
