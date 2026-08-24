#!/usr/bin/env python3
"""Tests for issue #289: log warning when non-reg growth falls back to flat investment_return rate.

DP#27: simulation_state.py silently falls back from non_reg_after_tax_return
to investment_return when portfolio composition data is missing. Adding a warning log
improves transparency for decisions worth hundreds of thousands of dollars.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import logging
from io import StringIO

from simulation_state import SimState, simulate_year_pure
from simulation_config import SimulationConfig
from countries.canada.strategies import STRATEGY_BALANCED


def _make_config(start_year=2026):
    """Create a minimal config for testing."""
    defaults = dict(
        projection_years=2,
        investment_return=0.07,
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
    return SimulationConfig(**defaults)


def _make_allocations(config):
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


class TestNonRegFallbackWarning(unittest.TestCase):
    """Verify that a warning is logged when non_reg_after_tax_return falls back."""

    def test_warning_logged_when_non_reg_after_tax_return_is_none(self):
        """When non_reg_after_tax_return is None, a warning should be logged."""
        config = _make_config()
        state = SimState.initial(config)
        allocations = _make_allocations(config)

        # Capture log output. Issue #584: the non_reg_growth rule (and its
        # fallback warning) now lives in rules_growth.py, registered
        # into simulate_year_pure's rule fold -- same warning, moved module.
        logger = logging.getLogger('rules_growth')
        handler = logging.StreamHandler(StringIO())
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        original_level = logger.level
        logger.setLevel(logging.WARNING)

        try:
            # Call simulate_year_pure without non_reg_after_tax_return
            result, next_state = simulate_year_pure(
                state=state, year=0, allocations=allocations, config=config,
                investment_return=0.07, mortgage_rate=0.05, heloc_rate=0.05,
                primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
                # non_reg_after_tax_return is NOT provided → should trigger warning
            )
            log_output = handler.stream.getvalue()

            # Should contain the warning about fallback
            self.assertIn('non_reg_after_tax_return', log_output.lower())
            self.assertIn('falling back', log_output.lower())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)

    def test_no_warning_when_non_reg_after_tax_return_provided(self):
        """When non_reg_after_tax_return is provided, no warning should be logged."""
        config = _make_config()
        state = SimState.initial(config)
        allocations = _make_allocations(config)

        # Capture log output. Issue #584: see the other test in this class
        # for why this targets the 'rules_growth' logger now.
        logger = logging.getLogger('rules_growth')
        handler = logging.StreamHandler(StringIO())
        handler.setLevel(logging.WARNING)
        logger.addHandler(handler)
        original_level = logger.level
        logger.setLevel(logging.WARNING)

        try:
            # Call simulate_year_pure WITH non_reg_after_tax_return
            result, next_state = simulate_year_pure(
                state=state, year=0, allocations=allocations, config=config,
                investment_return=0.07, mortgage_rate=0.05, heloc_rate=0.05,
                primary_marginal_rate=0.40, spouse_marginal_rate=0.20,
                non_reg_after_tax_return=0.04,  # Provided → should NOT trigger warning
            )
            log_output = handler.stream.getvalue()

            # Should NOT contain the warning about fallback
            self.assertNotIn('falling back', log_output.lower())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(original_level)


if __name__ == '__main__':
    unittest.main()