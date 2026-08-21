# Progress

## Status
Completed

## Tasks
- [x] Read full tax_calculator.py to understand structure
- [x] Create countries/canada/tax_calc.py with Canadian functions
- [x] Strip root tax_calculator.py to generic core + backward compat stubs
- [x] Update all callers throughout codebase
- [x] Update countries/canada/__init__.py with re-exports
- [x] Run tests to verify everything works (1491 passed)

## Files Changed
- `tax_calculator.py` — Stripped to generic core; Canadian functions replaced with deprecation wrappers + `QC_ABATEMENT` lazy proxy
- `countries/canada/tax_calc.py` — **NEW** Canadian-specific tax functions extracted from root
- `countries/canada/__init__.py` — Added Phase 5 re-exports for tax_calc
- `compare_scenarios.py` — Updated imports
- `countries/canada/asset_location.py` — Updated imports
- `countries/canada/cashout_optimizer.py` — Updated imports
- `countries/canada/family.py` — Updated imports
- `decide_refinance.py` — Updated imports
- `enumerate_scenarios.py` — Updated imports
- `family_integration.py` — Updated imports
- `family_optimize.py` — Updated imports
- `__init__.py` — Split imports between tax_calculator and countries.canada.tax_calc
- `optimize.py` — Updated imports
- `sensitivity.py` — Updated imports
- `simulation.py` — Updated imports
- `tests/test_investment_income.py` — Updated imports
- `tests/test_modules.py` — Updated imports
- `tests/test_scenario_seed.py` — Updated imports
- `tests/test_tax_calc_full.py` — Updated imports

## Notes
- All 1491 tests pass
- Backward-compat deprecation wrappers work correctly (emit DeprecationWarning)
- QC_ABATEMENT uses a lazy proxy class so `from tax_calculator import QC_ABATEMENT` still works
- `tax_on_investment_income` stays in root as generic dispatch, imports Canadian functions lazily on demand

## Review
- ✅ All generic functions remain in root: get_combined_brackets, marginal_rate, tax_on_income, effective_tax_rate, capital_gains_rate, InvestmentIncomeType, tax_on_investment_income
- ✅ All Canadian functions correctly moved to countries/canada/tax_calc.py with proper imports from root
- ✅ 10 function deprecation wrappers in root each emit DeprecationWarning with stacklevel=2
- ✅ QC_ABATEMENT has a lazy proxy class (_QC_ABATEMENTProxy) supporting arithmetic ops, comparison, hash, etc.
- ✅ No direct imports of moved Canadian functions from tax_calculator in any source files (all updated to import from countries.canada.tax_calc)
- ✅ countries/canada/__init__.py re-exports all Phase 5 public names
- ✅ All 1491 tests pass
- ✅ Deprecation warning confirmed: `from tax_calculator import federal_tax` emits DeprecationWarning
- ✅ No circular import risk: tax_calculator imports tax_calc only lazily (inside function bodies)
- Minor: QC_ABATEMENT proxy does NOT emit DeprecationWarning — inconsistent with function wrappers. Users importing QC_ABATEMENT from tax_calculator get no deprecation signal.
- Minor: Unused import on line 317 of tax_calculator.py — `from countries.canada.tax_calc import withholding_tax_drag` inside FOREIGN_DIVIDEND_US branch is never called; the WHT calculation is inline.
- Nit: Dead code — `@property`-decorated `_QUEBEC_TAX_BRACKETS_2026` at module level creates a non-callable property object. Never invoked; the `_BracketProxy` class handles access instead.
- Nit: Unused local variables `fed_data` and `prov_data` in `_compute_legacy_brackets()` (lines 64-65).
