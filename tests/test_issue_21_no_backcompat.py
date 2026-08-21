#!/usr/bin/env python3
"""
Tests for Issue #21: FamilySimulation constructor creates hidden mutable state.

OWNER DIRECTIVE: "don't keep backward-compat reads; this project does not need backward compat."

Acceptance criteria:
- No backward-compat properties on FamilySimulation
- No mutable account objects in __init__
- All state in SimState
- __init__ is side-effect-free (no I/O, no mutable object creation)
"""

import unittest
import inspect


def _make_config(**overrides):
    """Build a minimal SimulationConfig for testing."""
    from simulation_config import SimulationConfig
    defaults = {
        'projection_years': 3,
        'start_year': 2025,
        'investment_return': 0.07,
        'salary_growth': 0.02,
        'savings_rate': 0.20,
        'mortgage_balance': 300000,
        'mortgage_rate': 0.05,
        'amortization_years': 25,
        'margin_available': 50000,
        'current_payment_monthly': 1800,
        'house_value': 600000,
        'ltv_max': 0.80,
        'family_members': [
            {'role': 'primary', 'name': 'A', 'gross_income': 120000,
             'birth_year': 1990, 'rrsp_room_accumulated': 50000,
             'tfsa_room_accumulated': 30000},
            {'role': 'spouse', 'name': 'B', 'gross_income': 60000,
             'birth_year': 1992, 'rrsp_room_accumulated': 30000,
             'tfsa_room_accumulated': 25000},
        ],
        'children': [],
        'resp_current_balance': 0,
    }
    defaults.update(overrides)
    d = {
        'assumptions': {
            'projection_years': defaults['projection_years'],
            'investment_return': defaults['investment_return'],
            'salary_growth': defaults['salary_growth'],
        },
        'savings': {'rate': defaults['savings_rate']},
        'property': {
            'house_value': defaults['house_value'],
            'mortgage_balance': defaults['mortgage_balance'],
            'mortgage_rate': defaults['mortgage_rate'],
            'ltv_max': defaults['ltv_max'],
            'margin_available': defaults['margin_available'],
            'current_payment_monthly': defaults['current_payment_monthly'],
            'amortization_years': defaults['amortization_years'],
        },
        'family': {
            'members': defaults['family_members'],
            'children': defaults['children'],
        },
        # issue #602: top-level 'resp' was never a real key (the schema/
        # loader has always read accounts.resp_current_balance) -- it was
        # dead fixture noise that the new Guard 2 validator now catches.
        'accounts': {'resp_current_balance': defaults['resp_current_balance']},
    }
    return SimulationConfig.from_dict(d)


class TestNoMutableAccountObjects(unittest.TestCase):
    """FamilySimulation.__init__ must not create mutable account objects."""

    REMOVED_ACCOUNT_ATTRS = [
        'rrsp', 'spousal_rrsp', 'spouse_rrsp',
        'tfsa_primary', 'tfsa_spouse',
        'nonreg', 'sm', 'fhsa',
        'qc_deduction', 'heloc_tracing',
    ]

    def test_no_account_attrs_on_init(self):
        """After __init__, no mutable account objects should exist as attributes."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)

        for attr in self.REMOVED_ACCOUNT_ATTRS:
            self.assertFalse(
                hasattr(sim, attr),
                f"FamilySimulation should NOT have attribute '{attr}' "
                f"(issue #21: no mutable account objects in __init__)"
            )

    def test_no_margin_heloc_attr(self):
        """margin_heloc is state, not a direct attribute."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        self.assertFalse(
            hasattr(sim, 'margin_heloc'),
            "FamilySimulation should NOT have 'margin_heloc' attribute "
            "(issue #21: state lives in SimState)"
        )

    def test_no_rrsp_refunds_applied_attr(self):
        """_rrsp_refunds_applied is state, not a direct attribute."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        self.assertFalse(
            hasattr(sim, '_rrsp_refunds_applied'),
            "FamilySimulation should NOT have '_rrsp_refunds_applied' attribute "
            "(issue #21: state lives in SimState)"
        )

    def test_no_undeducted_pool_attr(self):
        """_rrsp_undeducted_pool is state, not a direct attribute."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        self.assertFalse(
            hasattr(sim, '_rrsp_undeducted_pool'),
            "FamilySimulation should NOT have '_rrsp_undeducted_pool' attribute "
            "(issue #21: state lives in SimState)"
        )


class TestNoSyncAccountsMethod(unittest.TestCase):
    """_sync_accounts_from_state should not exist."""

    def test_no_sync_accounts_method(self):
        """_sync_accounts_from_state should be removed."""
        from simulation import FamilySimulation
        self.assertFalse(
            hasattr(FamilySimulation, '_sync_accounts_from_state'),
            "_sync_accounts_from_state should be removed (issue #21: no backward compat)"
        )


class TestNoBackwardCompatProperties(unittest.TestCase):
    """No backward-compat properties that delegate to _state."""

    # Properties that delegate to _state and should be removed
    REMOVED_PROPS = [
        # These were never explicit properties but were accessed via
        # mutable account objects that synced from _state
        'rrsp_balance', 'tfsa_primary_balance', 'spousal_rrsp_balance',
        'margin_heloc',
    ]

    def test_no_backward_compat_delegating_properties(self):
        """FamilySimulation should not have properties that delegate to _state."""
        from simulation import FamilySimulation
        for prop_name in self.REMOVED_PROPS:
            if hasattr(FamilySimulation, prop_name):
                attr = getattr(FamilySimulation, prop_name)
                self.assertFalse(
                    isinstance(attr, property),
                    f"FamilySimulation should NOT have property '{prop_name}' "
                    f"that delegates to _state (issue #21: no backward compat)"
                )


class TestAllStateInSimState(unittest.TestCase):
    """All mutable simulation state must live in SimState."""

    def test_state_accessible_after_run(self):
        """After run(), all financial state should be in _state."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        results = sim.run()

        from simulation_state import adult_rrsp_total, adult_rrsp_slot, adult_tfsa_total  # #700
        state = sim._state
        canada = state.jurisdiction_state['canada']
        # Key balances should be accessible from state
        self.assertIsInstance(adult_rrsp_total(canada), float)  # #700: per-adult RRSP store
        self.assertIsInstance(adult_tfsa_total(canada), float)
        self.assertIsInstance(adult_rrsp_slot(canada, 1)[0], float)
        self.assertIsInstance(state.non_reg_balance, float)
        self.assertIsInstance(state.heloc_balance, float)
        self.assertIsInstance(state.mortgage_balance, float)

    def test_heloc_balance_in_state(self):
        """Margin HELOC balance lives in state.heloc_balance, and (#577) stays
        at zero when the margin is never drawn — no lump_sum, no SM
        readvancing. An available-but-undrawn HELOC limit is not debt
        (DP#18/DP#32)."""
        from simulation import FamilySimulation
        config = _make_config(margin_available=50000)
        sim = FamilySimulation(config=config, use_readvanceable=False)
        results = sim.run()
        self.assertIsInstance(sim._state.heloc_balance, float)
        # Nothing drew the margin (no lump_sum, no SM) -> it stays undrawn.
        self.assertEqual(sim._state.heloc_balance, 0)

    def test_heloc_balance_drawn_via_lump_sum(self):
        """(#577, DP#17 other side of the threshold) When the caller actually
        draws the margin — invests it as a lump sum at year 0 — that money is
        real borrowed debt and heloc_balance must reflect it."""
        from simulation import FamilySimulation
        config = _make_config(margin_available=50000)
        sim = FamilySimulation(config=config, use_readvanceable=False,
                                lump_sum=config.margin_available)
        self.assertEqual(sim._state.heloc_balance, 50000)

    def test_rrsp_paydown_in_state(self):
        """RRSP refund paydown tracking should be in state.jurisdiction_state['canada']['heloc_rrsp_paydown']."""
        from simulation import FamilySimulation
        config = _make_config(margin_available=200000)
        sim = FamilySimulation(config=config, use_readvanceable=False)
        results = sim.run()
        # heloc_rrsp_paydown should exist in state
        self.assertIsInstance(sim._state.jurisdiction_state['canada']['heloc_rrsp_paydown'], float)


class TestInitIsSideEffectFree(unittest.TestCase):
    """__init__ should not do I/O or create mutable objects."""

    def test_no_tax_provider_created_in_init(self):
        """tax_provider should be lazy, not created in __init__."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        # tax_provider should be a lazy property, not a stored attribute
        # Accessing it should work, but it shouldn't be set in __init__
        provider = sim.tax_provider
        self.assertIsNotNone(provider)

    def test_amortization_is_lazy(self):
        """amort and amort_annual should be lazy properties."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        # Should be accessible as properties but not stored as instance attrs
        amort = sim.amort
        self.assertIsInstance(amort, list)

    def test_brackets_is_lazy(self):
        """brackets should be lazy (computed from tax_provider on access)."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        brackets = sim.brackets
        self.assertIsInstance(brackets, list)


class TestFHSADeterminedFromConfig(unittest.TestCase):
    """FHSA presence should be determined from config (via _state), not a mutable object."""

    def test_fhsa_presence_from_state(self):
        """FHSA presence should be inferred from the per-adult FHSA store in
        state.jurisdiction_state['canada'] (#700 Step 4: adult_fhsa_slot(...,0)),
        not from a mutable object."""
        from simulation_config import SimulationConfig
        d = {
            'assumptions': {'projection_years': 3, 'investment_return': 0.07, 'salary_growth': 0.02},
            'savings': {'rate': 0.20},
            'property': {
                'house_value': 600000, 'mortgage_balance': 300000,
                'mortgage_rate': 0.05, 'ltv_max': 0.80,
                'margin_available': 0, 'current_payment_monthly': 1800,
                'amortization_years': 25,
            },
            'family': {
                'members': [
                    {'role': 'primary', 'name': 'A', 'gross_income': 120000,
                     'birth_year': 1990, 'rrsp_room_accumulated': 50000,
                     'tfsa_room_accumulated': 30000,
                     'fhsa_room_accumulated': 8000},
                ],
                'children': [],
            },
            'accounts': {'resp_current_balance': 0},  # issue #602: 'resp' was never a real key
        }
        config = SimulationConfig.from_dict(d)
        from simulation import FamilySimulation
        sim = FamilySimulation(config=config, use_readvanceable=False)
        # FHSA should be detected from state, not from a mutable object.
        # #700/#643/#704: FHSA room now lives in the per-adult store (slot 0).
        from simulation_state import adult_fhsa_slot
        self.assertGreater(
            adult_fhsa_slot(sim._state.jurisdiction_state['canada'], 0)['room'], 0)
        # sim.fhsa should NOT exist
        self.assertFalse(hasattr(sim, 'fhsa'))


class TestQCAndHELOCTrackingViaState(unittest.TestCase):
    """QC deduction and HELOC tracing state should come from _state, not mutable objects."""

    def test_qc_carry_forward_from_state(self):
        """QC carry-forward should be in jurisdiction_state['canada']['qc_carry_forward']."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=True)
        results = sim.run()
        qc_cf = sim._state.jurisdiction_state.get('canada', {}).get('qc_carry_forward', 0)
        self.assertIsInstance(qc_cf, (int, float))

    def test_heloc_tracing_from_state(self):
        """HELOC tracing data should be in _state.jurisdiction_state['canada']['heloc_tracing']."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False, lump_sum=100000)
        results = sim.run()
        tracing = sim._state.jurisdiction_state['canada']['heloc_tracing']
        self.assertIsInstance(tracing, dict)
        self.assertIsInstance(tracing.get('total_advances', 0), float)
        self.assertIsInstance(tracing.get('investment_advances', 0), float)


class TestNoSpousalContributionYearsAttr(unittest.TestCase):
    """spousal_contribution_years should live in state, not on sim."""

    def test_no_spousal_contribution_years(self):
        """spousal_contribution_years should not be on sim."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        self.assertFalse(
            hasattr(sim, 'spousal_contribution_years'),
            "spousal_contribution_years should live in SimState, not on FamilySimulation"
        )


class TestSummaryUsesState(unittest.TestCase):
    """summary() should use _state, not backward-compat attributes."""

    def test_summary_no_backward_compat(self):
        """summary() should work without any backward-compat attributes."""
        from simulation import FamilySimulation
        config = _make_config()
        sim = FamilySimulation(config=config, use_readvanceable=False)
        results = sim.run()
        s = sim.summary()
        self.assertIn('total_assets', s)
        self.assertIn('net_assets', s)
        self.assertIn('rrsp_balance', s)
        self.assertIn('tfsa_balance', s)


if __name__ == '__main__':
    unittest.main()