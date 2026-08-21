"""Lifedraft — Jurisdiction-Agnostic Financial Strategy Optimizer (DP#25).

The root package exposes ONLY jurisdiction-agnostic primitives.  Canadian
programs, account types, and convenience wrappers live in
``countries.canada``.  If code compiles with only the root package on
PYTHONPATH, it is jurisdiction-agnostic by construction.

Core modules
------------
tax_calculator   Combined tax brackets, marginal/effective rates, capital-gains
                  rate, investment-income helpers (province/bracket args only).
strategy          AllocationStrategy, StrategyEngine, FamilyState, StrategyType,
                  create_strategy_from_config, list_strategies.
simulation        SimulationConfig, FamilySimulation.
simulation_state  SimState, simulate_year_pure.
tax_data          TaxDataProvider, TaxBracket, TaxYearData.
return_model      ReturnModel, ReturnEngine, FixedReturn, StochasticReturn, build_return_model.
optimizer         Optimizer, RankedScenario, RiskMeasures, OptimizerMode, build_optimizer.
objective         ObjectiveFunction.
stress_scenarios  StressPath, run_stress_test.
output_plugins    TextReport, JsonReport, HtmlReport.
module_registry   CountryRegistry, default_registry, discover_country_packages.
compare_scenarios Head-to-head comparison of anchor life decisions.
decide_refinance  Refinance decision engine.

Country-specific code is in ``countries.canada`` — see its ``__init__.py``
for the full import surface (RRSPAccount, TSFAccount, federal_tax,
STRATEGIES, RatePath, HELOCPath, …).
"""

# ── Core / jurisdiction-agnostic modules ────────────────────────────
from tax_calculator import (
    marginal_rate, tax_on_income,
    effective_tax_rate, capital_gains_rate,
    InvestmentIncomeType, tax_on_investment_income,
)
from strategy import (
    AllocationStrategy, StrategyEngine, FamilyState,
    AllocationResult, StrategyType,
    create_strategy_from_config, list_strategies,
)
from simulation import SimulationConfig, FamilySimulation
from simulation_state import (
    SimState, simulate_year_pure,
)
from tax_data import TaxDataProvider, TaxBracket, TaxYearData
from return_model import (
    ReturnModel, ReturnEngine, FixedReturn, StochasticReturn, build_return_model,
)
from optimizer import Optimizer, RankedScenario, RiskMeasures, OptimizerMode, build_optimizer
from objective import ObjectiveFunction
from stress_scenarios import StressPath, run_stress_test
from output_plugins import TextReport, JsonReport, HtmlReport
from module_registry import CountryRegistry, default_registry, discover_country_packages
from simulate import (
    load_inputs, build_all_overlays, evaluate_overlay, enumerate_overlays,
    print_discovery, run_scipy_optimization, print_scipy_report,
)

__version__ = "0.5.0"

__all__ = [
    # Tax calculator (jurisdiction-agnostic)
    "marginal_rate", "tax_on_income",
    "effective_tax_rate", "capital_gains_rate",
    "InvestmentIncomeType", "tax_on_investment_income",

    # Strategy engine
    "AllocationStrategy", "StrategyEngine", "FamilyState",
    "AllocationResult", "StrategyType",
    "create_strategy_from_config", "list_strategies",
    # Simulation
    "SimulationConfig", "FamilySimulation",
    # Simulation state
    "SimState", "simulate_year_pure",

    # Tax data
    "TaxDataProvider", "TaxBracket", "TaxYearData",
    # Return model
    "ReturnModel", "ReturnEngine", "FixedReturn", "StochasticReturn", "build_return_model",
    # Optimizer
    "Optimizer", "RankedScenario", "RiskMeasures", "OptimizerMode", "build_optimizer",
    # Objective
    "ObjectiveFunction",
    # Stress scenarios
    "StressPath", "run_stress_test",
    # Output plugins
    "TextReport", "JsonReport", "HtmlReport",
    # Module registry
    "CountryRegistry", "default_registry", "discover_country_packages",
    # Simulate entry point
    "load_inputs", "build_all_overlays", "evaluate_overlay", "enumerate_overlays",
    "print_discovery", "run_scipy_optimization", "print_scipy_report",
]