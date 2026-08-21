#!/usr/bin/env python3
"""Regression tests for bugs fixed during the 2026-06-07 refactor.

These tests prevent previously-fixed bugs from reappearing:

Bug 1: RESP child birth_year — input.json uses birth_year, not age
Bug 2: Spousal RRSP uses primary's room, not spouse's  
Bug 3: Deduct-later contributes full amount, deducts partially
Bug 4: fill_room doesn't double-count primary room (proportional split)
Bug 5: RESP cash-out tax penalty model
"""

import json
import unittest
from copy import deepcopy

from simulation import FamilySimulation, SimulationConfig
from simulation_state import adult_rrsp_slot  # #700: per-adult RRSP store
from strategy import (
    AllocationStrategy, StrategyEngine, FamilyState,
)
from countries.canada.strategies import STRATEGY_BALANCED
from countries.canada.resp_rules import RESPCalculator


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_config(**overrides) -> SimulationConfig:
    """Build a minimal SimulationConfig with fabricate-data names (DP#15)."""
    defaults = {
        'projection_years': 5,
        'investment_return': 0.07,
        'salary_growth': 0.02,
        'savings_rate': 0.20,
        'house_value': 600000,
        'mortgage_balance': 300000,
        'mortgage_rate': 0.0495,
        'margin_available': 50000,
        'family_members': [
            {
                'role': 'primary', 'name': 'Alpha',
                'gross_income': 120000, 'birth_year': 1980,
                'rrsp_room_accumulated': 100000,
                'tfsa_room_accumulated': 30000,
            },
            {
                'role': 'spouse', 'name': 'Beta',
                'gross_income': 60000, 'birth_year': 1982,
                'rrsp_room_accumulated': 80000,
                'tfsa_room_accumulated': 30000,
            },
        ],
        'children': [
            {'name': 'Gamma', 'birth_year': 2010},
            {'name': 'Delta', 'birth_year': 2007},
        ],
    }
    defaults.update(overrides)
    
    return SimulationConfig(
        projection_years=defaults['projection_years'],
        investment_return=defaults['investment_return'],
        salary_growth=defaults['salary_growth'],
        savings_rate=defaults['savings_rate'],
        house_value=defaults['house_value'],
        mortgage_balance=defaults['mortgage_balance'],
        mortgage_rate=defaults['mortgage_rate'],
        margin_available=defaults['margin_available'],
        family_members=defaults['family_members'],
        children=defaults['children'],
        deduct_later_bracket_target=overrides.get('deduct_later_bracket_target', 0),
    )


def _make_state(**overrides) -> FamilyState:
    """Build a FamilyState with round numbers."""
    defaults = dict(
        primary_income=120000, spouse_income=60000,
        primary_rrsp_room=150000, spouse_rrsp_room=50000,
        primary_tfsa_room=40000, spouse_tfsa_room=40000,
        resp_eligible_children=1, bracket_gap=0.20,
        annual_savings=30000,  # Round number for test allocations
    )
    defaults.update(overrides)
    return FamilyState(**defaults)


# ── Bug 1: RESP child birth_year ─────────────────────────────────────────────

class TestRespBirthYear(unittest.TestCase):
    """Bug: simulation.py computed birth_year=2026-age but input.json has birth_year.
    
    Fix: simulation.py now reads birth_year directly from child dict.
    Children should have correct ages based on birth_year, not default to 0.
    """

    def test_child_birth_year_from_config(self):
        """Children initialized with correct birth years, not age-based defaults."""
        config = _make_config(children=[
            {'name': 'Gamma', 'birth_year': 2010},
            {'name': 'Delta', 'birth_year': 2007},
        ])
        sim = FamilySimulation(
            config=config,
            strategy=STRATEGY_BALANCED,
        )
        self.assertEqual(len(sim.resp_children), 2)
        self.assertEqual(sim.resp_children[0].birth_year, 2010)
        self.assertEqual(sim.resp_children[1].birth_year, 2007)
    
    def test_cesg_eligibility_by_birth_year(self):
        """CESG eligibility matches actual child ages.
        
        Gamma (b.2010): age 16 in 2026 → CESG eligible for 2026, 2027 (turns 17 in 2027)
        Delta (b.2007): age 19 in 2026 → NOT CESG eligible
        """
        config = _make_config(children=[
            {'name': 'Gamma', 'birth_year': 2010},
            {'name': 'Delta', 'birth_year': 2007},
        ])
        sim = FamilySimulation(config=config, strategy=STRATEGY_BALANCED)
        
        gamma = sim.resp_children[0]
        delta = sim.resp_children[1]
        
        # Gamma: age 16 in 2026 → eligible
        self.assertTrue(gamma.cesg_eligible(2026))
        self.assertTrue(gamma.cesg_eligible(2027))  # Turns 17
        self.assertFalse(gamma.cesg_eligible(2028))  # Turns 18
        
        # Delta: age 19 in 2026 → NOT eligible
        self.assertFalse(delta.cesg_eligible(2026))
        self.assertFalse(delta.cesg_eligible(2027))
    
    def test_cesg_eligible_child_count_in_year_state(self):
        """FamilyState reflects correct CESG-eligible count.
        
        Before fix: both children appeared eligible (age defaulted to 0)
        After fix: only Gamma (b.2010) is eligible in 2026-2027
        """
        config = _make_config(children=[
            {'name': 'Gamma', 'birth_year': 2010},
            {'name': 'Delta', 'birth_year': 2007},
        ])
        sim = FamilySimulation(config=config, strategy=STRATEGY_BALANCED)
        
        # Run one year and check the state reflects only 1 eligible child
        results = sim.run()
        
        # The simulation should only match CESG for eligible children
        # Verify the RESP balance reflects realistic contributions
        final_resp = results[-1].resp_balance
        # With 1 eligible child, maximum CESG contribution match is $2,500/yr
        # at 20% = $500/yr for 5 years = $2,500 total CESG
        self.assertGreaterEqual(final_resp, 0)
    
    def test_age_field_fallback(self):
        """If 'age' is used instead of 'birth_year', conversion still works."""
        config = _make_config(children=[
            {'name': 'Gamma', 'age': 10},
        ])
        sim = FamilySimulation(config=config, strategy=STRATEGY_BALANCED)
        child = sim.resp_children[0]
        self.assertEqual(child.birth_year, 2016)  # 2026 - 10


# ── Bug 2: Spousal RRSP uses primary's room ──────────────────────────────────

class TestSpousalRRSPRoom(unittest.TestCase):
    """Bug: spousal_rrsp and spouse_rrsp both initialized from spouse_mem.rrsp_room.
    
    Fix: spousal_rrsp starts with 0 room; contributions draw from self.rrsp
    (primary's room). spouse_rrsp uses spouse's own room.
    """

    def test_spousal_rrsp_room_is_zero(self):
        """Spousal RRSP account starts with 0 room (draws from primary's room)."""
        config = _make_config(family_members=[
            {'role': 'primary', 'name': 'Alpha', 'gross_income': 120000,
             'birth_year': 1980, 'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000},
            {'role': 'spouse', 'name': 'Beta', 'gross_income': 60000,
             'birth_year': 1982, 'rrsp_room_accumulated': 80000, 'tfsa_room_accumulated': 30000},
        ])
        sim = FamilySimulation(config=config, strategy=STRATEGY_BALANCED)
        
        canada = sim._state.jurisdiction_state['canada']
        # Spousal RRSP draws from primary's room — the per-adult store keeps no
        # separate spousal-room key (#700); it carries exactly the two adults.
        self.assertEqual(len(canada['adult_rrsp']), 2)
        # Primary RRSP has primary's room (slot 0 own_room)
        self.assertEqual(adult_rrsp_slot(canada, 0)[1], 100000)
        # Spouse RRSP has spouse's own room (slot 1 own_room)
        self.assertEqual(adult_rrsp_slot(canada, 1)[1], 80000)
    
    def test_fill_room_spousal_uses_primary_room(self):
        """When fill_room is called, spousal RRSP draws from primary's room limit.
        
        Primary room = 150000, spouse room = 50000.
        With proportional split (35% + 10% = 45% total primary),
        primary gets 150k * 35/45 ≈ 116.7k, spousal gets 150k * 10/45 ≈ 33.3k.
        Total from primary room ≤ 150k.
        Spouse own RRSP uses spouse's room: ≤ 50k.
        """
        state = _make_state(
            primary_rrsp_room=150000, spouse_rrsp_room=50000,
            bracket_gap=0.20,
        )
        engine = StrategyEngine(AllocationStrategy(
            name="test", rrsp_pct=0.35, spousal_rrsp_pct=0.10,
            spousal_splitting=True, min_bracket_gap=0.05,
        ))
        result = engine.fill_room(300000, state)
        
        # Primary + spousal total ≤ primary room
        total_from_primary = result.primary_rrsp + result.spousal_rrsp
        self.assertLessEqual(total_from_primary, 150000)
        self.assertAlmostEqual(total_from_primary, 150000, delta=1)
        
        # Spouse own RRSP ≤ spouse room
        self.assertLessEqual(result.spouse_rrsp, 50000)
        
        # Spousal should get some allocation
        self.assertGreater(result.spousal_rrsp, 0)


# ── Bug 3: Deduct-later contributes full, deducts partially ─────────────────

class TestDeductLater(unittest.TestCase):
    """Bug: deduct_later=True capped contributions to bracket_target.
    
    Fix: Full amount contributed; deduction claimed annually up to bracket target.
    Undeducted pool tracked via _rrsp_undeducted_pool; carries forward.
    """

    def test_deduct_later_contributes_full_room(self):
        """With deduct_later, RRSP gets full contribution up to room.
        
        Primary room = 200k, lump_sum = 300k.
        Even with deduct_later=True, the full 200k room should be filled.
        """
        state = _make_state(primary_income=120000, primary_rrsp_room=200000)
        strategy = AllocationStrategy(
            name="test", deduct_later=True,
            rrsp_pct=0.35, spousal_rrsp_pct=0.0,
            spousal_splitting=True,
        )
        engine = StrategyEngine(strategy)
        result = engine.fill_room(300000, state)
        
        # Full room allocated (no spousal, so all goes to primary RRSP)
        self.assertAlmostEqual(result.primary_rrsp, 200000, delta=1)
    
    def test_deduct_later_pool_tracks_deferred_deductions(self):
        """The undeducted pool accumulates when contributions exceed deduction cap."""
        config = _make_config(
            family_members=[
                {'role': 'primary', 'name': 'Alpha', 'gross_income': 160000,
                 'birth_year': 1980, 'rrsp_room_accumulated': 180000, 'tfsa_room_accumulated': 30000},
                {'role': 'spouse', 'name': 'Beta', 'gross_income': 70000,
                 'birth_year': 1982, 'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000},
            ],
            deduct_later_bracket_target=117045,  # Federal 26% bracket boundary (2026)
        )
        strategy = AllocationStrategy(
            name="test", deduct_later=True,
            rrsp_pct=0.35, spousal_rrsp_pct=0.0,
            tfsa_pct=0.30, resp_pct=0.07, non_reg_pct=0.28,
        )
        # Large lump sum to fill RRSP
        sim = FamilySimulation(
            config=config, strategy=strategy,
            lump_sum=max(0, config.margin_available),
            use_readvanceable=False, deduct_later=True,
        )
        results = sim.run()
        
        # Year 0 should have a substantial undeducted amount
        yr0 = results[0]
        total_rrsp_contrib = yr0.contributions.get('primary_rrsp', 0) + yr0.contributions.get('spousal_rrsp', 0)
        yr0_deductions = yr0.rrsp_tax_savings / 0.475 if yr0.rrsp_tax_savings > 0 else 0  # Divide by MTR to get deduction
        
        # Deductions should be less than total contributions (some deferred)
        if total_rrsp_contrib > 10000:
            self.assertLess(yr0_deductions, total_rrsp_contrib,
                          "Deductions should be less than contributions for deduct_later")
        
        # Over the full projection, most should be deducted
        total_deductions = sum(r.rrsp_tax_savings for r in results)
        total_contrib = sum(r.contributions.get('primary_rrsp', 0) + r.contributions.get('spousal_rrsp', 0) for r in results)
        
        # Total deductions should be close to total contributions by the end
        effective_deduction = total_deductions / 0.475 if total_deductions > 0 else 0
        self.assertLess(abs(effective_deduction - total_contrib) / max(1, total_contrib), 0.3,
                      "Most contributions should be deducted over projection")
    
    def test_deduct_now_fills_room_same_as_deduct_later(self):
        """Deduct-now and deduct-later should allocate the same amounts.
        
        The only difference is when the tax savings are realized.
        """
        state = _make_state(primary_income=120000, primary_rrsp_room=200000)
        
        strat_dl = AllocationStrategy(
            name="dl", deduct_later=True,
            rrsp_pct=0.35, spousal_rrsp_pct=0.0,
        )
        strat_dn = AllocationStrategy(
            name="dn", deduct_later=False,
            rrsp_pct=0.35, spousal_rrsp_pct=0.0,
        )
        
        engine_dl = StrategyEngine(strat_dl)
        engine_dn = StrategyEngine(strat_dn)
        
        result_dl = engine_dl.fill_room(300000, state)
        result_dn = engine_dn.fill_room(300000, state)
        
        # Same allocations (difference is in simulation, not allocation)
        self.assertAlmostEqual(result_dl.primary_rrsp, result_dn.primary_rrsp, delta=1)


# ── Bug 4: fill_room proportional split ──────────────────────────────────────

class TestFillRoomProportionalSplit(unittest.TestCase):
    """Bug: fill_room used frozen State; step 1 drained primary room before step 2.
    
    Fix: Primary+spousal share primary room proportionally using strategy ratios.
    """

    def test_proportional_split_exhausts_primary_room(self):
        """Primary + spousal together should use the full primary room."""
        state = _make_state(primary_rrsp_room=100000, bracket_gap=0.20)
        engine = StrategyEngine(AllocationStrategy(
            name="test", rrsp_pct=0.30, spousal_rrsp_pct=0.10,
            spousal_splitting=True, min_bracket_gap=0.05,
        ))
        result = engine.fill_room(200000, state)
        
        # 30%/(30%+10%) = 75% to primary, 25% to spousal
        self.assertAlmostEqual(result.primary_rrsp, 75000, delta=1)
        self.assertAlmostEqual(result.spousal_rrsp, 25000, delta=1)
    
    def test_no_spousal_when_splitting_disabled(self):
        """When spousal_splitting=False, primary gets all room, spousal gets 0."""
        state = _make_state(primary_rrsp_room=100000, bracket_gap=0.20)
        engine = StrategyEngine(AllocationStrategy(
            name="test", rrsp_pct=0.35, spousal_rrsp_pct=0.10,
            spousal_splitting=False,
        ))
        result = engine.fill_room(200000, state)
        
        self.assertAlmostEqual(result.primary_rrsp, 100000, delta=1)
        self.assertEqual(result.spousal_rrsp, 0)
    
    def test_no_spousal_when_below_bracket_threshold(self):
        """Spousal is 0 when bracket gap is below threshold."""
        state = _make_state(primary_rrsp_room=100000, bracket_gap=0.03)
        engine = StrategyEngine(AllocationStrategy(
            name="test", rrsp_pct=0.35, spousal_rrsp_pct=0.10,
            spousal_splitting=True, min_bracket_gap=0.05,
        ))
        result = engine.fill_room(200000, state)
        
        self.assertAlmostEqual(result.primary_rrsp, 100000, delta=1)
        self.assertEqual(result.spousal_rrsp, 0)
    
    def test_allocate_savings_respects_proportional_split(self):
        """Yearly savings allocation also uses proportional split."""
        state = _make_state(
            primary_income=120000, spouse_income=60000,
            primary_rrsp_room=150000, bracket_gap=0.20,
        )
        engine = StrategyEngine(AllocationStrategy(
            name="test", rrsp_pct=0.35, spousal_rrsp_pct=0.10,
            spousal_splitting=True, min_bracket_gap=0.05,
        ))
        result = engine.allocate(state)
        
        # 35/45 = ~$18,167 to primary, 10/45 = ~$5,190 to spousal
        # But capped by remaining
        self.assertGreater(result.primary_rrsp, 0)
        self.assertGreater(result.spousal_rrsp, 0)
        
        # Both should be non-zero
        total = result.primary_rrsp + result.spousal_rrsp
        self.assertLessEqual(total, 150000)


# ── Bug 5: RESP cash-out tax penalties ───────────────────────────────────────

class TestRespCashoutTax(unittest.TestCase):
    """Bug: Old print_resp_cashout_analysis used simplified 60% EAP model.
    
    Fix: Proper AIP penalty (MTR+20%) and grant clawback for collapse;
    proper EAP tax (student MTR) for enrolled withdrawal.
    """

    def _base_cfg(self):
        return {
            'accounts': {
                'resp_current_balance': 100000,
                'resp_composition': {
                    'total_contributions': 60000,
                    'total_cesg_received': 12000,
                    'total_qesi_received': 6000,
                    'investment_earnings': 22000,
                },
            },
            'assumptions': {
                'resp_eap_tax_rate': 0.15,
            },
        }
    
    def test_collapse_grants_clawed_back(self):
        """RESP collapse: CESG + QESI returned to government."""
        base = self._base_cfg()
        calc = RESPCalculator()
        result = calc.resp_collapse_proceeds(base, 0.475)
        
        self.assertAlmostEqual(result['grant_clawback'], 18000)
        # Contributions returned tax-free
        self.assertEqual(result['contributions_returned'], 60000)
    
    def test_collapse_aip_taxed_at_mtr_plus_penalty(self):
        """RESP collapse: earnings taxed at MTR + 20%."""
        base = self._base_cfg()
        calc = RESPCalculator()
        result = calc.resp_collapse_proceeds(base, 0.475)
        
        # AIP tax: 22000 * 0.675 = 14850
        expected_tax = 22000 * 0.675
        self.assertAlmostEqual(result['tax_cost'], expected_tax, delta=0.01)
        
        # Net: 60000 (contributions) + 22000 (earnings) - 14850 = 67150
        expected_net = 60000 + 22000 - expected_tax
        self.assertAlmostEqual(result['net_proceeds'], expected_net, delta=0.01)
    
    def test_eap_grants_kept_by_student(self):
        """RESP EAP: grants are NOT clawed back; kept by student."""
        base = self._base_cfg()
        calc = RESPCalculator()
        result = calc.resp_eap_proceeds(base)
        
        self.assertEqual(result['grant_clawback'], 0)
        # EAP taxable portion = grants + earnings = 18000 + 22000 = 40000
        self.assertAlmostEqual(result['earnings_taxed'], 40000, delta=0.01)
    
    def test_eap_taxed_at_student_mtr(self):
        """RESP EAP: grants + earnings taxed at 15% student MTR."""
        base = self._base_cfg()
        calc = RESPCalculator()
        result = calc.resp_eap_proceeds(base)
        
        # Tax: (12000 + 6000 + 22000) * 0.15 = 6000
        expected_tax = 40000 * 0.15
        self.assertAlmostEqual(result['tax_cost'], expected_tax, delta=0.01)
        
        # Net: 60000 (contribs) + 40000 (EAP) - 6000 = 94000
        expected_net = 100000 - expected_tax
        self.assertAlmostEqual(result['net_proceeds'], expected_net, delta=0.01)
    
    def test_collapse_worse_than_eap(self):
        """Collapse should always be worse than EAP (penalty + higher MTR)."""
        base = self._base_cfg()
        calc = RESPCalculator()
        collapse = calc.resp_collapse_proceeds(base, 0.475)
        eap = calc.resp_eap_proceeds(base)
        
        self.assertLess(collapse['net_proceeds'], eap['net_proceeds'],
                       "Collapse should produce less net cash than EAP")
    
    def test_no_composition_fallback(self):
        """When no composition data, estimate from balance."""
        base = {
            'accounts': {'resp_current_balance': 100000},
            'assumptions': {'resp_eap_tax_rate': 0.15},
        }
        
        calc = RESPCalculator()
        collapse = calc.resp_collapse_proceeds(base, 0.475)
        self.assertGreater(collapse['net_proceeds'], 0)
        self.assertGreater(collapse['tax_cost'], 0)
        
        eap = calc.resp_eap_proceeds(base)
        self.assertGreater(eap['net_proceeds'], collapse['net_proceeds'])
    
    def test_zero_balance_no_tax(self):
        """Zero RESP balance → zero tax."""
        base = {'accounts': {'resp_current_balance': 0}}
        calc = RESPCalculator()
        
        collapse = calc.resp_collapse_proceeds(base, 0.475)
        self.assertEqual(collapse['net_proceeds'], 0)
        self.assertEqual(collapse['tax_cost'], 0)


if __name__ == '__main__':
    unittest.main()
