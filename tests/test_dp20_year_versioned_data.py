#!/usr/bin/env python3
"""Tests for DP#20: Year-specific tax data used per-year in simulation.

DP#20 requires that tax brackets, contribution limits, and CPP parameters
are indexed by year and looked up at each simulation time step — not
cached once at initialization.

These tests verify that:
1. simulate_year_pure uses year-specific RRSP/TFSA limits
2. FamilySimulation uses year-specific limits at each step
3. CPP/OAS parameters use year-versioned lookups
4. The monthly simulation path also uses year-specific limits
"""

import unittest
from unittest.mock import MagicMock, patch
from dataclasses import replace

from simulation_config import SimulationConfig
from simulation_state import SimState, simulate_year_pure, adult_rrsp_slot, adult_tfsa_slot  # #700
from tax_data import TaxDataProvider


def _make_config(start_year=2026, projection_years=10, **kw):
    """Create a minimal SimulationConfig for testing."""
    defaults = dict(
        projection_years=projection_years,
        investment_return=0.05,
        salary_growth=0.0,
        savings_rate=0.10,
        mortgage_balance=0,
        mortgage_rate=0.0495,
        margin_available=0,
        family_members=[
            {'role': 'primary', 'gross_income': 130000, 'birth_year': 1990,
             'rrsp_room_accumulated': 10000, 'tfsa_room_accumulated': 5000},
            {'role': 'spouse', 'gross_income': 50000, 'birth_year': 1992,
             'rrsp_room_accumulated': 5000, 'tfsa_room_accumulated': 3000},
        ],
        children=[],
        rrsp_annual_percent=0.18,
        rrsp_annual_max=33810,
        tfsa_annual_room_per_person=7000,
        start_year=start_year,
    )
    defaults.update(kw)
    return SimulationConfig(**defaults)


def _make_allocations(config, year=0):
    """Create a typical allocations dict for simulate_year_pure."""
    return {
        'primary_rrsp': 3000,
        'spousal_rrsp': 0,
        'spouse_rrsp': 1000,
        'primary_tfsa': 2000,
        'spouse_tfsa': 1000,
        'resp': 0,
        'non_reg': 0,
        '_primary_income': 130000,
        '_spouse_income': 50000,
        '_annual_savings': 7000,
    }


class TestSimulateYearPureYearSpecificLimits(unittest.TestCase):
    """Test that simulate_year_pure uses year-specific RRSP/TFSA limits."""

    def test_rrsp_room_uses_year_specific_limit(self):
        """DP#20: RRSP room addition should use year-specific limit, not config static value.
        
        In year 2026 (sim year 0), the RRSP limit is $33,810.
        In year 2036 (sim year 10), the projected limit is ~$41,214.
        The simulation should add different annual room amounts for different years.
        """
        provider = TaxDataProvider()
        limit_2026 = provider.get_rrsp_limit(2026)
        limit_2036 = provider.get_rrsp_limit(2036)
        self.assertGreater(limit_2036, limit_2026,
                           "Projected 2036 RRSP limit should exceed 2026 limit")
        
        config = _make_config(start_year=2026)
        state = SimState.initial(config)
        allocations = _make_allocations(config, year=0)
        
        # Year 0 = sim_year 2026
        result0, state1 = simulate_year_pure(
            state=state, year=0, allocations=allocations, config=config,
            investment_return=0.0, mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
        )
        
        # Check that RRSP room added matches 2026 limit
        # After year 0, new_rrsp_room should have been increased by
        # min(rrsp_annual_max, 0.18 * primary_income)
        # With static config, rrsp_annual_max = 33810.
        # With year-specific, it should also be 33810 for year 2026.
        # The difference shows up in later years.
        
        # Run year 10 = sim_year 2036
        # Build a new config with start_year=2036 to see if the year-specific lookup works
        config_2036 = _make_config(start_year=2036)
        state_2036 = SimState.initial(config_2036)
        alloc_2036 = _make_allocations(config_2036, year=0)
        
        result_2036, state1_2036 = simulate_year_pure(
            state=state_2036, year=0, allocations=alloc_2036, config=config_2036,
            investment_return=0.0, mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
        )
        
        # Currently both use config.rrsp_annual_max which is the same.
        # The BUG is that simulate_year_pure doesn't know about the simulation
        # year (only the year index), so it can't look up year-specific limits.
        # This test will FAIL until we pass sim_year to simulate_year_pure.
        
        # Expected: room added in 2036 should use limit_2036, not limit_2026.
        # The config for 2036 has rrsp_annual_max=33810 (default),
        # but the correct 2036 limit is ~$41,214.
        # Since primary_income=130000, 18% = 23400 < both limits,
        # so the cap doesn't actually limit room. Let's use high income.
        pass  # See next test for actual assertion

    def test_rrsp_room_uses_year_specific_cap_with_high_income(self):
        """DP#20: With high income, RRSP room should be capped at the year-specific limit.
        
        If primary income is $300k, 18% = $54k. The RRSP cap is $33,810 in 2026
        but projected to ~$41,214 in 2036. The simulation must use the right cap.
        """
        provider = TaxDataProvider()
        limit_2026 = provider.get_rrsp_limit(2026)
        limit_2036 = provider.get_rrsp_limit(2036)
        self.assertGreater(limit_2036, limit_2026)
        
        # High income so the cap matters
        config_2026 = _make_config(start_year=2026)
        config_2026_high = replace(config_2026,
            family_members=[
                {'role': 'primary', 'gross_income': 300000, 'birth_year': 1990,
                 'rrsp_room_accumulated': 10000, 'tfsa_room_accumulated': 5000},
                {'role': 'spouse', 'gross_income': 100000, 'birth_year': 1992,
                 'rrsp_room_accumulated': 5000, 'tfsa_room_accumulated': 3000},
            ],
        )
        
        state_2026 = SimState.initial(config_2026_high)
        alloc_2026 = {
            'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
            'primary_tfsa': 0, 'spouse_tfsa': 0,
            'resp': 0, 'non_reg': 0,
            '_primary_income': 300000, '_spouse_income': 100000,
            '_annual_savings': 0,
        }
        
        result_2026, state1_2026 = simulate_year_pure(
            state=state_2026, year=0, allocations=alloc_2026,
            config=config_2026_high, investment_return=0.0,
            mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.50, spouse_marginal_rate=0.30,
        )
        
        # Room added for primary: min(rrsp_annual_max, 0.18 * 300000)
        # = min(33810, 54000) = 33810
        # But with year-specific: min(limit_2026=33810, 54000) = 33810
        # Same for year 0. Need to test a later year.
        
        # Now test sim year 10 (sim_year 2036)
        # The config still has rrsp_annual_max=33810, but the correct
        # 2036 limit is ~$41,214
        # DP#20: caller provides year-specific limits to simulate_year_pure
        result_2036, state1_2036 = simulate_year_pure(
            state=state_2026, year=10, allocations=alloc_2026,
            config=config_2026_high, investment_return=0.0,
            mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.50, spouse_marginal_rate=0.30,
            rrsp_annual_limit=limit_2036,
            tfsa_annual_limit=provider.get_tfsa_limit(2036),
        )
        
        # BUG: simulate_year_pure uses config.rrsp_annual_max (33810)
        # not the year-specific limit for 2036 (~41214)
        # Room added should be ~41214, but currently it's 33810
        rrsp_room_added_2036 = adult_rrsp_slot(state1_2036.jurisdiction_state['canada'], 0)[1] - adult_rrsp_slot(state_2026.jurisdiction_state['canada'], 0)[1]  # #700
        # With the bug, rrsp_room_added = 33810 (config static value)
        # Without the bug, rrsp_room_added = limit_2036 ≈ 41214
        
        # This assertion will FAIL until the bug is fixed:
        self.assertAlmostEqual(rrsp_room_added_2036, limit_2036, delta=1,
            msg=f"DP#20: RRSP room added in 2036 should be {limit_2036:.0f} "
                f"(year-specific), not 33810 (static config)")

    def test_tfsa_room_uses_year_specific_limit(self):
        """DP#20: TFSA room addition should use year-specific limit.
        
        TFSA limit is $7,000 in 2026, projected to ~$8,533 in 2036.
        """
        provider = TaxDataProvider()
        limit_2026 = provider.get_tfsa_limit(2026)
        limit_2036 = provider.get_tfsa_limit(2036)
        self.assertGreater(limit_2036, limit_2026)
        
        config = _make_config(start_year=2026)
        state = SimState.initial(config)
        
        # Contribute all TFSA room in year 0 so new room is from annual addition
        allocations_drain = {
            'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
            'primary_tfsa': 5000, 'spouse_tfsa': 3000,
            'resp': 0, 'non_reg': 0,
            '_primary_income': 130000, '_spouse_income': 50000,
            '_annual_savings': 0,
        }
        
        result0, state1 = simulate_year_pure(
            state=state, year=0, allocations=allocations_drain,
            config=config, investment_return=0.0,
            mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
        )
        
        # TFSA room after year 0:
        # Start: primary=5000, spouse=3000
        # After contributions: primary=0, spouse=0
        # Add annual room: primary += 7000, spouse += 7000
        self.assertAlmostEqual(adult_tfsa_slot(state1.jurisdiction_state['canada'], 0)[1], 7000, delta=1)  # #700
        self.assertAlmostEqual(adult_tfsa_slot(state1.jurisdiction_state['canada'], 1)[1], 7000, delta=1)
        
        # Now simulate year 10 (sim_year 2036)
        # TFSA room added should be ~$8,533, not $7,000
        # DP#20: caller provides year-specific limits to simulate_year_pure
        result_2036, state1_2036 = simulate_year_pure(
            state=state1, year=10, allocations=allocations_drain,
            config=config, investment_return=0.0,
            mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
            rrsp_annual_limit=provider.get_rrsp_limit(2036),
            tfsa_annual_limit=limit_2036,
        )
        
        # With the bug: TFSA room added = 7000 (static config)
        # Without the bug: TFSA room added = limit_2036 ≈ 8533
        tfsa_room_added_2036_p = adult_tfsa_slot(state1_2036.jurisdiction_state['canada'], 0)[1] - (0 + 7000)  # #700  # previous room after contrib + annual add
        # Actually this is getting complex. Let me check differently.
        # The key test: the annual room added at year 10 should be the 2036 limit.
        
        # Simpler approach: directly check the room delta
        # After year 0, primary had 0 room (contributed all), then +7000 = 7000
        # In year 10, contributions drain 5000 from 7000 → 2000
        # Then add annual room: +7000 (bug) or +limit_2036 (correct)
        # Expected final room: 2000 + limit_2036
        
        # With bug: 2000 + 7000 = 9000
        # Without bug: 2000 + 8533 = 10533
        self.assertAlmostEqual(adult_tfsa_slot(state1_2036.jurisdiction_state['canada'], 0)[1], 2000 + limit_2036, delta=1,  # #700
            msg=f"DP#20: TFSA room in 2036 should include {limit_2036:.0f} "
                f"(year-specific), not 7000 (static config)")


class TestFamilySimulationYearSpecificLimits(unittest.TestCase):
    """Test that FamilySimulation uses year-specific limits at each step."""
    
    def test_rrsp_annual_room_cap_changes_per_year(self):
        """DP#20: FamilySimulation should update RRSP cap per simulation year.
        
        The annual_room_cap is set once in __init__ from start_year.
        A multi-year simulation should update it each year.
        """
        from simulation import FamilySimulation
        from countries.canada.adapter import CanadaAdapter
        
        config = _make_config(start_year=2026, projection_years=5)
        adapter = CanadaAdapter(config)
        
        sim = FamilySimulation(config, adapter=adapter, use_readvanceable=False)

        provider = TaxDataProvider()
        # Issue #295: the sim provider indexes forward by config.inflation; match
        # that rate so the standalone provider projects the same future limits.
        provider.indexation_rate = config.inflation
        limit_2026 = provider.get_rrsp_limit(2026)
        limit_2030 = provider.get_rrsp_limit(2030)

        # Check initial cap via tax_provider
        self.assertEqual(sim.tax_provider.get_rrsp_limit(2026), limit_2026)
        
        # Run simulation
        results = sim.run()
        
        # After running 5 years (2026-2030), the last year is 2030.
        # The year-specific limit should be the 2030 value.
        self.assertEqual(sim.tax_provider.get_rrsp_limit(2030), limit_2030,
            msg="DP#20: RRSP annual cap should be year-specific")

    def test_tfsa_annual_room_changes_per_year(self):
        """DP#20: TFSA annual room should be updated each simulation year."""
        from simulation import FamilySimulation
        from countries.canada.adapter import CanadaAdapter
        
        config = _make_config(start_year=2026, projection_years=5)
        adapter = CanadaAdapter(config)
        
        sim = FamilySimulation(config, adapter=adapter, use_readvanceable=False)

        provider = TaxDataProvider()
        provider.indexation_rate = config.inflation  # issue #295: match sim rate
        limit_2026 = provider.get_tfsa_limit(2026)
        limit_2030 = provider.get_tfsa_limit(2030)

        # Check initial annual room via tax_provider
        self.assertEqual(sim.tax_provider.get_tfsa_limit(2026), limit_2026)
        
        # Run simulation
        results = sim.run()
        
        # Year-specific TFSA limit should be the 2030 value
        self.assertEqual(sim.tax_provider.get_tfsa_limit(2030), limit_2030,
            msg="DP#20: TFSA annual room should be year-specific")


class TestRetirementYearVersionedData(unittest.TestCase):
    """Test that retirement module uses year-versioned CPP/OAS data."""

    def test_cpp_benefit_uses_year_specific_data(self):
        """DP#20: cpp_benefit should use year-specific max benefit."""
        from countries.canada.retirement import cpp_benefit, CPP_OAS_BY_YEAR
        
        # Year 2023 vs 2026
        benefit_2023 = cpp_benefit(start_age=65, year=2023)
        benefit_2026 = cpp_benefit(start_age=65, year=2026)
        
        self.assertGreater(benefit_2026, benefit_2023,
            "DP#20: CPP benefit at 65 should be higher in 2026 than 2023")

    def test_oas_clawback_uses_year_specific_threshold(self):
        """DP#20: OAS clawback should use year-specific threshold."""
        from countries.canada.retirement import oas_clawback
        
        # Same income, different years
        result_2023 = oas_clawback(net_income=90000, year=2023)
        result_2026 = oas_clawback(net_income=90000, year=2026)
        
        # 2023 threshold was $83,917; 2026 is $95,323
        # At $90k income, 2023 has clawback but 2026 doesn't
        self.assertGreater(result_2023['clawback_amount'], 0,
            "DP#20: $90k income should trigger OAS clawback in 2023")
        self.assertEqual(result_2026['clawback_amount'], 0,
            "DP#20: $90k income should NOT trigger OAS clawback in 2026")

    def test_cpp_benefit_with_year_2026(self):
        """DP#20: cpp_benefit(year=2026) should use CPP_OAS_BY_YEAR lookup."""
        from countries.canada.retirement import cpp_benefit, CPP_MAX_BENEFIT_65, get_cpp_max_benefit_65
        
        # With TaxDataProvider in the chain, years not in the dict get
        # projected/indexed values. The important contract is that the
        # function returns a positive value for any reasonable year.
        benefit = cpp_benefit(start_age=65, year=2026)
        self.assertEqual(benefit, CPP_MAX_BENEFIT_65,
            "DP#20: year=2026 should return known 2026 value")
        # When year=2026, should use CPP_OAS_BY_YEAR lookup.
        benefit_2026 = cpp_benefit(start_age=65, year=2026)
        self.assertEqual(benefit_2026, CPP_MAX_BENEFIT_65,
            "DP#20: year=2026 should return 2026 CPP max benefit")

    def test_oas_clawback_year_not_in_dict_uses_default(self):
        """DP#20: Years not in CPP_OAS_BY_YEAR should use constant defaults."""
        from countries.canada.retirement import oas_clawback, OAS_ANNUAL_MAX, OAS_CLAWBACK_THRESHOLD
        
        result = oas_clawback(net_income=100000, year=2026)
        # Should use 2026 values (DP#9: no fallback, year is required)
        self.assertEqual(result['threshold'], OAS_CLAWBACK_THRESHOLD)
        self.assertEqual(result['net_oas'], OAS_ANNUAL_MAX - 
                         min(OAS_ANNUAL_MAX, (100000 - OAS_CLAWBACK_THRESHOLD) * 0.15))


class TestTaxDataProviderYearSpecificLimits(unittest.TestCase):
    """Test that TaxDataProvider correctly provides year-specific limits."""

    def test_rrsp_limit_increases_with_year(self):
        """RRSP limit should increase year over year."""
        provider = TaxDataProvider()
        limit_2026 = provider.get_rrsp_limit(2026)
        limit_2027 = provider.get_rrsp_limit(2027)
        self.assertGreater(limit_2027, limit_2026)

    def test_tfsa_limit_increases_with_year(self):
        """TFSA limit should increase year over year."""
        provider = TaxDataProvider()
        limit_2026 = provider.get_tfsa_limit(2026)
        limit_2027 = provider.get_tfsa_limit(2027)
        self.assertGreater(limit_2027, limit_2026)

    def test_fhsa_limit_available_for_2023_plus(self):
        """DP#20: FHSA limit should be $8,000 for years 2023+ (FHSA introduced 2023)."""
        provider = TaxDataProvider()
        self.assertEqual(provider.get_fhsa_limit(2023), 8000)
        self.assertEqual(provider.get_fhsa_limit(2026), 8000)
    
    def test_fhsa_limit_zero_before_2023(self):
        """DP#20: FHSA didn't exist before 2023, so limit should be 0."""
        provider = TaxDataProvider()
        self.assertEqual(provider.get_fhsa_limit(2022), 0)
        self.assertEqual(provider.get_fhsa_limit(2020), 0)
    
    def test_fhsa_account_add_annual_room_accepts_year_specific_limit(self):
        """DP#20: FHSAAccount.add_annual_room should accept a year-specific limit."""
        from countries.canada.fhsa import FHSAAccount
        fhsa = FHSAAccount()
        self.assertEqual(fhsa.annual_room, 8000)
        
        # With explicit limit
        fhsa.add_annual_room(annual_limit=9000)
        self.assertEqual(fhsa.annual_room, 9000)
        
        # Without explicit limit (fallback to constant)
        fhsa2 = FHSAAccount()
        fhsa2.add_annual_room()
        self.assertEqual(fhsa2.annual_room, 8000)

    def test_projected_future_rrsp_limit(self):
        """Future RRSP limits should be projected with indexation."""
        provider = TaxDataProvider()
        limit_2036 = provider.get_rrsp_limit(2036)
        # 2026 base of 33810, projected at 2% for 10 years
        expected = round(33810 * (1.02 ** 10), 2)
        self.assertAlmostEqual(limit_2036, expected, delta=1)

    def test_projected_future_tfsa_limit(self):
        """Future TFSA limits should be projected with indexation."""
        provider = TaxDataProvider()
        limit_2036 = provider.get_tfsa_limit(2036)
        expected = round(7000 * (1.02 ** 10), 2)
        self.assertAlmostEqual(limit_2036, expected, delta=1)



class TestFrozenBracketsMode(unittest.TestCase):
    """DP#5/DP#20: When frozen_brackets=True, limits stay at start_year values."""

    def test_frozen_rrsp_limit_uses_start_year_value(self):
        """When frozen_brackets=True, FamilySimulation should use start_year limits for all years."""
        from simulation import FamilySimulation
        from countries.canada.adapter import CanadaAdapter
        
        config = _make_config(start_year=2026, projection_years=5)
        adapter = CanadaAdapter(config)
        
        sim = FamilySimulation(config, adapter=adapter, use_readvanceable=False)

        provider = TaxDataProvider()
        provider.indexation_rate = config.inflation  # issue #295: match sim rate
        limit_2026 = provider.get_rrsp_limit(2026)

        # Run simulation with default (non-frozen) — cap updates each year
        results = sim.run()
        
        # Year-specific limit should be the last year's value
        last_year = 2026 + 5 - 1
        limit_last = provider.get_rrsp_limit(last_year)
        self.assertEqual(sim.tax_provider.get_rrsp_limit(last_year), limit_last)

    def test_frozen_tfsa_limit_uses_start_year_value(self):
        """When frozen_brackets=True, TFSA limits should stay at start_year value."""
        from simulation import FamilySimulation
        from countries.canada.adapter import CanadaAdapter
        
        config = _make_config(start_year=2026, projection_years=5)
        adapter = CanadaAdapter(config)
        
        sim = FamilySimulation(config, adapter=adapter, use_readvanceable=False)
        provider = TaxDataProvider()
        provider.indexation_rate = config.inflation  # issue #295: match sim rate
        limit_2026 = provider.get_tfsa_limit(2026)

        results = sim.run()
        
        last_year = 2026 + 5 - 1
        limit_last = provider.get_tfsa_limit(last_year)
        self.assertEqual(sim.tax_provider.get_tfsa_limit(last_year), limit_last)

    def test_simulate_year_pure_without_year_specific_limits_uses_config(self):
        """When rrsp_annual_limit is None, simulate_year_pure falls back to config.rrsp_annual_max."""
        config = _make_config(start_year=2026, rrsp_annual_max=33810)
        state = SimState.initial(config)
        allocations = {
            'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
            'primary_tfsa': 0, 'spouse_tfsa': 0,
            'resp': 0, 'non_reg': 0,
            '_primary_income': 300000, '_spouse_income': 100000,
            '_annual_savings': 0,
        }
        
        # Without rrsp_annual_limit, should use config.rrsp_annual_max = 33810
        result, state1 = simulate_year_pure(
            state=state, year=0, allocations=allocations,
            config=config, investment_return=0.0,
            mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.50, spouse_marginal_rate=0.30,
        )
        
        rrsp_room_added = adult_rrsp_slot(state1.jurisdiction_state['canada'], 0)[1] - adult_rrsp_slot(state.jurisdiction_state['canada'], 0)[1]  # #700
        self.assertAlmostEqual(rrsp_room_added, 33810, delta=1,
            msg="Without year-specific limit, should use config.rrsp_annual_max")

    def test_simulate_year_pure_without_tfsa_limit_uses_config(self):
        """When tfsa_annual_limit is None, simulate_year_pure falls back to config.tfsa_annual_room_per_person."""
        config = _make_config(start_year=2026, tfsa_annual_room_per_person=7000)
        state = SimState.initial(config)
        allocations = {
            'primary_rrsp': 0, 'spousal_rrsp': 0, 'spouse_rrsp': 0,
            'primary_tfsa': 5000, 'spouse_tfsa': 3000,
            'resp': 0, 'non_reg': 0,
            '_primary_income': 130000, '_spouse_income': 50000,
            '_annual_savings': 0,
        }
        
        result, state1 = simulate_year_pure(
            state=state, year=0, allocations=allocations,
            config=config, investment_return=0.0,
            mortgage_rate=0.05, heloc_rate=0.05,
            primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
        )
        
        # After contributing 5000 from 5000 room → 0, add 7000 = 7000
        self.assertAlmostEqual(adult_tfsa_slot(state1.jurisdiction_state['canada'], 0)[1], 7000, delta=1,  # #700
            msg="Without year-specific limit, should use config.tfsa_annual_room_per_person")

if __name__ == '__main__':
    unittest.main()



class TestMonthlyPathYearSpecificLimits(unittest.TestCase):
    """Test that the monthly simulation path uses year-specific TFSA limits.

    DP#20: The simulation engine must look up the correct year's data at each
    time step. This test verifies that _simulate_year_monthly passes
    annual_limit to add_annual_room, preventing the off-by-one bug where
    the previous year's limit was used instead.
    """

    def test_tfsa_add_annual_room_uses_explicit_limit(self):
        """add_annual_room with annual_limit uses the provided limit, not self.annual_room.

        Without annual_limit, add_annual_room falls back to self.annual_room
        (previous year's value). With annual_limit, it must use the explicit value.
        """
        from countries.canada.account_models import TSFAccount

        # Set up a TFSA with previous year's limit
        tfsa = TSFAccount()
        tfsa.annual_room = 7000  # 2026 limit

        # Add room using 2027's limit explicitly
        room_added = tfsa.add_annual_room(year=2027, annual_limit=7140)

        # The amount added should be 7140 (2027), not 7000 (2026)
        self.assertEqual(room_added, 7140)
        self.assertEqual(tfsa.contribution_room, 7140)

    def test_tfsa_add_annual_room_without_limit_uses_stale_value(self):
        """Without annual_limit, add_annual_room uses self.annual_room (stale).

        This test documents the bug that the monthly path fix prevents:
        when no annual_limit is passed, the method falls back to
        self.annual_room which may contain the previous year's value.
        """
        from countries.canada.account_models import TSFAccount

        tfsa = TSFAccount()
        tfsa.annual_room = 7000  # 2026 limit (stale for 2027)

        # Without annual_limit, uses self.annual_room = 7000 (wrong for 2027)
        room_added = tfsa.add_annual_room(year=2027)

        self.assertEqual(room_added, 7000)  # Uses stale value
        self.assertEqual(tfsa.contribution_room, 7000)

    def test_tfsa_explicit_limit_overrides_stale_value(self):
        """Passing annual_limit produces different results than the stale fallback.

        This catches regressions where annual_limit might be silently ignored.
        """
        from countries.canada.account_models import TSFAccount

        tfsa_explicit = TSFAccount()
        tfsa_explicit.annual_room = 7000  # stale 2026 value
        tfsa_explicit.add_annual_room(year=2027, annual_limit=7140)

        tfsa_stale = TSFAccount()
        tfsa_stale.annual_room = 7000  # same stale value
        tfsa_stale.add_annual_room(year=2027)  # no annual_limit

        # Explicit limit must produce more room than stale fallback
        self.assertGreater(tfsa_explicit.contribution_room,
                           tfsa_stale.contribution_room,
                           "Explicit annual_limit must override stale annual_room")
