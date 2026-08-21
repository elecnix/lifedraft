#!/usr/bin/env python3
"""Tests for issue #25: Migrate Canada-specific SimState fields to jurisdiction_state.

OWNER DIRECTIVE: "implement long-term stage 3 right now, with no backward compatibility."

These tests verify that:
1. No Canada-specific fields exist at SimState top-level (dataclass fields)
2. All Canada fields are in jurisdiction_state['canada'] dict
3. simulate_year_pure reads/writes through jurisdiction_state['canada']
4. HelocTracingState and QcDeductionState live in countries/canada/sim_state.py
5. No backward compatibility shims exist (_CanadaProperty, qc_deduction property)
"""

import ast
import dataclasses
import sys
import os
import unittest
from dataclasses import fields

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation_state import SimState, simulate_year_pure, adult_rrsp_slot, adult_rrsp_total, adult_tfsa_slot, adult_tfsa_total  # #700
from simulation_config import SimulationConfig


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


# ── 1. No Canada-specific fields at SimState top-level ─────────────────────

class TestNoCanadaFieldsAtSimStateTopLevel(unittest.TestCase):
    """Verify that Canada-specific fields are NOT dataclass fields on SimState.

    These fields must live in jurisdiction_state['canada'] dict, not as
    individual dataclass fields. This is the core of issue #25.
    """

    # All fields that are Canada-specific and must NOT be SimState dataclass fields
    CANADA_FIELDS = [
        'rrsp_balance', 'rrsp_room',
        'spousal_rrsp_balance', 'spousal_rrsp_room',
        'spouse_rrsp_balance', 'spouse_rrsp_room',
        'tfsa_primary_balance', 'tfsa_primary_room',
        'tfsa_spouse_balance', 'tfsa_spouse_room',
        'resp_balances',
        'readvance_heloc_balance', 'sm_investment_balance',
        'sm_investment_cost_basis',
        'readvance_total_interest_paid', 'readvance_total_tax_saved',
        'heloc_tracing',
        'spousal_contribution_years',
        'rrsp_ledger',
        'rrsp_deduction_carry_forward',
        'heloc_rrsp_paydown',
        'fhsa_balance', 'fhsa_room',
        'fhsa_lifetime_used', 'fhsa_lifetime_limit',
    ]

    def test_no_canada_dataclass_fields(self):
        """Canada-specific fields must NOT be dataclass fields on SimState."""
        simstate_field_names = {f.name for f in fields(SimState)}
        violations = [f for f in self.CANADA_FIELDS if f in simstate_field_names]
        self.assertEqual(
            violations, [],
            f"These Canada-specific fields are still SimState dataclass fields "
            f"(must be in jurisdiction_state['canada'] only): {violations}"
        )

    def test_only_universal_fields_at_top_level(self):
        """Only universal fields should remain on SimState dataclass."""
        simstate_field_names = {f.name for f in fields(SimState)}
        # These are the expected universal fields
        universal_fields = {
            'non_reg_balance', 'non_reg_acb',
            'mortgage_balance', 'heloc_balance',
            # Issue #688: the emergency reserve is a cash sleeve, not a tax
            # construct -- a savings balance held out of the market has the
            # same meaning in every jurisdiction, so it belongs at the top
            # level, not in jurisdiction_state['canada']. (Which ACCOUNT it is
            # carved out of is jurisdiction-shaped, and that resolution happens
            # in input_contract.py, not here.)
            'emergency_reserve_balance',
            # Issue #689: a revolving credit facility (liabilities[kind=
            # line_of_credit]) is a debt, not a tax construct -- "money the
            # household has drawn against a revolving limit" means the same
            # thing in every jurisdiction, exactly like mortgage_balance and
            # heloc_balance above. Whether it is SECURED (and so consumes a
            # property's charge) is jurisdiction-shaped, and that resolution
            # happens in input_contract.py, not here -- the same split
            # emergency_reserve_balance makes.
            'credit_facility_balance',
            # Issue #763: closed-end consumer loans (car_loan/student_loan/
            # personal_loan) -- per-loan DRAWN balances, parallel to
            # SimulationConfig.consumer_loans. Universal, same reasoning as
            # credit_facility_balance above: an amortizing consumer debt is a
            # money-the-household-owes fact that means the same thing in every
            # jurisdiction, not a tax construct. Whether it is SECURED (and so
            # would consume a property's charge) is refused at the contract
            # boundary -- consumer loans are modeled unsecured here.
            'consumer_loan_balances',
            # Issue #759: fixed-term installment obligations -- per-plan
            # remaining-payment balances, parallel to
            # SimulationConfig.installments. Universal, same reasoning as
            # consumer_loan_balances above: a committed payment schedule is
            # a money-the-household-owes fact that means the same thing in
            # every jurisdiction, not a tax construct. Whether it is
            # non-discretionary is enforced at the contract boundary.
            'installment_balances',
            # Issue #692: per-property net equity (value minus the mortgage/HELOC
            # secured against it) for couple-owned non-principal real estate,
            # parallel to SimulationConfig.properties. Universal, same reasoning
            # as consumer_loan_balances above: real estate a household owns is a
            # net-worth fact that means the same thing in every jurisdiction; the
            # jurisdiction-shaped parts (CCA, PRE, recapture) are later bites and
            # resolved outside this seam, not here.
            'property_equities',
            # Issue #936: balance held in a declared deposit product (a HISA/GIC
            # a household funds via a rate schedule), parallel to
            # SimulationConfig.deposit_products. Universal, same reasoning as
            # consumer_loan_balances above: a cash deposit balance is a net-worth
            # fact that means the same thing in every jurisdiction; the
            # interest-tax character is applied at the growth rule, not here.
            'deposit_product_balance',
            # Issue #967: the outstanding balances of mid-horizon mortgages
            # originated by properties' `purchase.financing`, parallel to
            # SimulationConfig.properties. Universal, same reasoning as
            # consumer_loan_balances above: an amortizing mortgage is a
            # balance-sheet liability that means the same thing in every
            # jurisdiction; the interest's tax character (deductible for a
            # rental, non-deductible for a cottage) is applied at the rental
            # fold, not here.
            'second_property_mortgage_balances',
            'jurisdiction_state',
        }
        # Every SimState field should be in the universal set
        extra = simstate_field_names - universal_fields
        # Allow 'qc_deduction' property to exist as a backward compat — 
        # but issue #25 says NO backward compat, so it should be gone too
        self.assertEqual(
            extra, set(),
            f"SimState has unexpected fields not in the universal set: {extra}"
        )


# ── 2. All Canada fields in jurisdiction_state['canada'] ───────────────────

class TestCanadaFieldsInJurisdictionState(unittest.TestCase):
    """Verify that all Canada-specific fields are accessible via jurisdiction_state['canada']."""

    def test_rrsp_balance_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_rrsp', canada)  # #700: RRSP now lives in the per-adult store

    def test_rrsp_room_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_rrsp', canada)  # #700: RRSP now lives in the per-adult store

    def test_tfsa_primary_balance_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_tfsa', canada)  # #700: TFSA now in the per-adult store

    def test_tfsa_primary_room_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_tfsa', canada)  # #700: TFSA now in the per-adult store

    def test_tfsa_spouse_balance_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_tfsa', canada)  # #700: TFSA now in the per-adult store

    def test_tfsa_spouse_room_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_tfsa', canada)  # #700: TFSA now in the per-adult store

    def test_spousal_rrsp_balance_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_rrsp', canada)  # #700: RRSP now lives in the per-adult store

    def test_spouse_rrsp_balance_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_rrsp', canada)  # #700: RRSP now lives in the per-adult store

    def test_spouse_rrsp_room_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('adult_rrsp', canada)  # #700: RRSP now lives in the per-adult store

    def test_resp_balances_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('resp_balances', canada)

    def test_readvance_heloc_balance_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('readvance_heloc_balance', canada)

    def test_sm_investment_balance_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('sm_investment_balance', canada)

    def test_sm_investment_cost_basis_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('sm_investment_cost_basis', canada)

    def test_readvance_total_interest_paid_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('readvance_total_interest_paid', canada)

    def test_readvance_total_tax_saved_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('readvance_total_tax_saved', canada)

    def test_heloc_tracing_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('heloc_tracing', canada)

    def test_spousal_contribution_years_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('spousal_contribution_years', canada)

    def test_rrsp_ledger_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('rrsp_ledger', canada)

    def test_rrsp_deduction_carry_forward_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('rrsp_deduction_carry_forward', canada)

    def test_heloc_rrsp_paydown_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIn('heloc_rrsp_paydown', canada)

    def test_fhsa_fields_in_canada(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        # #700/#643/#704: FHSA/LIRA/LIF now live in per-adult stores, not flat
        # canada keys. The Canada-fields-in-jurisdiction-state point (#25) holds.
        self.assertIn('adult_fhsa', canada)
        self.assertIn('adult_lira', canada)
        self.assertIn('adult_lif', canada)


# ── 3. simulate_year_pure works through jurisdiction_state ─────────────────

class TestSimulateYearPureUsesJurisdictionState(unittest.TestCase):
    """Verify that simulate_year_pure reads/writes Canada fields through jurisdiction_state."""

    def _mort_data(self, state, principal=10000):
        return {
            'end_balance': max(0, adult_rrsp_total(state.jurisdiction_state['canada']) and 190000),
            'total_payment': 14000, 'total_interest': 10000,
            'total_principal': principal,
        }

    def test_rrsp_balance_grows_via_jurisdiction_state(self):
        """After simulate_year_pure, rrsp_balance is in canada dict."""
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = {
            'primary_rrsp': 10000,
            '_primary_income': 120000, '_spouse_income': 50000,
            '_annual_savings': 34000,
        }
        _, new_state = simulate_year_pure(
            state, 0, allocs, cfg, investment_return=0.07,
            mortgage_data={'end_balance': 190000, 'total_payment': 14000,
                          'total_interest': 10000, 'total_principal': 10000},
        )
        # rrsp_balance should be in jurisdiction_state['canada']
        canada = new_state.jurisdiction_state['canada']
        self.assertIn('adult_rrsp', canada)
        self.assertGreater(adult_rrsp_total(canada), 0)

    def test_tfsa_balance_grows_via_jurisdiction_state(self):
        """After simulate_year_pure, tfsa balances are in canada dict."""
        cfg = _make_config()
        state = SimState.initial(cfg)
        allocs = {
            'primary_tfsa': 5000, 'spouse_tfsa': 3000,
            '_primary_income': 120000, '_spouse_income': 50000,
            '_annual_savings': 34000,
        }
        _, new_state = simulate_year_pure(
            state, 0, allocs, cfg, investment_return=0.07,
            mortgage_data={'end_balance': 190000, 'total_payment': 14000,
                          'total_interest': 10000, 'total_principal': 10000},
        )
        canada = new_state.jurisdiction_state['canada']
        self.assertIn('adult_tfsa', canada)  # #700: TFSA now in the per-adult store
        self.assertIn('adult_tfsa', canada)  # #700: TFSA now in the per-adult store
        self.assertGreater(adult_tfsa_total(canada), 0)


# ── 4. HelocTracingState and QcDeductionState in canada/sim_state.py ────────

class TestCanadaClassesMovedToCanadaModule(unittest.TestCase):
    """Verify that Canada-specific classes are in countries/canada/sim_state.py."""

    def test_heloc_tracing_state_in_canada_sim_state(self):
        """HelocTracingState should be importable from countries.canada.sim_state."""
        from countries.canada.sim_state import HelocTracingState
        self.assertTrue(dataclasses.is_dataclass(HelocTracingState))

    def test_qc_deduction_state_in_canada_sim_state(self):
        """QcDeductionState should be importable from countries.canada.sim_state."""
        from countries.canada.sim_state import QcDeductionState
        self.assertTrue(dataclasses.is_dataclass(QcDeductionState))


# ── 5. No backward compatibility shims ─────────────────────────────────────

class TestNoBackwardCompatibilityShims(unittest.TestCase):
    """Verify that backward compatibility shims are removed (no backward compat)."""

    def test_no_canada_property_descriptor(self):
        """_CanadaProperty descriptor should not exist in simulation_state.py."""
        import simulation_state
        self.assertFalse(
            hasattr(simulation_state, '_CanadaProperty'),
            "_CanadaProperty descriptor should be removed (no backward compat per issue #25)"
        )

    def test_no_qc_deduction_property_on_simstate(self):
        """SimState.qc_deduction property should not exist (no backward compat)."""
        # qc_deduction should NOT be a property on SimState
        # It should only be accessible via jurisdiction_state['canada']['qc_carry_forward']
        state = SimState.initial(_make_config())
        # Accessing state.qc_deduction should raise AttributeError (not DeprecationWarning)
        with self.assertRaises(AttributeError):
            _ = state.qc_deduction

    def test_no_canada_imports_in_simulation_state(self):
        """simulation_state.py must not import from countries.canada (DP#25)."""
        filepath = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'simulation_state.py'
        )
        with open(filepath) as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.module and 'countries.canada' in node.module:
                    self.fail(
                        f"simulation_state.py imports from countries.canada (DP#25 violation): "
                        f"line {node.lineno}: from {node.module}"
                    )


# ── 6. Initial state correctness ───────────────────────────────────────────

class TestInitialStateCorrectness(unittest.TestCase):
    """Verify that SimState.initial() correctly populates jurisdiction_state['canada']."""

    def test_rrsp_balance_initial(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertEqual(adult_rrsp_total(canada), 0)

    def test_rrsp_room_initial(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertEqual(adult_rrsp_slot(canada, 0)[1], 100000)

    def test_tfsa_primary_room_initial(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertEqual(adult_tfsa_slot(canada, 0)[1], 30000)

    def test_spouse_rrsp_balance_initial(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertEqual(adult_rrsp_slot(canada, 1)[0], 0)

    def test_resp_balances_initial(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIsInstance(canada['resp_balances'], list)
        self.assertEqual(len(canada['resp_balances']), 1)

    def test_readvance_heloc_balance_initial(self):
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertEqual(canada['readvance_heloc_balance'], 0)

    def test_heloc_tracing_is_dict(self):
        """heloc_tracing in canada dict should be a plain dict, not a HelocTracingState."""
        state = SimState.initial(_make_config())
        canada = state.jurisdiction_state['canada']
        self.assertIsInstance(canada['heloc_tracing'], dict)

    def test_total_assets_includes_canada_fields(self):
        """total_assets() should include RRSP, TFSA, FHSA, RESP, SM from canada dict."""
        state = SimState.initial(_make_config())
        # total_assets should work even with all fields in jurisdiction_state
        total = state.total_assets()
        self.assertGreaterEqual(total, 0)


# ── 7. Deep copy / fork independence ───────────────────────────────────────

class TestForkIndependence(unittest.TestCase):
    """Verify that forking SimState creates independent copies of canada dict."""

    def test_fork_creates_independent_canada_dict(self):
        """Mutating canada dict on fork should not affect original."""
        state = SimState.initial(_make_config())
        forked = SimState.fork(state)
        # Mutate forked canada
        forked.jurisdiction_state['canada']['readvance_heloc_balance'] = 99999
        # Original should be unchanged
        self.assertEqual(state.jurisdiction_state['canada']['readvance_heloc_balance'], 0)

    def test_deepcopy_creates_independent_canada_dict(self):
        """Deep-copying should create independent canada dicts."""
        from copy import deepcopy
        state = SimState.initial(_make_config())
        copied = deepcopy(state)
        copied.jurisdiction_state['canada']['readvance_heloc_balance'] = 99999
        self.assertEqual(state.jurisdiction_state['canada']['readvance_heloc_balance'], 0)


if __name__ == '__main__':
    unittest.main()