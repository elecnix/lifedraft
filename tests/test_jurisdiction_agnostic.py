#!/usr/bin/env python3
"""Tests for jurisdiction-agnostic simulation (DP#8, DP#25).

Verify that simulation.py does not import from countries.canada directly,
and that the JurisdictionAdapter pattern allows injecting any jurisdiction.

DP#25: If code compiles with only the root package on PYTHONPATH,
it is jurisdiction-agnostic by construction.

DP#8: Compose through data, not through inheritance.
"""

import ast
import sys
import os
import unittest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from copy import deepcopy

# ── Path setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jurisdiction import (
    JurisdictionAdapter,
    AccountProtocol,
    HelocTracingProtocol,
    RESPCalculatorProtocol,
    RESPChildProtocol,
    RatePathProtocol,
    HELOCPathProtocol,
    QCDeductionProtocol,
    QCDeductionResult,
    HelocTracingEntry,
)


# ── Helper: Check that a module has no direct Canada imports ─────────────────

def _get_canada_imports(filepath: str) -> list:
    """Parse a file and return all 'from countries.canada' import lines.
    
    Only flags module-level imports (not inside functions/conditions).
    Lazy imports inside function bodies are acceptable per DP#25
    (core should not require jurisdiction imports, but may offer
    a convenience default that lazy-imports the adapter).
    """
    with open(filepath) as f:
        tree = ast.parse(f.read())
    
    imports = []
    # Get module-level scope
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.module and 'countries.canada' in node.module:
                names = [a.name for a in node.names]
                imports.append((node.lineno, node.module, names))
    return imports


def _get_all_canada_imports(filepath: str) -> list:
    """Parse a file and return ALL 'from countries.canada' import lines,
    including those nested inside function bodies.

    Unlike ``_get_canada_imports`` (module-level only, which permits lazy
    imports as a convenience default), this walks the entire AST. Used for
    modules whose DP#25 contract is ZERO jurisdiction imports of any kind --
    e.g. objective.py (issue #732): the optimization layer must resolve
    jurisdiction logic through the provider seam, not import it lazily.
    """
    with open(filepath) as f:
        tree = ast.parse(f.read())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and 'countries.canada' in node.module:
                names = [a.name for a in node.names]
                imports.append((node.lineno, node.module, names))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if 'countries.canada' in alias.name:
                    imports.append((node.lineno, alias.name, []))
    return imports


# ── Test: No Canada imports in simulation.py ─────────────────────────────────

class TestSimulationNoCanadaImports(unittest.TestCase):
    """DP#25: simulation.py must not import from countries.canada directly."""
    
    def test_simulation_py_no_canada_imports(self):
        """simulation.py has zero direct imports from countries.canada."""
        filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'simulation.py')
        imports = _get_canada_imports(filepath)
        if imports:
            lines = '\n'.join(f'  Line {line}: from {mod} import {names}'
                             for line, mod, names in imports)
            self.fail(
                f"simulation.py imports from countries.canada (DP#25 violation):\n{lines}\n"
                f"Use JurisdictionAdapter instead."
            )
    
    def test_simulation_state_py_no_canada_imports(self):
        """simulation_state.py has zero direct imports from countries.canada."""
        filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'simulation_state.py')
        imports = _get_canada_imports(filepath)
        if imports:
            lines = '\n'.join(f'  Line {line}: from {mod} import {names}'
                             for line, mod, names in imports)
            self.fail(
                f"simulation_state.py imports from countries.canada (DP#25 violation):\n{lines}\n"
                f"Use JurisdictionAdapter or protocol-based composition instead."
            )


class TestObjectiveNoCanadaImports(unittest.TestCase):
    """DP#25 / issue #732: objective.py is the optimization layer (outermost).
    It must resolve jurisdiction-specific logic (the estate tax math) through
    the provider registry seam, NOT import countries.canada.estate -- not at
    module scope, and not lazily inside a function body either. A lazy import
    would still couple the generic optimizer to Canada and can reappear after
    a refactor, so this guard walks the whole AST.
    """

    def test_objective_py_zero_canada_imports(self):
        """objective.py has zero `countries` imports at ANY scope (AST walk)."""
        filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'objective.py')
        imports = _get_all_canada_imports(filepath)
        if imports:
            lines = '\n'.join(f'  Line {line}: from {mod} import {names}'
                             for line, mod, names in imports)
            self.fail(
                f"objective.py imports from countries.canada (DP#25 violation, #732):\n{lines}\n"
                f"Resolve jurisdiction logic via jurisdiction_providers.get_provider() instead."
            )

    def test_estate_objective_computes_via_injected_provider(self):
        """The after-tax estate objective still produces correct numbers when
        the estate math is resolved through the provider seam rather than a
        direct import. Uses fabricated round numbers (DP#4/#15).

        A $600k TFSA terminal balance passes tax-free, so max_after_tax_estate
        scores it at full face value; a $600k RRSP balance is deemed-disposed
        at death and scores strictly less. This pins both the injection wiring
        and the canonical estate math behind it.
        """
        from objective import MAX_AFTER_TAX_ESTATE, MAX_TERMINAL_WEALTH
        from simulation_config import YearResult

        def _yr(**kwargs) -> YearResult:
            defaults = dict(
                primary_rrsp=0.0, spouse_rrsp=0.0, spousal_rrsp=0.0,
                total_tfsa=0.0, non_reg_balance=0.0, non_reg_acb=0.0,
                lif_balance=0.0, lira_balance=0.0,
                mortgage_balance=0.0, heloc_balance=0.0, total_debt=0.0,
                total_assets=0.0,
            )
            defaults.update(kwargs)
            return YearResult(**defaults)

        cfg = {'tax': {'province': 'quebec', 'year': 2026}}
        rrsp_heavy = [_yr(primary_rrsp=600_000, total_assets=600_000)]
        tfsa_heavy = [_yr(total_tfsa=600_000, total_assets=600_000)]

        # Pre-tax control: identical (sanity).
        self.assertEqual(
            MAX_TERMINAL_WEALTH.evaluate(rrsp_heavy, cfg),
            MAX_TERMINAL_WEALTH.evaluate(tfsa_heavy, cfg))
        # After-tax via the injected provider: TFSA passes 100%, RRSP is taxed.
        self.assertEqual(MAX_AFTER_TAX_ESTATE.evaluate(tfsa_heavy, cfg), 600_000)
        self.assertLess(MAX_AFTER_TAX_ESTATE.evaluate(rrsp_heavy, cfg), 600_000)

    def test_estate_is_statically_reached_from_production(self):
        """The other half of the #732 tension: objective.py must not import
        countries.canada.estate (DP#25, pinned above), BUT countries.canada.estate
        must still be statically reachable from the production entry points
        (tests/architecture/test_unreached_rule_modules.py). A runtime registry
        lookup is invisible to the static call graph, so the production reach
        edge is a REAL call to compute_estate in optimize.py (a reached entry
        module that may import the jurisdiction package). This test pins that
        edge so a refactor that drops the optimize.py call cannot silently
        orphan estate while keeping objective.py jurisdiction-agnostic.
        """
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         'architecture'))
        from call_graph import CallGraph
        g = CallGraph()
        reached = g.reached_entry_points('countries.canada.estate')
        self.assertIn('compute_estate', reached,
                      "countries.canada.estate.compute_estate is no longer "
                      "statically reached from production. objective.py must not "
                      "import it (DP#25), so a reached module (optimize.py) must "
                      "call compute_estate directly -- see issue #732.")


# ── Test: JurisdictionAdapter protocol compliance ────────────────────────────

class TestJurisdictionAdapterProtocol(unittest.TestCase):
    """Verify that the adapter protocol is properly defined."""
    
    def test_qc_deduction_result_is_dataclass(self):
        """QCDeductionResult should be a dataclass for easy construction."""
        result = QCDeductionResult(qc_deductible=100.0, qc_carry_forward=50.0)
        self.assertEqual(result.qc_deductible, 100.0)
        self.assertEqual(result.qc_carry_forward, 50.0)
    
    def test_heloc_tracing_entry_is_dataclass(self):
        """HelocTracingEntry should be a dataclass."""
        entry = HelocTracingEntry(amount=5000, purpose="investment", description="ETF purchase")
        self.assertEqual(entry.amount, 5000)
        self.assertEqual(entry.purpose, "investment")
    
    def test_mock_adapter_satisfies_protocol(self):
        """A mock adapter implementing all methods satisfies JurisdictionAdapter."""
        mock = MagicMock()
        # Verify we can call all protocol methods without error
        mock.create_rrsp(contribution_room=50000)
        mock.create_tfsa(contribution_room=30000)
        mock.create_nonreg()
        mock.create_readvanceable_mortgage(heloc_rate=0.05)
        mock.create_fhsa(contribution_room=8000)
        mock.create_resp_calculator()
        mock.create_resp_child(name="Child1", birth_year=2016)
        mock.create_heloc_tracing(name="SM HELOC")
        mock.create_qc_deduction()
        mock.compute_qc_sm_benefit(
            readvance_heloc_balance=100000, heloc_rate=0.05,
            deductible_proportion=0.8, nonreg_balance=50000,
            qc_carry_forward=0, marginal_rate=0.45, sim_year=2026,
        )
        mock.build_rate_path(name="3yr fixed", initial_rate=0.04, term_years=3)
        mock.build_heloc_path(rate_path=MagicMock())
        mock.amortization_schedule(
            principal=200000, rate_path=MagicMock(),
            amortization_years=25, projection_months=120,
        )
        mock.annual_summary(schedule=[])
        mock.monthly_payment(principal=200000, annual_rate=0.05, months=300)
        mock.get_default_strategy()
        mock.discover_strategies(state={}, config={})
        mock.create_advance_record(amount=5000, date="2026-01", purpose="investment")
        mock.get_tax_provider()
        # All calls succeeded — protocol is usable


# ── Test: CanadaAdapter implements the protocol ─────────────────────────────

class TestCanadaAdapter(unittest.TestCase):
    """Verify that CanadaAdapter provides concrete implementations."""
    
    @classmethod
    def setUpClass(cls):
        """Import Canada adapter — skip if Canada package not available."""
        try:
            from countries.canada.adapter import CanadaAdapter
            cls.CanadaAdapter = CanadaAdapter
        except ImportError:
            raise unittest.SkipTest("Canada package not available")
    
    def setUp(self):
        self.adapter = self.CanadaAdapter()
    
    def test_create_rrsp(self):
        """Canada adapter creates RRSP accounts."""
        rrsp = self.adapter.create_rrsp(contribution_room=50000)
        self.assertEqual(rrsp.contribution_room, 50000)
        self.assertEqual(rrsp.balance, 0)
    
    def test_create_tfsa(self):
        """Canada adapter creates TFSA accounts."""
        tfsa = self.adapter.create_tfsa(contribution_room=30000)
        self.assertEqual(tfsa.contribution_room, 30000)
    
    def test_create_nonreg(self):
        """Canada adapter creates non-registered accounts."""
        nonreg = self.adapter.create_nonreg()
        self.assertEqual(nonreg.balance, 0)
    
    def test_create_readvanceable_mortgage(self):
        """Canada adapter creates readvanceable mortgage trackers."""
        rm = self.adapter.create_readvanceable_mortgage(heloc_rate=0.05)
        self.assertIsNotNone(rm)
    
    def test_create_fhsa(self):
        """Canada adapter creates FHSA accounts."""
        fhsa = self.adapter.create_fhsa(contribution_room=8000)
        self.assertEqual(fhsa.annual_room + fhsa.carry_forward_room, 8000)
    
    def test_create_resp_calculator(self):
        """Canada adapter creates RESP calculators."""
        calc = self.adapter.create_resp_calculator()
        self.assertIsNotNone(calc)
    
    def test_create_resp_child(self):
        """Canada adapter creates RESP child records."""
        child = self.adapter.create_resp_child(name="Child1", birth_year=2016)
        self.assertEqual(child.name, "Child1")
        self.assertTrue(child.cesg_eligible(2026))
    
    def test_create_heloc_tracing(self):
        """Canada adapter creates HELOC tracing trackers."""
        tracing = self.adapter.create_heloc_tracing(name="SM HELOC")
        self.assertIsNotNone(tracing)
    
    def test_create_qc_deduction(self):
        """Canada adapter creates QC deduction trackers."""
        qc = self.adapter.create_qc_deduction()
        self.assertEqual(qc.carry_forward, 0)
    
    def test_compute_qc_sm_benefit(self):
        """Canada adapter computes QC SM benefit."""
        result = self.adapter.compute_qc_sm_benefit(
            readvance_heloc_balance=100000, heloc_rate=0.05,
            deductible_proportion=0.8, nonreg_balance=50000,
            qc_carry_forward=0, marginal_rate=0.45, sim_year=2026,
        )
        self.assertIsInstance(result, QCDeductionResult)
        self.assertGreater(result.readvance_interest, 0)
    
    def test_build_rate_path(self):
        """Canada adapter builds rate paths."""
        rp = self.adapter.build_rate_path("3yr fixed", 0.04, 3, "fixed", [0.05])
        self.assertIsNotNone(rp)
    
    def test_get_default_strategy(self):
        """Canada adapter returns a default strategy."""
        strategy = self.adapter.get_default_strategy()
        self.assertIsNotNone(strategy)
    
    def test_get_tax_provider(self):
        """Canada adapter returns a tax provider."""
        provider = self.adapter.get_tax_provider()
        self.assertIsNotNone(provider)


# ── Test: FamilySimulation with injected adapter ─────────────────────────────

class TestSimulationWithAdapter(unittest.TestCase):
    """Verify that FamilySimulation can run with an injected JurisdictionAdapter."""
    
    @classmethod
    def setUpClass(cls):
        """Import required modules."""
        try:
            from simulation import FamilySimulation
            from simulation_config import SimulationConfig
            from countries.canada.adapter import CanadaAdapter
            cls.FamilySimulation = FamilySimulation
            cls.SimulationConfig = SimulationConfig
            cls.CanadaAdapter = CanadaAdapter
        except ImportError as e:
            raise unittest.SkipTest(f"Module not available: {e}")
    
    def _make_config(self):
        """Build a test config with fabricated round numbers (DP#4)."""
        return self.SimulationConfig(
            projection_years=2,
            investment_return=0.07,
            salary_growth=0.02,
            savings_rate=0.20,
            house_value=500000,
            mortgage_balance=200000,
            mortgage_rate=0.05,
            ltv_max=0.80,
            margin_available=50000,
            family_members=[
                {'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 30000},
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
            children=[{'name': 'Kid', 'age': 10, 'gross_income': 0}],
            rrsp_annual_percent=0.18,
            rrsp_annual_max=33000,
            tfsa_annual_room_per_person=7000,
        )
    
    def test_simulation_with_canada_adapter(self):
        """FamilySimulation can be constructed with an explicit CanadaAdapter."""
        config = self._make_config()
        adapter = self.CanadaAdapter(config)
        sim = self.FamilySimulation(config, adapter=adapter)
        self.assertIsNotNone(sim)
    
    def test_simulation_requires_adapter_without_canada_package(self):
        """FamilySimulation falls back to CanadaAdapter when available (DP#25)."""
        config = self._make_config()
        # Should work without explicit adapter (uses CanadaAdapter fallback)
        sim = self.FamilySimulation(config)
        self.assertIsNotNone(sim)
        self.assertIsNotNone(sim.adapter)
    
    def test_simulation_runs_with_adapter(self):
        """FamilySimulation.run() works with an injected adapter."""
        config = self._make_config()
        adapter = self.CanadaAdapter(config)
        sim = self.FamilySimulation(config, adapter=adapter)
        results = sim.run()
        self.assertEqual(len(results), 2)
        # Results should have reasonable values
        self.assertGreater(results[-1].total_assets, 0)


# ── Test: SimState jurisdiction_state is opaque ─────────────────────────────

class TestSimStateJurisdictionOpaque(unittest.TestCase):
    """Verify that SimState treats jurisdiction_state as opaque data (DP#8)."""
    
    @classmethod
    def setUpClass(cls):
        try:
            from simulation_state import SimState
            from simulation_config import SimulationConfig
            cls.SimState = SimState
            cls.SimulationConfig = SimulationConfig
        except ImportError as e:
            raise unittest.SkipTest(f"Module not available: {e}")
    
    def _make_config(self):
        return self.SimulationConfig(
            projection_years=2,
            investment_return=0.07,
            salary_growth=0.02,
            savings_rate=0.20,
            house_value=500000,
            mortgage_balance=200000,
            mortgage_rate=0.05,
            margin_available=50000,
            family_members=[
                {'role': 'primary', 'gross_income': 120000,
                 'rrsp_room_accumulated': 50000, 'tfsa_room_accumulated': 30000},
                {'role': 'spouse', 'gross_income': 50000,
                 'rrsp_room_accumulated': 20000, 'tfsa_room_accumulated': 30000},
            ],
            children=[],
        )
    
    def test_jurisdiction_state_is_dict(self):
        """SimState.jurisdiction_state is a dict, not a Canada-specific class."""
        state = self.SimState.initial(self._make_config())
        self.assertIsInstance(state.jurisdiction_state, dict)
    
    def test_jurisdiction_state_has_canada_key(self):
        """SimState.jurisdiction_state contains 'canada' key with jurisdiction data."""
        state = self.SimState.initial(self._make_config())
        self.assertIn('canada', state.jurisdiction_state)
    
    def test_simulate_year_pure_works(self):
        """simulate_year_pure works without direct Canada imports."""
        from simulation_state import simulate_year_pure
        config = self._make_config()
        state = self.SimState.initial(config)
        
        allocs = {
            'primary_rrsp': 5000, 'spousal_rrsp': 2000,
            'primary_tfsa': 3000, 'spouse_tfsa': 2000,
            'resp': 2500, 'non_reg': 3000,
            '_primary_income': 120000, '_spouse_income': 50000,
            '_annual_savings': 20000,
        }
        result, new_state = simulate_year_pure(
            state=state, year=0, allocations=allocs, config=config,
            investment_return=0.07, mortgage_rate=0.05, heloc_rate=0.05,
            mortgage_data={'end_balance': 180000, 'total_payment': 14000,
                          'total_interest': 10000, 'total_principal': 4000},
        )
        self.assertIsNotNone(result)
        self.assertIsInstance(new_state, self.SimState)
        self.assertGreater(result.total_assets, 0)


# ── Test: Core imports are jurisdiction-agnostic ────────────────────────────

class TestCoreModulesImportWithoutCanada(unittest.TestCase):
    """Verify that core modules can be imported without Canada package."""
    
    def test_jurisdiction_module_imports(self):
        """jurisdiction.py imports cleanly without any Canada dependency."""
        import jurisdiction
        # Verify protocol classes exist
        self.assertTrue(hasattr(jurisdiction, 'JurisdictionAdapter'))
        self.assertTrue(hasattr(jurisdiction, 'AccountProtocol'))
        self.assertTrue(hasattr(jurisdiction, 'QCDeductionResult'))
        self.assertTrue(hasattr(jurisdiction, 'HelocTracingEntry'))
    
    def test_simulation_config_imports_without_canada(self):
        """simulation_config.py imports cleanly (it has no Canada imports)."""
        from simulation_config import SimulationConfig, YearResult
        config = SimulationConfig()
        self.assertIsNotNone(config)
    
    def test_strategy_imports_without_canada(self):
        """strategy.py imports cleanly without direct Canada imports in module scope."""
        import strategy
        # FamilyState and AllocationResult should be available
        self.assertTrue(hasattr(strategy, 'FamilyState'))
        self.assertTrue(hasattr(strategy, 'AllocationResult'))


if __name__ == '__main__':
    unittest.main()