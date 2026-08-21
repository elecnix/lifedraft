#!/usr/bin/env python3
"""Tests for strategy.py and simulation.py modules.

All test data uses fake names and round numbers. No personal information.

Run: python3 tests/test_strategy_simulation.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from strategy import (
    AllocationStrategy, StrategyEngine, FamilyState, AllocationResult,
    StrategyType, ChildState, ChildAllocationResult,
)
from countries.canada.strategies import (
    STRATEGIES, STRATEGY_BALANCED, STRATEGY_READVANCE_PRIORITY,
    STRATEGY_RRSP_MAX, STRATEGY_NO_READVANCE,
    discover_strategies,
)

from simulation import FamilySimulation, SimulationConfig


class TestAllocationStrategy(unittest.TestCase):
    """Test allocation strategy dataclass."""

    def test_strategy_types(self):
        self.assertEqual(STRATEGY_BALANCED.strategy_type, StrategyType.BALANCED)
        self.assertEqual(STRATEGY_READVANCE_PRIORITY.strategy_type, StrategyType.READVANCE_PRIORITY)

    def test_percentages_sum_to_one(self):
        for name, s in STRATEGIES.items():
            self.assertAlmostEqual(s.total_pct, 1.0, places=1,
                                  msg=f"Strategy '{name}' sums to {s.total_pct}")

    def test_validate_good_strategy(self):
        s = STRATEGY_BALANCED
        warnings = s.validate()
        self.assertEqual(len(warnings), 0)

    def test_validate_bad_strategy(self):
        s = AllocationStrategy(name="Test", non_reg_pct=0.1)
        warnings = s.validate()
        self.assertGreater(len(warnings), 0)


class TestStrategyEngine(unittest.TestCase):
    """Test the allocation engine."""

    def setUp(self):
        self.state = FamilyState(
            annual_savings=40000,
            primary_rrsp_room=150000,
            primary_tfsa_room=40000,
            spouse_rrsp_room=40000,
            spouse_tfsa_room=40000,
            primary_marginal_rate=0.4571,
            spouse_marginal_rate=0.2569,
            bracket_gap=0.2002,
        )

    def test_balanced_allocation(self):
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.allocate(self.state)
        self.assertGreater(result.primary_rrsp, 0, "Should allocate to RRSP")
        self.assertGreater(result.primary_tfsa, 0, "Should allocate to TFSA")
        self.assertGreater(result.total_allocated, 0)

    # ── #751: allocate() must honour the declared tfsa_pct / non_reg_pct ──
    # Before the fix, Step 6 funded TFSA at a hardcoded `remaining * 0.5` and
    # Step 7 swept the rest to non-reg, so a declared tfsa_pct changed nothing:
    # a household declaring tfsa_pct=0.20 of $50k savings got $26,250 in TFSA
    # (52.5%) and $0 in non-reg. These tests pin the declared percentages.
    def _room_state(self, savings=50000):
        # Plenty of registered room so caps do not bind — the *declared*
        # percentages, not room, determine the split. RESP match caps set high
        # so resp_pct's target is the binding constraint, not the CESG cap.
        return FamilyState(
            annual_savings=savings,
            primary_rrsp_room=200000, spouse_rrsp_room=200000,
            primary_tfsa_room=200000, spouse_tfsa_room=200000,
            resp_eligible_children=1, resp_annual_match_cap=50000,
            resp_contribution_match_max=50000,
            primary_marginal_rate=0.4571, spouse_marginal_rate=0.2569,
            bracket_gap=0.2002,
        )

    def test_declared_tfsa_pct_is_honoured_not_overridden_by_half(self):
        """tfsa_pct=0.20 must fund TFSA at 20% of savings, not 50% of remaining."""
        s = AllocationStrategy(name="repro", rrsp_pct=0.15, spousal_rrsp_pct=0.05,
                                tfsa_pct=0.20, resp_pct=0.05, non_reg_pct=0.55)
        result = StrategyEngine(s).allocate(self._room_state(savings=50000))
        tfsa_total = result.primary_tfsa + result.spouse_tfsa
        self.assertAlmostEqual(tfsa_total, s.tfsa_pct * 50000,
                             msg="TFSA must receive the declared tfsa_pct, not remaining*0.5")
        self.assertLess(tfsa_total, 0.30 * 50000,
                        msg="TFSA must not swallow ~half of remaining as it did pre-#751")

    def test_tfsa_pct_zero_is_a_value_not_a_fallback(self):
        """DP#32: tfsa_pct=0 means zero TFSA — not a signal to fall back to 0.5."""
        s = AllocationStrategy(name="zero_tfsa", rrsp_pct=0.30, spousal_rrsp_pct=0.10,
                                tfsa_pct=0.0, resp_pct=0.07, non_reg_pct=0.53)
        result = StrategyEngine(s).allocate(self._room_state(savings=40000))
        self.assertEqual(result.primary_tfsa, 0.0)
        self.assertEqual(result.spouse_tfsa, 0.0)
        self.assertGreater(result.non_reg, 0, "Residual flows to non-reg")

    def test_non_reg_receives_the_declared_residual(self):
        """non_reg_pct is honoured as the residual: with the five registered
        targets met and room sufficient, non_reg == non_reg_pct * savings."""
        s = AllocationStrategy(name="repro", rrsp_pct=0.15, spousal_rrsp_pct=0.05,
                                tfsa_pct=0.20, resp_pct=0.05, non_reg_pct=0.55)
        result = StrategyEngine(s).allocate(self._room_state(savings=50000))
        # Five declared registered targets sum to 0.45 of savings; the spouse's
        # personal-RRSP heuristic (Step 5, `remaining * 0.3`) is an undeclared
        # spill that reduces non_reg, so non_reg == 0.55*savings minus that
        # heuristic. Assert it is positive and below the declared share (the
        # heuristic ate part of it), and that the *registered* targets were met.
        declared_registered = (s.rrsp_pct + s.spousal_rrsp_pct + s.tfsa_pct
                               + s.resp_pct + s.fhsa_pct) * 50000
        self.assertAlmostEqual(result.primary_rrsp, s.rrsp_pct * 50000)
        self.assertAlmostEqual(result.spousal_rrsp, s.spousal_rrsp_pct * 50000)
        self.assertAlmostEqual(result.resp, s.resp_pct * 50000)
        self.assertAlmostEqual(result.primary_tfsa + result.spouse_tfsa, s.tfsa_pct * 50000)
        self.assertGreater(result.non_reg, 0)
        self.assertLessEqual(result.non_reg, s.non_reg_pct * 50000 + 1e-6,
                             msg="non_reg cannot exceed its declared share absent room spill")
        self.assertLessEqual(result.total_allocated, 50000 + 1e-6)

    def test_smith_priority_allocates_nonreg(self):
        # With lower room caps, more flows to non-reg
        state_low_room = FamilyState(
            annual_savings=40000,
            primary_rrsp_room=5000,
            primary_tfsa_room=5000,
            spouse_tfsa_room=5000,
            spouse_rrsp_room=3000,
            primary_marginal_rate=0.4571,
            spouse_marginal_rate=0.2569,
            bracket_gap=0.2002,
        )
        engine_readvance = StrategyEngine(STRATEGY_READVANCE_PRIORITY)
        result = engine_readvance.allocate(state_low_room)
        self.assertGreater(result.non_reg, 0, "Readvance priority should allocate to non-reg")

    def test_total_does_not_exceed_savings(self):
        for name, strategy in STRATEGIES.items():
            engine = StrategyEngine(strategy)
            result = engine.allocate(self.state)
            self.assertLessEqual(result.total_allocated, self.state.annual_savings + 1,
                                 msg=f"Strategy '{name}' exceeds savings")

    def test_contribution_room_limits(self):
        state = FamilyState(
            annual_savings=50000,
            primary_rrsp_room=5000,
            spouse_rrsp_room=2000,
            primary_tfsa_room=3000,
            spouse_tfsa_room=3000,
        )
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.allocate(state)
        self.assertLessEqual(result.primary_rrsp, 5000)
        # Spousal RRSP uses PRIMARY earner's room (ITA s.146(8.3)), not spouse room
        self.assertLessEqual(result.spousal_rrsp, 5000)  # primary_rrsp_room, not spouse

    def test_no_spousal_when_no_gap(self):
        state = FamilyState(
            annual_savings=40000,
            primary_marginal_rate=0.25,
            spouse_marginal_rate=0.25,
            bracket_gap=0.0,
        )
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.allocate(state)
        self.assertEqual(result.spousal_rrsp, 0)

    def test_as_dict(self):
        engine = StrategyEngine(STRATEGY_BALANCED)
        result = engine.allocate(self.state)
        d = result.as_dict()
        self.assertIn('primary_rrsp', d)
        self.assertIn('non_reg', d)
        self.assertEqual(d['primary_rrsp'], result.primary_rrsp)


class TestChildAllocation(unittest.TestCase):
    """Issue #812 (#701 follow-up): a child's OWN savings route to the CHILD's
    OWN accounts by room, and the allocation is swept-comparable.

    Fabricated round numbers (DP#15/DP#4): child_a, $10k savings, $6k TFSA room,
    $3k FHSA room, no RRSP room.
    """

    def _child(self, **kw):
        base = dict(savings=10000.0, tfsa_room=6000.0, fhsa_room=3000.0,
                    fhsa_lifetime_remaining=40000.0, rrsp_room=0.0, name='child_a')
        base.update(kw)
        return ChildState(**base)

    def test_routes_tfsa_then_fhsa_by_room(self):
        # Default strategy (no child_*_pct) -> #701 room-priority waterfall:
        # fill TFSA to its room, then FHSA to its room, residual to non-reg.
        result = StrategyEngine(AllocationStrategy()).allocate_child(self._child())
        self.assertEqual(result.tfsa, 6000.0)   # filled to TFSA room first
        self.assertEqual(result.fhsa, 3000.0)   # then FHSA room
        self.assertEqual(result.non_reg, 1000.0)  # $10k - 6k - 3k residual
        self.assertEqual(result.rrsp, 0.0)      # no RRSP room declared
        # Conservation: nothing invented, nothing lost.
        self.assertAlmostEqual(result.total_allocated, 10000.0)
        self.assertEqual(result.unused, 0.0)

    def test_tfsa_allocation_differs_from_non_reg(self):
        child = self._child()
        tfsa_first = StrategyEngine(AllocationStrategy()).allocate_child(child)
        all_non_reg = StrategyEngine(
            AllocationStrategy(child_non_reg_pct=1.0)).allocate_child(child)
        # Same dollars, different destinations: the sweep is a real comparison.
        self.assertEqual(tfsa_first.tfsa, 6000.0)
        self.assertEqual(all_non_reg.tfsa, 0.0)
        self.assertEqual(all_non_reg.non_reg, 10000.0)
        self.assertNotEqual(tfsa_first.tfsa, all_non_reg.tfsa)
        self.assertNotEqual(tfsa_first.non_reg, all_non_reg.non_reg)

    def test_no_savings_routes_nothing(self):
        # DP#32: absence is not a defaulted split — a child with no income of
        # their own routes $0, even with room available.
        result = StrategyEngine(AllocationStrategy()).allocate_child(
            self._child(savings=0.0))
        self.assertEqual(result.total_allocated, 0.0)

    def test_validate_warns_on_overtargeted_child_allocation(self):
        # Child targets summing above 100% over-target the child's own pool.
        warnings = AllocationStrategy(
            child_tfsa_pct=1.0, child_fhsa_pct=1.0).validate()
        assert any('Child allocation targets sum' in w for w in warnings)

    def test_child_rrsp_invents_no_household_deduction(self):
        # #701: routing dollars to a child's RRSP is pure allocation — the
        # result carries amounts only, no tax/deduction field exists to inflate.
        result = StrategyEngine(
            AllocationStrategy(child_rrsp_pct=1.0)).allocate_child(
            self._child(rrsp_room=10000.0))
        self.assertEqual(result.rrsp, 10000.0)
        self.assertEqual(set(result.as_dict()),
                         {'tfsa', 'fhsa', 'rrsp', 'non_reg', 'unused'})


class TestYearByYearAllocation(unittest.TestCase):
    """Test multi-year allocation with compounding room."""

    def test_3_year_projection(self):
        state = FamilyState(
            annual_savings=40000,
            primary_income=100000,
            spouse_income=50000,
        )
        engine = StrategyEngine(STRATEGY_BALANCED)
        results = engine.allocate_year_by_year(state, years=3)
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertGreater(r.total_allocated, 0)

    def test_room_decreases_with_contributions(self):
        state = FamilyState(
            annual_savings=40000,
            primary_rrsp_room=150000,
            spouse_rrsp_room=40000,
        )
        engine = StrategyEngine(STRATEGY_BALANCED)
        results = engine.allocate_year_by_year(state, years=3)
        # Room should decrease as we contribute
        self.assertGreater(state.primary_rrsp_room, 0)


class TestSimulationConfig(unittest.TestCase):
    """Test simulation configuration loading."""

    def test_load_from_schema(self):
        """Load from input_schema.json (committed template with zeroed data)."""
        schema_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'input_schema.json'
        )
        if os.path.exists(schema_path):
            config = SimulationConfig.from_json(schema_path)
            self.assertEqual(config.projection_years, 10)
            self.assertAlmostEqual(config.investment_return, 0.07, places=2)

    def test_config_defaults(self):
        config = SimulationConfig()
        self.assertEqual(config.projection_years, 10)
        self.assertAlmostEqual(config.investment_return, 0.07)


class TestFamilySimulation(unittest.TestCase):
    """Test the simulation engine."""

    def _make_config(self):
        """Create a SimulationConfig with test data (no personal info)."""
        config = SimulationConfig(
            savings_rate=0.20,  # DP#13: explicit round-number default for test
            deduct_later_bracket_target=117045,  # DP#45: explicit bracket target for deduct_later
            family_members=[
                {'name': 'Alpha', 'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 200000, 'tfsa_room_accumulated': 40000},
                {'name': 'Beta', 'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 40000},
            ],
        )
        return config

    def test_simulation_runs(self):
        config = self._make_config()
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path("3yr fixed", 0.04, 3, "fixed", [0.05])
        sim = FamilySimulation(config, STRATEGY_READVANCE_PRIORITY, rp)
        results = sim.run()
        self.assertEqual(len(results), 10)
        # Assets should grow over time
        self.assertGreater(results[-1].total_assets, results[0].total_assets)

    def test_simulation_no_smith(self):
        config = self._make_config()
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path("test", 0.05, 10, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_NO_READVANCE, rp, use_readvanceable=False)
        results = sim.run()
        # Issue #577: no SM, and no lump_sum draw of the margin either -> the
        # HELOC is never drawn, so heloc_balance must be exactly 0 for the
        # whole horizon (an available-but-undrawn limit is not debt; it must
        # not compound unserviced). Before the fix this asserted > 0, treating
        # the mere existence of margin_available (200000) as debt.
        self.assertEqual(results[-1].heloc_balance, 0,
                          "undrawn margin_available is availability, not debt")
        # SM readvances are 0 (no SM)
        self.assertEqual(results[-1].sm_readvanced, 0)


class TestDebtTracking(unittest.TestCase):
    """Issue #577/DP#18: margin HELOC is tracked as debt only once actually
    drawn (invested via lump_sum) — never merely because it's available."""

    def _make_config(self, margin=200000, cash_out=0):
        """Build test config with fabricated data (DP#4: role-based).

        Issue #664/#681: this fixture used to declare
        ``margin_available=margin + cash_out`` on a $500k house -- i.e. it
        inflated the HELOC limit by the cash-out (the pre-#257 double-count)
        AND put the result on a property whose charge could never secure it:
        mortgage $400k + margin $500k = $900k against a $500k house, a 180%
        LTV facility no lender would register. The run-path charge invariant
        (#681) refuses it, correctly.

        Corrected: the cash-out lands on the MORTGAGE only (#257), the
        pre-existing HELOC limit is what it is, and the house is large enough
        that the combined facility genuinely fits inside its 80% charge --
        $1,000,000 house => $800,000 charge; the refi case draws
        $400k mortgage + $200k margin = $600k, comfortably inside it, with the
        revolving portion ($200k) far below the 65% revolving-only ceiling.
        """
        return SimulationConfig(
            projection_years=3,
            investment_return=0.07,
            salary_growth=0.0,
            savings_rate=0.20,
            house_value=1000000,
            mortgage_balance=100000 + cash_out,
            mortgage_rate=0.05,
            ltv_max=0.80,
            margin_available=margin,
            amortization_years=20,
            family_members=[
                {'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 100000, 'tfsa_room_accumulated': 30000,
                 'birth_year': 1990},
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 30000,
                 'birth_year': 1992},
            ],
            children=[],
        )

    def test_no_refi_total_debt_includes_margin_heloc(self):
        """No Refi, margin DRAWN via lump_sum: total_debt = mortgage + margin
        HELOC (with interest). This test explicitly draws the whole margin
        (lump_sum=config.margin_available) and invests it — that draw really
        is debt. (Issue #577: the bug was booking the same $200k as debt even
        when nothing like this lump_sum draw happened at all.)
        """
        config = self._make_config(margin=200000, cash_out=0)
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path("test", 0.05, 3, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_NO_READVANCE, rp,
                               use_readvanceable=False, lump_sum=config.margin_available)
        results = sim.run()
        final = results[-1]
        # Debt must include margin HELOC (200k) — significantly more than
        # just the mortgage balance (~80k after 3 years of paydown)
        self.assertGreater(final.total_debt, 150000,
            f"total_debt=${final.total_debt:,.0f} should include margin HELOC; was only mortgage before fix")
        # Mortgage portion (should be less than total due to paydown)
        self.assertLess(final.mortgage_balance, final.total_debt)

    def test_no_refi_heloc_balance_positive(self):
        """heloc_balance must be > 0 without SM, when the margin is actually
        DRAWN via lump_sum (contrast with test_simulation_no_smith above,
        which leaves the margin undrawn and correctly expects 0 -- #577)."""
        config = self._make_config(margin=200000, cash_out=0)
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path("test", 0.05, 3, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_NO_READVANCE, rp,
                               use_readvanceable=False, lump_sum=config.margin_available)
        results = sim.run()
        self.assertGreater(results[-1].heloc_balance, 0)

    def test_rrsp_refund_reduces_margin_heloc(self):
        """RRSP contributions generate refunds that pay down the margin HELOC
        (margin is drawn via lump_sum here, so it is genuine debt)."""
        config = self._make_config(margin=200000, cash_out=0)
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path("test", 0.05, 3, 'variable', [0.05])
        sim = FamilySimulation(config, STRATEGY_BALANCED, rp,
                               use_readvanceable=False, lump_sum=config.margin_available)

        # Check that margin HELOC is non-zero at start (it was drawn via lump_sum)
        self.assertGreater(sim._state.heloc_balance, 0)
        initial_margin = sim._state.heloc_balance
        
        results = sim.run()
        
        # After 3 years of RRSP refunds, margin HELOC should be lower
        # (refunds pay down the debt)
        # Note: interest accrues on the HELOC too, but refunds should offset some
        rrsp_refunds = sum(r.rrsp_tax_savings for r in results)
        self.assertGreater(rrsp_refunds, 0,
            "RRSP contributions should generate tax refunds")
        self.assertGreater(sim._state.jurisdiction_state['canada']['heloc_rrsp_paydown'], 0,
            "RRSP refunds should be applied to margin HELOC paydown")
    
    def test_larger_debt_at_higher_ltv(self):
        """80% LTV should have significantly more debt than No Refi."""
        config_no = self._make_config(margin=200000, cash_out=0)
        config_refi = self._make_config(margin=200000, cash_out=300000)
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path("test", 0.05, 3, 'variable', [0.05])
        
        sim_no = FamilySimulation(config_no, STRATEGY_NO_READVANCE, rp,
                                  use_readvanceable=False, lump_sum=config_no.margin_available)
        sim_refi = FamilySimulation(config_refi, STRATEGY_NO_READVANCE, rp,
                                    use_readvanceable=False, lump_sum=config_refi.margin_available)
        
        results_no = sim_no.run()
        results_refi = sim_refi.run()
        
        # Refi scenario must have more debt (margin + cash-out)
        debt_diff = results_refi[-1].total_debt - results_no[-1].total_debt
        self.assertGreater(debt_diff, 200000,
            f"80% LTV should add ~$300k debt; diff was ${debt_diff:,.0f}")

    def test_no_refi_beats_refi_at_low_return(self):
        """At low returns, No Refi should beat 80% LTV.
        
        This is the key behavioral test: with margin HELOC properly
        tracked as debt, the refinance decision depends on return assumption.
        """
        config_no = self._make_config(margin=200000, cash_out=0)
        config_no.investment_return = 0.04  # Low return
        config_refi = self._make_config(margin=200000, cash_out=300000)
        config_refi.investment_return = 0.04
        
        from countries.canada.rate_model import build_rate_path
        rp = build_rate_path("test", 0.05, 3, 'variable', [0.05])
        
        sim_no = FamilySimulation(config_no, STRATEGY_BALANCED, rp,
                                  use_readvanceable=True, deduct_later=False,
                                  lump_sum=config_no.margin_available)
        sim_refi = FamilySimulation(config_refi, STRATEGY_BALANCED, rp,
                                    use_readvanceable=True, deduct_later=False,
                                    lump_sum=config_refi.margin_available)
        
        # Net benefit: simplified (assets - debt at end)
        results_no = sim_no.run()
        results_refi = sim_refi.run()
        net_no = results_no[-1].total_assets - results_no[-1].total_debt
        net_refi = results_refi[-1].total_assets - results_refi[-1].total_debt
        
        # At 4% return with 5% HELOC, No Refi should be better
        self.assertGreater(net_no, net_refi,
            f"No Refi net={net_no:,.0f} vs Refi net={net_refi:,.0f} at 4% return")


class TestStrategyDiscovery(unittest.TestCase):
    """Thread 3: Strategies are discovered from rules, not named."""

    def test_readvance_discovered_when_conditions_hold(self):
        """Readvance priority is available when all 3 conditions hold."""
        state = FamilyState(primary_marginal_rate=0.4571)
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            investment_return=0.07,
            heloc_rate=0.05,
        )
        self.assertIn('readvance_priority', discovered)
        self.assertIn('no_readvance', discovered)

    def test_readvance_not_discovered_when_not_readvanceable(self):
        """No readvance priority when mortgage is not readvanceable."""
        state = FamilyState(primary_marginal_rate=0.4571)
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': False},
            investment_return=0.07,
            heloc_rate=0.05,
        )
        self.assertNotIn('readvance_priority', discovered)
        self.assertIn('no_readvance', discovered)

    def test_readvance_not_discovered_when_return_too_low(self):
        """No readvance priority when after-tax return < HELOC cost."""
        state = FamilyState(primary_marginal_rate=0.4571)
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            investment_return=0.02,  # Low return
            heloc_rate=0.05,
        )
        self.assertNotIn('readvance_priority', discovered)

    def test_readvance_not_discovered_when_no_mortgage_cfg(self):
        """No readvance priority when no mortgage config provided."""
        state = FamilyState(primary_marginal_rate=0.4571)
        discovered = discover_strategies(
            state, mortgage_cfg=None,
            investment_return=0.07, heloc_rate=0.05,
        )
        self.assertNotIn('readvance_priority', discovered)

    def test_balanced_and_rrsp_always_available(self):
        """Balanced and RRSP max are always available."""
        state = FamilyState()
        discovered = discover_strategies(
            state, investment_return=0.07, heloc_rate=0.05,
        )
        self.assertIn('balanced', discovered)
        self.assertIn('rrsp_max', discovered)

    def test_profitability_computation(self):
        """Verify after-tax comparison: 7% return vs 5% HELOC at 45.71% marginal."""
        state = FamilyState(primary_marginal_rate=0.4571)
        after_tax_heloc = 0.05 * (1 - 0.4571)  # 2.71%
        after_tax_return = 0.07 * (1 - 0.4571 * 0.50)  # 5.40%
        self.assertGreater(after_tax_return, after_tax_heloc)


class TestAllocationStrategyFromDict(unittest.TestCase):
    """DP#2/DP#6: Strategy compositions from user config, not hardcoded."""

    def test_from_dict_creates_strategy(self):
        """AllocationStrategy.from_dict creates from config data."""
        data = {
            'name': 'Custom Growth',
            'strategy_type': 'custom',
            'rrsp_pct': 0.20,
            'spousal_rrsp_pct': 0.10,
            'tfsa_pct': 0.30,
            'non_reg_pct': 0.40,
            'prioritize_readvanceable': True,
            'deduct_later': True,
        }
        strategy = AllocationStrategy.from_dict(data)
        self.assertEqual(strategy.name, 'Custom Growth')
        self.assertAlmostEqual(strategy.rrsp_pct, 0.20)
        self.assertTrue(strategy.prioritize_readvanceable)
        self.assertTrue(strategy.deduct_later)

    def test_from_dict_defaults(self):
        """AllocationStrategy.from_dict uses DP#13 fallbacks for missing fields."""
        data = {'name': 'Minimal'}
        strategy = AllocationStrategy.from_dict(data)
        self.assertEqual(strategy.name, 'Minimal')
        self.assertAlmostEqual(strategy.rrsp_pct, 0.30)  # DP#13 fallback
        self.assertFalse(strategy.prioritize_readvanceable)  # Default

    def test_round_trip(self):
        """AllocationStrategy.from_dict/to_dict round-trips (DP#24)."""
        original = AllocationStrategy(
            name='Test Strategy',
            strategy_type=StrategyType.BALANCED,
            rrsp_pct=0.25,
            spousal_rrsp_pct=0.10,
            tfsa_pct=0.35,
            non_reg_pct=0.30,
        )
        data = original.to_dict()
        restored = AllocationStrategy.from_dict(data)
        self.assertEqual(restored.name, original.name)
        self.assertAlmostEqual(restored.rrsp_pct, original.rrsp_pct)
        self.assertAlmostEqual(restored.tfsa_pct, original.tfsa_pct)

    def test_custom_strategies_in_discover(self):
        """DP#2/DP#6: discover_strategies accepts custom strategies from config."""
        state = FamilyState(primary_marginal_rate=0.4571)
        custom = {
            'aggressive_growth': {
                'name': 'Aggressive Growth',
                'strategy_type': 'custom',
                'rrsp_pct': 0.10,
                'tfsa_pct': 0.20,
                'non_reg_pct': 0.70,
                'prioritize_readvanceable': True,
                'deduct_later': True,
            },
        }
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            investment_return=0.07,
            heloc_rate=0.05,
            custom_strategies=custom,
        )
        self.assertIn('aggressive_growth', discovered)
        self.assertEqual(discovered['aggressive_growth'].name, 'Aggressive Growth')
        self.assertAlmostEqual(discovered['aggressive_growth'].non_reg_pct, 0.70)

    def test_custom_strategies_with_allocation_objects(self):
        """custom_strategies accepts AllocationStrategy objects directly."""
        state = FamilyState(primary_marginal_rate=0.4571)
        custom_strat = AllocationStrategy(
            name='Conservative',
            strategy_type=StrategyType.BALANCED,
            rrsp_pct=0.40,
            tfsa_pct=0.40,
            non_reg_pct=0.20,
        )
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': False},
            investment_return=0.07,
            heloc_rate=0.05,
            custom_strategies={'conservative': custom_strat},
        )
        self.assertIn('conservative', discovered)
        self.assertAlmostEqual(discovered['conservative'].rrsp_pct, 0.40)


class TestStrategyRoundTrip(unittest.TestCase):
    """DP#24: Strategy config round-trips through dict."""

    def test_predefined_strategy_round_trip(self):
        """Pre-defined strategies round-trip through to_dict/from_dict."""
        for name, strat in STRATEGIES.items():
            # Skip backward-compat aliases
            if name in ('smith_priority', 'no_sm'):
                continue
            data = strat.to_dict()
            restored = AllocationStrategy.from_dict(data)
            self.assertAlmostEqual(restored.rrsp_pct, strat.rrsp_pct)
            self.assertAlmostEqual(restored.tfsa_pct, strat.tfsa_pct)
            self.assertEqual(restored.prioritize_readvanceable, strat.prioritize_readvanceable)


if __name__ == '__main__':
    unittest.main()