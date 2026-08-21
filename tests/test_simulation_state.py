#!/usr/bin/env python3
"""Unit tests for simulation_state.py — SimState and simulate_year_pure (DP#26).

Tests verify:
- SimState.initial() creates correct state from config
- simulate_year_pure is a pure function (same inputs → same outputs)
- State forking via dataclasses.replace works
- Fold over simulate_year produces multi-year projections
- HELOC tracing proportion is correct
- Quebec deduction carry-forward is tracked
- SM tax savings depend on deductible proportion
- No mutation of input state
- Edge cases: zero room, zero savings, zero balance
- investment_return default is None, raises ValueError if omitted (issue #28)

Issue #25: All Canada-specific fields are accessed through
jurisdiction_state['canada'] dict, not top-level SimState attributes.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from dataclasses import replace
from copy import deepcopy

from simulation_state import (
    SimState, simulate_year_pure,
    compute_heloc_deductible_proportion,
    adult_rrsp_slot, adult_rrsp_total,  # #700: per-adult RRSP store
    adult_tfsa_slot, adult_tfsa_total,  # #700: per-adult TFSA store
    adult_fhsa_slot,  # #700/#643/#704: per-adult FHSA store
)
from simulation import SimulationConfig, YearResult


def _make_config():
    """Build a test config with fabricated round numbers (DP#4)."""
    return SimulationConfig(
        projection_years=5,
        investment_return=0.07,
        salary_growth=0.02,
        savings_rate=0.20,
        house_value=500000,
        mortgage_balance=200000,
        mortgage_rate=0.05,
        ltv_max=0.80,
        margin_available=100000,
        family_members=[
            {'role': 'primary', 'gross_income': 120000,
             'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000},
            {'role': 'spouse', 'gross_income': 50000,
             'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 30000},
        ],
        children=[{'name': 'Kid', 'age': 10, 'gross_income': 0}],
        rrsp_annual_percent=0.18,
        rrsp_annual_max=33000,
        tfsa_annual_room_per_person=7000,
    )


def _base_allocs(year=0):
    """Fabricated allocations (DP#4: role-based, round numbers)."""
    return {
        'primary_rrsp': 10000, 'spousal_rrsp': 5000,
        'primary_tfsa': 5000, 'spouse_tfsa': 3000,
        'resp': 2500, 'non_reg': 5000,
        '_primary_income': 120000, '_spouse_income': 50000,
        '_annual_savings': 34000,
    }


def _mort_data(state, principal=10000):
    """Fabricated mortgage data."""
    return {
        'end_balance': max(0, state.mortgage_balance - principal),
        'total_payment': 14000, 'total_interest': 10000,
        'total_principal': principal,
    }


# Helper to access Canada fields through jurisdiction_state (issue #25)
def _c(state, key, default=0):
    """Read a Canada-specific field from jurisdiction_state['canada']."""
    return state.jurisdiction_state.get('canada', {}).get(key, default)


def _rrsp_total(state):
    """#700: household RRSP fair-market value from the per-adult store."""
    return adult_rrsp_total(state.jurisdiction_state.get('canada', {}))


def _rrsp_room(state, index=0):
    """#700: the index-th adult's own RRSP room from the per-adult store."""
    return adult_rrsp_slot(state.jurisdiction_state.get('canada', {}), index)[1]


def _tfsa_total(state):
    """#700: household TFSA fair-market value from the per-adult store."""
    return adult_tfsa_total(state.jurisdiction_state.get('canada', {}))


def _tfsa_room(state, index=0):
    """#700: the index-th adult's TFSA room from the per-adult store."""
    return adult_tfsa_slot(state.jurisdiction_state.get('canada', {}), index)[1]


# ── SimState.initial ──────────────────────────────────────────────────────

class TestSimStateInitial(unittest.TestCase):
    def test_reads_rrsp_room(self):
        state = SimState.initial(_make_config())
        self.assertEqual(_rrsp_room(state), 100000)
    
    def test_reads_tfsa_room(self):
        state = SimState.initial(_make_config())
        self.assertEqual(_tfsa_room(state, 0), 30000)
        self.assertEqual(_tfsa_room(state, 1), 30000)
    
    def test_mortgage_balance(self):
        state = SimState.initial(_make_config())
        self.assertEqual(state.mortgage_balance, 200000)
    
    def test_resp_balances_per_child(self):
        state = SimState.initial(_make_config())
        self.assertEqual(len(_c(state, 'resp_balances', [])), 1)
        self.assertEqual(_c(state, 'resp_balances', [0])[0], 0)
    
    def test_zero_initial_balances(self):
        state = SimState.initial(_make_config())
        self.assertEqual(_rrsp_total(state), 0)
        self.assertEqual(_tfsa_total(state), 0)
        self.assertEqual(state.non_reg_balance, 0)
    
    def test_heloc_tracing_initially_zero(self):
        state = SimState.initial(_make_config())
        tracing = _c(state, 'heloc_tracing', {})
        self.assertEqual(tracing.get('total_advances', 0), 0)
    
    def test_qc_deduction_initially_zero(self):
        state = SimState.initial(_make_config())
        self.assertEqual(state.jurisdiction_state['canada']['qc_carry_forward'], 0)
    
    # ── Debt tracking (issue #577: margin_available is availability, not a
    # balance owed — heloc_balance stays 0 at cold SimState.initial()
    # regardless of margin size, since nothing has been drawn yet. A real
    # draw is booked by FamilySimulation.__init__ when the caller actually
    # invests the margin via lump_sum — see
    # test_strategy_simulation.py::TestDebtTracking and
    # tests/test_issue_577_undrawn_heloc_margin.py) ──

    def test_heloc_stays_undrawn_when_margin_available(self):
        """SimState.initial must NOT book margin_available as debt (#577):
        an undrawn HELOC limit is availability, not a balance owed."""
        state = SimState.initial(_make_config())
        self.assertEqual(state.heloc_balance, 0)

    def test_heloc_zero_regardless_of_margin_size(self):
        """Any positive margin_available must still produce zero heloc_balance
        at cold SimState.initial() (#577) — the size of the credit *limit*
        has no bearing on the drawn *balance*, which is 0 until something
        actually draws it."""
        cfg = _make_config()
        cfg.margin_available = 250000
        state = SimState.initial(cfg)
        self.assertEqual(state.heloc_balance, 0)

    def test_heloc_zero_when_no_margin(self):
        """Zero margin → zero heloc (edge case; both sides of the #577
        threshold collapse to the same correct answer: no limit, no draw,
        no debt, either way)."""
        cfg = _make_config()
        cfg.margin_available = 0
        state = SimState.initial(cfg)
        self.assertEqual(state.heloc_balance, 0)
    
    def test_total_debt_includes_margin_heloc(self):
        """total_debt() must include the initial margin HELOC."""
        state = SimState.initial(_make_config())
        expected = state.mortgage_balance + state.heloc_balance + _c(state, 'readvance_heloc_balance')
        self.assertEqual(state.total_debt(), expected)
    
    def test_total_debt_with_sm_heloc(self):
        """total_debt includes all three components."""
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        canada['readvance_heloc_balance'] = 25000
        expected = state.mortgage_balance + state.heloc_balance + canada['readvance_heloc_balance']
        self.assertEqual(state.total_debt(), expected)


# ── Pure function property ────────────────────────────────────────────────

class TestSimulateYearPure(unittest.TestCase):
    def test_same_inputs_same_outputs(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        mort = _mort_data(state)
        
        r1, s1 = simulate_year_pure(state, 0, allocs, cfg, investment_return=0.07, mortgage_data=mort)
        r2, s2 = simulate_year_pure(state, 0, allocs, cfg, investment_return=0.07, mortgage_data=mort)
        
        self.assertEqual(r1.total_assets, r2.total_assets)
        self.assertEqual(_rrsp_total(s1), _rrsp_total(s2))
    
    def test_no_mutation_of_input_state(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        original_room = _rrsp_room(state)
        
        simulate_year_pure(state, 0, _base_allocs(), cfg, investment_return=0.07, mortgage_data=_mort_data(state))
        
        self.assertEqual(_rrsp_room(state), original_room)


# ── State forking ──────────────────────────────────────────────────────────

class TestStateForking(unittest.TestCase):
    def test_fork_with_replace(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        forked = replace(state, mortgage_balance=100000)
        
        self.assertEqual(forked.mortgage_balance, 100000)
        self.assertEqual(state.mortgage_balance, 200000)
    
    def test_fork_independent_evolution(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        forked = replace(state, mortgage_balance=100000)
        
        _, state2 = simulate_year_pure(state, 0, _base_allocs(), cfg, investment_return=0.07, 
                                        mortgage_data=_mort_data(state))
        _, forked2 = simulate_year_pure(forked, 0, _base_allocs(), cfg, investment_return=0.07,
                                         mortgage_data=_mort_data(forked, principal=5000))
        
        self.assertNotEqual(state2.mortgage_balance, forked2.mortgage_balance)


# ── HELOC tracing ──────────────────────────────────────────────────────────

class TestHelocTracing(unittest.TestCase):
    def test_investment_only_100pct_deductible(self):
        tracing = {'total_advances': 100000, 'investment_advances': 100000}
        self.assertAlmostEqual(compute_heloc_deductible_proportion(tracing), 1.0)
    
    def test_mixed_advances_partial_deductible(self):
        tracing = {
            'total_advances': 100000,
            'investment_advances': 36000,
            'rrsp_advances': 50000,
            'tfsa_advances': 14000,
        }
        self.assertAlmostEqual(compute_heloc_deductible_proportion(tracing), 0.36)
    
    def test_no_investment_zero_deductible(self):
        tracing = {
            'total_advances': 100000,
            'investment_advances': 0,
            'rrsp_advances': 70000,
            'tfsa_advances': 30000,
        }
        self.assertAlmostEqual(compute_heloc_deductible_proportion(tracing), 0.0)
    
    def test_zero_advances_zero_deductible(self):
        tracing = {}
        self.assertAlmostEqual(compute_heloc_deductible_proportion(tracing), 0.0)

    def test_positive_yield_keeps_purpose_proportion(self):
        """Issue #549: a positive yield satisfies the §20(1)(c) income test,
        so the proportion is still the purpose-traced fraction."""
        tracing = {'total_advances': 100000, 'investment_advances': 100000}
        self.assertAlmostEqual(
            compute_heloc_deductible_proportion(tracing, yield_rate=0.02), 1.0)

    def test_zero_yield_zero_deductible(self):
        """Issue #549: zero yield fails the §20(1)(c) income-producing test —
        federal deductible proportion collapses to 0 even with full investment
        tracing."""
        tracing = {'total_advances': 100000, 'investment_advances': 100000}
        self.assertAlmostEqual(
            compute_heloc_deductible_proportion(tracing, yield_rate=0.0), 0.0)
        self.assertAlmostEqual(
            compute_heloc_deductible_proportion(tracing, yield_rate=-0.01), 0.0)
    
    def test_tracing_updated_by_simulate_year(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = {'non_reg': 10000, '_primary_income': 120000, '_spouse_income': 50000, '_annual_savings': 34000}
        _, new_state = simulate_year_pure(state, 0, allocs, cfg, investment_return=0.07, mortgage_data=_mort_data(state))
        
        tracing = _c(new_state, 'heloc_tracing', {})
        self.assertEqual(tracing.get('investment_advances', 0), 10000)
        self.assertEqual(tracing.get('total_advances', 0), 10000)


# ── Smith Manoeuvre ────────────────────────────────────────────────────────

class TestSmithManoeuvre(unittest.TestCase):
    def test_sm_readvances_principal(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        mort = _mort_data(state, principal=10000)
        
        _, new_state = simulate_year_pure(
            state, 0, allocs, cfg, investment_return=0.07, use_readvanceable=True,
            mortgage_data=mort, mortgage_rate=0.05, heloc_rate=0.055,
        )
        
        self.assertGreater(_c(new_state, 'readvance_heloc_balance'), 0)
        self.assertGreater(_c(new_state, 'sm_investment_balance'), 0)
    
    def test_sm_no_smith_zero_heloc(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        
        _, new_state = simulate_year_pure(state, 0, allocs, cfg, investment_return=0.07, use_readvanceable=False)

        self.assertEqual(_c(new_state, 'readvance_heloc_balance'), 0)
        self.assertEqual(_c(new_state, 'sm_investment_balance'), 0)

    def _run_sm_year(self, yield_rate):
        cfg = replace(_make_config(), non_reg_yield_rate=yield_rate)
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        mort = _mort_data(state, principal=10000)
        result, _ = simulate_year_pure(
            state, 0, allocs, cfg, investment_return=0.07, use_readvanceable=True,
            mortgage_data=mort, mortgage_rate=0.05, heloc_rate=0.055,
        )
        return result

    def test_yielding_holding_is_deductible(self):
        """Issue #549: an income-producing SM holding qualifies federally
        (positive deductible proportion) and in QC (positive quantum)."""
        r = self._run_sm_year(yield_rate=0.02)
        self.assertGreater(r.sm_deductible_proportion, 0)
        self.assertGreater(r.sm_qc_deductible, 0)

    def test_zero_yield_holding_not_deductible_fed_and_qc(self):
        """Issue #549: a zero-yield SM holding produces ZERO deductible interest
        BOTH federally (sm_deductible_proportion → 0) and in QC (sm_qc_deductible
        → 0). Province symmetry: the §20(1)(c) income-producing test gates the
        federal qualification, not just the QC quantum limit."""
        r = self._run_sm_year(yield_rate=0.0)
        # Federal §20(1)(c) qualification fails: zero deductible proportion.
        self.assertEqual(r.sm_deductible_proportion, 0.0)
        # Quebec quantum limit is also zero (no investment income).
        self.assertEqual(r.sm_qc_deductible, 0.0)
        # No SM tax savings at all when nothing is deductible.
        self.assertEqual(r.readvance_tax_savings, 0.0)


# ── Multi-year fold ─────────────────────────────────────────────────────

class TestMultiYearFold(unittest.TestCase):
    def test_five_year_projection(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        results = []
        
        for year in range(5):
            allocs = _base_allocs(year)
            result, state = simulate_year_pure(
                state, year, allocs, cfg, investment_return=0.07,
                mortgage_rate=0.05, mortgage_data=_mort_data(state),
            )
            results.append(result)
        
        self.assertEqual(len(results), 5)
        for i in range(1, len(results)):
            self.assertGreater(results[i].total_assets, results[i-1].total_assets)
    
    def test_debt_decreases_over_time(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        results = []
        
        for year in range(5):
            allocs = _base_allocs(year)
            result, state = simulate_year_pure(
                state, year, allocs, cfg, investment_return=0.07,
                mortgage_rate=0.05, mortgage_data=_mort_data(state),
            )
            results.append(result)
        
        self.assertLess(results[-1].mortgage_balance, results[0].mortgage_balance)


# ── Edge cases ────────────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):
    def test_zero_room_no_contribution(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        canada = state.jurisdiction_state['canada']
        canada['adult_rrsp']['primary']['own_room'] = 0  # #700
        canada['adult_tfsa']['primary']['room'] = 0  # #700
        allocs = {'primary_rrsp': 10000, '_primary_income': 120000, 
                  '_spouse_income': 50000, '_annual_savings': 34000}
        
        _, new_state = simulate_year_pure(state, 0, allocs, cfg, investment_return=0.07, 
                                           mortgage_data=_mort_data(state))
        
        self.assertEqual(_rrsp_total(new_state), 0)
    
    def test_zero_savings(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = {'_primary_income': 120000, '_spouse_income': 50000, '_annual_savings': 0}
        
        result, _ = simulate_year_pure(state, 0, allocs, cfg, investment_return=0.07, mortgage_data=_mort_data(state))
        
        self.assertEqual(result.rrsp_tax_savings, 0)


if __name__ == '__main__':
    unittest.main()

# ── FHSA contribution and room tracking ───────────────────────────────────

def _fhsa_state(cfg=None, fhsa_room=8000, fhsa_balance=0, fhsa_lifetime_used=0, fhsa_lifetime_limit=40000):
    """Create a SimState with FHSA fields set via jurisdiction_state (DP#9/DP#25).

    #700/#643/#704: FHSA is a per-adult store keyed by adult id (slot 0 =
    primary drives the compute), not flat canada scalars."""
    if cfg is None:
        cfg = _make_config()
    state = SimState.initial(cfg)
    canada = state.jurisdiction_state.get('canada')
    if isinstance(canada, dict):
        pid = next(iter(canada.get('adult_fhsa', {'primary': None})), 'primary')
        canada['adult_fhsa'] = {pid: {
            'balance': fhsa_balance, 'room': fhsa_room,
            'lifetime_used': fhsa_lifetime_used, 'lifetime_limit': fhsa_lifetime_limit,
        }}
    return state


# #700/#643/#704: read an FHSA field from the per-adult store's slot 0 (the
# account the single-slot compute drives), mapping the old flat field names.
_FHSA_FIELD = {'fhsa_balance': 'balance', 'fhsa_room': 'room',
               'fhsa_lifetime_used': 'lifetime_used',
               'fhsa_lifetime_limit': 'lifetime_limit'}


def _cf(state, flat_key):
    canada = state.jurisdiction_state.get('canada', {})
    return adult_fhsa_slot(canada, 0)[_FHSA_FIELD[flat_key]]


class TestFHSASimulation(unittest.TestCase):
    """Test FHSA contributions flowing through simulate_year_pure (issue #124)."""

    def test_fhsa_contribution_flows_into_balance(self):
        state = _fhsa_state(fhsa_room=8000)
        allocs = {
            'fhsa': 8000,
            '_primary_income': 120000,
            '_spouse_income': 50000,
            '_annual_savings': 34000,
        }
        result, new_state = simulate_year_pure(
            state, 0, allocs, _make_config(), investment_return=0.07,
            fhsa_contribution=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertGreater(_cf(new_state, 'fhsa_balance'), 0)
        self.assertLess(_cf(new_state, 'fhsa_room'), 8000)

    def test_fhsa_contribution_clamped_to_room(self):
        state = _fhsa_state(fhsa_room=3000)
        result, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=8000, mortgage_data=_mort_data(state),
        )
        self.assertAlmostEqual(_cf(new_state, 'fhsa_room'), 0)

    def test_fhsa_contribution_in_total_assets(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_balance=0)
        allocs = {'fhsa': 8000, '_primary_income': 120000, '_spouse_income': 50000}
        result, new_state = simulate_year_pure(
            state, 0, allocs, _make_config(), investment_return=0.07,
            fhsa_contribution=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertGreaterEqual(result.total_assets, _cf(new_state, 'fhsa_balance'))

    def test_fhsa_annual_room_added(self):
        state = _fhsa_state(fhsa_room=0, fhsa_balance=0)
        result, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=0, fhsa_annual_limit=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertEqual(_cf(new_state, 'fhsa_room'), 8000)

    def test_fhsa_annual_limit_none_no_room_added(self):
        state = _fhsa_state(fhsa_room=0, fhsa_balance=0)
        result, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=0, fhsa_annual_limit=None,
            mortgage_data=_mort_data(state),
        )
        self.assertEqual(_cf(new_state, 'fhsa_room'), 0)

    def test_fhsa_carry_forward_exact_value(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_balance=0)
        _, state_y1 = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=0, fhsa_annual_limit=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertEqual(_cf(state_y1, 'fhsa_room'), 16000)

    def test_fhsa_carry_forward_capped_at_one_year(self):
        state = _fhsa_state(fhsa_room=12000, fhsa_balance=0)
        _, state_y1 = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=0, fhsa_annual_limit=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertEqual(_cf(state_y1, 'fhsa_room'), 16000)

    def test_fhsa_contribution_then_annual_room(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_balance=0)
        _, state_y1 = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=8000, fhsa_annual_limit=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertEqual(_cf(state_y1, 'fhsa_room'), 8000)

    def test_fhsa_growth_applied(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_balance=0)
        allocs = {'fhsa': 8000, '_primary_income': 120000, '_spouse_income': 50000}
        result, new_state = simulate_year_pure(
            state, 0, allocs, _make_config(), investment_return=0.07,
            fhsa_contribution=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertAlmostEqual(_cf(new_state, 'fhsa_balance'), 8000 * 1.07, places=1)

    def test_fhsa_zero_contribution_when_no_room(self):
        state = _fhsa_state(fhsa_room=0, fhsa_balance=0)
        result, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=5000, mortgage_data=_mort_data(state),
        )
        self.assertAlmostEqual(_cf(new_state, 'fhsa_balance'), 0)

    def test_fhsa_pure_function_no_mutation(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_balance=1000)
        original_room = _cf(state, 'fhsa_room')
        original_bal = _cf(state, 'fhsa_balance')
        simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=8000, mortgage_data=_mort_data(state),
        )
        self.assertEqual(_cf(state, 'fhsa_room'), original_room)
        self.assertEqual(_cf(state, 'fhsa_balance'), original_bal)

    def test_fhsa_lifetime_limit_in_jurisdiction_state(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state.get('canada')
        self.assertIsInstance(canada, dict)
        # #700/#643/#704: FHSA is a per-adult store; slot 0 carries the limit.
        self.assertIn('adult_fhsa', canada)
        self.assertEqual(adult_fhsa_slot(canada, 0)['lifetime_limit'], 40000)

    def test_fhsa_lifetime_used_in_jurisdiction_state(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state.get('canada')
        self.assertIn('adult_fhsa', canada)
        self.assertEqual(adult_fhsa_slot(canada, 0)['lifetime_used'], 0.0)

    def test_fhsa_lifetime_used_increases_with_contribution(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_lifetime_used=0)
        _, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=5000, fhsa_annual_limit=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertAlmostEqual(_cf(new_state, 'fhsa_lifetime_used'), 5000)

    def test_fhsa_contribution_clamped_by_lifetime_remaining(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_lifetime_used=37000, fhsa_lifetime_limit=40000)
        result, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=8000, mortgage_data=_mort_data(state),
        )
        self.assertAlmostEqual(_cf(new_state, 'fhsa_lifetime_used'), 40000)
        self.assertAlmostEqual(_cf(new_state, 'fhsa_balance') / 1.07, 3000, places=1)

    def test_fhsa_lifetime_used_cannot_exceed_limit(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_lifetime_used=39000, fhsa_lifetime_limit=40000)
        _, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            _make_config(), investment_return=0.07, fhsa_contribution=8000, mortgage_data=_mort_data(state),
        )
        self.assertLessEqual(_cf(new_state, 'fhsa_lifetime_used'), _cf(new_state, 'fhsa_lifetime_limit'))

    def test_fhsa_balance_specifically_in_total_assets(self):
        state = _fhsa_state(fhsa_room=8000, fhsa_balance=5000)
        allocs = {'fhsa': 8000, '_primary_income': 120000, '_spouse_income': 50000}
        result, new_state = simulate_year_pure(
            state, 0, allocs, _make_config(), investment_return=0.07,
            fhsa_contribution=8000,
            mortgage_data=_mort_data(state),
        )
        non_fhsa_assets = (
            _rrsp_total(new_state) + _tfsa_total(new_state) + new_state.non_reg_balance
        )
        self.assertGreater(result.total_assets, non_fhsa_assets)


class TestFHSASimulationExtra(unittest.TestCase):
    """Additional FHSA tests per quality gate re-review #2."""

    def test_fhsa_balance_and_room_in_jurisdiction_state(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state.get('canada')
        self.assertIsInstance(canada, dict)
        # #700/#643/#704: balance/room now live in the per-adult FHSA store.
        self.assertIn('adult_fhsa', canada)
        slot = adult_fhsa_slot(canada, 0)
        self.assertIn('balance', slot)
        self.assertIn('room', slot)

    def test_fhsa_custom_lifetime_limit_preserved(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        canada = state.jurisdiction_state.get('canada')
        if isinstance(canada, dict):
            # #700/#643/#704: set the store's slot 0, not flat canada keys.
            pid = next(iter(canada.get('adult_fhsa', {'primary': None})), 'primary')
            canada['adult_fhsa'] = {pid: {
                'balance': 0.0, 'room': 8000,
                'lifetime_used': 0.0, 'lifetime_limit': 50000,
            }}
        _, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            cfg, investment_return=0.07, fhsa_contribution=0, fhsa_annual_limit=8000,
            mortgage_data=_mort_data(state),
        )
        self.assertEqual(_cf(new_state, 'fhsa_lifetime_limit'), 50000)

    def test_fhsa_contribution_room_is_computed(self):
        from countries.canada.fhsa import FHSAAccount
        f = FHSAAccount(annual_room=8000, carry_forward_room=4000)
        self.assertEqual(f.annual_room + f.carry_forward_room, 12000)

    def test_fhsa_16000_room_allows_full_contribute(self):
        from countries.canada.fhsa import FHSAAccount
        f = FHSAAccount(annual_room=8000, carry_forward_room=8000)
        actual = f.contribute(16000)
        self.assertAlmostEqual(actual, 16000)


class TestCanadaPropertyDescriptor(unittest.TestCase):
    """Test jurisdiction_state error handling (issue #25: no _CanadaProperty)."""

    def test_canada_state_must_be_dict(self):
        """jurisdiction_state['canada'] must be a dict for simulate_year_pure to work."""
        cfg = _make_config()
        state = SimState.initial(cfg)
        # Setting it to a non-dict should still allow construction
        # but simulate_year_pure should handle gracefully
        state.jurisdiction_state['canada'] = None
        # simulate_year_pure should initialize a default canada dict
        result, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            cfg, investment_return=0.07, mortgage_data=_mort_data(state),
        )
        # Should have created a valid canada dict
        self.assertIsInstance(new_state.jurisdiction_state.get('canada'), dict)


class TestLegacyJurisdictionStateDeprecation(unittest.TestCase):
    """Test that legacy object-based jurisdiction_state is handled (issue #25)."""

    def test_object_based_canada_replaced_with_dict(self):
        """simulate_year_pure replaces non-dict canada with a default dict."""
        cfg = _make_config()
        state = SimState.initial(cfg)
        state.jurisdiction_state['canada'] = 'not_a_dict'
        # Should not crash — simulate_year_pure should create a fresh dict
        result, new_state = simulate_year_pure(
            state, 0, {'_primary_income': 120000, '_spouse_income': 50000},
            cfg, investment_return=0.07, mortgage_data=_mort_data(state),
        )
        self.assertIsInstance(new_state.jurisdiction_state.get('canada'), dict)


# ── Issue #28: investment_return default must not silently mask missing return_model ─

class TestInvestmentReturnRequired(unittest.TestCase):
    """simulate_year_pure must require an explicit investment_return (issue #28)."""

    def test_raises_value_error_when_investment_return_omitted(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        with self.assertRaises(ValueError) as ctx:
            simulate_year_pure(state, 0, allocs, cfg, mortgage_data=_mort_data(state))
        self.assertIn('investment_return', str(ctx.exception))

    def test_raises_value_error_when_investment_return_is_none(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        with self.assertRaises(ValueError):
            simulate_year_pure(
                state, 0, allocs, cfg,
                investment_return=None,
                mortgage_data=_mort_data(state),
            )

    def test_works_with_explicit_investment_return(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        result, new_state = simulate_year_pure(
            state, 0, allocs, cfg,
            investment_return=0.07,
            mortgage_data=_mort_data(state),
        )
        self.assertGreater(result.total_assets, 0)

    def test_zero_investment_return_works(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        result, new_state = simulate_year_pure(
            state, 0, allocs, cfg,
            investment_return=0.0,
            mortgage_data=_mort_data(state),
        )
        self.assertGreater(result.total_assets, 0)

    def test_negative_investment_return_works(self):
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = _base_allocs()
        result, new_state = simulate_year_pure(
            state, 0, allocs, cfg,
            investment_return=-0.10,
            mortgage_data=_mort_data(state),
        )
        self.assertIsInstance(result.total_assets, float)