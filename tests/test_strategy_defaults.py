#!/usr/bin/env python3
"""Tests for issue #23: discover_strategies must not encode financial
assumptions as parameter defaults.

The function previously had investment_return=0.07 and heloc_rate=0.05
as hardcoded defaults. These are financial opinions that should come from
the simulation's ReturnModel or be explicitly required from the caller.

Run: python3 tests/test_strategy_defaults.py
"""

import unittest

from countries.canada.strategies import discover_strategies
from return_model import FixedReturn, VariableReturn
from strategy import FamilyState


class TestDiscoverStrategiesRequiresFinancialParams(unittest.TestCase):
    """Issue #23: discover_strategies must not hardcode financial assumptions."""

    def _state(self, marginal=0.4571):
        return FamilyState(primary_marginal_rate=marginal)

    # -- investment_return must be provided explicitly or via return_model --

    def test_missing_investment_return_raises_type_error(self):
        """Calling without investment_return or return_model must raise TypeError."""
        state = self._state()
        with self.assertRaises(TypeError):
            discover_strategies(
                state,
                mortgage_cfg={'heloc_readvance': True},
                heloc_rate=0.05,
            )

    def test_missing_heloc_rate_raises_type_error(self):
        """Calling without heloc_rate must raise TypeError."""
        state = self._state()
        with self.assertRaises(TypeError):
            discover_strategies(
                state,
                mortgage_cfg={'heloc_readvance': True},
                investment_return=0.07,
            )

    def test_missing_all_financial_params_raises_type_error(self):
        """Calling with no financial params at all must raise TypeError."""
        state = self._state()
        with self.assertRaises(TypeError):
            discover_strategies(state)

    def test_missing_all_financial_params_with_mortgage_cfg_raises_type_error(self):
        """Even with mortgage_cfg, missing financial params must raise TypeError."""
        state = self._state()
        with self.assertRaises(TypeError):
            discover_strategies(state, mortgage_cfg={'heloc_readvance': True})

    # -- return_model can provide investment_return --

    def test_return_model_provides_investment_return(self):
        """When return_model is given, investment_return is derived from it."""
        state = self._state()
        model = FixedReturn(rate=0.07)
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            return_model=model,
            heloc_rate=0.05,
        )
        # At 7% return, 5% HELOC, 45.71% marginal — readvance should be discovered
        self.assertIn('readvance_priority', discovered)

    def test_return_model_low_return_excludes_readvance(self):
        """Low return via ReturnModel should exclude readvance_priority."""
        state = self._state()
        model = FixedReturn(rate=0.02)  # Low return
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            return_model=model,
            heloc_rate=0.05,
        )
        self.assertNotIn('readvance_priority', discovered)
        self.assertIn('no_readvance', discovered)

    def test_variable_return_model_provides_investment_return(self):
        """VariableReturn model should provide investment_return from year 0."""
        state = self._state()
        model = VariableReturn(rates=[0.07, 0.08, 0.06], fallback=0.07)
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            return_model=model,
            heloc_rate=0.05,
        )
        self.assertIn('readvance_priority', discovered)

    # -- explicit investment_return still works --

    def test_explicit_investment_return_and_heloc_rate(self):
        """Explicit investment_return and heloc_rate should still work."""
        state = self._state()
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            investment_return=0.07,
            heloc_rate=0.05,
        )
        self.assertIn('readvance_priority', discovered)

    def test_explicit_low_return_excludes_readvance(self):
        """Explicit low investment_return should exclude readvance."""
        state = self._state()
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            investment_return=0.02,
            heloc_rate=0.05,
        )
        self.assertNotIn('readvance_priority', discovered)

    # -- return_model takes precedence over explicit investment_return --

    def test_return_model_overrides_explicit_investment_return(self):
        """When both return_model and investment_return are given,
        return_model should take precedence."""
        state = self._state()
        model = FixedReturn(rate=0.02)  # Low via model
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            return_model=model,
            investment_return=0.07,  # High explicit — but model wins
            heloc_rate=0.05,
        )
        # With model at 2%, readvance should NOT be discovered
        self.assertNotIn('readvance_priority', discovered)

    # -- capital_gains_inclusion still defaults to 0.50 --

    def test_capital_gains_inclusion_defaults_to_50(self):
        """capital_gains_inclusion=0.50 is a tax rule, not a financial opinion.
        It should remain as a default."""
        state = self._state(marginal=0.4571)
        # With 7% return and 5% HELOC, profitability depends on CG inclusion
        # At 50% inclusion: after_tax_return = 0.07*(1-0.4571*0.50) = 5.40% > after_tax_heloc = 2.71%
        discovered = discover_strategies(
            state,
            mortgage_cfg={'heloc_readvance': True},
            investment_return=0.07,
            heloc_rate=0.05,
            # capital_gains_inclusion NOT specified — uses default 0.50
        )
        self.assertIn('readvance_priority', discovered)

    # -- balanced and rrsp_max always available regardless of params --

    def test_balanced_and_rrsp_always_available(self):
        """Balanced and RRSP max strategies are always available."""
        state = self._state()
        discovered = discover_strategies(
            state,
            investment_return=0.07,
            heloc_rate=0.05,
        )
        self.assertIn('balanced', discovered)
        self.assertIn('rrsp_max', discovered)


if __name__ == '__main__':
    unittest.main()
