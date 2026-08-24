#!/usr/bin/env python3
"""
Mortgage Refinance + Smith Manoeuvre Strategy Optimizer

Thin CLI that delegates to modular engines:
  - countries/canada/strategies.py: discover_strategies() for strategy enumeration (DP#6)
  - simulation.py: FamilySimulation for year-by-year projection (DP#3, DP#8)
  - rate_model.py: build_rate_path for mortgage rate paths

NOTE: The old evaluate_strategy() and generate_strategies() have been
removed. Use evaluate_strategy_with_simulation() and discover_strategies()
from strategy.py instead.
"""

import contract_schema
from tax_data import default_tax_provider
import json
import argparse
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional

from tax_calculator import (
    marginal_rate, tax_on_income,
)
from countries.canada.tax_calc import (
    federal_tax, quebec_tax,
)
from strategy import (
    AllocationStrategy, StrategyEngine, FamilyState, AllocationResult,
    create_strategy_from_config,
)
from countries.canada.strategies import STRATEGIES, discover_strategies
from simulation import FamilySimulation
from charge_limits import ChargeLimitExceededError, MissingRefinanceAmortizationError
from config_access import (
    set_return_rate,
    resolve_return_rate,
    has_readvanceable_facility,
    resolve_heloc_rate,
)
from scenario_overlay import ScenarioOverlay, build_overlay_config, refinance_amortization_fallback
from simulation_config import SimulationConfig
from year_result import YearResult
from member_config import find_member_by_role  # DP#25 (#998): data-layer helper
# DP#25 (#998): inject the simulation-layer callables scenario_discovery needs
# (marginal_rate, strategy types/engine, rate resolvers) so the scenario layer
# has ZERO runtime imports from tax_calculator / strategy / simulation_config.
# Importing simulation_deps configures the injection point at import time.
import simulation_deps  # noqa: F401  (import side-effect: configures scenario_discovery)
from countries.canada.rate_model import (
    RatePath, HELOCPath, build_rate_path, build_broker_scenarios,
    amortization_schedule, annual_summary, monthly_payment,
)
from countries.canada.cashout_optimizer import compute_min_extraction, print_cashout_report
from countries.canada.retirement import (
    DrawdownOptimizer, RetirementState, project_retirement,
    get_oas_annual_max,  # DP#20 year-versioned (#1029)
)
from countries.canada.lsif_credit import compute_lsif_credit, lsif_from_config, LSIFPurchase
from countries.canada.zev_incentive import compute_izev_incentive, zev_purchase_from_dict
from countries.canada.provinces.quebec.roulez_vert import compute_roulez_vert_rebate
# Issue #732 (DP#25): objective.py resolves the estate math through the
# jurisdiction provider seam and cannot import countries.canada.estate. The
# static reach-detector (tests/architecture/test_unreached_rule_modules.py)
# follows CALLS from the production entry points, and a runtime registry
# lookup is invisible to it -- so the production reach edge to
# countries.canada.estate.compute_estate must be a real CALL in a reached
# module. optimize.py is that reached module (it already imports
# countries.canada.* directly), and it needs the reporting estate value
# below, so it calls compute_estate directly via the shared arg-prep helper
# objective._estate_call_args (one source for the terminal-YearResult ->
# compute_estate mapping, no restated logic).
from countries.canada.estate import compute_estate
from objective import (
    ObjectiveFunction, MAX_NET_BENEFIT,
    estate_is_declared, _estate_call_args,
    OBJECTIVES, get_objective,
)
from liquidation_waterfall import summarize_solvency
from runway import RunwayResult, RunwayCurvePoint, shift_income_scenario_dates
from decumulation import (
    summarize_drawdown_shortfall, worst_drawdown_shortfall, ranking_key,
    shortfall_of,
)
import model_fidelity
from datetime import date as _date

import os
import atexit
import collections
import multiprocessing
from concurrent.futures import ProcessPoolExecutor


# DP#13/DP#20: fallback OAS annual amount used by compute_net_benefit() when
# the household's config supplies no ``assumptions.oas_annual``. This is a
# named fallback for ABSENT input only -- an explicit ``0`` is honoured (the
# ``dict.get`` calls below use it as the dict.get default, NOT
# ``x or DEFAULT``, so DP#32 is respected: a configured zero stays zero).
#
# Issue #1029 (the deliberate decision #986 deferred): the fallback AMOUNT is
# read live from the year-versioned government table
# ``countries.canada.retirement.get_oas_annual_max(year)`` -- the same source
# every other consumer uses (pension_split_optimizer via #331,
# simulation_rules, retirement) -- instead of a frozen literal. The relevant
# year is the household's simulation start year (``cfg['tax']['start_year']``,
# which run_optimization always writes into the objective cfg); a hand-built
# config without that block falls back to ``_CURRENT_YEAR``, the same
# current-year convention compute_net_benefit already uses for its age and
# LSIF math. For 2026 this reads 8908 (pre-#1029 it was the stale frozen
# 8500), so optimizer net-benefit numbers MOVE for households omitting
# ``assumptions.oas_annual`` -- that delta is the intended correctness fix.
_CURRENT_YEAR = 2026


def _default_oas_annual(cfg: Dict) -> float:
    """Year-versioned OAS maximum for ABSENT ``assumptions.oas_annual`` (#1029).

    Reads the live government table for the household's simulation start year;
    an unknown year raises ValueError from ``get_oas_annual_max`` rather than
    silently coercing (DP#32).
    """
    start_year = cfg.get('tax', {}).get('start_year')
    if start_year is None:
        start_year = _CURRENT_YEAR
    return get_oas_annual_max(start_year)


# ── Scenario-sweep parallelism (perf) ────────────────────────────────────────
# The optimizer evaluates each SCENARIO -- one ``run_optimization`` over a
# (refinance option x mortgage structure x income scenario) cell -- as an
# INDEPENDENT pure fold (DP#26: ``simulate_year_pure`` reads nothing off
# ``self``), so the outer sweeps that loop ``run_optimization`` are
# embarrassingly parallel. We dispatch each whole ``run_optimization`` scenario
# to a persistent process pool and collect the results IN INPUT ORDER
# (``pool.map`` preserves order), so the subsequent deterministic sort produces
# byte-identical output to the serial version regardless of which worker
# finished first. Returns are deterministic (``FixedReturn``), so every
# scenario's fold is reproducible in any worker; parallelism never changes a
# scenario's math. A scenario runs its own (small) strategy loop SERIALLY
# inside its worker, so there is never a nested pool.
#
# Worker count resolution (highest priority first):
#   1. an explicit ``set_workers(n)`` call (the CLI ``--workers`` flag);
#   2. the ``OPTIMIZE_WORKERS`` environment variable;
#   3. a sensible default of ``(os.cpu_count() or 2) - 1`` (leave one core free).
# A resolved count of ``1`` (or a single-scenario sweep) takes the SERIAL path
# — no pool, no pickling — so ``--workers 1`` is provably identical to the
# pre-parallel code path.
_WORKERS: Optional[int] = None
_POOL: Optional[ProcessPoolExecutor] = None

# Sentinel wrapping a scenario that ``run_optimization`` REFUSED with one of the
# two typed, EXPECTED infeasibilities (over the shared charge; a cash-out with
# no declared re-amortization term). The serial LTV sweep caught these
# per-candidate and recorded a refusal marker instead of aborting; carrying the
# reason back across the pool boundary preserves that exact behaviour.
_ScenarioRefused = collections.namedtuple('_ScenarioRefused', ['reason'])


# ── Risk-objective stochastic ensemble (issue #937) ──────────────────────────
# A distributional (risk) objective -- one whose ``rank_from_distribution`` is
# set, e.g. ``max_cvar_terminal`` / ``max_p10_terminal`` -- ranks a candidate by
# a tail statistic of its terminal-wealth distribution, so it needs a
# DISTRIBUTION, not the single deterministic path the main sweep evaluates. When
# such an objective is selected AND the household declared a stochastic return
# model, ``evaluate_strategy_with_simulation`` re-runs the candidate over
# ``RISK_ENSEMBLE_PATHS`` re-seeded return paths and ranks by the aggregate. A
# per-path objective (the default, and every deterministic household including
# the golden fixture) never enters this branch -> byte-identical to before.
# Seeds are ``RISK_ENSEMBLE_SEED_BASE + i`` (DP#23: reproducible). The path count
# is read from the environment at import so every pool worker agrees on it.
RISK_ENSEMBLE_SEED_BASE = 42
_DEFAULT_RISK_ENSEMBLE_PATHS = 200


def _risk_paths_from_env(env_val: Optional[str]) -> int:
    """Resolve the ensemble path count from ``RISK_ENSEMBLE_PATHS`` (an unset or
    blank value -> the default; any set value is floored at 1)."""
    if env_val is not None and env_val.strip() != '':
        return max(1, int(env_val))
    return _DEFAULT_RISK_ENSEMBLE_PATHS


RISK_ENSEMBLE_PATHS = _risk_paths_from_env(os.environ.get('RISK_ENSEMBLE_PATHS'))


def _resolve_workers() -> int:
    """The effective worker count, resolved once and cached.

    An explicit ``set_workers`` wins; else ``OPTIMIZE_WORKERS``; else a default
    that leaves one core free. Always at least 1 (serial)."""
    global _WORKERS
    if _WORKERS is None:
        env = os.environ.get('OPTIMIZE_WORKERS')
        if env is not None and env.strip() != '':
            _WORKERS = max(1, int(env))
        else:
            # DP#32: os.cpu_count() may return None (undeterminable) -- an
            # explicit None-check, never `... or 2`, which would also swallow a
            # legitimate count. Leave one core free for the parent/OS.
            cpus = os.cpu_count()
            cpus = cpus if cpus is not None else 2
            _WORKERS = max(1, cpus - 1)
    return _WORKERS


def set_workers(n: int) -> None:
    """Override the scenario-sweep worker count (CLI ``--workers``).

    ``n <= 1`` forces the serial path. Any already-running pool sized to a
    different count is shut down so the next sweep rebuilds it at the new size."""
    global _WORKERS, _POOL
    resolved = max(1, int(n))
    if resolved != _WORKERS and _POOL is not None:
        _POOL.shutdown()
        _POOL = None
    _WORKERS = resolved


def _in_worker() -> bool:
    """True when running inside a pool worker (belt-and-braces: a scenario's
    own strategy loop must never try to open a second, nested pool)."""
    return multiprocessing.parent_process() is not None


def _get_pool() -> ProcessPoolExecutor:
    """The lazily-created persistent worker pool, sized to ``_resolve_workers``.

    Created on the first parallel sweep and reused across every subsequent
    sweep, so workers (and their inherited tax_data memo, via copy-on-write on
    fork) are paid for once, not per sweep."""
    global _POOL
    if _POOL is None:
        _POOL = ProcessPoolExecutor(max_workers=_resolve_workers())
        atexit.register(_shutdown_pool)
    return _POOL


def _shutdown_pool() -> None:
    global _POOL
    if _POOL is not None:
        _POOL.shutdown()
        _POOL = None


def _run_scenario_task(payload: Dict):
    """Worker entry point: run ONE scenario's ``run_optimization`` fold.

    A module-level function (picklable) that unpacks the scenario's
    ``run_optimization`` keyword arguments and returns its ranked results.
    ``catch_refusals`` mirrors the serial LTV sweep's narrow per-candidate
    catch: the two typed, EXPECTED infeasibilities become a ``_ScenarioRefused``
    the parent records as a refusal marker; ANY other exception propagates (a
    bug must fail loud, #657). No scenario math is duplicated or altered here."""
    kwargs = payload['kwargs']
    if payload.get('catch_refusals'):
        try:
            return run_optimization(**kwargs)
        except (ChargeLimitExceededError, MissingRefinanceAmortizationError) as exc:
            return _ScenarioRefused(f"{type(exc).__name__}: {exc}")
    return run_optimization(**kwargs)


def _map_scenarios(payloads: List[Dict]) -> List:
    """Run a list of scenarios, returning each one's result IN INPUT ORDER.

    Serial (no pool, no pickling) when the resolved worker count is 1, the
    sweep has a single scenario, or we are already inside a worker; otherwise
    dispatched across the persistent pool via ``pool.map``, which preserves
    input order. Order preservation is what lets each caller's deterministic
    sort reproduce the serial ranking exactly (the hard requirement:
    byte-identical ``--json`` output)."""
    if not payloads:
        return []
    if _resolve_workers() <= 1 or len(payloads) == 1 or _in_worker():
        return [_run_scenario_task(p) for p in payloads]
    return list(_get_pool().map(_run_scenario_task, payloads))


class ObjectiveSelectionError(ValueError):
    """A --objective / decisions.objective name that names no registered
    objective. Raised (DP#32) naming the bad value and the valid names, so a
    typo is refused loudly rather than silently scoring the run under the
    default. Issue #862."""


def _list_objectives_text() -> List[str]:
    """The registry rendered for --list-objectives: name + one-line
    description, in registration order. Issue #862 (DP#22): the objectives are
    data; this lists the sockets the plug can go into."""
    lines = []
    for name, obj in OBJECTIVES.items():
        desc = (obj.description or "").split(". ")[0].strip()
        lines.append(f"  {name:<32} {desc}")
    return lines


def resolve_objective(cli_name: Optional[str], cfg: Dict) -> ObjectiveFunction:
    """Resolve the ObjectiveFunction a run is scored under (issue #862, DP#22).

    Resolution order, highest priority first:
      1. the CLI ``--objective <name>`` override (ad-hoc comparison), if given;
      2. the contract's declared ``decisions.objective`` (carried onto the
         internal config as ``cfg['objective']`` by input_contract), if any;
      3. the historical default ``MAX_NET_BENEFIT``.

    The optimizer ranks, it doesn't choose (DP#22); this is the one place the
    choice is read. An unknown name -- from either source -- is refused loudly
    (DP#32) via ``ObjectiveSelectionError``, naming the bad value and listing
    the valid names, never silently falling back to the default. The
    contract-sourced name was already validated at the ingestion boundary
    (input_contract), so the only way step 2 raises is a hand-built internal
    config; validating here too keeps this function honest on its own.
    """
    name = cli_name if cli_name is not None else cfg.get('objective')
    if name is None:
        return MAX_NET_BENEFIT
    try:
        return get_objective(name)
    except (KeyError, ValueError):
        raise ObjectiveSelectionError(
            f"Unknown objective: {name!r}. The optimizer ranks strategies under "
            f"a chosen objective (DP#22); an unknown name is refused rather than "
            f"silently scored under the default (DP#32). "
            f"Valid objectives: {sorted(OBJECTIVES)}."
        )


def compute_net_benefit(results: List[YearResult], cfg: Dict) -> float:
    """Compute net benefit from simulation YearResult list.

    Net benefit = total_assets - total_debt + cumulative tax savings
    - estimated withdrawal taxes on RRSP and capital gains.

    Auto-includes retirement drawdown analysis when birth_year data
    is available in cfg, otherwise uses simplified 30% withdrawal tax.

    This is a pure function: same inputs → same output (DP#3).
    """
    if not results:
        return 0.0

    final = results[-1]
    brackets = default_tax_provider().get_combined_brackets()

    # Cumulative tax savings over the projection
    total_rrsp_savings = sum(yr.rrsp_tax_savings for yr in results)
    total_sm_savings = sum(yr.readvance_tax_savings for yr in results)
    # Issue #850: the s.20(1)(c) deduction on the mortgage ADVANCE and on the
    # DRAWN revolving line. Without this term, ranking advance-vs-line prices
    # the rate gap and interest capitalization ONLY -- i.e. it ranks a
    # different question than the one #849 asks. 0.0 for a household that
    # borrowed no lump sum, so a household that never asked this question sees
    # exactly the number it saw before (DP#32).
    total_traced_savings = sum(yr.traced_borrowing_tax_savings for yr in results)

    # ── RRSP withdrawal tax: use retirement module if age data available ──
    members = cfg.get('family', {}).get('members', [])
    primary = find_member_by_role(members, 'primary', {})  # #699 seam
    birth_year = primary.get('birth_year')
    
    rrsp_withdrawal_tax = 0
    if final.total_rrsp > 0:
        if birth_year and birth_year > 1900:
            # Auto-include retirement drawdown analysis (DP#16)
            current_age = 2026 - birth_year
            # Standard retirement age: 65, or current+10 if already past 55
            retirement_age = max(current_age + 10, 65)
            # DP#19: use actual ACB tracked by simulation, not a rough estimate
            non_reg_acb = getattr(final, 'non_reg_acb',
                                   final.non_reg_balance * 0.5)  # Fallback for old results
            # DP#16/issue #232: Read CPP/OAS from config instead of hardcoding.
            # Per issue #232: retirement_income=0 was a placeholder. Now compute actual
            # retirement income from CPP monthly estimate, OAS, pension, and LIF withdrawal.
            cpp_monthly_estimated = primary.get('cpp_monthly_estimated', 0)
            cpp_start_age = primary.get('cpp_start_age', 65)
            oas_start_age = primary.get('oas_start_age', 65)
            oas_defer_months = primary.get('oas_defer_months', 0)
            pension_income_annual = primary.get('pension_income_annual', 0)
            # Compute CPP annual from monthly estimate
            cpp_annual = cpp_monthly_estimated * 12 if cpp_monthly_estimated > 0 else 0
            # Compute OAS annual from config or defaults
            oas_annual = cfg.get('assumptions', {}).get('oas_annual', _default_oas_annual(cfg))
            # LIF withdrawal from simulation results (issue #230)
            lif_withdrawal = getattr(final, 'lif_withdrawal', 0)
            ret_state = RetirementState(
                rrif_balance=final.total_rrsp,  # RRSP becomes RRIF at retirement
                tfsa_balance=final.total_tfsa,
                non_reg_balance=final.non_reg_balance,
                non_reg_acb=non_reg_acb,
                age=retirement_age,
                annual_expenses=cfg.get('assumptions', {}).get('retirement_expenses', 60000),
                cpp_start_age=cpp_start_age,
                cpp_annual=cpp_annual,
                oas_annual=oas_annual,
                lif_balance=getattr(final, 'lif_balance', 0),
                lif_jurisdiction=primary.get('lira', {}).get('jurisdiction', 'federal'),
                lif_birth_year=birth_year,
            )
            ret_results = project_retirement(ret_state, investment_return=resolve_return_rate(cfg))
            rrsp_withdrawal_tax = sum(r.get('tax_owed', 0) for r in ret_results)
        else:
            # DP#13/issue #232: retirement_income should come from config.
            # Compute actual retirement income from CPP + OAS + pension + LIF.
            cpp_monthly_estimated = primary.get('cpp_monthly_estimated', 0)
            cpp_annual_income = cpp_monthly_estimated * 12 if cpp_monthly_estimated > 0 else 0
            oas_annual = cfg.get('assumptions', {}).get('oas_annual', _default_oas_annual(cfg))
            pension_income_annual = primary.get('pension_income_annual', 0)
            lif_withdrawal = getattr(final, 'lif_withdrawal', 0)
            retirement_income = cpp_annual_income + oas_annual + pension_income_annual + lif_withdrawal
            rrsp_withdrawal_tax = (tax_on_income(retirement_income + final.total_rrsp, brackets)
                                   - tax_on_income(retirement_income, brackets))

    # Capital gains tax on non-reg (DP#19: use tracked ACB)
    cg_inclusion = cfg.get('assumptions', {}).get('capital_gains_inclusion', 0.50)
    # Use tracked ACB if available (from YearResult.non_reg_acb),
    # otherwise estimate from cumulative contributions
    non_reg_acb = getattr(final, 'non_reg_acb', None)
    if non_reg_acb is not None:
        nonreg_gains = max(0, final.non_reg_balance - non_reg_acb)
    else:
        nonreg_gains = max(0, final.non_reg_balance - sum(yr.contributions.get('non_reg', 0) for yr in results))
    # DP#16/issue #232: Compute retirement income from CPP + OAS + pension + LIF
    # Per issue #232: the placeholder retirement_income=0 understates the marginal
    # rate applied to capital gains. Use actual CPP/OAS/pension data from config.
    cpp_monthly_for_cg = primary.get('cpp_monthly_estimated', 0)
    cpp_annual_for_cg = cpp_monthly_for_cg * 12 if cpp_monthly_for_cg > 0 else 0
    oas_annual_for_cg = cfg.get('assumptions', {}).get('oas_annual', _default_oas_annual(cfg))
    pension_income_for_cg = primary.get('pension_income_annual', 0)
    lif_withdrawal_for_cg = getattr(final, 'lif_withdrawal', 0)
    retirement_income = cpp_annual_for_cg + oas_annual_for_cg + pension_income_for_cg + lif_withdrawal_for_cg
    cg_tax = nonreg_gains * cg_inclusion * marginal_rate(retirement_income + nonreg_gains * cg_inclusion, brackets)

    # RESP withdrawal tax
    resp_eap_portion = cfg.get('assumptions', {}).get('resp_eap_taxable_portion', 0.60)
    resp_eap_rate = cfg.get('assumptions', {}).get('resp_eap_tax_rate', 0.15)
    resp_tax = final.resp_balance * resp_eap_portion * resp_eap_rate

    # DP#16/issue #231: LSIF tax credit computation.
    # A spouse below the LSIF income threshold is eligible for credits on FTQ
    # purchases up to $5k/yr; a primary above the threshold is ineligible for the
    # provincial credit. Eligibility is decided by the lsif_credit module from the
    # income data in the config — names and incomes are never hardcoded here.
    # DP#13: birth_year is sourced from config; the placeholder (LSIFPurchase's
    # default of 2000) is a clearly-dated stand-in, not a real person's year.
    lsif_credit_total = 0.0
    lsif_purchase = lsif_from_config(cfg, birth_year=primary.get('birth_year', LSIFPurchase.birth_year), year=2026)
    if lsif_purchase is not None and lsif_purchase.amount > 0:
        lsif_result = compute_lsif_credit(lsif_purchase, year=2026)
        lsif_credit_total = lsif_result.federal_credit + lsif_result.quebec_credit

    # Also check spouse LSIF eligibility (the below-threshold spouse is the typically eligible one)
    spouse_mem = find_member_by_role(members, 'spouse', {})  # #699 seam
    spouse_lsif_purchase = lsif_from_config(cfg, birth_year=spouse_mem.get('birth_year', LSIFPurchase.birth_year), year=2026)
    if spouse_lsif_purchase is not None and spouse_lsif_purchase.amount > 0:
        spouse_lsif_result = compute_lsif_credit(spouse_lsif_purchase, year=2026)
        lsif_credit_total += spouse_lsif_result.federal_credit + spouse_lsif_result.quebec_credit

    # DP#16: zero-emission vehicle incentives. Fires only when the household
    # declares a zev_purchases[] acquisition; absent the block this is 0.0 and
    # every existing household's number is byte-identical (DP#32).
    #
    # Two INDEPENDENT programs are priced per acquisition and summed: the
    # federal iZEV incentive (closed 2025-03-31) and, for a Quebec household,
    # the provincial Roulez vert rebate. Each decides its own dated eligibility
    # from the acquisition date -- neither reads the other, and a household may
    # receive both, one, or neither.
    #
    # KNOWN SIMPLIFICATION, shared verbatim with lsif_credit_total above: the
    # incentive is added to the terminal objective undiscounted, as though
    # received at the horizon rather than in the acquisition year. It is
    # therefore not compounded over the years between. This understates an
    # early acquisition relative to a late one. Correcting it means routing the
    # incentive through the yearly fold as a real inflow, which is the decision
    # dimension's job, not this module's.
    zev_incentive_total = 0.0
    _province = cfg.get('tax', {}).get('province')
    for _entry in cfg.get('zev_purchases', []):
        _purchase = zev_purchase_from_dict(_entry)
        zev_incentive_total += compute_izev_incentive(_purchase).amount
        if _province == 'quebec':
            zev_incentive_total += compute_roulez_vert_rebate(
                acquisition_date=_purchase.acquisition_date,
                msrp=_purchase.trim_msrp,
                propulsion=_purchase.propulsion,
                is_quebec_resident=True,
            ).amount

    # Issue #1034: price the SM sleeve's terminal deemed disposition with the
    # SAME estate code path compute_after_tax_estate uses (DP#9 -- one
    # spelling, not a parallel marginal_rate computation). final.total_assets
    # carries the SM sleeve (optimize.py:437), so pre-#1034 this objective
    # taxed non_reg_balance's accrued gain but left the SM sleeve's entire
    # embedded gain untaxed -- an unpriced thumb on the scale in favour of
    # leverage that let flipping --objective between max_net_benefit and
    # max_after_tax_estate reverse the sign of the leverage recommendation.
    # sm_investment_cost_basis is on YearResult since #1032 (344106b). The
    # estate path is invoked ONLY when an SM sleeve is present (the golden
    # household and every sleeve-less YearResult get sm_deemed_tax = 0.0 ->
    # byte-identical, DP#32). D3: ``non_reg_acb`` MUST be a float -- compute_estate
    # prices the non-reg pot too and requires a float ACB; a None ACB cannot be
    # priced, and silently substituting $0 tax would let the sleeve's entire
    # embedded gain escape (AGENTS.md: a plausible answer from absent data is
    # worse than crashing). The production fold always tracks a float ACB, so
    # this only fires for a hand-crafted YearResult -- raise loudly.
    sm_deemed_tax = 0.0
    if getattr(final, 'sm_investment_balance', 0.0) > 0.0:
        if getattr(final, 'non_reg_acb', None) is None:
            raise ValueError(
                "compute_net_benefit cannot price the SM sleeve's terminal "
                "deemed disposition when final.non_reg_acb is None: the estate "
                "path (compute_estate) prices the non-reg pot too and requires a "
                "float ACB. The fold always tracks a float ACB; a hand-crafted "
                "YearResult must supply one (or set sm_investment_balance=0 to "
                "skip the sleeve). Silently substituting $0 tax would let the "
                "sleeve's entire embedded gain escape (DP#32).")
        # D11 (#1072): the ranking path precomputes the EstateResult once per
        # strategy and stashes it on cfg (keyed to id(results)) so this
        # objective, the net_benefit report column, and the after_tax_estate
        # report column all reuse ONE compute_estate call. N1: keyed to
        # id(results) so it cannot leak across different results --
        # _risk_ensemble_scores calls objective.evaluate for N ensemble paths
        # with the SAME cfg dict, so an un-keyed stash would price every
        # ensemble path with the representative path's estate.
        # N1: identity on the list itself (``is``), with the list held by
        # the stash so CPython's list free-list cannot recycle a freed
        # address into a stale match. id() would be silently fallible; the
        # strong reference makes the match correct BY CONSTRUCTION.
        _precomputed = (cfg.get('_precomputed_estate_result')
                        if cfg.get('_precomputed_estate_for') is results
                        else None)
        if _precomputed is not None:
            sm_deemed_tax = _precomputed.sm_investment_tax
        else:
            _sm_estate_args = _estate_call_args(results, cfg)
            if _sm_estate_args is not None:
                sm_deemed_tax = compute_estate(**_sm_estate_args).sm_investment_tax

    return (final.total_assets - final.total_debt
            + total_rrsp_savings + total_sm_savings + total_traced_savings
            - rrsp_withdrawal_tax - cg_tax - resp_tax - sm_deemed_tax
            + lsif_credit_total + zev_incentive_total)


def _risk_ensemble_scores(
    config: SimulationConfig,
    adapter,
    strategy: AllocationStrategy,
    rate_path: RatePath,
    use_readvanceable: bool,
    deduct_later: bool,
    lump_sum: float,
    free_cash: float,
    objective: ObjectiveFunction,
    cfg_dict: Dict,
    seed: int = RISK_ENSEMBLE_SEED_BASE,
) -> Optional[List[float]]:
    """Per-path terminal scores over a re-seeded stochastic-return ensemble
    (issue #937), or ``None`` when the household declared no stochastic return
    model.

    Only the INVESTMENT return varies across the ensemble -- the mortgage
    ``rate_path`` and every other candidate parameter are held fixed, so the
    spread the risk objective sees is pure sequence-of-returns risk on the
    portfolio. Each member re-seeds the household's OWN declared return model
    (``dataclasses.replace`` re-runs ``ReturnModel.__post_init__`` to regenerate
    that seed's rate path), so the mean/sigma are the household's, never
    invented here (DP#32). Returning ``None`` when the model is not stochastic
    lets the caller keep the single deterministic score -- an absent
    distribution is not a zero distribution.

    DP#23: ``seed`` threads the reproducible RNG base to the caller. The default
    (``RISK_ENSEMBLE_SEED_BASE = 42``) preserves the historical behaviour
    byte-for-byte; ``seed=0`` is a valid, falsy seed and is honoured as-is.
    """
    from dataclasses import replace
    from return_model import build_return_model_from_config

    data = config.return_model_data
    if not data:
        return None
    base = build_return_model_from_config(data)
    if base.type not in ('stochastic', 'mean_reverting'):
        return None

    scores: List[float] = []
    for i in range(RISK_ENSEMBLE_PATHS):
        rm = replace(base, seed=seed + i)
        sim = FamilySimulation(
            config=config,
            adapter=adapter,
            strategy=strategy,
            rate_path=rate_path,
            use_readvanceable=use_readvanceable,
            deduct_later=deduct_later,
            lump_sum=lump_sum,
            free_cash=free_cash,
            return_model=rm,
        )
        scores.append(objective.evaluate(sim.run(), cfg_dict))
    return scores


def evaluate_strategy_with_simulation(
    name: str,
    strategy: AllocationStrategy,
    config: SimulationConfig,
    rate_path: RatePath,
    use_readvanceable: bool = True,
    deduct_later: bool = False,
    lump_sum: float = 0.0,
    free_cash: float = 0.0,
    objective: ObjectiveFunction = None,
    include_year_by_year: bool = True,
) -> Dict:
    """Evaluate a strategy using the modular simulation engine.

    This replaces the old monolithic evaluate_strategy().
    The caller provides a pre-built SimulationConfig and RatePath,
    keeping this function pure and composable.

    Args:
        objective: ObjectiveFunction for scoring (default: MAX_NET_BENEFIT).
                   Per DP#22, the optimizer ranks, it doesn't choose.

    The returned dict always carries ``year_by_year`` — the serialized per-year
    series (issue #248), which the JSONL session logger (issue #239) persists
    alongside the summary so successive runs can be compared.

    When ``include_year_by_year`` is False (issue #1058), the per-year
    serialization is skipped and ``year_by_year`` is an empty list.  This is
    the opt-in fast path for score-only callers (VOI sweep) that never read
    the series.  DP#13: the default (True) preserves existing behaviour;
    the flag is an explicit opt-in, never a silent fallback.
    """
    # DP#22: default to MAX_NET_BENEFIT for backward compatibility
    if objective is None:
        objective = MAX_NET_BENEFIT
    # DP#8: Create a jurisdiction adapter for FamilySimulation
    from countries.canada.adapter import CanadaAdapter
    adapter = CanadaAdapter(config)
    
    sim = FamilySimulation(
        config=config,
        adapter=adapter,
        strategy=strategy,
        rate_path=rate_path,
        use_readvanceable=use_readvanceable,
        deduct_later=deduct_later,
        lump_sum=lump_sum,
        free_cash=free_cash,
    )
    results = sim.run()

    # Build config dict for net benefit calculation
    cfg_dict = {
        'assumptions': {
            'capital_gains_inclusion': config.capital_gains_inclusion,
            'resp_eap_taxable_portion': config.resp_eap_taxable_portion,
            'resp_eap_tax_rate': config.resp_eap_tax_rate,
        },
        'family': {'members': config.family_members},
        # Issue #580: max_after_tax_estate needs house FMV and jurisdiction
        # (year-versioned brackets, DP#20) to price the deemed disposition.
        # 'start_year' (not the terminal calendar year -- YearResult.year is a
        # 1-indexed relative offset, not a calendar year) lets the objective
        # derive the terminal year as start_year + len(results) - 1.
        # Additive keys only -- existing objectives ignore what they don't read.
        'property': {'house_value': config.house_value},
        'tax': {'province': config.province, 'start_year': config.start_year},
        # epic #603 Track C Phase 2c (#600): the DECLARED estate elections.
        # Without this the objective falls back to
        # objective._UNDECLARED_ESTATE_DEFAULTS and the
        # `estate_elections_not_declared` caveat fires -- i.e. an empty estate
        # block here would silently re-create the exact five assumptions this
        # phase exists to eliminate, and the fidelity output would say so.
        'estate': config.estate_data,
    }

    # D11 (#1072): compute the estate ONCE per strategy and reuse it for the
    # objective's SM deemed-disposition tax (compute_net_benefit reads it), the
    # net_benefit report column, and the after_tax_estate report column.
    # compute_estate is expensive on a large SM sleeve; pre-#1072 it ran 2-3x
    # per strategy. Stashed on the per-strategy cfg_dict under a private key.
    # N1: keyed by IDENTITY to the results list (``is``, held by the stash so
    # CPython's list free-list cannot recycle a freed address into a stale
    # match) so the stash cannot leak across different ``results`` --
    # _risk_ensemble_scores calls objective.evaluate for N ensemble paths with
    # the SAME cfg_dict, so an un-keyed stash would price every ensemble path
    # with the representative path's estate (a >$1M silent error on a net_benefit
    # + rank_from_distribution objective).
    _estate_args = _estate_call_args(results, cfg_dict)
    _precomputed_estate = (compute_estate(**_estate_args)
                           if _estate_args is not None else None)
    cfg_dict['_precomputed_estate_result'] = _precomputed_estate
    cfg_dict['_precomputed_estate_for'] = results  # strong ref; is-compared

    net = objective.evaluate(results, cfg_dict)
    # Issue #937: a distributional (risk) objective ranks by a tail statistic of
    # the terminal-wealth DISTRIBUTION, so it scores the candidate over a
    # stochastic-return ensemble rather than by this single representative path.
    # ``results`` above stays the representative trajectory the report shows; the
    # RANKING score becomes the aggregate over the ensemble. Absence-safe: a
    # per-path objective (rank_from_distribution is None) or a household with no
    # stochastic return model keeps ``net`` unchanged -> byte-identical ranking.
    objective_score = net
    if objective.rank_from_distribution is not None:
        _ensemble = _risk_ensemble_scores(
            config, adapter, strategy, rate_path, use_readvanceable,
            deduct_later, lump_sum, free_cash, objective, cfg_dict)
        if _ensemble is not None:
            objective_score = objective.aggregate(_ensemble, cfg_dict)
    brackets = default_tax_provider().get_combined_brackets()

    # Also compute net_benefit for backward-compatible reporting
    net_benefit = compute_net_benefit(results, cfg_dict)

    # Issue #672: net_benefit's withdrawal-tax estimate never models death --
    # #661's VOI sweep measured that the estate election levers (spousal
    # rollover, TFSA successor holder, principal-residence designation,
    # rollover_overrides, life insurance) move max_after_tax_estate by
    # $84,998 on a reference household and move net_benefit by exactly $0.
    # Computed UNCONDITIONALLY here -- not gated on which `objective` argument
    # was passed -- so a household that declared its estate elections can see
    # the figure net_benefit is blind to without a separate CLI invocation.
    # See model_fidelity.py's `net_benefit_omits_estate_elections` caveat,
    # which names this objective wherever net_benefit is the one reported.
    # Issue #732 (DP#25): call compute_estate DIRECTLY here (not via
    # objective.compute_after_tax_estate) so the static reach-detector sees a
    # real production edge optimize -> countries.canada.estate.compute_estate.
    # objective._estate_call_args is the shared pure arg-prep (no countries
    # import in objective.py); the registry-only path objective uses would be
    # invisible to the call graph. The result is identical to
    # compute_after_tax_estate(...).net_estate -- same function, same args.
    # D11 (#1072): reuse the per-strategy EstateResult computed above (the
    # objective and net_benefit column already read it via the cfg stash) --
    # compute_estate now runs exactly ONCE per strategy. The static
    # reach-detector still sees the compute_estate call in the precompute above,
    # so the optimize -> countries.canada.estate production edge is preserved.
    after_tax_estate = (_precomputed_estate.net_estate
                        if _precomputed_estate is not None else 0.0)

    # Cumulative values for reporting
    total_rrsp_savings = sum(yr.rrsp_tax_savings for yr in results)
    total_sm_savings = sum(yr.readvance_tax_savings for yr in results)

    final = results[-1]

    # DP#8/DP#25: thread the per-year series through to the reporting layer.
    # The reports compose through this data; they do not recompute it (issue #248).
    # Issue #1058: score-only callers (VOI sweep) never read the series, so
    # skip the ~77k asdict calls per run when the flag is False (DP#13: the
    # default True preserves existing behaviour; False is explicit opt-in).
    if include_year_by_year:
        from dataclasses import asdict
        year_by_year = [asdict(yr) for yr in results]
    else:
        year_by_year = []

    # Issue #679: the solvency verdict travels WITH the ranking row, so no
    # caller can read `net_benefit` without `ruined` being right there beside
    # it. Signalled as DATA, never by raising -- issue #657's `except
    # Exception: score = -inf` would swallow an exception here and rank a
    # RUINED strategy as merely a bad one, which is precisely the
    # "crashing is indistinguishable from bad" defect that bug is about.
    solvency = summarize_solvency(results)
    # Issue #707: the decumulation shortfall verdict travels WITH the ranking
    # row, right beside `solvency`/`ruined`, so no caller can read
    # `net_benefit` without `exhausted` being right there beside it. Signalled
    # as DATA, never by raising -- same reason as solvency above.
    drawdown_shortfall = summarize_drawdown_shortfall(results)
    # Issue #758: the runway verdict (months-to-ruin) travels WITH the ranking
    # row, right beside `solvency`/`ruined` and `drawdown_shortfall`/`exhausted`.
    # Composes #679's solvency fold into a months figure -- the number a
    # household actually wants before signing a mortgage, and the one that
    # decides whether a bad year forces the sale of the house. The shock date
    # is read off the config's dated income segments (#674); the projection's
    # start calendar year off the config. Signalled as DATA, never by raising
    # -- same reason as solvency/drawdown above (issue #657).
    from runway import compute_runway, shock_date_from_members
    shock_date = shock_date_from_members(config.family_members)
    runway = compute_runway(results, shock_date=shock_date,
                            start_year=config.start_year)

    # Issue #170: the RRSP-refusal verdict travels WITH the ranking row, right
    # beside drawdown_shortfall/solvency -- a scenario whose declared RRSP
    # contributions were clipped to room must be readable off the row, not
    # rediscovered from the source. Signalled as DATA (same bridge as #707).
    from rules_contributions import summarize_rrsp_refusal
    rrsp_refusal = summarize_rrsp_refusal(results)

    return {
        'strategy': name,
        'year_by_year': year_by_year,
        'solvency': solvency,
        'ruined': solvency['ruined'],
        'drawdown_shortfall': drawdown_shortfall,
        'exhausted': drawdown_shortfall['exhausted'],
        'rrsp_refusal': rrsp_refusal,
        'runway': runway.to_dict(),
        'total_invested': final.total_assets + final.total_debt - final.non_reg_balance,
        'TFSA': final.total_tfsa,
        'RRSP': final.primary_rrsp,
        'Spousal_RRSP': final.spousal_rrsp,
        'RESP': final.resp_balance,
        'Non_Reg': final.non_reg_balance,
        'total_debt': final.total_debt,
        'sm_deductible_debt': final.heloc_balance,
        'readvanceable_mortgage': use_readvanceable,
        'deduct_later': deduct_later,
        'rrsp_total_savings': total_rrsp_savings,
        'sm_10yr_savings': total_sm_savings,
        'future_value': final.total_assets,
        'net_benefit': net_benefit,
        # Issue #672: reported ALONGSIDE net_benefit, always -- see the
        # comment at after_tax_estate's computation above.
        'after_tax_estate': after_tax_estate,
        'objective_name': objective.name,
        'objective_score': objective_score,
    }


# ── CLI Entry Point ──────────────────────────────────────────────────────────

def _cashout_for_ltv(cfg: Dict, ltv: float) -> float:
    """Cash-out implied by refinancing the base mortgage up to ``ltv`` (>= 0)."""
    house_value = cfg.get('property', {}).get('house_value', 0)
    orig_mortgage = cfg.get('property', {}).get('mortgage_balance', 0)
    return max(0.0, house_value * ltv - orig_mortgage)


def _scenario_overlay(cfg: Dict, ltv: float, label: str) -> ScenarioOverlay:
    """Build the authoritative ScenarioOverlay for refinancing ``cfg`` to ``ltv``.

    Issue #259: this is the SINGLE place optimize.py derives a refinance
    scenario, so it routes through the same ``build_overlay_config`` /
    ``apply_overlay`` machinery as ``simulate.py`` (cash-out -> mortgage debt,
    proceeds invested; #664: margin_available reduced by the same cash-out,
    not inflated). ``mortgage_rate`` is taken from the base cfg so the
    overlay does not silently change the rate.

    Issue #655: ``ltv`` here is an exploratory sweep level (the household has
    not committed to it), not a specific declared refinance option, so the
    amortization uses ``refinance_amortization_fallback`` (real declared
    ``property.refinance_amortization_years`` if present, else the DP#13
    round placeholder) rather than requiring one -- apply_overlay itself
    stays strict for callers that DO have a specific declared option.
    """
    return ScenarioOverlay(
        label=label,
        cash_out=_cashout_for_ltv(cfg, ltv),
        mortgage_rate=cfg.get('property', {}).get('mortgage_rate', 0.05),
        ltv=ltv,
        refinance_amortization_years=refinance_amortization_fallback(cfg),
    )


# Issue #846: the generic LTV ladder this module sweeps when the household has
# NOT declared decisions.mortgage.refinance_options. It is a discovery device --
# an opinion about where to look, authored by this tool, not by the household --
# so a contract that DOES declare its refinance options outranks it outright
# (DP#13: a default is a fallback for absent input, never a way to coerce a
# supplied one).
DEFAULT_LTV_LADDER = [0.0, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]


def declared_refinance_candidates(cfg: Dict) -> List[Dict]:
    """The household's DECLARED refinance options, resolved to candidate rows.

    DP#9: the ONE mapping from ``decisions.mortgage.refinance_options`` to
    candidates, routed through ``discover_anchors`` -- so every surface that
    scores a declared option (the LTV exploration's #853 union AND the #845
    structure cross) agrees on what that option MEANS (its derived ``ltv`` from
    ``cash_out``; the adapter drops the declared ``.ltv``).

    Returns the declared options ONLY -- no auto-discovered ladder. Issue #853's
    union (declared options ANNOTATE the swept curve) is a property of the LTV
    EXPLORATION table (``refinance_candidates``), not of every consumer: the
    #845 structure cross deliberately scores structures at the declared options
    only, because each cell is a full simulation and the notary-day structural
    choice is compared "A vs B at the leverage you would actually pick", not
    across a 7-rung ladder nobody asked for.
    """
    from scenario_discovery import discover_anchors
    return [{
        'id': o['id'],
        'label': o['label'],
        'cash_out': o['cash_out'],
        'ltv': o['ltv'],
        'source': 'declared',
    } for o in discover_anchors(cfg)['refinance']]


def refinance_candidates(cfg: Dict, ltv_steps: List[float] = None) -> List[Dict]:
    """The refinance candidates optimize.py's refinance analysis explores.

    Issue #846: ``decisions.mortgage.refinance_options`` was a schema-validated,
    schema-REQUIRED contract leaf that input_contract.py faithfully mapped onto
    ``cfg['scenarios']['refinance']`` -- and that NOTHING in optimize.py ever
    read. Measured before this fix: raising a declared option's ``cash_out``
    from $50,000 to $400,000 left every number optimize.py printed byte-
    identical, because ``run_ltv_exploration`` substituted the hardcoded ladder
    above for the household's declaration. The household could not influence the
    optimizer's refinance analysis at all -- "parsed, mapped, then never passed"
    (AGENTS.md), the same defect family as #713/#714/#830.

    Issue #853 / DP#33 -- a declaration is a LENS, not a BLINDFOLD. #846 made a
    declared candidate list REPLACE the ladder outright, so a household that
    declared ``{no_cash_out, cash_out_80}`` never learned that 50/60/70% LTV
    existed at all -- if the optimum lived at 70%, they saw only their own two
    guesses ranked against each other and concluded the better guess was optimal.
    So now the household's declaration ANNOTATES the full sweep instead of
    truncating it: the whole ``DEFAULT_LTV_LADDER`` is explored, each declared
    option MARKS the rung it coincides with (or is INSERTED in situ when it falls
    between rungs), and every row is flagged so ``_print_refinance_basis`` can put
    a ``★`` beside the household's own options. The declaration then shows both
    "how do my options compare to each other" AND "where do they sit on the whole
    curve" -- including whether a rung they did NOT declare beats both.

    ``ltv_steps`` (an explicit caller-supplied ladder) still wins over BOTH the
    declaration and the union -- it is a direct instruction from a caller that has
    already decided exactly what to sweep -- but ``_print_refinance_basis`` then
    says so loudly rather than quietly presenting the ladder as though it were the
    declaration (#845: never two contradictory authoritative answers from one run).

    Returns one dict per candidate: ``{id, label, cash_out, ltv, source, declared,
    declared_id, declared_label, annotated}``. ``source`` is ``'declared'`` (an
    inserted between-rung option the household typed) or ``'ladder'`` (a swept
    rung); ``declared`` marks any rung a declaration lands on; ``annotated`` is
    True whenever the declaration was unioned over the sweep (the #853 default).
    """
    declared = cfg.get('scenarios', {}).get('refinance', [])
    if ltv_steps is None and declared:
        # DP#33 (#853): union the declared options OVER the full ladder rather
        # than replacing it.
        from scenario_discovery import annotate_declared_over_sweep
        swept = [_ladder_candidate(cfg, ltv) for ltv in DEFAULT_LTV_LADDER]
        return annotate_declared_over_sweep(swept, declared_refinance_candidates(cfg),
                                            key='ltv')

    if ltv_steps is None:
        ltv_steps = DEFAULT_LTV_LADDER
    # No declaration (or an explicit caller ladder): a plain sweep. DP#32/#674:
    # emit the same annotation keys as the union branch so every consumer can
    # subscript them unconditionally -- here they are all the "not declared"
    # value, never absent.
    return [_ladder_candidate(cfg, ltv, annotated=False) for ltv in ltv_steps]


def _ladder_candidate(cfg: Dict, ltv: float, annotated: bool = True) -> Dict:
    """One swept LTV-ladder rung with its #853 annotation keys (issue #853).

    ``annotated`` is True when this rung is part of a declaration-annotated union
    (the #853 default) and False for a plain ladder-only sweep, so
    ``_print_refinance_basis`` can tell "the full curve with your options marked"
    apart from "a generic ladder / an explicit override".
    """
    return {
        'id': f'ltv_{ltv:.2f}',
        'label': f'LTV {ltv:.0%}',
        'cash_out': _cashout_for_ltv(cfg, ltv),
        'ltv': ltv,
        'source': 'ladder',
        'declared': False,
        'declared_id': None,
        'declared_label': None,
        'annotated': annotated,
    }


def _candidate_overlay(cfg: Dict, candidate: Dict) -> ScenarioOverlay:
    """The authoritative ScenarioOverlay for ONE refinance candidate (#846).

    Same construction ``_scenario_overlay`` makes for a swept LTV level -- it is
    ``cash_out``, not ``ltv``, that ``apply_overlay`` books as mortgage debt and
    invests, so a declared option carrying a real ``cash_out`` routes through the
    identical machinery with no special case (DP#9).
    """
    return ScenarioOverlay(
        label=candidate['label'],
        cash_out=candidate['cash_out'],
        mortgage_rate=cfg.get('property', {}).get('mortgage_rate', 0.05),
        ltv=candidate['ltv'],
        refinance_amortization_years=refinance_amortization_fallback(cfg),
    )


def run_ltv_exploration(cfg: Dict, input_path: str = "input.json",
                        ltv_steps: List[float] = None,
                        objective: ObjectiveFunction = None) -> List[Dict]:
    """Run strategies at each refinance candidate.

    At each candidate, the cash-out increases margin_available and the
    mortgage_balance grows. This lets us compare strategies across
    all refinance levels.

    Issue #846: the candidates come from ``refinance_candidates`` -- the
    household's declared ``decisions.mortgage.refinance_options`` when it
    declared any, else this module's generic ``DEFAULT_LTV_LADDER``. Before this
    fix the ladder was unconditional, so a declared option was silently ignored
    and this table contradicted the structure ranking (#845).

    Returns list of results dicts with 'ltv' field added.

    Args:
        objective: ObjectiveFunction for scoring (default: MAX_NET_BENEFIT).
        ltv_steps: explicit ladder; overrides BOTH the declaration and the
            default (and is reported as such -- see _print_ltv_exploration).
    """
    candidates = refinance_candidates(cfg, ltv_steps)
    declared_count = len(cfg.get('scenarios', {}).get('refinance', []))

    # Each refinance candidate is an INDEPENDENT run_optimization fold; build
    # the scenario list and run them across cores (perf), collecting IN INPUT
    # ORDER. A candidate that ``run_optimization`` REFUSES (the two typed,
    # EXPECTED infeasibilities below) comes back as a ``_ScenarioRefused`` from
    # its worker rather than aborting the whole pool -- the SAME graceful,
    # per-candidate refusal the serial sweep recorded (#891). Any OTHER
    # exception still propagates (fail loud, #657): the worker does not catch it.
    scenarios = []
    for candidate in candidates:
        # issue #259: derive the scenario through the SAME authoritative overlay
        # path as the headline and simulate.py (build_overlay_config), so
        # cash-out → mortgage debt, proceeds invested, margin not inflated.
        overlay = _candidate_overlay(cfg, candidate)
        scenarios.append((candidate, overlay, {
            'kwargs': dict(cfg=cfg, input_path=input_path, objective=objective,
                           overlay=overlay),
            # Issue #891: a DECLARED candidate whose cash-out breaches the 80%
            # charge limit is REFUSED-and-SKIPPED, not fatal -- exactly the
            # graceful refusal the (refinance x structure) cross already records
            # in _compose_structure_cell. build_overlay_config -> apply_overlay
            # raises these two typed, EXPECTED infeasibilities; the worker
            # records the reason IN WORDS (DP#32/#681) and the sweep moves on,
            # so ONE infeasible declared option can no longer abort the whole
            # sweep and hide every feasible rung below it.
            'catch_refusals': True,
        }))

    all_results = []
    for (candidate, overlay, _payload), outcome in zip(
            scenarios, _map_scenarios([s[2] for s in scenarios])):
        if isinstance(outcome, _ScenarioRefused):
            all_results.append({
                'ltv': candidate['ltv'],
                'cashout': overlay.cash_out,
                'refinance_id': candidate['id'],
                'refinance_label': candidate['label'],
                'refinance_source': candidate['source'],
                'refinance_declared': candidate['declared'],
                'refinance_declared_id': candidate['declared_id'],
                'refinance_declared_label': candidate['declared_label'],
                'refinance_annotated': candidate['annotated'],
                'refinance_declared_count': declared_count,
                # Issue #891: NO net_benefit -- this is a refusal marker, not a
                # scored data point; _print_ltv_exploration keeps it out of the
                # ranking tables and names it in a loud NOT SCORED notice.
                'refinance_refused': True,
                'refinance_refusal': outcome.reason,
            })
            continue

        ltv_results = outcome
        # Add LTV info to each result
        for r in ltv_results:
            r['ltv'] = candidate['ltv']
            r['cashout'] = overlay.cash_out
            # issue #846: every row records WHICH candidate produced it and
            # where that candidate came from, so the printed basis is read off
            # the same rows whose net_benefit is reported -- not re-derived, and
            # so unable to disagree with them (DP#9).
            r['refinance_id'] = candidate['id']
            r['refinance_label'] = candidate['label']
            r['refinance_source'] = candidate['source']
            # issue #853 / DP#33: carry the annotation so the printed basis can
            # mark which rungs are the household's own declared options, in situ
            # on the full swept curve, rather than truncating the curve to them.
            r['refinance_declared'] = candidate['declared']
            r['refinance_declared_id'] = candidate['declared_id']
            r['refinance_declared_label'] = candidate['declared_label']
            r['refinance_annotated'] = candidate['annotated']
            r['refinance_declared_count'] = declared_count
            all_results.append(r)

    return all_results


def _print_refinance_basis(results: List[Dict]) -> None:
    """State WHERE the refinance candidates below came from (#845/#846).

    The reader must never have to infer whether this table swept their declared
    ``decisions.mortgage.refinance_options`` or a ladder this tool made up. Read
    off the same rows whose net_benefit is printed, so the basis cannot disagree
    with the numbers (DP#9).
    """
    annotated = any(r.get('refinance_annotated') for r in results)
    declared_counts = {r.get('refinance_declared_count', 0) for r in results}
    declared_count = max(declared_counts) if declared_counts else 0

    if annotated and declared_count > 0:
        # Issue #853 / DP#33: the declaration is a LENS on the full sweep, not a
        # blindfold that hides it. The whole LTV ladder was explored; the
        # household's own options are MARKED in situ (★) rather than replacing
        # the curve, so a rung they did not declare can still win and be seen.
        marked = sorted({r.get('refinance_declared_label')
                         for r in results if r.get('refinance_declared')})
        print(f"\n  ✅ BASIS: the FULL LTV sweep, with your {declared_count} declared refinance")
        print(f"      option(s) (decisions.mortgage.refinance_options) marked ★ in situ (#853).")
        print(f"      The whole curve is ranked, NOT just your options — a rung you did not declare")
        print(f"      can still win, and you will see it. ★ = your declared option:")
        for label in marked:
            print(f"        ★ {label}")
        return

    if declared_count > 0:
        # A caller forced an explicit ladder while the household HAD declared
        # options. Overriding is allowed; doing it quietly is not -- that is
        # exactly how #845's two contradictory authoritative answers happened.
        print(f"\n  ⚠️  BASIS: a generic LTV ladder authored by this tool — it OVERRIDES the")
        print(f"      {declared_count} refinance option(s) you declared at "
              f"decisions.mortgage.refinance_options.")
        print(f"      The rows below are NOT your declared options and must not be read as a")
        print(f"      ranking of them (#845).")
        return

    print(f"\n  ℹ️  BASIS: a generic LTV ladder authored by this tool — you declared no")
    print(f"      decisions.mortgage.refinance_options, so there is nothing to rank against it.")
    print(f"      Declare them to have this table sweep YOUR options instead (#846).")


# Issue #890 removed ``_print_narrowing_notice``. #846 gave optimize.py a loud
# "declared candidates REPLACED the auto-discovered exploration" notice; #853
# then excluded ``refinance`` from it (optimize.py's LTV table unions the
# declared options over the ladder). #890 completed DP#33 for the other three
# dimensions -- mortgage / strategy / resp_action now ANNOTATE their sweep in
# ``discover_anchors`` too -- so NO declared dimension narrows in optimize.py and
# the notice had nothing left to say (its output was unreachable, DP#9). The one
# surface where a declaration still narrows -- ``refinance`` in simulate.py's
# grid -- keeps the notice there (``simulate.print_discovery`` ->
# ``format_narrowings``); optimize.py no longer emits one.


def structure_deductibility_caveat_lines() -> List[str]:
    """What this ranking now prices, and the one limitation that remains (#850).

    #845 makes advance-vs-line rankable at equal leverage; #850 now prices the
    s.20(1)(c) asymmetry that motivates the choice — a ``borrowing_purpose``
    trace deducts interest on the invested portion of both the advance and the
    drawn line, and the advance's deductible balance amortizes away with the
    principal (the erosion #849 names) while the line's does not. Only the
    portion of the lump that lands in a NON-registered account is traced as
    deductible; a dollar borrowed to fill RRSP/TFSA room is not (correct).

    Remaining limitation (issue on ``apply_sm_interest``): the Quebec deduction
    cap is applied but valued at the combined fed+QC rate, and the FEDERAL
    deduction has no such cap. That understates the deductible benefit on the
    LINE specifically (the capped leg), i.e. it works against the line. The
    ranking has been shown robust to it: removing the cap from the line's
    federal leg entirely still leaves the advance ahead. Stated here rather
    than left implied (DP#32). Extracted as a tested helper so ``main()`` gains
    no statements.
    """
    return [
        "",
        "  ℹ️  DEDUCTIBILITY IS PRICED (issue #850): interest on the invested portion of both the",
        "      advance and the drawn line is deducted; the advance's deductible balance amortizes",
        "      away with principal, the line's does not (the s.20(1)(c) asymmetry #849 asks about).",
        "      Only the NON-registered portion of the lump is deductible (filling RRSP/TFSA is not).",
        "  ⚠️  One known limit: apply_sm_interest values the Quebec cap at the combined fed+QC rate",
        "      and the federal deduction has no cap — this UNDERSTATES the line's benefit, not the",
        "      advance's. The ranking is robust to it (advance still leads with the cap removed).",
    ]


def _print_structure_deductibility_caveat() -> None:
    """Print :func:`structure_deductibility_caveat_lines` (issue #850)."""
    for line in structure_deductibility_caveat_lines():
        print(line)


def _print_headline_basis(cfg: Dict, ltv_max: float) -> None:
    """The RANKED STRATEGIES banner, and the plan it was actually computed on.

    Issue #845: this header used to read ``(current LTV: 80%)`` -- and 80% is
    ``ltv_max``, the MAXIMUM this household may borrow to, not the LTV it is
    currently at (52% on the shipped example). ``run_income_scenario_
    exploration`` refinances every strategy TO ``ltv_max``, so the table's basis
    is a maximum cash-out refinance: close to the opposite of "current". Naming
    the cash-out DOLLARS beside it makes the plan being ranked unmistakable.

    Issue #846: this table, unlike the LTV EXPLORATION, does NOT yet honour
    ``decisions.mortgage.refinance_options`` -- it pins to ``ltv_max`` for every
    household. Stated rather than left to be discovered, so a household that
    declared a no-cash-out option cannot read a max-cash-out ranking as its own
    plan. Making the headline honour the declaration is real follow-up work (it
    needs a decision about WHICH declared option the headline ranks at), not
    something to fake with a label.
    """
    print(f"  🏆 RANKED STRATEGIES — every strategy refinanced to MAX LTV "
          f"{ltv_max:.0%} (cash-out ${_cashout_for_ltv(cfg, ltv_max):,.0f})")
    print(f"{'=' * 120}")
    declared = cfg.get('scenarios', {}).get('refinance', [])
    if declared:
        print(f"  ⚠️  BASIS: ltv_max — NOT the {len(declared)} option(s) you declared at")
        print(f"      decisions.mortgage.refinance_options. See LTV EXPLORATION above for those,")
        print(f"      ranked head-to-head. These two tables model DIFFERENT plans (#845/#846).")


def _print_refinance_refusals(results: List[Dict]) -> None:
    """Name every declared refinance option the engine REFUSED to score, and why
    (issue #891, DP#32/#681).

    Mirror of ``_print_structure_refusals``: a declared option whose cash-out
    breaches the 80% charge limit is ABSENT from the tables above (it was never
    simulable), and an absence read as a poor ranking would be exactly the
    silent-drop #681 forbids. So it is named here with its reason IN WORDS.
    """
    refused = [r for r in results if r.get('refinance_refused')]
    if not refused:
        return
    print(f"\n  ⚠️  NOT SCORED — {len(refused)} declared refinance option(s) the engine REFUSED:")
    for r in refused:
        print(f"      • '{r.get('refinance_label')}' (cash-out ${r.get('cashout', 0):,.0f})")
        print(f"        {r.get('refinance_refusal')}")
    print(f"      These options are ABSENT from the tables above — do not read their absence as a")
    print(f"      poor ranking; the charge cannot support them at all (#891/#664).")


def _print_ltv_exploration(results: List[Dict]) -> None:
    """Print formatted LTV exploration results."""
    print(f"\n{'=' * 120}")
    print(f"  📊 LTV EXPLORATION — Strategies at each LTV level")
    print(f"{'=' * 120}")

    # Issue #891: a refused over-limit declared option is a marker row carrying
    # no net_benefit -- keep it out of the ranking tables (it was never scored)
    # and name it in a loud NOT SCORED notice below, exactly as the structure
    # cross does. Feasible rows alone drive every table.
    refusals = [r for r in results if r.get('refinance_refused')]
    results = [r for r in results if not r.get('refinance_refused')]

    _print_refinance_basis(results)

    # Best strategy at each LTV
    print(f"\n  Best strategy per LTV level:")
    print(f"  {'LTV':>5s}  {'Cash-out':>10s}  {'Strategy':<40s}  {'Net Benefit':>12s}  {'Total Debt':>12s}  {'TFSA':>10s}  {'RRSP':>10s}")
    print(f"  {'-' * 100}")
    
    ltvs = sorted(set(r.get('ltv', 0) for r in results))
    for ltv in ltvs:
        ltv_rows = [r for r in results if r.get('ltv') == ltv]
        best = max(ltv_rows, key=lambda r: r.get('net_benefit', 0))
        cash = best.get('cashout', 0)
        strategy_name = best.get('strategy', '?')
        if best.get('deduct_later'):
            strategy_name += ' 📋'
        net = best.get('net_benefit', 0)
        debt = best.get('total_debt', 0)
        tfsa = best.get('TFSA', 0)
        rrsp = best.get('RRSP', 0)
        # Issue #853 / DP#33: mark the rungs the household declared with ★ so the
        # declaration is a visible lens on the full curve, not a hidden filter.
        marker = ''
        declared_labels = sorted({r.get('refinance_declared_label')
                                  for r in ltv_rows if r.get('refinance_declared')})
        if declared_labels:
            marker = '  ★ ' + ', '.join(declared_labels)
        print(f"  {ltv:>4.0%}  ${cash:>9,.0f}  {strategy_name:<40s}  ${net:>10,.0f}  ${debt:>10,.0f}  ${tfsa:>9,.0f}  ${rrsp:>9,.0f}{marker}")
    
    # Strategy comparison across LTVs
    print(f"\n  Strategy comparison (averaged across LTVs):")
    print(f"  {'Strategy':<40s}  {'Avg Net':>10s}  {'Max Net':>10s}")
    print(f"  {'-' * 65}")
    
    strategies = sorted(set(r.get('strategy', '?') for r in results))
    strat_data = []
    for s in strategies:
        s_rows = [r for r in results if r.get('strategy') == s]
        avg_net = sum(r.get('net_benefit', 0) for r in s_rows) / len(s_rows) if s_rows else 0
        max_net = max(r.get('net_benefit', 0) for r in s_rows) if s_rows else 0
        strat_data.append((s, avg_net, max_net))
    
    strat_data.sort(key=lambda x: x[1], reverse=True)
    for s, avg, mx in strat_data:
        label = s
        if any(r.get('deduct_later') for r in results if r.get('strategy') == s):
            label += ' 📋'
        print(f"  {label:<40s}  ${avg/1000:>8,.0f}k  ${mx/1000:>8,.0f}k")

    # Issue #891: name any declared option refused for breaching the charge
    # limit, from the SAME rows the sweep returned (DP#9) -- never a silent drop.
    _print_refinance_refusals(refusals)

    print()


def _print_estate_ranking(results_sorted: List[Dict], results: List[Dict]) -> None:
    """Issue #672: the same strategies, ranked by ``max_after_tax_estate``
    instead of ``net_benefit`` -- printed side by side with the RANKED
    STRATEGIES table above, not as a replacement for it (DP#22: the optimizer
    ranks, it doesn't choose; the two objectives answer different questions
    and the user picks which one matters to them).

    ``net_benefit`` deducts an ESTIMATED pre-death withdrawal tax and has
    EXACTLY ZERO sensitivity to the estate election levers (spousal
    rollover, TFSA successor holder, principal-residence designation,
    rollover_overrides, life insurance) -- measured by #661's VOI sweep at
    $84,998 on a reference household, $0 on this one. Called only when
    ``objective.estate_is_declared(cfg)`` (a contract-sourced run always
    declares ``estate`` -- it is a required schema key).
    """
    print(f"\n{'=' * 120}")
    print(f"  🏛️  AFTER-TAX ESTATE — same strategies, ranked by what reaches your heirs (issue #672)")
    print(f"{'=' * 120}")
    print(f"  net_benefit above deducts an ESTIMATED pre-death withdrawal tax and does NOT price the")
    print(f"  estate elections you declared (spousal rollover, successor holder, PR designation, ...).")
    print(f"  This ranks the SAME strategies by max_after_tax_estate: the ITA s.70(5)/s.146(8.8) deemed")
    print(f"  disposition applied to the terminal balance sheet, given those elections.")

    estate_sorted = sorted(results, key=lambda r: r.get('after_tax_estate', 0), reverse=True)
    print(f"\n  {'#':<3} {'Strategy':<55} {'Net benefit':>13} {'After-tax estate':>18}")
    print(f"  {'-' * 95}")
    for i, r in enumerate(estate_sorted):
        name = r.get('strategy', '?')
        if r.get('deduct_later'):
            name += ' 📋'
        print(f"  {i+1:<3} {name:<55} ${r.get('net_benefit', 0)/1000:>10,.0f}k "
              f"${r.get('after_tax_estate', 0)/1000:>15,.0f}k")

    if results_sorted and estate_sorted:
        top_net = results_sorted[0]
        top_estate = estate_sorted[0]
        same_strategy = (top_net.get('strategy') == top_estate.get('strategy')
                          and top_net.get('deduct_later') == top_estate.get('deduct_later'))
        if not same_strategy:
            print(f"\n  ⚠️  THE TWO OBJECTIVES DISAGREE on the top strategy:")
            print(f"      net_benefit ranks first:          {top_net.get('strategy', '?')}"
                  f"{' 📋' if top_net.get('deduct_later') else ''}")
            print(f"      max_after_tax_estate ranks first: {top_estate.get('strategy', '?')}"
                  f"{' 📋' if top_estate.get('deduct_later') else ''}")

    print()


def _apply_income_scenario(cfg: Dict, income_scenario: Dict) -> Dict:
    """DP#18: overlay one income scenario onto a base config -- returns a
    MODIFIED COPY, never replaces the base outright.

    ``income_scenario`` is one entry of ``scenario_discovery.discover_anchors
    (cfg)['income']`` (keys: id, label, primary_income, spouse_income,
    primary_segments, spouse_segments). A role with no segments (``[]``, the
    empty-list convention ``_convert_income_scenarios`` uses for "this
    scenario did not override this person") is left exactly as the base
    config states it, never coerced to 0 (DP#32; #665's "job loss for one
    earner must not silently zero the other earner's income" finding).

    Issue #674 fix: a role WITH segments no longer overwrites
    ``gross_income`` directly (that was the flat-forever bug -- a "job
    loss" scenario could only ever mean "permanently lower income for the
    whole projection"). It instead attaches ``income_segments`` -- the
    dated, kind-classified schedule -- alongside the untouched base
    ``gross_income``. ``simulation.py``'s ``_income_components_for_year``
    consults ``income_segments`` first for whatever calendar year it is
    asked about, and falls back to the base (salary-grown) ``gross_income``
    for any year -- or any part of a year -- no segment covers. This is
    what lets a scenario express "N months of EI, then re-employment" and
    lets RRSP-room accrual tell an EI dollar from an employment dollar
    (ITA s.146(1); ``EARNED_INCOME_KINDS``).
    """
    cfg_variant = deepcopy(cfg)
    members = cfg_variant.get('family', {}).get('members', [])
    # Subscript, not ``.get(..., [])``: BOTH producers of an income anchor
    # (``_convert_income_scenarios`` and the auto-discovered baseline
    # ``_discover_income_scenarios``) always emit these keys -- `[]` where
    # the scenario overrides nobody. So an absent key is not "no override",
    # it is a broken producer, and it must crash rather than silently drop
    # a schedule the household declared (DP#32; #665's "parsed, mapped,
    # then never passed").
    primary_segments = income_scenario['primary_segments']
    spouse_segments = income_scenario['spouse_segments']
    for m in members:
        if m.get('role') == 'primary' and primary_segments:
            m['income_segments'] = primary_segments
        elif m.get('role') == 'spouse' and spouse_segments:
            m['income_segments'] = spouse_segments
    return cfg_variant


def run_income_scenario_exploration(cfg: Dict, input_path: str = "input.json",
                                     objective: ObjectiveFunction = None,
                                     ltv_max: float = None) -> List[Dict]:
    """Run the full optimizer once per declared income scenario (issue #665).

    ``decisions.income[]`` in the input contract exists so a household can
    ask the risk question a ranked table cannot otherwise answer: "does the
    recommended strategy survive a salary cut, or a job loss covered only by
    EI?" Before this fix, ``input_contract.py`` mapped the block to
    ``cfg['scenarios']['income']`` and NOTHING downstream ever read it --
    ``run_optimization`` always ran the base config's income only, so every
    declared scenario was silently discarded and the ranked table never
    contained the answer the household actually asked for.

    Uses ``scenario_discovery.discover_anchors(cfg)['income']`` as the ONE
    source of income scenarios -- the same list ``simulate.py``'s
    ``enumerate_overlays``/``build_all_overlays`` already consume (DP#8: one
    mapping, not a second one invented here). When the contract declares NO
    income scenarios, ``discover_anchors`` still returns exactly one entry
    (an auto-discovered "current income" scenario) -- there is always at
    least one scenario to run, made explicit by data, never an implicit
    ``or [None]``-style default (DP#13/DP#32: see optimizer.py's
    ``GridOptimizer.optimize`` for the same fix applied to that engine).

    Returns the UNION of every income scenario's ranked results (N income
    scenarios x M discovered strategies), each result tagged with
    ``income_scenario_id``/``income_scenario_label`` so callers can group by
    scenario and see whether the winning strategy changes (DP#29).
    """
    from scenario_discovery import discover_anchors
    income_scenarios = discover_anchors(cfg)['income']
    if ltv_max is None:
        ltv_max = cfg.get('property', {}).get('ltv_max', 0.80)

    # Each income scenario is an INDEPENDENT run_optimization fold; build the
    # scenario list, run them across cores (perf), and collect IN INPUT ORDER
    # so the tagging below and the final sort are byte-identical to serial.
    scenarios = []
    for inc in income_scenarios:
        cfg_variant = _apply_income_scenario(cfg, inc)
        overlay = _scenario_overlay(cfg_variant, ltv_max, label=f"LTV {ltv_max:.0%}")
        scenarios.append((inc, overlay, {'kwargs': dict(
            cfg=cfg_variant, input_path=input_path, objective=objective,
            overlay=overlay)}))

    all_results: List[Dict] = []
    for (inc, overlay, _payload), results in zip(
            scenarios, _map_scenarios([s[2] for s in scenarios])):
        for r in results:
            r['income_scenario_id'] = inc['id']
            r['income_scenario_label'] = inc['label']
            r['income_primary'] = inc.get('primary_income')
            r['income_spouse'] = inc.get('spouse_income')
            # Issue #789: tag each result with the cash_out DOLLARS and LTV
            # of the scenario that produced it (the overlay applied at
            # ltv_max), so every report consumer (category_bests,
            # optimal_refi_level, the execution plan, the cash-flow
            # re-simulation) labels each entry from the SAME row whose
            # net_benefit/year_by_year it reports -- one source of truth
            # (DP#9). Pre-#789 the rows carried neither key, so
            # _category_bests read r.get('cash_out', 0) -> 0 for every row
            # and labelled the max-refinance winner "No Refinance".
            r['cash_out'] = overlay.cash_out
            r['ltv'] = ltv_max
        all_results.extend(results)

    all_results.sort(key=lambda r: ranking_key(r, r.get('net_benefit', 0)), reverse=True)
    return all_results


def run_runway_sweep(cfg: Dict, shock_dates: List[_date],
                     input_path: str = "input.json",
                     objective: ObjectiveFunction = None,
                     ltv_max: float = None) -> List[RunwayCurvePoint]:
    """Sweep the shock DATE (issue #758, hard part #5): re-run the household's
    ONE authored income-shock scenario with its dated segments shifted to
    begin on each date in ``shock_dates``, and return the runway curve.

    The same household has different runway for a shock today vs in 12
    months -- assets have grown, debts amortized, an obligation may have
    ended. Rather than ask the household to hand-author one
    ``decisions.income[]`` entry per shifted date, this re-uses the single
    authored shock shape and shifts its ``from``/``to`` dates by the same
    delta (``runway.shift_income_scenario_dates``, DP#9: one shock shape,
    swept by data). DP#18: each variant is the base config + a date-shifted
    income overlay, never a rebuild from scratch.

    For each shock date, the WINNING strategy's runway is reported (the
    strategy the household would actually run). Returns one
    :class:`RunwayCurvePoint` per shock date, in the order supplied.

    Returns ``[]`` when the household authored no dated shock scenario at
    all (no ``decisions.income[]`` entry carries any segments) -- there is
    nothing to sweep, and the caller says so rather than inventing a shock.
    """
    from scenario_discovery import discover_anchors
    income_scenarios = discover_anchors(cfg)['income']
    # The shock scenario is the first income anchor that actually overrides
    # anyone's income with dated segments. The auto-discovered baseline
    # carries empty segment lists (no override), so it is skipped.
    shock = None
    for s in income_scenarios:
        if s['primary_segments'] or s['spouse_segments']:
            shock = s
            break
    if shock is None:
        return []

    all_segs = list(shock['primary_segments']) + list(shock['spouse_segments'])
    # Explicit absence test (DP#32): a segment always carries 'from'
    # (#674's _require); min over a non-empty list.
    original = min(_date.fromisoformat(seg['from']) for seg in all_segs)
    if ltv_max is None:
        ltv_max = cfg.get('property', {}).get('ltv_max', 0.80)

    points: List[RunwayCurvePoint] = []
    for new_date in shock_dates:
        shifted = dict(shock)
        shifted['primary_segments'] = (
            shift_income_scenario_dates(shock['primary_segments'], original, new_date)
            if shock['primary_segments'] else [])
        shifted['spouse_segments'] = (
            shift_income_scenario_dates(shock['spouse_segments'], original, new_date)
            if shock['spouse_segments'] else [])
        cfg_variant = _apply_income_scenario(cfg, shifted)
        overlay = _scenario_overlay(cfg_variant, ltv_max, label=f"LTV {ltv_max:.0%}")
        results = run_optimization(cfg_variant, input_path, objective=objective,
                                    overlay=overlay)
        # The winner's runway (the strategy the household would actually run).
        best = max(results, key=lambda r: r.get('net_benefit', 0))
        rw = best.get('runway', _absent_runway())
        bracket = rw.get('runway_months_bracket')
        if bracket is None:
            bracket = (None, None)
        points.append(RunwayCurvePoint(
            shock_date=new_date,
            runway_months=rw.get('runway_months'),
            runway_months_bracket=tuple(bracket) if rw.get('engaged') else (None, None),
            stress_begins_months=rw.get('stress_begins_months'),
            survives_horizon_months=rw.get('survives_horizon_months'),
            engaged=rw.get('engaged', False),
            unengaged_reason=rw.get('unengaged_reason'),
        ))
    return points


def _absent_runway() -> Dict:
    """The un-engaged runway dict for a synthetic/older ranking row that
    carries no ``runway`` (issue #758). Explicit absence (DP#32): a row with
    no runway is reported as un-engaged and named, never a falsy-coerced
    '0 months' that would flatter."""
    return RunwayResult(engaged=False, unengaged_reason=(
        "no runway summary on this row (older/synthetic result) -- runway "
        "could not be evaluated. This is NOT a finding of safety.")).to_dict()


def winners_by_income_scenario(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the income-scenario report (issue #665): for each
    income scenario present in ``results`` (in first-seen/declaration order),
    find the winning strategy and record whether it differs from the FIRST
    scenario's winner (the household's base/current-income case).

    Split out from ``_print_income_scenario_report`` so "does the
    recommendation change across income scenarios" is a directly testable
    fact, not something only observable by parsing printed text.

    Issue #679: each row also carries the winner's ``solvency`` summary and a
    ``ruined`` flag. This is the whole point of the feature -- a scenario the
    household CANNOT SURVIVE must not be representable in this list as merely
    a smaller number. ``ruined`` being a first-class key here (not a
    formatting decision inside the printer) is what makes
    "a ruined scenario never reports an unqualified terminal net benefit" a
    testable assertion.

    Returns one dict per scenario:
        {id, label, strategy, deduct_later, net_benefit, changed_from_base,
         ruined, solvency}
    ``changed_from_base`` is always False for the first scenario itself.
    """
    scenario_ids = list(dict.fromkeys(r['income_scenario_id'] for r in results))
    winners = []
    base_strategy = None
    for sid in scenario_ids:
        rows = [r for r in results if r['income_scenario_id'] == sid]
        best = max(rows, key=lambda r: r.get('net_benefit', 0))
        strategy = best.get('strategy', '?')
        if base_strategy is None:
            base_strategy = strategy
        # DP#32: an explicit default, never `or {}` -- a row that carries no
        # solvency summary at all (an older/synthetic result) is ABSENT, and
        # absence must not be silently laundered into "solvent".
        solvency = best.get('solvency', {})
        winners.append({
            'id': sid,
            'label': rows[0]['income_scenario_label'],
            'strategy': strategy,
            'deduct_later': best.get('deduct_later', False),
            'net_benefit': best.get('net_benefit', 0),
            'changed_from_base': strategy != base_strategy,
            'ruined': bool(solvency.get('ruined', False)),
            'solvency': solvency,
            # Issue #707: decumulation shortfall beside the solvency verdict.
            # Explicit absence test (DP#32): a row carrying no summary is
            # given an all-False one, never via `.get(k) or DEFAULT`.
            'drawdown_shortfall': (
                best['drawdown_shortfall'] if 'drawdown_shortfall' in best
                else summarize_drawdown_shortfall([])),
            'exhausted': bool(
                (best.get('drawdown_shortfall')
                 if best.get('drawdown_shortfall') is not None else {})
                .get('exhausted', False)),
            # Issue #758: runway (months-to-ruin) beside the solvency verdict.
            # Explicit absence test (DP#32): a synthetic row carrying no
            # runway is given an un-engaged one, never a falsy-coerced number.
            'runway': best['runway'] if 'runway' in best else _absent_runway(),
        })
    return winners


def _print_income_scenario_report(results: List[Dict]) -> None:
    """Print the winning strategy under EACH declared income scenario, and
    flag explicitly whether the recommendation changes vs. the first
    (current-income) scenario -- issue #665: "a strategy that is optimal at
    full income and ruinous on EI is the entire point of the feature," and
    the tool must say so, not just rank silently.

    Only prints when the contract declares MORE than one income scenario --
    a household that never authored decisions.income[] gets the single
    auto-discovered "current income" scenario and this section is skipped
    (nothing to compare).

    Issue #679: a RUINED scenario's terminal figure is NOT printed as a
    dollar amount here. It is replaced by ``RUIN (year N)``, because the
    entire defect this fixes is that the tool answered "what happens if I
    lose my job?" with a large, reassuring number that is only reachable on
    the assumption the job loss did not destroy the household. A household
    reading `$4.4M` concludes job loss is survivable and merely expensive.
    The ledger figure is still reported -- immediately below, explicitly
    labelled NOT ACHIEVABLE -- because suppressing it entirely would hide
    what the engine computed; the requirement is that it can never be
    mistaken for an achievable outcome (DP#32: fail loudly).
    """
    winners = winners_by_income_scenario(results)
    if len(winners) <= 1:
        return

    print(f"\n{'=' * 120}")
    print(f"  ⚖️  RECOMMENDATION BY INCOME SCENARIO (decisions.income[] -- issue #665)")
    print(f"{'=' * 120}")
    print(f"\n  {'Scenario':<28} {'Winning strategy':<40} {'Net Benefit':>14}")
    print(f"  {'-' * 90}")

    base_label = winners[0]['label']
    base_strategy = winners[0]['strategy']
    for w in winners:
        strategy_name = w['strategy'] + (' 📋' if w['deduct_later'] else '')
        if w['ruined']:
            ruin_year = w['solvency'].get('first_ruin_year')
            verdict = f"RUIN (yr {ruin_year})" if ruin_year else "RUIN"
            print(f"  {w['label']:<28} {strategy_name:<40} {verdict:>14}")
            print(f"    ⛔ INSOLVENT: this household cannot fund its own obligations under "
                  f"'{w['label']}'.")
            print(f"       The ledger figure (${w['net_benefit']:,.0f}) is NOT ACHIEVABLE -- it "
                  f"assumes the shortfall never happened.")
            print(f"       See the SOLVENCY section below for the runway, the year it runs out, "
                  f"and what it was forced to sell.")
        elif not w['solvency'].get('engaged'):
            # Issue #733: a scenario whose cash-flow identity was never
            # checked (no `household_budget.annual_living_costs` declared,
            # the normal case for every existing contract, #679) must not
            # render here as an ordinary, achievable figure -- that IS the
            # defect #679 exists to kill, just reached through the row that
            # falls back to the bare number instead of through 'ruined'.
            # Marked INLINE, in the cell itself (not only 74 lines below in
            # the SOLVENCY footer, where a household reading top-to-bottom
            # never reaches it before drawing a conclusion).
            verdict = f"${w['net_benefit']:,.0f} (UNCHECKED)"
            print(f"  {w['label']:<28} {strategy_name:<40} {verdict:>22}")
        else:
            print(f"  {w['label']:<28} {strategy_name:<40} ${w['net_benefit']:>12,.0f}")
        if w['changed_from_base']:
            print(f"    ⚠ Recommendation CHANGES under '{w['label']}': "
                  f"{w['strategy']} (vs. '{base_label}' recommends {base_strategy})")

    print()
    _print_solvency_report(winners)
    # Issue #758: the months-to-ruin metric, right beside the solvency verdict.
    _print_runway_report(winners)


def _apply_structure_scenario(cfg: Dict, structure: Dict) -> Dict:
    """DP#18: overlay ONE candidate mortgage structure onto ``cfg`` --
    returns a MODIFIED COPY, never replaces the base outright (issue #687).

    ``structure`` is one entry of ``scenario_discovery.discover_anchors
    (cfg)['mortgage_structure']`` -- delegates the actual charge-split math
    and both OSFI B-20 refusal checks to ``property_structure.
    apply_structure_overlay`` (DP#9: one mechanism, not one per caller).
    """
    from property_structure import apply_structure_overlay
    cfg_variant = deepcopy(cfg)
    cfg_variant['property'] = apply_structure_overlay(cfg_variant.get('property', {}), structure)
    return cfg_variant


def _apply_sourcing_scenario(cfg: Dict, structure: Dict) -> Dict:
    """DP#18: overlay ONE candidate structure onto a config whose refinance
    overlay has ALREADY been applied -- returns a MODIFIED COPY (#845/#849).

    The cash-out counterpart of ``_apply_structure_scenario``: delegates to
    ``property_structure.apply_sourcing_overlay``, which splits the surplus the
    refinance just advanced between an amortizing advance and a revolving
    draw at a FIXED registered charge (DP#9: the math lives in ONE place, next
    to ``apply_structure_overlay``, not here).
    """
    from property_structure import apply_sourcing_overlay
    cfg_variant = deepcopy(cfg)
    cfg_variant['property'] = apply_sourcing_overlay(cfg_variant.get('property', {}), structure)
    return cfg_variant


def structure_refinance_bases(cfg: Dict) -> List[Dict]:
    """The refinance option(s) the STRUCTURE ranking is scored at (#845).

    #845's defect: ``run_mortgage_structure_exploration`` scored every
    structure at the household's CURRENT charge, silently ignoring
    ``decisions.mortgage.refinance_options`` -- so the irreversible,
    notary-day structural choice was ranked at a leverage the report does not
    recommend, and a structure with no way to source the surplus at that
    leverage was partly measured on "cannot execute this plan" rather than on
    "worse at equal leverage".

    So: a household that DECLARED refinance options gets its structures scored
    at EACH of them (``declared_refinance_candidates``, the same one mapping the
    LTV table uses -- DP#9, the two surfaces cannot disagree about what a
    declared option means). A household that declared none keeps EXACTLY
    today's single basis -- its current charge, cash-out $0, no overlay
    applied at all (DP#13: a default is a fallback for absent input, never a
    way to coerce a supplied one; and this exploration must not sprout a
    7-rung generic ladder nobody asked for).

    Issue #853 note: the LTV EXPLORATION table (``refinance_candidates``) unions
    the declared options OVER the full ladder and marks them ★ in situ (DP#33).
    This structure cross deliberately does NOT -- it scores structures at the
    DECLARED options only. Each cell here is a full simulation, and the notary-
    day structural choice is compared "A vs B at the leverage you would actually
    pick" (#849), not across a ladder that would quadruple an already-expensive
    cross. Both surfaces still resolve a declared option through the same
    ``declared_refinance_candidates`` mapping, so DP#9 holds: they agree on what
    each declared option MEANS, they differ only in breadth.
    """
    declared = cfg.get('scenarios', {}).get('refinance', [])
    if declared:
        return declared_refinance_candidates(cfg)

    prop = cfg.get('property', {})
    house_value = prop.get('house_value', 0)
    mortgage_balance = prop.get('mortgage_balance', 0)
    return [{
        'id': 'current_charge',
        'label': 'Current charge (no refinance option declared)',
        'cash_out': 0.0,
        'ltv': (mortgage_balance / house_value) if house_value > 0 else 0.0,
        'source': 'current',
    }]


def structure_refinance_cells(cfg: Dict) -> List[Dict]:
    """The (refinance option x structure) cross this ranking scores (#845/#849).

    One cell per pair, in declaration order, each:
        {basis, structure, cfg, refusal}

    ``cfg`` is the composed scenario config -- the refinance overlay applied
    FIRST (the charge grows to that option's cash-out), THEN the structure
    split -- or ``None`` when the pair was REFUSED, in which case ``refusal``
    says why IN WORDS (DP#32/#681: a cell the engine would not simulate is
    named and explained, never silently dropped from a ranking).

    Issue #1075: a structure that declares ``tranches`` (the 3-tranche
    readvanceable form) expands into ONE cell PER SWEEP POINT -- each a
    concrete ``{house, investment, line}`` split of the basis's charge,
    enumerated by ``_tranche_sweep_points`` -- so the optimizer can GENERATE
    the tranche amounts, not merely rank fixed ones. All points share the
    structure's ``id``, so the existing per-structure ranking groups them and
    the winning row carries its own ``tranche_amounts``.

    Pure: composes configs, runs no simulation. ``run_mortgage_structure_
    exploration`` scores these cells and ``_print_structure_report`` reports
    the refusals from the SAME list, so what was scored and what was refused
    cannot disagree (DP#9).
    """
    from scenario_discovery import discover_anchors
    anchors = discover_anchors(cfg)
    cells: List[Dict] = []
    for basis in structure_refinance_bases(cfg):
        for structure in anchors['mortgage_structure']:
            cells.extend(_compose_structure_cell(cfg, basis, structure))
    return cells


def _tranche_sweep_points(cfg: Dict, structure: Dict) -> List[Dict]:
    """Issue #1075: enumerate the concrete 3-tranche splits the optimizer
    sweeps for ONE tranches-declared structure at ONE refinance basis.

    The split is of the basis's POST-REFINANCE charge, and BOTH axes move:

      - the HOUSE amount is swept from its floor up to the charge: the
        floor is the house tranche's declared ``min_house_floor``, defaulting
        to 60% of the charge (``property_structure._tranche_house_floor``),
        and the steps are fixed 25k increments (coarsened to 50k/100k while
        the house x split product would exceed ``_TRANCHE_SWEEP_CELL_CAP``
        cells -- the optimizer stays fast, and a range that cannot fit
        refuses loudly instead of silently dropping points, DP#32). The
        charge and the cash-back threshold are always ON the grid, so the
        incentive-boundary point is evaluated. The house tranche's
        ``min_amount`` is NOT the floor anymore: it is the CASH-BACK
        THRESHOLD, and putting the house mortgage below it -- FORGOING the
        incentive -- is exactly the trade-off this sweep prices;
      - the SURPLUS (charge - house) is split between the deductible
        investment tranche and the line at 10% steps (0..100%, 11 points --
        the finer grid replacing the old 5-point 25% ladder, so a 10% step
        is reachable).

    Each point's amounts SUM to the charge by construction (a partition,
    DP#18); the overlay's own sum check is the backstop. Each point also
    carries the cash-back facts it was scored at -- ``cash_back_amount`` /
    ``cash_back_threshold`` (read off the declared origination cash-back
    flow, when one is CONDITIONAL on the house amount; None otherwise) and
    ``cash_back_credited`` (house >= the threshold) -- the cell composition
    withholds the inflow when the credit is forgone, and the report states
    the verdict beside the winning split (DP#9: the printed verdict is the
    condition the printed net benefit was scored under).

    Returns one dict per point, each a copy of ``structure`` carrying its
    concrete ``tranche_amounts`` and the derived ``revolving_share`` (the
    line's share of the charge -- the #687 tag other machinery reads), or an
    empty list when the basis's charge leaves no feasible split (the sweep
    FLOOR alone exceeds the charge) -- the caller records the refusal.

    Raises:
        ValueError: the structure's ``tranches`` spec is invalid
            (``property_structure._validate_tranche_spec``: unknown kind,
            overlapping kinds, ``min_amount``/``min_house_floor`` on a
            non-house tranche, ``deductible`` on a non-investment tranche,
            ``rate_type`` without a rate) -- validated HERE, before any
            point is enumerated, so an invalid template form is refused
            ONCE, as one named cell, never per-sweep-point (DP#32, issue
            #1075).
        ChargeLimitExceededError: the house sweep floor exceeds the charge,
            or the house range cannot fit inside ``_TRANCHE_SWEEP_CELL_CAP``
            cells even at the coarsest step -- refused loudly, naming the
            remedy (a declared ``min_house_floor`` narrowing the range),
            never silently dropping sweep points (DP#32).
    """
    from charge_limits import _CHARGE_TOLERANCE
    from property_structure import (
        _structure_label,
        _tranche_house_floor,
        _validate_tranche_spec,
    )
    prop = cfg.get('property', {})
    charge = prop.get('mortgage_balance', 0.0) + prop.get('margin_available', 0.0)
    by_kind = _validate_tranche_spec(structure)
    floor = _tranche_house_floor(by_kind, charge)
    if floor > charge + _CHARGE_TOLERANCE:
        return []
    label = _structure_label(structure)
    house_tranche = by_kind.get('house')
    # The sweep's cash-back facts (issue #1075, optimizer half): the
    # declared origination inflow's amount and condition, when the flow is
    # CONDITIONAL (carries ``min_house_amount``). Presence-based (DP#32):
    # no conditional cash-back -> both stay None -> every point carries
    # ``cash_back_credited`` None (nothing to credit, forgo, or gate) and
    # the report prints no note -- byte-identical to pre-#1075.
    cash_back_amount = None
    cash_back_threshold = None
    if house_tranche is None:
        # A structure with no house tranche has no house mortgage to sweep
        # -- house pinned at 0, the surplus split between investment and
        # line, exactly as before the house dimension existed.
        house_amounts = [0.0]
        anchor_threshold = None
    else:
        # The cash-back threshold the grid anchors on: the strictest of the
        # structure's declared ``min_amount`` and the declared origination
        # cash-back's ``min_house_amount`` (carried on the flow -- the
        # sweep's credit condition), when either lies inside the swept range.
        anchor_threshold = house_tranche.get('min_amount')
        for cf in cfg.get('cash_flows', []):
            condition = cf.get('min_house_amount')
            if condition is not None:
                cash_back_amount = cf['amount']
                cash_back_threshold = condition
                if anchor_threshold is None or condition > anchor_threshold:
                    anchor_threshold = condition
        house_amounts = _tranche_house_amounts(
            charge, floor, anchor_threshold, label)
    points = []
    for house in house_amounts:
        surplus = charge - house
        for fraction in _TRANCHE_SPLIT_FRACTIONS:
            investment = surplus * fraction
            line = surplus - investment
            point = dict(structure)
            point['tranche_amounts'] = {
                'house': house, 'investment': investment, 'line': line}
            point['revolving_share'] = (line / charge) if charge > 0 else 0.0
            # DP#32: presence-based, never a fabricated boolean -- a sweep
            # with no conditional cash-back carries None (nothing to credit
            # or forgo, nothing to gate). ``cash_back_amount`` and
            # ``cash_back_threshold`` are set TOGETHER (one flow carries
            # both), so a declared condition always has its threshold.
            if cash_back_amount is None:
                point['cash_back_credited'] = None
            else:
                point['cash_back_credited'] = (
                    house >= cash_back_threshold - 1e-6)
            point['cash_back_amount'] = cash_back_amount
            point['cash_back_threshold'] = cash_back_threshold
            points.append(point)
    return points


def _tranche_house_amounts(charge: float, floor: float,
                           threshold: Optional[float], label: str) -> List[float]:
    """The house amounts ONE 3-tranche sweep enumerates (issue #1075,
    optimizer half): from ``floor`` up to the ``charge`` in fixed
    ``_TRANCHE_HOUSE_STEPS`` increments, with the charge always included as
    the top and the cash-back ``threshold`` (when it lies inside the range)
    included as an anchor -- the boundary where the incentive flips is
    exactly what this sweep exists to price, so it must be evaluated, not
    merely straddled. The step coarsens (25k -> 50k -> 100k) while the
    house count x split count would exceed ``_TRANCHE_SWEEP_CELL_CAP``; a
    range that still cannot fit refuses loudly (DP#32 -- never silently
    drop sweep points, and never hand the user a surprise 30-minute sweep).
    """
    from charge_limits import _CHARGE_TOLERANCE
    split_count = len(_TRANCHE_SPLIT_FRACTIONS)
    for step in _TRANCHE_HOUSE_STEPS:
        amounts = {floor}
        h = floor
        while h <= charge - _CHARGE_TOLERANCE:
            amounts.add(round(h))
            h += step
        if threshold is not None and floor - _CHARGE_TOLERANCE <= threshold <= charge:
            amounts.add(threshold)
        amounts.add(charge)
        amounts = sorted(amounts)
        if len(amounts) * split_count <= _TRANCHE_SWEEP_CELL_CAP:
            return amounts
    raise ChargeLimitExceededError(
        f"structure {label!r}: its house sweep range of "
        f"${floor:,.0f}..${charge:,.0f} cannot fit inside the "
        f"{_TRANCHE_SWEEP_CELL_CAP}-cell sweep cap even at the coarsest "
        f"step -- declare a min_house_floor nearer the charge (or a smaller "
        f"charge) to narrow the range, or the sweep would silently drop "
        f"candidate splits (issue #1075)."
    )


# Issue #1075 (optimizer half): the sweep's fixed axes -- the house-amount
# steps (coarsened while the cell product exceeds the cap) and the
# investment/line split ladder (10% steps, the finer grid replacing the old
# 5-point 25% ladder), and the per-structure cell cap that keeps the
# optimizer fast (house grid x split grid).
_TRANCHE_HOUSE_STEPS = (25_000, 50_000, 100_000)
_TRANCHE_SPLIT_FRACTIONS = tuple(i / 10 for i in range(11))
_TRANCHE_SWEEP_CELL_CAP = 150


def _compose_structure_cell(cfg: Dict, basis: Dict, structure: Dict) -> List[Dict]:
    """Compose the (refinance option, structure) cell(s) -- refinance FIRST,
    then the structure split (#845). See ``structure_refinance_cells``.

    Returns a LIST (the #687 share form composes ONE cell; a tranches-
    declared structure composes one cell PER sweep point).

    A cash-out of 0 takes NO overlay at all and keeps ``apply_structure_
    overlay``'s existing split of the current charge, byte-for-byte as before
    this fix: at cash-out $0 there is no surplus to source, so the sourcing
    question this PR adds does not arise and must not perturb the answer.

    Issue #1075: a sweep point carrying ``tranche_amounts`` is applied by the
    tranche machinery (``property_structure._apply_tranched_structure`` via
    ``apply_structure_overlay``) against the basis's post-refinance charge --
    the amounts ARE the drawn/room position, so the share-form sourcing
    re-split does not apply (see ``apply_sourcing_overlay``'s routing
    comment). The basis is still applied first (for a cash-out basis, the
    refinance grows the charge and books the surplus the sweep's investment
    tranche represents).
    """
    cells = []
    if structure.get('tranches') is not None and structure.get('tranche_amounts') is None:
        # The template form: expand into the sweep points for this basis.
        try:
            if basis['cash_out'] > 0:
                cfg_basis = build_overlay_config(cfg, _candidate_overlay(cfg, basis))
            else:
                cfg_basis = cfg
            points = _tranche_sweep_points(cfg_basis, structure)
        except (ChargeLimitExceededError, MissingRefinanceAmortizationError,
                ValueError) as exc:
            # ValueError LAST: ``ChargeLimitExceededError``/``MissingRefinance
            # AmortizationError`` subclass it, so the typed refusals win, and
            # the catch-all turns ``_validate_tranche_spec``'s invalid-spec
            # ValueError (issue #1075: unknown/overlapping kinds, min_amount on
            # a non-house tranche, deductible on a non-investment tranche,
            # rate_type without rate, unpriced line) into the named refusal
            # cell, never a crash (DP#32).
            return [{'basis': basis, 'structure': structure, 'cfg': None,
                     'refusal': f"{type(exc).__name__}: {exc}"}]
        if not points:
            return [{'basis': basis, 'structure': structure, 'cfg': None,
                     'refusal': "ChargeLimitExceededError: the house tranche's "
                                "sweep floor (the declared min_house_floor, "
                                "defaulting to 60% of the charge) exceeds this "
                                "basis's registered charge -- no 3-tranche "
                                "split exists to sweep (issue #1075)."}]
        for point in points:
            cells.extend(_compose_structure_cell(cfg, basis, point))
        return cells

    cell = {'basis': basis, 'structure': structure, 'cfg': None, 'refusal': None}
    try:
        if structure.get('tranche_amounts') is not None:
            # Issue #1075 sweep point: apply the basis (the refinance, so the
            # charge the amounts partition is the basis's charge), then the
            # tranche amounts directly.
            if basis['cash_out'] > 0:
                cfg_basis = build_overlay_config(cfg, _candidate_overlay(cfg, basis))
            else:
                cfg_basis = cfg
            cell['cfg'] = _apply_structure_scenario(cfg_basis, structure)
            # Issue #1075 (optimizer half): a CONDITIONAL origination
            # cash-back (the flow carries its ``min_house_amount`` -- see
            # input_contract.py) is withheld when this point's house tranche
            # is below the threshold. The sweep point's own
            # ``cash_back_credited`` flag -- computed by
            # ``_tranche_sweep_points`` from the SAME threshold -- is the
            # single source of truth, so the verdict the report prints and
            # the config the engine was scored at cannot disagree (DP#9). A
            # flow with no condition is never touched (byte-identical for
            # the unconditional and pre-#1075 paths); ``cell['cfg']`` is a
            # fresh deep copy, so stripping is local to this cell.
            if structure.get('cash_back_credited') is False:
                cell['cfg']['cash_flows'] = [
                    cf for cf in cell['cfg']['cash_flows']
                    if cf.get('min_house_amount') is None]
        elif basis['cash_out'] > 0:
            cfg_basis = build_overlay_config(cfg, _candidate_overlay(cfg, basis))
            cell['cfg'] = _apply_sourcing_scenario(cfg_basis, structure)
        else:
            cell['cfg'] = _apply_structure_scenario(cfg, structure)
    except (ChargeLimitExceededError, MissingRefinanceAmortizationError,
            ValueError) as exc:
        # ValueError LAST: the typed refusals first (they subclass ValueError),
        # then the catch-all for ``_validate_tranche_spec``'s invalid-spec
        # ValueError -- a tranches-declared structure the engine would not
        # simulate is NAMED and explained in the refusal cell, never allowed
        # to crash the ranking (DP#32, issue #1075).
        cell['refusal'] = f"{type(exc).__name__}: {exc}"
    return [cell]


def run_mortgage_structure_exploration(cfg: Dict, input_path: str = "input.json",
                                        objective: ObjectiveFunction = None,
                                        cells: List[Dict] = None) -> List[Dict]:
    """Run the full optimizer once per (refinance option) x (mortgage
    structure) x (income scenario) cell -- issues #687/#845/#849.

    ``decisions.mortgage.structure_options[]`` exists so a household facing
    a genuinely irreversible structural choice at a refinance/renewal --
    the whole charge as a plain amortizing mortgage vs. the same amount
    flagged readvanceable vs. a smaller mortgage plus an undrawn revolving
    line -- can ask the tool to rank them, rather than hand-authoring one
    contract document per structure and diffing the outputs (the exact
    workaround this issue exists to end).

    Crossed with EVERY declared income scenario (``decisions.income[]``,
    #665) so the household can see whether the answer *changes* under job
    loss -- a structure that is optimal at full income and ruinous on EI is
    precisely what this composition must surface (DP#5: anchor decisions,
    overlay sensitivities; DP#22: the optimizer ranks, it doesn't choose).

    Issues #845/#849 -- the cross this used to refuse. Every structure used
    to be scored at the household's CURRENT charge, silently ignoring
    ``decisions.mortgage.refinance_options``. So the ranking of an
    IRREVERSIBLE, notary-day choice was computed at a leverage the report does
    not recommend, and structure A -- which at cash-out $0 has neither a line
    NOR an advance to source the surplus from -- was partly measured on
    "cannot execute this plan" rather than on "worse at equal leverage".

    Now each declared refinance option is applied FIRST (``build_overlay_
    config``: the charge grows to that option's cash-out), THEN the structure
    split (``apply_sourcing_overlay``), THEN the income overlay -- and the
    PRODUCT is ranked, every row tagged with the option it was scored at. A
    household that declares no refinance option keeps exactly the old single
    basis (``structure_refinance_bases``, DP#13).

    That composition is what makes #849's question -- "take the surplus as a
    cheaper amortizing ADVANCE, or draw it from the dearer but interest-only
    LINE?" -- rankable at last. At a FIXED registered charge, the tap is
    ``revolving_share``, not ``cash_out``: ``cash_out`` can say how much, never
    from where. ``revolving_share = 0`` sources the whole surplus as an
    advance; ``revolving_share >= cash_out/charge`` sources it all from the
    line; in between splits it. See ``apply_sourcing_overlay``.

    The old rationale for scoring at cash-out $0 -- "this compares SPLITS of
    the same charge, and sweeping LTV per structure would let every structure
    top itself back up and hide the difference" -- survives INSIDE each
    refinance option: the charge is fixed by the option, identically for every
    structure, and only the split moves. What it never justified was pinning
    that fixed charge to the CURRENT one while the report recommended another.

    Issue #735 FIXED the limitation this docstring used to record here:
    ``run_optimization`` no longer draws a declared revolving line in full
    at year 0 by default. Per structure, the draw-fraction candidates
    (``scenario_discovery._discover_draw_fraction_options``, gated on that
    STRUCTURE's own carved-out ``margin_available`` -- ``[0.0]`` alone for
    a structure with no revolving component at all, e.g. structure A) are
    swept, so a structure that carves out a line is ranked across the REAL
    trade-off: "keep the room undrawn as standby liquidity" all the way to
    "draw and invest the whole thing" -- not silently pinned to the latter.

    Returns the UNION of every (refinance option, structure, income scenario,
    draw fraction) combination's ranked results, each result tagged with
    ``structure_id``/``structure_label``/``structure_revolving_share``/
    ``structure_readvanceable``, ``income_scenario_id``/
    ``income_scenario_label`` (DP#8: reuses the same income-scenario
    tagging shape ``run_income_scenario_exploration`` uses), AND
    ``structure_basis_id``/``structure_basis_label``/
    ``structure_basis_source``/``structure_basis_cash_out``/
    ``structure_basis_ltv`` (#845: the basis travels ON the row); the
    draw-fraction itself is on each row's own ``draw_fraction`` key
    (``run_optimization``'s own per-result tag).

    Args:
        cells: the composed cross (``structure_refinance_cells``). Defaults to
            computing it -- ``main`` passes its own so the report's refusal
            notice is read off the SAME cells that were scored (DP#9).
    """
    from scenario_discovery import discover_anchors, _discover_draw_fraction_options
    income_scenarios = discover_anchors(cfg)['income']
    if cells is None:
        cells = structure_refinance_cells(cfg)

    # Each (cell x income scenario) is an INDEPENDENT run_optimization fold;
    # enumerate them exactly as the serial nested loop did, run them across
    # cores (perf), and collect IN INPUT ORDER so the per-row tagging below and
    # the final sort are byte-identical to serial.
    scenarios = []
    for cell in cells:
        # DP#32/#681: a refused cell is NOT scored and NOT silently dropped --
        # _print_structure_report names it and prints its reason, read off this
        # same list (DP#9).
        if cell['cfg'] is None:
            continue
        basis, structure = cell['basis'], cell['structure']
        cfg_structure = cell['cfg']
        if basis['cash_out'] > 0:
            # Issue #845/#849: on a CASH-OUT basis the draw is IMPLIED by the
            # sourcing split -- apply_sourcing_overlay already booked
            # min(cash_out, revolving) as the line's opening balance, and
            # optimizer.py invests `margin_available * draw_fraction +
            # cash_out`. Sweeping the fraction here would invest the same
            # borrowed dollar twice (see apply_sourcing_overlay's docstring).
            # The "keep the line undrawn" question is answered by the
            # structure's OWN share instead: a line larger than the surplus
            # keeps its residual room undrawn, and the cash-out $0 basis below
            # still sweeps the fraction exactly as #735 built it.
            draw_fraction_options = [0.0]
        elif structure.get('tranches') is not None:
            # Issue #1075: a tranches-declared structure's line AMOUNT is
            # already the swept variable (each sweep point carries its own
            # line), so the #735 draw-fraction ladder would re-ask the drawn/
            # undrawn question a second time at a different granularity and
            # multiply every point by 4 for no new information. The drawn/
            # undrawn status of the line is decided by the sweep point (and,
            # on a cash-out basis, by the year-0 lump-sum sourcing) -- not by
            # the fraction ladder. Pinned to [0.0].
            draw_fraction_options = [0.0]
        else:
            # issue #735: THIS structure's own carved-out margin_available
            # decides whether the draw-fraction question is even askable here
            # -- structure A (no revolving component) sweeps nothing extra;
            # structure C (a standing line) does.
            draw_fraction_options = _discover_draw_fraction_options(cfg_structure)
        for inc in income_scenarios:
            cfg_variant = _apply_income_scenario(cfg_structure, inc)
            scenarios.append((basis, structure, inc, {'kwargs': dict(
                cfg=cfg_variant, input_path=input_path, objective=objective,
                draw_fraction_options=draw_fraction_options)}))

    all_results: List[Dict] = []
    for (basis, structure, inc, _payload), results in zip(
            scenarios, _map_scenarios([s[3] for s in scenarios])):
        for r in results:
            r['structure_id'] = structure['id']
            r['structure_label'] = structure['label']
            r['structure_revolving_share'] = structure.get('revolving_share')
            r['structure_readvanceable'] = structure.get('readvanceable')
            # Issue #1075: the sweep point this row was scored at, carried ON
            # the row -- so the winning row's OWN tranche amounts are what the
            # report prints (DP#9: the printed optimal split cannot disagree
            # with the row that produced it) -- and the point's cash-back
            # verdict (whether the conditional origination cash-back was
            # credited under this house amount), which the report states
            # beside the split.
            r['structure_tranche_amounts'] = structure.get('tranche_amounts')
            r['structure_cash_back_amount'] = structure.get('cash_back_amount')
            r['structure_cash_back_threshold'] = structure.get('cash_back_threshold')
            r['structure_cash_back_credited'] = structure.get('cash_back_credited')
            r['income_scenario_id'] = inc['id']
            r['income_scenario_label'] = inc['label']
            # Issue #845: the refinance option -- and the leverage -- this
            # row was scored at, carried ON the row whose net_benefit is
            # reported, so the printed basis is read off the same rows and
            # cannot disagree with them (DP#9, #848's shape).
            r['structure_basis_id'] = basis['id']
            r['structure_basis_label'] = basis['label']
            r['structure_basis_source'] = basis['source']
            r['structure_basis_cash_out'] = basis['cash_out']
            r['structure_basis_ltv'] = basis['ltv']
        all_results.extend(results)

    all_results.sort(key=lambda r: ranking_key(r, r.get('net_benefit', 0)), reverse=True)
    return all_results


def winners_by_structure_scenario(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the structure-ranking report (issue #687): the
    winning strategy for each (structure, income scenario) pair present in
    ``results``, so "do these structures rank differently, and does the
    ranking change under job loss" is a directly testable fact, not
    something only observable by parsing printed text (mirrors
    ``winners_by_income_scenario``'s split, DP#8).

    Returns one dict per (refinance option, structure, income scenario) triple
    (#845 -- two structures are only comparable at the SAME charge), in
    first-seen/declaration order:
        {structure_basis_id, structure_basis_label, structure_basis_cash_out,
         structure_basis_ltv, structure_id, structure_label,
         income_scenario_id, income_scenario_label, strategy, deduct_later,
         net_benefit, ruined, solvency}
    """
    # Issue #845: the refinance option is part of the key. Two structures are
    # only comparable at the SAME charge, so collapsing 'advance at cash-out $0'
    # and 'advance at cash-out $480,000' into one winner would silently pick the
    # better LEVERAGE and report it as the better STRUCTURE -- the exact
    # confusion #845 exists to end. A row that carries no basis tag is the
    # pre-#845 single basis (``structure_basis_id``), so a caller that never
    # crossed anything gets byte-identical grouping.
    pairs = list(dict.fromkeys(
        (structure_basis_id(r), r['structure_id'], r['income_scenario_id'])
        for r in results))
    winners = []
    for bid, sid, iid in pairs:
        rows = [r for r in results
                if structure_basis_id(r) == bid
                and r['structure_id'] == sid and r['income_scenario_id'] == iid]
        best = max(rows, key=lambda r: r.get('net_benefit', 0))
        # DP#32: an explicit default, never `or {}` -- see
        # winners_by_income_scenario's identical comment.
        solvency = best.get('solvency', {})
        winners.append({
            # Issue #845: the basis travels onto the winner too -- a winner
            # without the charge it won at is exactly what #845 filed.
            'structure_basis_id': bid,
            'structure_basis_label': rows[0].get('structure_basis_label'),
            'structure_basis_cash_out': rows[0].get('structure_basis_cash_out'),
            'structure_basis_ltv': rows[0].get('structure_basis_ltv'),
            'structure_id': sid,
            'structure_label': rows[0]['structure_label'],
            'income_scenario_id': iid,
            'income_scenario_label': rows[0]['income_scenario_label'],
            'strategy': best.get('strategy', '?'),
            'deduct_later': best.get('deduct_later', False),
            # issue #735: which draw fraction won for THIS structure -- the
            # question "keep this structure's line undrawn, or draw it?" is
            # answered per-structure, not assumed away.
            'draw_fraction': best.get('draw_fraction', 0.0),
            # issue #1075: the 3-tranche sweep point that won for THIS
            # structure -- the optimal {house, investment, line} split, read
            # off the row that produced it (None for a share-form structure)
            # -- and the cash-back verdict the split was scored under.
            'tranche_amounts': best.get('structure_tranche_amounts'),
            'cash_back_amount': best.get('structure_cash_back_amount'),
            'cash_back_threshold': best.get('structure_cash_back_threshold'),
            'cash_back_credited': best.get('structure_cash_back_credited'),
            'net_benefit': best.get('net_benefit', 0),
            'ruined': bool(solvency.get('ruined', False)),
            'solvency': solvency,
            # Issue #707: decumulation shortfall beside the solvency verdict.
            # Explicit absence test (DP#32): a row carrying no summary is
            # given an all-False one, never via `.get(k) or DEFAULT`.
            'drawdown_shortfall': (
                best['drawdown_shortfall'] if 'drawdown_shortfall' in best
                else summarize_drawdown_shortfall([])),
            'exhausted': bool(
                (best.get('drawdown_shortfall')
                 if best.get('drawdown_shortfall') is not None else {})
                .get('exhausted', False)),
            # Issue #758: runway (months-to-ruin) beside the solvency verdict,
            # so every mortgage-structure option in the ranking shows BOTH
            # numbers at once -- a structure that wins on terminal net worth
            # but halves your runway is not obviously the better structure.
            'runway': best['runway'] if 'runway' in best else _absent_runway(),
        })
    return winners


def structure_ranking_by_income_scenario(winners: List[Dict]) -> Dict[str, List[Dict]]:
    """Group ``winners_by_structure_scenario``'s rows by income scenario,
    each group sorted by ``net_benefit`` descending (issue #687) -- so "do
    the structures rank differently" and "does the ranking change under
    job loss" are both directly answerable from this, not just printable.

    Returns ``{income_scenario_id: [row, ...]}``, rows sorted best-first,
    in first-seen income-scenario order (dict insertion order, Python
    3.7+).

    Issue #845: give this the winners of ONE refinance option. Structures are
    only comparable at the SAME charge, and this groups by income scenario
    ALONE -- feeding it a whole cross would rank 'advance at cash-out $0'
    against 'advance at $480,000' in one table and report the better LEVERAGE
    as the better STRUCTURE. ``_print_structure_report`` splits by
    ``structure_basis_id`` before calling this, which is why it can.
    """
    by_income: Dict[str, List[Dict]] = {}
    for w in winners:
        by_income.setdefault(w['income_scenario_id'], []).append(w)
    for iid in by_income:
        by_income[iid].sort(key=lambda w: w.get('net_benefit', 0), reverse=True)
    return by_income


def structure_basis_id(row: Dict) -> str:
    """The refinance option ONE structure-ranking row was scored at (#845).

    DP#32: an explicit fallback for a row that predates the basis tags (the
    fabricated rows the pure-logic tests build, and any caller still passing
    the old shape) -- 'current_charge' is exactly what
    ``structure_refinance_bases`` calls the no-declaration basis, so the two
    producers agree on the name (DP#9).
    """
    basis_id = row.get('structure_basis_id')
    return 'current_charge' if basis_id is None else basis_id


def _print_structure_refusals(cells: Optional[List[Dict]]) -> None:
    """Name every (refinance option x structure) cell the engine REFUSED to
    score, and why (#845, DP#32/#681).

    A cell missing from the tables above is otherwise indistinguishable from
    one that was never asked for. #681's rule -- an infeasible scenario is
    recorded with the reason IN WORDS, never collapsed to silence -- applies to
    this new cross too.
    """
    if not cells:
        return
    refused = [c for c in cells if c['refusal'] is not None]
    if not refused:
        return
    print(f"\n  ⚠️  NOT SCORED — {len(refused)} (refinance option x structure) cell(s) the engine refused:")
    for c in refused:
        print(f"      • '{c['structure']['label']}' at '{c['basis']['label']}'")
        print(f"        {c['refusal']}")
    print(f"      These cells are ABSENT from the tables above — do not read their absence as a")
    print(f"      poor ranking (#845).")


def _print_structure_report(results: List[Dict], cells: Optional[List[Dict]] = None) -> None:
    """Issue #845: print ONE structure ranking PER refinance option the cross
    scored, each stating its own basis, then name every refused cell.

    Delegates each option's table to ``_print_structure_report_for_basis``
    below; a household that declared no refinance option has exactly one basis
    and sees exactly the report it saw before this fix.
    """
    for basis_id in dict.fromkeys(structure_basis_id(r) for r in results):
        _print_structure_report_for_basis(
            [r for r in results if structure_basis_id(r) == basis_id])
    _print_structure_refusals(cells)


def _print_structure_report_for_basis(results: List[Dict]) -> None:
    """Issue #687: print the mortgage-STRUCTURE ranking -- all-mortgage vs.
    readvanceable vs. mortgage+revolving-line -- PER declared income scenario
    (DP#5/DP#22: the optimizer ranks, the household chooses).

    Only prints when the contract actually declares
    ``decisions.mortgage.structure_options`` with more than one candidate
    (``scenario_discovery`` returns a single auto-discovered 'declared'
    identity option otherwise) -- a household that never asked this
    question sees nothing extra, same gating discipline as
    ``_print_income_scenario_report``.

    Issue #1075 exception: a single tranches-declared structure still prints
    -- its ranking has one row per income scenario, but the OPTIMAL
    3-TRANCHE SPLIT block below is the deliverable (the optimizer GENERATED
    the amounts), so a tranche sweep must not be silenced by the
    one-candidate gate.

    Issue #733's fix applies here too (DP#32): a row whose scenario was
    never solvency-checked is marked ``(UNCHECKED)`` inline, and a ruined
    row prints ``RUIN (yr N)``, never a bare achievable-looking figure --
    a NEW ranking surface must not reintroduce the defect #733 just closed
    on the income-scenario table.
    """
    winners = winners_by_structure_scenario(results)
    structure_ids = list(dict.fromkeys(w['structure_id'] for w in winners))
    has_tranche_rows = any(w.get('tranche_amounts') for w in winners)
    if len(structure_ids) <= 1 and not has_tranche_rows:
        return

    ranking = structure_ranking_by_income_scenario(winners)
    income_ids = list(dict.fromkeys(w['income_scenario_id'] for w in winners))

    print(f"\n{'=' * 120}")
    print(f"  🏗️  MORTGAGE STRUCTURE RANKING (decisions.mortgage.structure_options -- issue #687)")
    print(f"{'=' * 120}")
    _print_structure_deductibility_caveat()

    # Issue #845: state the leverage this ranking was computed at, BEFORE the
    # table -- the structural choice (line vs no line) is irreversible on notary
    # day, and a reader must not mistake this ranking's basis for the LTV
    # sweep's. The two tables answer different questions at different leverage.
    basis_ltvs = {r.get('structure_basis_ltv') for r in results
                  if r.get('structure_basis_ltv') is not None}
    # DP#32: explicit absence-testing. A cash-out of exactly 0 is a REAL basis
    # (the household's current charge, or a declared no-cash-out option), not
    # an unset one -- `or 0` would make the two indistinguishable.
    basis_cash_outs = {r.get('structure_basis_cash_out') for r in results
                       if r.get('structure_basis_cash_out') is not None}
    basis_cash_out = max(basis_cash_outs) if basis_cash_outs else 0.0
    basis_labels = [r.get('structure_basis_label') for r in results
                    if r.get('structure_basis_label') is not None]
    if basis_ltvs and basis_cash_out <= 0:
        basis_ltv = max(basis_ltvs)
        print(f"\n  📍 BASIS: computed at CASH-OUT $0 — your current leverage "
              f"(LTV {basis_ltv:.1%}). NO cash-out sweep.")
        if basis_labels:
            print(f"      Refinance option: '{basis_labels[0]}' "
                  f"(decisions.mortgage.refinance_options).")
        print(f"      These structures are ranked as SPLITS of the charge you already carry, so the")
        print(f"      comparison isolates structure from leverage. If the LTV EXPLORATION above")
        print(f"      recommends a different LTV, these structures were NOT ranked at that leverage")
        print(f"      — do not read the two tables as one plan (#845).")
    elif basis_ltvs:
        # Issue #845/#849: a REAL cash-out basis. The reader must not mistake
        # this for the LTV sweep's table (which ranks STRATEGIES across
        # leverage) -- this one ranks STRUCTURES at ONE fixed charge.
        basis_ltv = max(basis_ltvs)
        print(f"\n  ✅ BASIS: your declared refinance option "
              f"'{basis_labels[0] if basis_labels else '?'}' — "
              f"CASH-OUT ${basis_cash_out:,.0f} (LTV {basis_ltv:.1%}).")
        print(f"      Every structure below is scored at THAT charge, so they are comparable to each")
        print(f"      other AT the leverage this option takes — not at your current one (#845).")
        print(f"      This is NOT the LTV-sweep basis: that table ranks STRATEGIES across cash-out")
        print(f"      levels; this one ranks STRUCTURES at ONE fixed charge.")
        print(f"      ⚖️  ADVANCE vs LINE (#849): at this fixed charge, the structure's")
        print(f"          revolving_share IS the tap — 0% takes the whole ${basis_cash_out:,.0f} surplus as an")
        print(f"          amortizing mortgage advance; a share large enough to hold it draws the whole")
        print(f"          surplus from the revolving line instead; in between splits it, line first.")

    # DP#32 / model_fidelity (#585): issue #735 FIXED the approximation this
    # block used to warn about (a revolving line used to be drawn in full,
    # unconditionally, at year 0 -- `lump_sum = margin_available +
    # cash_out`). Now each structure that carves out a line is evaluated at
    # SEVERAL draw fractions (0%/25%/50%/100% of that structure's own
    # margin_available -- scenario_discovery._discover_draw_fraction_options)
    # and the winning row below already reflects the best one FOUND, so a
    # structure carrying an undrawn line is no longer scored as though it
    # had been spent. Still disclosed here (DP#32: the reader should not
    # have to infer this from a column header) -- the DRAWN FRACTION each
    # winning row actually assumed is what makes the row's real leverage
    # legible, not something to take on faith.
    # DP#32: explicit absence-testing, never `x or 0` -- a structure with a
    # declared share of exactly 0.0 is a REAL declaration (structure A), not
    # an unset one, and the two must stay distinguishable.
    shares = [r.get('structure_revolving_share') for r in results]
    # Issue #1075: a tranches-declared structure's drawn/undrawn question is
    # answered by ITS OWN line amount and the cash-out sourcing -- the #735
    # draw-fraction ladder is pinned to [0.0] for it (see
    # run_mortgage_structure_exploration), so the share-form disclosures
    # below would describe a sweep that did not happen (DP#32). Print the
    # tranche-specific disclosure instead.
    has_tranche_rows = any(r.get('structure_tranche_amounts') for r in results)
    if has_tranche_rows:
        if basis_cash_out > 0:
            print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED at this cash-out (#1075):")
            print(f"      Each tranche point's line is drawn by the cash-out sourcing: "
                  f"min(${basis_cash_out:,.0f} cash-out, its line amount) comes off the line, ")
            print(f"      the remainder of the surplus stays on the mortgage as the (deductible)"
                  f" investment tranche, and any residual line room stays UNDRAWN standby")
            print(f"      liquidity. The line is NOT also swept at #735 draw fractions.")
        else:
            print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED (#1075):")
            print(f"      The line AMOUNT is the swept variable -- each sweep point carries its own")
            print(f"      line, and the winning row's split is printed below. The #735 draw-")
            print(f"      fraction ladder is pinned to undrawn: at cash-out $0 the line is standby")
            print(f"      liquidity, and drawing it is a separate decision this ranking does not")
            print(f"      make for the tranched form.")
    elif any(s is not None and s > 0 for s in shares) and basis_cash_out > 0:
        # Issue #845/#849: on a cash-out basis the draw is NOT swept -- it is
        # IMPLIED by the sourcing split (run_mortgage_structure_exploration
        # pins draw_fraction to 0.0; apply_sourcing_overlay already booked
        # min(cash_out, revolving) as the line's opening balance). Printing
        # #735's "evaluated at several draw fractions" here would describe a
        # sweep that did not happen (DP#32).
        print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED at this cash-out (#845/#849):")
        print(f"      The line's draw is NOT swept here — it is IMPLIED by the sourcing split. Each")
        print(f"      structure draws min(cash-out, its revolving segment) of the "
              f"${basis_cash_out:,.0f} surplus")
        print(f"      from the line and takes the remainder as a mortgage advance; any room left over")
        print(f"      stays UNDRAWN standby liquidity. Total borrowed is identical across structures,")
        print(f"      so what the ranking below measures is the SOURCE, not the amount.")
    elif any(s is not None and s > 0 for s in shares):
        print(f"\n  ℹ️  HOW THE REVOLVING SEGMENT IS MODELLED (issue #735):")
        print(f"      A structure that carves out a line is evaluated at SEVERAL draw fractions "
              f"of that line")
        print(f"      (0%/25%/50%/100% of ITS OWN margin_available) -- the winning row below is "
              f"the best one")
        print(f"      found, and shows its own drawn fraction inline. A 0% row means the line "
              f"won UNDRAWN:")
        print(f"      standby liquidity, not leverage.")

    base_income_id = income_ids[0]
    base_winning_structure = ranking[base_income_id][0]['structure_id']
    base_winning_label = ranking[base_income_id][0]['structure_label']
    base_income_label = ranking[base_income_id][0]['income_scenario_label']

    # issue #735: which structure_ids actually carry a revolving segment at
    # all -- only those get a "(draw N%)" annotation; a line-free structure
    # (e.g. structure A) would otherwise show a meaningless "draw 0%" on
    # every one of its own rows.
    #
    # Issue #845: NOT on a cash-out basis. There the draw_fraction is pinned to
    # 0.0 because the draw is IMPLIED by the sourcing split -- so a literal
    # "(draw 0%)" would tell the reader the line won UNDRAWN, when
    # apply_sourcing_overlay in fact drew min(cash_out, revolving) of it. The
    # exact opposite of the truth, in the column meant to make the row's real
    # leverage legible. The disclosure block above says what happened instead.
    structures_with_a_line = set() if basis_cash_out > 0 else {
        r['structure_id'] for r in results
        # Issue #1075: a tranches-declared row's "(draw N%)" annotation would
        # be the #735 sweep's marker, but the fraction is pinned to [0.0] for
        # the tranched form -- its line status is carried by its own amounts,
        # not by a fraction it never swept.
        if (r.get('structure_revolving_share') not in (None, 0.0)
            or r.get('structure_readvanceable'))
        and not r.get('structure_tranche_amounts')
    }

    for iid in income_ids:
        rows = ranking[iid]
        label = rows[0]['income_scenario_label']
        print(f"\n  Under '{label}':")
        # Issue #758: runway sits BESIDE net benefit for every structure --
        # a structure that wins on terminal net worth but halves your runway
        # is not obviously the better structure, and the household must see
        # both numbers at once (the comparison that matters before a notary).
        print(f"  {'#':<3} {'Structure':<48} {'Strategy':<20} {'Net Benefit':>14} {'Runway':>22}")
        print(f"  {'-' * 110}")
        for i, w in enumerate(rows):
            strategy_name = w['strategy'] + (' 📋' if w['deduct_later'] else '')
            if w['structure_id'] in structures_with_a_line:
                strategy_name += f" (draw {w.get('draw_fraction', 0.0):.0%})"
            runway_txt = _format_runway(w.get('runway', {}))
            if w['ruined']:
                ruin_year = w['solvency'].get('first_ruin_year')
                verdict = f"RUIN (yr {ruin_year})" if ruin_year else "RUIN"
                print(f"  {i+1:<3} {w['structure_label']:<48} {strategy_name:<20} {verdict:>14} {runway_txt:>22}")
            elif not w['solvency'].get('engaged'):
                verdict = f"${w['net_benefit']:,.0f} (UNCHECKED)"
                print(f"  {i+1:<3} {w['structure_label']:<48} {strategy_name:<20} {verdict:>20} {runway_txt:>22}")
            else:
                print(f"  {i+1:<3} {w['structure_label']:<48} {strategy_name:<20} ${w['net_benefit']:>13,.0f} {runway_txt:>22}")

        winning_structure = rows[0]['structure_id']
        if iid != base_income_id and winning_structure != base_winning_structure:
            print(f"    ⚠ Best STRUCTURE changes under '{label}': "
                  f"'{rows[0]['structure_label']}' wins here (vs. '{base_winning_label}' "
                  f"under '{base_income_label}')")

    # Issue #1075: the OPTIMAL 3-tranche split -- the whole point of a
    # tranches-declared structure is that the optimizer GENERATES the amounts
    # (house / deductible investment / line) rather than the household fixing
    # them. Read off the winning rows' OWN ``tranche_amounts`` (DP#9: the
    # printed split is the very split the printed net benefit was scored at),
    # and on a cash-out basis the sourcing (#849: the surplus is drawn
    # line-first up to the structure's line, the rest as a mortgage advance)
    # is stated beside it.
    tranche_rows = [w for w in winners if w.get('tranche_amounts')]
    if tranche_rows:
        print(f"\n  🧱  OPTIMAL 3-TRANCHE SPLIT (structure_options.tranches -- issue #1075):")
        for w in tranche_rows:
            a = w['tranche_amounts']
            total = a['house'] + a['investment'] + a['line']
            print(f"      • '{w['structure_label']}' under '{w['income_scenario_label']}':")
            print(f"          house ${a['house']:,.0f}  +  investment ${a['investment']:,.0f}"
                  f" (deductible)  +  line ${a['line']:,.0f}  =  ${total:,.0f} charge")
            print(f"          net benefit ${w['net_benefit']:,.0f}"
                  f" (strategy {w['strategy']})")
            # Issue #1075 (optimizer half): state the cash-back verdict the
            # winning split was scored under -- credited (house >= the
            # threshold) or FORGONE (house below it, the trade-off this
            # sweep exists to price). Only a CONDITIONAL cash-back prints
            # anything (``cash_back_amount`` is carried only when the
            # declared origination inflow is conditional): a household with
            # no such declaration sees the exact pre-#1075 report.
            if w.get('cash_back_amount') is not None:
                thresh = w.get('cash_back_threshold')
                if w.get('cash_back_credited'):
                    verdict = (f"cash-back ${w['cash_back_amount']:,.0f} CREDITED "
                               f"(house ${a['house']:,.0f} >= the "
                               f"${thresh:,.0f} threshold)")
                else:
                    verdict = (f"cash-back ${w['cash_back_amount']:,.0f} FORGONE "
                               f"(house ${a['house']:,.0f} below the "
                               f"${thresh:,.0f} threshold)")
                print(f"          {verdict}")
            if basis_cash_out > 0:
                line_draw = min(basis_cash_out, a['line'])
                advance = basis_cash_out - line_draw
                print(f"          cash-out sourcing (#849): ${advance:,.0f} as a mortgage advance,"
                      f" ${line_draw:,.0f} drawn from the line")
    print()


# Human labels for the waterfall's source names (liquidation_waterfall's
# LiquidationSource.name). DP#2-adjacent: presentation text, not a rule.
_LIQUIDATION_SOURCE_LABELS = {
    'emergency_reserve': 'Emergency reserve (cash sleeve)',
    'revolving_credit': 'Revolving credit facility',
    'non_reg': 'Non-registered (taxable sale)',
    'tfsa': 'TFSA',
    'registered': 'RRSP/RRIF (fully taxable)',
}

# Issue #758: the runway rendering is ONE spelling (DP#9), in runway.py, so
# the console report and the TXT/HTML reports say the same thing.
from runway import format_runway as _format_runway  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# Issue #1011: property-purchase FUNDING as a swept, ranked decision axis.
# ═══════════════════════════════════════════════════════════════════════════
#
# A dated property PURCHASE could be funded one fixed way (``purchase.
# financing``, #967) but not several -- "buy this home and let the engine
# choose how to fund it (all-cash vs. down-payment+mortgage at some LTV)" was
# not expressible; an author had to hand-build one contract variant per
# funding method and diff. ``purchase.funding_options`` (#1011) declares the
# candidate funding methods; the optimizer ENUMERATES them, re-optimises per
# candidate, and RANKS by the active objective (DP#22). The shape mirrors
# ``run_mortgage_structure_exploration`` (#687): the declared options array is
# the trigger (DP#16), the sweep is a dedicated exploration (not a grid
# overlay dimension -- a per-property purchase decision does not fit the
# household-level grid), and every result row is tagged with the funding it
# was scored at so the printed basis cannot disagree with the numbers (DP#9).


def run_property_funding_exploration(cfg: Dict, input_path: str = "input.json",
                                     objective: ObjectiveFunction = None,
                                     cells: Optional[List[Dict]] = None
                                     ) -> List[Dict]:
    """Run the full optimizer once per (funding candidate x income scenario)
    cell -- issue #1011.

    ``properties[].purchase.funding_options`` exists so a household buying a
    mid-horizon property can ask the tool to RANK the ways to fund it -- all-
    cash from the portfolio vs. a down payment + originated mortgage at a
    declared LTV -- rather than hand-authoring one contract document per
    funding method and diffing the outputs (the exact ad-hoc, script-y
    workflow the contract-native design exists to end). The funding choice
    materially changes the answer (liquidation tax drag & lost compounding on
    a cash purchase vs. non-deductible mortgage interest on a financed one),
    so it belongs in the optimizer's search, not in the author's hand.

    Crossed with EVERY declared income scenario (``decisions.income[]``,
    #665) so the household can see whether the answer *changes* under job
    loss -- a funding choice that is optimal at full income and ruinous on EI
    is precisely what this composition must surface (DP#5: anchor decisions,
    overlay sensitivities; DP#22: the optimizer ranks, it doesn't choose).

    Each candidate funding is applied via ``property_structure.
    apply_property_funding_overlay`` (which rebuilds the property's
    ``purchase.financing`` block and ``net_equity``/``secured_share`` for the
    chosen method), THEN the income overlay -- and the PRODUCT is ranked,
    every row tagged with ``property_funding_id``/``property_funding_label``
    and ``income_scenario_id``/``income_scenario_label`` (DP#8: reuses the
    same income-scenario tagging shape ``run_income_scenario_exploration``
    uses).

    Returns the UNION of every (funding, income scenario) cell's ranked
    results. When the household declares NO ``funding_options`` this is never
    called -- ``main`` gates it on a declaration, so the golden trajectory is
    byte-identical to #967/#696 (DP#32).

    Args:
        cells: the funding cells (``scenario_discovery.
            discover_property_funding_cells``). Defaults to computing them.
    """
    from scenario_discovery import discover_anchors, discover_property_funding_cells
    income_scenarios = discover_anchors(cfg)['income']
    if cells is None:
        cells = discover_property_funding_cells(cfg)

    # Each (cell x income scenario) is an INDEPENDENT run_optimization fold;
    # enumerate them exactly as the serial nested loop did, run them across
    # cores (perf), and collect IN INPUT ORDER so the per-row tagging below
    # and the final sort are byte-identical to serial.
    scenarios = []
    for cell in cells:
        cfg_funded = _apply_property_funding_scenario(cfg, cell['assignment'])
        for inc in income_scenarios:
            cfg_variant = _apply_income_scenario(cfg_funded, inc)
            scenarios.append((cell, inc, {'kwargs': dict(
                cfg=cfg_variant, input_path=input_path, objective=objective)}))

    all_results: List[Dict] = []
    for (cell, inc, _payload), results in zip(
            scenarios, _map_scenarios([s[2] for s in scenarios])):
        for r in results:
            r['property_funding_id'] = cell['id']
            r['property_funding_label'] = cell['label']
            r['income_scenario_id'] = inc['id']
            r['income_scenario_label'] = inc['label']
        all_results.extend(results)

    all_results.sort(
        key=lambda r: ranking_key(r, r.get('objective_score', r.get('net_benefit', 0))),
        reverse=True)
    return all_results


def _apply_property_funding_scenario(cfg: Dict, assignment: Dict[str, Dict]) -> Dict:
    """DP#18: overlay ONE candidate funding assignment onto ``cfg`` --
    returns a MODIFIED COPY, never replaces the base outright (issue #1011).

    ``assignment`` is one cell's ``assignment`` from
    ``scenario_discovery.discover_property_funding_cells`` -- delegates the
    financing-block rebuild + net_equity/secured_share recompute to
    ``property_structure.apply_property_funding_overlay`` (DP#9: one
    mechanism, not one per caller).
    """
    from property_structure import apply_property_funding_overlay
    return apply_property_funding_overlay(cfg, assignment)


def winners_by_property_funding(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the funding ranking report (issue #1011): for each
    funding candidate present in ``results`` (in first-seen/declaration
    order), find the winning strategy and record it.

    Split out from ``_print_property_funding_report`` so "which funding does
    the objective prefer" is a directly testable fact, not something only
    observable by parsing printed text -- the same split
    ``winners_by_structure_scenario`` makes for #687.

    Returns one dict per funding candidate:
        {id, label, strategy, net_benefit, objective_score}
    The winner is selected by the RESOLVED objective's score (defaulting to
    net_benefit), so a non-default ``decisions.objective`` reorders the
    funding ranking exactly as it reorders the headline (DP#22).
    """
    seen = list(dict.fromkeys(r['property_funding_id'] for r in results))
    winners = []
    for fid in seen:
        rows = [r for r in results if r['property_funding_id'] == fid]
        best = max(rows, key=lambda r: r.get(
            'objective_score', r.get('net_benefit', 0)))
        winners.append({
            'id': fid,
            'label': rows[0]['property_funding_label'],
            'strategy': best.get('strategy', '?'),
            'net_benefit': best.get('net_benefit', 0),
            'objective_score': best.get(
                'objective_score', best.get('net_benefit', 0)),
        })
    return winners


def _print_property_funding_report(results: List[Dict]) -> None:
    """Issue #1011: print the property-purchase FUNDING ranking -- one row per
    declared funding candidate, ranked by the active objective, naming the
    winning strategy each funding produces. The objective-winner is the top
    row (DP#22: the optimizer ranks, the user reads the winner)."""
    winners = winners_by_property_funding(results)
    if not winners:
        return
    obj_name = _objective_name_for_results(results) or 'max_net_benefit'
    print(f"\n  🏠  PROPERTY FUNDING RANKING (purchase.funding_options -- issue #1011)")
    print(f"      objective: {obj_name}")
    print(f"\n  {'#':<3} {'Funding':<36} {'Strategy':<30} {'Net':>9}")
    print(f"  {'-'*82}")
    for i, w in enumerate(winners):
        print(f"  {i+1:<3} {w['label']:<36} {w['strategy']:<30} "
              f"${w['net_benefit']/1000:>7.0f}k")
    print(f"\n  The objective-winner is row 1. Each funding method is a real")
    print(f"  re-optimisation, not a restated input (DP#22).")


def run_borrow_to_invest_exploration(cfg: Dict, input_path: str = "input.json",
                                     objective: ObjectiveFunction = None
                                     ) -> List[Dict]:
    """Run the full optimizer once per (borrow-to-invest amount rung x income
    scenario) cell -- issue #1036.

    ``decisions.borrow_to_invest`` exists so a mortgage-free household (or any
    household with a declared HELOC) can ask the tool to RANK drawing $X
    against home equity and investing it in non-reg -- a one-shot, non-
    readvanceable leverage decision independent of any mortgage -- rather than
    hand-authoring one contract document per draw amount and diffing the
    outputs. The amount ladder is the declared options array; the optimizer
    also runs the implicit amount=0 (no-draw) baseline so 'do nothing' is
    always the frame of reference (DP#33: a declaration is a lens, not a
    blindfold -- the sweep is not replaced by the declared set, it is
    annotated by it).

    The draw reuses the year-0 margin-draw machinery already built for the
    ``draw_fraction`` axis: each declared amount becomes a
    ``draw_fraction = amount / margin_available`` rung, and ``run_optimization``
    books ``lump_sum = margin_available * draw_fraction`` via
    ``initial_state_for_run`` -> ``heloc_balance``, invests it in non-reg, and
    the existing ``apply_margin_heloc_interest`` + ``sm_interest`` Leg 3 price
    the interest and deduct it under ITA s.20(1)(c) (traced 100% investment --
    a borrow-to-invest draw has no personal portion). No readvanceable
    facility is required: a mortgage-free household with a HELOC gets a
    leveraged trajectory without any SM readvance.

    Crossed with EVERY declared income scenario (``decisions.income[]``,
    #665) so the household can see whether the answer *changes* under job
    loss -- a leverage choice that is optimal at full income and ruinous on
    EI is precisely what this composition must surface (DP#5: anchor
    decisions, overlay sensitivities; DP#22: the optimizer ranks, it doesn't
    choose).

    Returns the UNION of every (amount rung, income scenario) cell's ranked
    results, each tagged with ``borrow_to_invest_id``/``borrow_to_invest_label``
    /``borrow_to_invest_amount`` and ``income_scenario_id``/
    ``income_scenario_label``. When the household declares NO
    ``borrow_to_invest`` this is never called -- ``main`` gates it on a
    declaration, so the golden trajectory is byte-identical (DP#32).
    """
    from scenario_discovery import discover_anchors
    options = list(cfg.get('borrow_to_invest_options', []))
    if not options:
        return []
    # D2 / #1081 / #1037: REFUSE borrow-to-invest under any regime where
    # outstanding HELOC debt distorts the ranking toward 'borrow the most and
    # die owing it', until the asset-liability coupling (#1037) and/or the
    # objective's debt-floor (#1081) land. The root cause is NOT
    # liquidate_to_target -- it is that `min_after_tax_estate` scores
    # `-net_estate`, and outstanding debt reduces net_estate 1:1, so 'borrow
    # the maximum and die owing it' ranks highest (proven dollar-exact: every
    # borrowed dollar left outstanding buys exactly one dollar of objective
    # score, terminal assets identical across rungs). #1068 (the #1065
    # insolvency floor) does NOT cover this: every net_estate here is positive,
    # so its branch never fires (verified by transplant). This refusal is at
    # the EXPLORATION INVOCATION point (not at load-time contract mapping) so
    # it sees the RESOLVED objective -- including a `--objective
    # min_after_tax_estate` CLI override, which a load-time check cannot catch
    # (the objective is resolved per-run, not per-document). Borrow-to-invest
    # stays available under the accumulation / wealth-maximising objectives
    # (e.g. the default max_net_benefit, max_after_tax_estate), never shipping
    # a ranked table that rewards dying in debt (DP#32: refuse loudly, never
    # silently wrong).
    _obj_name = getattr(objective, 'name', None)
    _liq = cfg.get('retirement', {}).get('liquidate_to_target') is True
    _d2_reasons = []
    if _obj_name == 'min_after_tax_estate':
        _d2_reasons.append(
            "the run is scored under min_after_tax_estate, which scores "
            "-net_estate; outstanding borrow-to-invest HELOC debt reduces "
            "net_estate dollar-for-dollar, so 'borrow the maximum and die "
            "owing it' ranks highest (the inverted incentive filed as "
            "#1081). This fires whether or not liquidate_to_target is set.")
    if _liq:
        _d2_reasons.append(
            "retirement.liquidate_to_target is true: the die-with-zero "
            "drawdown liquidates the borrowed non-reg pot to fund spending "
            "while the matching HELOC is never repaid in decumulation "
            "(apply_sm_unwind is gated on the SM sleeve, which this draw "
            "does not touch), so the household spends the borrowed proceeds "
            "and dies owing the HELOC (the unwind coupling is #1037).")
    if _d2_reasons:
        raise ValueError(
            "decisions.borrow_to_invest is declared but the run is scored "
            "under a regime where outstanding borrow-to-invest debt distorts "
            "the ranking toward 'borrow the most and die owing it'. Refusing "
            "rather than shipping that table (DP#32). Reason(s):\n  - "
            + "\n  - ".join(_d2_reasons)
            + "\n\nborrow-to-invest remains available under the accumulation "
            "/ wealth-maximising objectives (e.g. max_net_benefit, "
            "max_after_tax_estate). Track #1081 (the min_after_tax_estate debt "
            "floor) and #1037 (the asset-liability unwind coupling) for when "
            "this refusal can lift.")
    margin_available = cfg.get('property', {}).get('margin_available', 0)
    income_scenarios = discover_anchors(cfg)['income']
    # The amount ladder: the implicit no-draw baseline + one rung per declared
    # option. The baseline is the frame of reference every draw is read
    # against (DP#33); without it, a declared $50k draw that beats a $100k draw
    # would also silently beat 'do nothing', and the household would never see
    # that the better answer was to not borrow at all.
    cells = [{'id': 'no_draw', 'label': 'No draw (baseline)',
              'amount': 0.0, 'draw_fraction': 0.0}]
    for opt in options:
        cells.append({
            'id': opt['id'], 'label': opt['label'],
            'amount': opt['amount'],
            'draw_fraction': opt['amount'] / margin_available,
            # Issue #1040: hold_draw=true opts this rung's draw OUT of the
            # RRSP-refund HELOC paydown sweep. Carried on the cell so the
            # per-cell cfg_variant below can set the engine-facing flag.
            'hold_draw': opt.get('hold_draw', False),
        })

    scenarios = []
    for cell in cells:
        for inc in income_scenarios:
            cfg_variant = _apply_income_scenario(cfg, inc)
            # D3 / #1036: honour target_account=non_reg. fill_room
            # (strategy.py) is a registered-first waterfall, so by default a
            # borrow-to-invest draw would land ~75% in RRSP/TFSA -- the exact
            # s.18(11) landing the schema refuses. Reuse the EXISTING
            # borrowed-money-to-deductible-non-reg mechanism
            # (property.refinance_advance_deductible_non_reg -> fill_room's
            # deductible_non_reg_first) to front-load the WHOLE draw into
            # non_reg before the registered waterfall runs, so the declared
            # target_account is actually executed (DP#32: a declared decision
            # the engine does not execute is the defect class this PR closes).
            # The no-draw baseline (amount=0) sets 0 -- a no-op (there is no
            # lump_sum to route). This is the ONE place borrow-to-invest
            # diverges from a plain draw_fraction sweep.
            # N1: ADD to any declared #792 refinance advance split rather than
            # clobbering it -- a declared household decision must not be
            # silently overwritten (DP#32). fill_room caps deductible_non_reg_
            # first at the year-0 lump sum, and this exploration's lump sum is
            # the borrow-to-invest draw (no refinance cash-out here), so the
            # additive value still routes the whole draw to non_reg; the
            # declared #792 split is preserved (added to), not discarded.
            existing_split = cfg_variant['property'].get('refinance_advance_deductible_non_reg')
            if existing_split is None:
                existing_split = 0
            cfg_variant['property']['refinance_advance_deductible_non_reg'] = existing_split + cell['amount']
            # Issue #1040: a hold_draw option books its draw through the same
            # initial_state_for_run -> heloc_balance machinery, but opts OUT
            # of the rrsp_refund_heloc_paydown sweep: set the engine-facing
            # flag this cell's runs read (property.borrow_to_invest_hold_draw
            # -> SimulationConfig.hold_borrow_to_invest_draw -> the paydown
            # rule). The no-draw baseline and non-hold-draw rungs never set
            # it -- absent stays False, the pre-#1040 sweep behaviour,
            # byte-identical (DP#18: the decision modifies a key the engine
            # actually reads; DP#32: absence is the fallback).
            if cell.get('hold_draw'):
                cfg_variant['property']['borrow_to_invest_hold_draw'] = True
            scenarios.append((cell, inc, {'kwargs': dict(
                cfg=cfg_variant, input_path=input_path, objective=objective,
                draw_fraction_options=[cell['draw_fraction']])}))

    all_results: List[Dict] = []
    for (cell, inc, _payload), results in zip(
            scenarios, _map_scenarios([s[2] for s in scenarios])):
        for r in results:
            r['borrow_to_invest_id'] = cell['id']
            r['borrow_to_invest_label'] = cell['label']
            r['borrow_to_invest_amount'] = cell['amount']
            r['income_scenario_id'] = inc['id']
            r['income_scenario_label'] = inc['label']
        all_results.extend(results)

    all_results.sort(
        key=lambda r: ranking_key(r, r.get('objective_score', r.get('net_benefit', 0))),
        reverse=True)
    return all_results


def winners_by_borrow_to_invest(results: List[Dict]) -> List[Dict]:
    """Pure logic half of the borrow-to-invest ranking report (issue #1036):
    for each amount rung present in ``results`` (in SCORE order -- best first,
    because ``run_borrow_to_invest_exploration`` sorts by objective before
    ``dict.fromkeys``; the no-draw baseline is NOT necessarily row 1, it is the
    frame of reference ranked on its merits, DP#33), find the winning strategy
    and record it. Split out from ``_print_borrow_to_invest_report`` so 'which
    draw amount does the objective prefer' is a directly testable fact, not
    something only observable by parsing printed text (mirrors
    ``winners_by_property_funding``).

    Returns one dict per amount rung:
        {id, label, amount, strategy, net_benefit, objective_score}
    The winner is selected by the RESOLVED objective's score (defaulting to
    net_benefit), so a non-default ``decisions.objective`` reorders the
    borrow-to-invest ranking exactly as it reorders the headline (DP#22).
    """
    seen = list(dict.fromkeys(r['borrow_to_invest_id'] for r in results))
    winners = []
    for bid in seen:
        rows = [r for r in results if r['borrow_to_invest_id'] == bid]
        best = max(rows, key=lambda r: r.get(
            'objective_score', r.get('net_benefit', 0)))
        winners.append({
            'id': bid,
            'label': rows[0]['borrow_to_invest_label'],
            'amount': rows[0]['borrow_to_invest_amount'],
            'strategy': best.get('strategy', '?'),
            'net_benefit': best.get('net_benefit', 0),
            'objective_score': best.get(
                'objective_score', best.get('net_benefit', 0)),
        })
    return winners


def _print_borrow_to_invest_report(results: List[Dict]) -> None:
    """Issue #1036: print the borrow-to-invest ranking -- one row per amount
    rung in SCORE order (best first; the no-draw baseline is ranked on its
    merits, not pinned to row 1, DP#33), naming the winning strategy each
    rung produces. The objective-winner is the top row (DP#22: the optimizer
    ranks, the user reads the winner).

    D9: the numeric column shown is the RESOLVED objective's score
    (``objective_score`` -- the value that drove the order), not ``net_benefit``.
    Under ``min_after_tax_estate`` the score is the negated after-tax estate
    and ``net_benefit`` decreases monotonically down the ranking, so showing
    ``net_benefit`` would contradict the order; showing the score makes the
    table self-consistent. Under ``max_net_benefit`` the two are equal."""
    winners = winners_by_borrow_to_invest(results)
    if not winners:
        return
    obj_name = _objective_name_for_results(results) or 'max_net_benefit'
    print(f"\n  🏦  BORROW-TO-INVEST RANKING (decisions.borrow_to_invest -- issue #1036)")
    print(f"      objective: {obj_name}")
    print(f"\n  {'#':<3} {'Draw':<36} {'Amount':>10} {'Strategy':<28} {'Score':>12}")
    print(f"  {'-'*92}")
    for i, w in enumerate(winners):
        amt = f"${w['amount']/1000:.0f}k" if w['amount'] else '—'
        score = w.get('objective_score', w.get('net_benefit', 0))
        print(f"  {i+1:<3} {w['label']:<36} {amt:>10} {w['strategy']:<28} "
              f"{score:>12,.0f}")
    print(f"\n  The objective-winner is row 1. The no-draw baseline (—) is the")
    print(f"  frame of reference every draw is read against (DP#33); each draw")
    print(f"  amount is a real re-optimisation, not a restated input (DP#22).")


def _print_decumulation_shortfall_report(results: List[Dict], cfg: Dict) -> None:
    """Issue #707's console deliverable: a plan that runs out of money before
    the horizon is surfaced as a FIRST-CLASS output, not a confident terminal
    number.

    "The money runs out in year N, $G short of the net spending target" --
    that is the answer to "can I retire on this?", and the terminal
    net-benefit figure is not. Mirrors ``_print_solvency_report`` (#679):
    the two shortfalls are distinct (solvency = the cash-flow identity
    against declared ``household_budget.annual_living_costs``; this = the
    retirement drawdown against ``retirement.spending_target``), and a run
    may hit either, both, or neither.

    The caveat text itself is the single registered ``decumulation_shortfall``
    Approximation in model_fidelity.py -- this function renders THAT
    definition (so the console and the TXT/JSON/HTML reports say the same
    thing, one spelling, DP#9), then adds a per-scenario table the reports
    already carry as data.

    Skipped with a loud DP#32 notice when no scenario ever engaged the
    drawdown (no member retired within the horizon, or no spending_target):
    "0 shortfall years" for a run that never checked is the most dangerous
    thing this section could print.
    """
    engaged = [r for r in results
               if shortfall_of(r) is not None
               and shortfall_of(r).get('engaged')]
    if not engaged:
        print(f"  ℹ️  DECUMULATION NOT CHECKED -- no member retires within the "
              f"horizon or no retirement.spending_target was declared, so the "
              f"drawdown shortfall (issue #707) could not be evaluated. This is "
              f"NOT a finding of safety: the household has not been checked, not "
              f"cleared.")
        print()
        return

    exhausted = [r for r in engaged if r['drawdown_shortfall'].get('exhausted')]
    # Render the registered caveat's own summary + findings (one spelling).
    active = {a.id: a for a in model_fidelity.active_approximations(
        cfg, _objective_name_for_results(results))}
    approx = active.get('decumulation_shortfall')

    print(f"{'=' * 120}")
    print(f"  📉  DECUMULATION SHORTFALL (issue #707) -- did the money last?")
    print(f"{'=' * 120}")
    if not exhausted:
        print(f"  ✅ No shortfall: every scenario that drew down met its net "
              f"spending target from its own assets through the horizon.")
        print()
        return

    if approx is not None:
        print(f"\n  ⛔ {approx.summary}")
        ctx = model_fidelity.FidelityContext(cfg=cfg, objective_name=_objective_name_for_results(results))
        for finding in approx.findings_for(ctx):
            print(f"     {finding}")

    print(f"\n  {'Scenario':<40} {'1st shortfall':>15} {'Gap $':>12} "
          f"{'Shortfall yrs':>15} {'Total unmet $':>15}")
    print(f"  {'-' * 100}")
    for r in engaged:
        s = r['drawdown_shortfall']
        # Explicit absence test (DP#32): not `r.get('strategy') or ...`.
        label = r.get('strategy')
        if label is None:
            label = r.get('label')
        if label is None:
            label = '?'
        label = label[:39]
        if s.get('exhausted'):
            yr = s.get('first_shortfall_year')
            yr_txt = f"year {yr}" if yr else "?"
            print(f"  {label:<40} {yr_txt:>15} ${s.get('first_shortfall_gap', 0):>11,.0f} "
                  f"{s.get('shortfall_years', 0):>15} ${s.get('total_unmet', 0):>14,.0f}")
    print()


def _objective_name_for_results(results: List[Dict]) -> Optional[str]:
    """The objective the ranked results were scored on, for the model_fidelity
    caveat's objective-sensitive predicates. Mirrors output_plugins'
    _objective_name but lives here so the console report (optimize.py) does
    not import output_plugins (DP#25: optimization layer must not reach back
    into the reporting layer)."""
    if not results:
        return None
    best = max(results, key=lambda r: r.get('net_benefit', 0))
    # Explicit: `.get()` already returns None when absent; no `or None`
    # (DP#32 -- a present falsy objective name would be clobbered by `or`).
    return best.get('objective_name')


def _print_solvency_report(winners: List[Dict]) -> None:
    """Issue #679's actual deliverable: solvency as a FIRST-CLASS output.

    "Months of runway, the year the money runs out, and what the household
    was forced to sell" -- these three facts ARE the answer to the job-loss
    question. The terminal net-benefit figure is not; it is the number the
    household reads when nobody tells it the truth.

    Skipped entirely (with a loud DP#32 notice) when the solvency module was
    never engaged, because a household that never declared
    ``household_budget.annual_living_costs`` has not been found SAFE -- it
    has not been CHECKED, and printing "0 shortfalls" for it would be the
    single most dangerous thing this report could say.
    """
    engaged = [w for w in winners if w['solvency'].get('engaged')]
    if not engaged:
        print(f"  ℹ️  SOLVENCY NOT CHECKED -- the contract declares no "
              f"`household_budget.annual_living_costs`, so the cash-flow identity "
              f"(issue #679)")
        print(f"      could not be evaluated. This is NOT a finding of solvency: the "
              f"household has not been checked, not cleared.")
        print()
        return

    print(f"{'=' * 120}")
    print(f"  🩺  SOLVENCY BY INCOME SCENARIO (issue #679) -- can the household actually "
          f"fund its obligations?")
    print(f"{'=' * 120}")
    print(f"\n  {'Scenario':<28} {'Runway':>10} {'1st shortfall':>15} {'Ruin':>10} "
          f"{'Forced-sale tax':>17} {'Realised loss':>15}")
    print(f"  {'-' * 100}")

    for w in engaged:
        s = w['solvency']
        runway = f"{s.get('runway_months_at_start', 0.0):.1f} mo"
        first = s.get('first_shortfall_year')
        first_txt = f"year {first}" if first else "none"
        ruin_year = s.get('first_ruin_year')
        ruin_txt = f"year {ruin_year}" if ruin_year else "no"
        tax = s.get('forced_liquidation_tax', 0.0)
        loss = s.get('forced_liquidation_realized_loss', 0.0)
        print(f"  {w['label']:<28} {runway:>10} {first_txt:>15} {ruin_txt:>10} "
              f"${tax:>16,.0f} ${loss:>14,.0f}")

    for w in engaged:
        s = w['solvency']
        if not s.get('first_shortfall_year'):
            continue
        print(f"\n  ── '{w['label']}' -- what the household was FORCED TO SELL ──")
        print(f"     Shortfall years: {s.get('shortfall_years', 0)}"
              f"   |   Declared reserve at start: "
              f"{s.get('runway_months_at_start', 0.0):.1f} months of essential outflows")
        by_source = s.get('forced_liquidation_gross_by_source', {})
        if by_source:
            # Printed in WATERFALL ORDER (the order actually drawn), because the
            # order and its cost are the answer the household needs -- not an
            # alphabetical list.
            for src in ('emergency_reserve', 'revolving_credit', 'non_reg', 'tfsa', 'registered'):
                if src in by_source and by_source[src] > 0:
                    label = _LIQUIDATION_SOURCE_LABELS.get(src, src)
                    print(f"       {label:<36} ${by_source[src]:>14,.0f} (gross drawn)")
        else:
            print(f"       (nothing left to sell -- every source was already empty)")
        if s.get('uncovered_shortfall', 0.0) > 0:
            print(f"     ⛔ UNCOVERED SHORTFALL: ${s['uncovered_shortfall']:,.0f} -- the waterfall "
                  f"exhausted EVERY source and the household is still short.")
        if s.get('credit_facility_unrepresentable'):
            # Issue #689. An honest understatement beats a silent one.
            print(f"     ⚠️  RESILIENCE UNDERSTATED (issue #689): the waterfall's second step -- a "
                  f"revolving, unsecured credit")
            print(f"        facility (a line of credit) -- CANNOT be declared in the input contract "
                  f"today, so it was drawn as $0.")
            print(f"        A household that HOLDS such a facility is more resilient than this "
                  f"report shows. The HELOC margin is")
            print(f"        deliberately NOT substituted: a HELOC is SECURED against the same "
                  f"charge as the mortgage (#664/#681),")
            print(f"        and spending investment-loan room as an emergency line would model a "
                  f"different product.")
    print()


def _print_runway_report(winners: List[Dict]) -> None:
    """Issue #758's console deliverable: months-to-ruin as a FIRST-CLASS
    output, beside the solvency verdict.

    "You have N months if the shock lands now" -- that is the number a
    household wants before signing a mortgage, and the bracket + the
    interpolation label travel with it so a year-granular engine never
    prints a false-precision month. Mirrors ``_print_solvency_report`` /
    ``_print_decumulation_shortfall_report``: the facts are DATA on the
    ranking row (``runway``), this only renders them.

    Skipped with a loud DP#32 notice when no scenario engaged the cash-flow
    identity -- "UNCHECKED" for every row is the only honest thing to say.
    """
    # A row carrying no `runway` (an older/synthetic winner) is ABSENT, not
    # an un-engaged one -- guard with `in`, never truthiness (DP#32).
    with_runway = [w for w in winners if 'runway' in w and w['runway'].get('engaged')]
    if not with_runway:
        print(f"  ℹ️  RUNWAY NOT CHECKED (issue #758) -- no scenario engaged the "
              f"cash-flow identity (declare `household_budget.annual_living_costs` "
              f"and a dated `decisions.income[]` shock).")
        print(f"      This is NOT a finding of safety: the household has not been "
              f"checked, not cleared.")
        print()
        return

    print(f"{'=' * 120}")
    print(f"  🛟  RUNWAY — months to insolvency after the income shock (issue #758)")
    print(f"{'=' * 120}")
    print(f"  The headline is a LABELLED interpolation inside an honest bracket")
    print(f"  (the engine steps in years; ~N mo is a point estimate, [lo–hi] is the")
    print(f"  structural range). '>=N mo (survives)' = the cushion outlasts the horizon.")
    print(f"\n  {'Scenario':<28} {'Runway':>22} {'Stress begins':>16} {'Caveats':<40}")
    print(f"  {'-' * 110}")
    for w in with_runway:
        rw = w['runway']
        runway_txt = _format_runway(rw)
        stress = rw.get('stress_begins_months')
        stress_txt = f"~{stress:.0f} mo" if stress is not None else "—"
        caveats = []
        if rw.get('relies_on_credit_facility'):
            caveats.append("leans on credit line")
        if rw.get('drew_registered'):
            caveats.append("drew RRSP (taxed at low yr-rate)")
        caveat_txt = "; ".join(caveats) if caveats else "—"
        print(f"  {w['label']:<28} {runway_txt:>22} {stress_txt:>16}   {caveat_txt:<40}")
    print(f"\n  Interpolation method: linear within the ruin year (uniform monthly")
    print(f"  burn); the fraction is the #679 waterfall's own covered/shortfall.")
    print(f"  Runway UNDERSTATES reality: all spend treated as rigid (no")
    print(f"  discretionary/non-discretionary split exists in the contract yet) and")
    print(f"  contributions are counted as committed -- a household in real distress")
    print(f"  stops both, so the true runway is longer. See the model-fidelity section.")
    print()


def _run_and_print_runway_sweep(cfg: Dict, input_path: str, ltv_max: float,
                                objective: ObjectiveFunction = None) -> List:
    """Run the shock-date sweep (issue #758 hard part #5) and print the curve.

    The shock date is the swept dimension: the household's ONE authored
    ``decisions.income[]`` shock scenario, re-run with its dated segments
    shifted to begin now, +12 months, and +24 months. Returns the curve
    (also surfaced in JSON via the caller). Prints nothing and returns []
    when the household authored no dated shock -- there is nothing to sweep,
    and a silent empty table is the only honest thing to show.
    """
    from scenario_discovery import discover_anchors
    income_scenarios = discover_anchors(cfg)['income']
    shock = None
    for s in income_scenarios:
        if s['primary_segments'] or s['spouse_segments']:
            shock = s
            break
    if shock is None:
        return []
    all_segs = list(shock['primary_segments']) + list(shock['spouse_segments'])
    original = min(_date.fromisoformat(seg['from']) for seg in all_segs)
    shock_dates = [original,
                  _date(original.year + 1, original.month, original.day),
                  _date(original.year + 2, original.month, original.day)]
    curve = run_runway_sweep(cfg, shock_dates, input_path=input_path,
                             ltv_max=ltv_max, objective=objective)
    # shock was found above, so run_runway_sweep returns one point per
    # shock_date (never [] here); print the curve.
    print(f"{'=' * 120}")
    print(f"  📈  RUNWAY vs SHOCK DATE (issue #758) -- the curve, not a point")
    print(f"{'=' * 120}")
    print(f"  The same household has different runway for a shock now vs in a year:")
    print(f"  assets grow, debts amortize, an obligation may end. ~N mo is a labelled")
    print(f"  interpolation; [lo-hi] is the structural bracket.\n")
    print(f"  {'Shock date':<14} {'Runway':<24} {'Bracket':<18} {'Stress begins':>14}")
    print(f"  {'-' * 76}")
    from runway import format_curve_point
    for p in curve:
        runway_txt, bracket_txt = format_curve_point(p)
        stress = p.stress_begins_months
        stress_txt = f"~{stress:.0f} mo" if stress is not None else "-"
        print(f"  {p.shock_date.isoformat():<14} {runway_txt:<24} {bracket_txt:<18} {stress_txt:>14}")
    print()
    return curve


def _record_runway_sweep(args, cfg: Dict, input_path: str, ltv_max: float,
                         objective: ObjectiveFunction = None) -> List:
    """Issue #758: run the shock-date sweep when ``--runway-sweep`` is set,
    print it, and record the curve onto ``cfg['assumptions']['runway_sweep']``
    so the JSON report can emit it. Returns ``[]`` when the flag is unset or
    no shock was authored. Extracted from ``main`` so the opt-in branch is
    unit-testable without driving the whole CLI (main is not unit-tested
    end-to-end; this helper is)."""
    if not getattr(args, 'runway_sweep', False):
        return []
    curve = _run_and_print_runway_sweep(cfg, input_path, ltv_max, objective=objective)
    # One spelling, every surface (DP#9/DP#32): RunwayCurvePoint.to_dict.
    cfg.setdefault('assumptions', {})['runway_sweep'] = [
        p.to_dict() for p in curve]
    return curve


def run_optimization(cfg: Dict, input_path: str = "input.json",
                     objective: ObjectiveFunction = None,
                     overlay: ScenarioOverlay = None,
                     draw_fraction_options: List[float] = None,
                     include_year_by_year: bool = True) -> List[Dict]:
    """Run full optimization using the modular engines.

    Reads input.json config, discovers strategies, runs simulations,
    and returns ranked results.

    Args:
        cfg: Configuration dict (from input.json)
        input_path: Path to input.json
        objective: ObjectiveFunction for scoring (default: MAX_NET_BENEFIT).
                   Per DP#22, the user picks the objective; the optimizer ranks.
        overlay: Optional ScenarioOverlay. When provided (issue #259), the
                 scenario config is built through the authoritative
                 ``build_overlay_config`` / ``apply_overlay`` path — the SAME
                 machinery ``simulate.py`` uses — so a refinance cash-out is
                 recorded as debt exactly once and its proceeds invested. When
                 ``None``, ``cfg`` is used as-is (legacy behaviour).
        draw_fraction_options: issue #735 -- candidate fractions of
                 margin_available to draw and invest at year 0 (e.g.
                 [0.0, 0.25, 0.5, 1.0]). Defaults to ``[0.0]`` (undrawn, NOT
                 a sweep) -- this is the hot path most of the test suite and
                 every non-structure caller runs through, and multiplying
                 every one of them by 4 candidates by default would be an
                 unasked-for, unbounded slowdown (DP#31: the search
                 dimension is a caller's CHOICE). ``run_mortgage_structure_
                 exploration`` below is the caller that actually asks the
                 draw-fraction question, per-structure.

    The returned dicts always carry ``year_by_year`` (issue #248), which the
    JSONL session logger (issue #239) persists via ``--save-session``.
    When ``include_year_by_year`` is False (issue #1058), each dict's
    ``year_by_year`` key is an empty list — score-only callers that never
    read the series skip ~77k ``asdict`` calls per run.  DP#13: the default
    (True) preserves existing behaviour; the flag is explicit opt-in.
    """
    if draw_fraction_options is None:
        draw_fraction_options = [0.0]
    # DP#22: default to MAX_NET_BENEFIT for backward compatibility
    if objective is None:
        objective = MAX_NET_BENEFIT

    # issue #259: route scenario construction through the one authoritative path.
    if overlay is not None:
        cfg = build_overlay_config(cfg, overlay)

    brackets = default_tax_provider().get_combined_brackets()
    use_new_format = 'family' in cfg and 'members' in cfg.get('family', {})

    if use_new_format:
        # ── New input.json format ──
        # Use from_dict to capture any LTV-level modifications
        config = SimulationConfig.from_dict(cfg)
        primary = config.member_by_role('primary', {})  # #699 seam
        spouse_mem = config.member_by_role('spouse', {})
        income = primary.get('gross_income', 0)  # DP#13: personal data, not a default
        spouse_income = spouse_mem.get('gross_income', 0)

        # Discover strategies from rules (DP#6)
        # Issue #1000: populate declared FHSA room so discover_strategies can
        # gate FHSA-enabled variants on it (DP#14 -- declared room that
        # activates the store must reach the allocation SEARCH, not just the
        # engine). Mirrors scenario_discovery._build_family_state: a missing
        # fhsa_first_time_buyer_since means never eligible, so room is zeroed
        # and no FHSA variants are generated. The lifetime default is the
        # Canada package figure (_canada_fhsa_limits), the SAME source
        # SimState.initial uses when fhsa_lifetime_limit is absent -- so the
        # gate's `fhsa_lifetime_remaining > 0` agrees with the store the
        # engine actually builds.
        fhsa_room_declared = primary.get('fhsa_room_accumulated', 0)
        fhsa_lifetime_declared = primary.get('fhsa_lifetime_limit')
        if primary.get('fhsa_first_time_buyer_since') is None:
            fhsa_room_declared = 0
            fhsa_lifetime_declared = 0
        if fhsa_lifetime_declared is None:
            # engine default when the contract omits the lifetime cap
            from simulation_state import _canada_fhsa_limits
            fhsa_lifetime_declared = _canada_fhsa_limits()[2] if fhsa_room_declared > 0 else 0
        state = FamilyState(
            primary_income=income,
            spouse_income=spouse_income,
            primary_marginal_rate=marginal_rate(income, brackets),
            spouse_marginal_rate=marginal_rate(spouse_income, brackets) if spouse_income > 0 else 0,
            primary_rrsp_room=primary.get('rrsp_room_accumulated', 0),
            spouse_rrsp_room=spouse_mem.get('rrsp_room_accumulated', 0),
            primary_tfsa_room=primary.get('tfsa_room_accumulated', 0),
            spouse_tfsa_room=spouse_mem.get('tfsa_room_accumulated', 0),
            fhsa_room=fhsa_room_declared,
            fhsa_lifetime_remaining=fhsa_lifetime_declared,
        )
        # Extract financial assumptions from config (issue #23: no hardcoded defaults)
        # Issue #677: this used to read cfg['property']['mortgage_rate'] as a
        # stand-in for the HELOC's own rate -- understating the after-tax SM
        # cost by however much the two rates differ, in every scenario this
        # optimizer's own ranking path scores, for the common shape of a
        # cheap legacy fixed mortgage alongside a floating HELOC. Resolved
        # via the one canonical helper instead (config_access.
        # resolve_heloc_rate): the household's OWN declared property.
        # heloc_rate wins outright; default=0.0 only matters when this
        # household has no readvanceable facility at all, in which case the
        # has_readvanceable_facility() filter below strips any
        # readvance_priority result out of `discovered` unconditionally --
        # the value is computed and discarded, never surfaced (#663).
        opt_heloc_rate = resolve_heloc_rate(cfg, default=0.0)
        opt_investment_return = resolve_return_rate(cfg)
        # issue #663: discover_strategies() reads mortgage_cfg['heloc_readvance']
        # directly (a flat key -- see its docstring / countries/canada/
        # strategies.py). Passing the full cfg here (rather than
        # cfg['property']) meant that lookup always missed (heloc_readvance
        # lives under cfg['property']['heloc_readvance'], never at cfg's top
        # level), so readvance_priority was NEVER discovered via this path --
        # for any household, with or without a real HELOC. Fixed so the
        # "refuse to rank when unavailable" gate below is meaningful: a
        # household that DOES have a profitable readvanceable facility must
        # actually see it ranked, not silently miss it via a shape mismatch.
        # issue #713: the household's OWN authored strategies
        # (decisions.contribution_strategy[] -> cfg['strategies']) enter the
        # search space here. Absent => None => discover_strategies keeps its
        # discovered defaults (DP#13: a default is a fallback for absent input,
        # never a way to coerce a supplied one). Before this, the parameter
        # existed, the contract had the data, and NOTHING connected them: a
        # user who wrote down three strategies got back a ranking of the
        # engine's two built-in ones.
        discovered = discover_strategies(
            state, cfg.get('property', {}),
            investment_return=opt_investment_return,
            heloc_rate=opt_heloc_rate,
            custom_strategies=cfg.get('strategies'),
        )
        # issue #663/DP#32: hard-gate readvanceable/Smith-Manoeuvre strategies
        # on the explicit has_readvanceable_facility() predicate, not just the
        # heloc_readvance flag threaded through discover_strategies above.
        # Refuses to rank a strategy this household structurally cannot
        # execute even if heloc_readvance were ever set without a matching
        # facility (a data inconsistency) -- never silently scored, never
        # swallowed to -inf inside the simulation.
        if not has_readvanceable_facility(cfg):
            discovered = {
                name: strat for name, strat in discovered.items()
                if not strat.prioritize_readvanceable
            }

        # Build rate path from config
        rate_path = build_rate_path(
            name="Current variable",
            initial_rate=config.mortgage_rate,
            term_years=config.projection_years,
            rate_type='variable',
            renewal_rates=[config.mortgage_rate],
        )

        # issue #257: invested lump sum = HELOC draw (pre-existing undrawn margin)
        # + cash-out proceeds. margin_available is NOT inflated by cash_out, so the
        # cash-out is sourced from config.cash_out (set by apply_overlay /
        # run_at_each_ltv). This keeps invested capital == total new debt, each
        # borrowed dollar counted exactly once.
        cashout = config.cash_out

        # issue #735: what fraction of margin_available is actually drawn
        # and invested at year 0 is a DECISION, not a constant -- the
        # ``draw_fraction_options`` PARAMETER (default [0.0], undrawn, see
        # this function's docstring) decides whether this run explores that
        # trade-off at all; this call site never re-derives its own sweep
        # from ``cfg``, so a caller that did not ask for the sweep does not
        # silently get 4x the simulation runs.
        results = []
        for name, strategy in discovered.items():
            use_readvanceable = strategy.prioritize_readvanceable
            deduct_later = strategy.deduct_later
            for draw_fraction in draw_fraction_options:
                lump_sum = config.margin_available * draw_fraction + cashout
                # `name` (unsuffixed) is deliberately what's passed as
                # ``result['strategy']`` -- pass 2 below looks candidates up
                # in `discovered` BY this exact key (`discovered.get(winner
                # ['strategy'])`), so it must round-trip unchanged. The
                # draw-fraction dimension is distinguished by the separate
                # `draw_fraction` field, not by mangling the strategy name.
                result = evaluate_strategy_with_simulation(
                    name=name,
                    strategy=strategy,
                    config=config,
                    rate_path=rate_path,
                    use_readvanceable=use_readvanceable,
                    deduct_later=deduct_later,
                    lump_sum=lump_sum,
                    objective=objective,
                    include_year_by_year=include_year_by_year,
                )
                result['drawdown_order_id'] = 'configured'
                result['draw_fraction'] = draw_fraction
                results.append(result)

        # DP#22: sort by objective score, not hardcoded net_benefit
        results.sort(key=lambda r: ranking_key(r, r.get('objective_score', r.get('net_benefit', 0))), reverse=True)

        # ── Issue #618: decumulation search, pass 2 ──────────────────────
        # Pass 1 above only ever varies ACCUMULATION levers (rrsp_pct,
        # tfsa_pct, use_smith, deduct_later, ...) via discover_strategies();
        # `config.retirement_data['drawdown_order']` was a fixed input every
        # candidate shared. Two households that differ ONLY in withdrawal
        # order can differ by $500k+ in after-tax estate (#618) -- a signal
        # pass 1 cannot see no matter which objective scores it.
        #
        # Guarding the combinatorics: rather than a full cross-product
        # (N_accum accumulation strategies x N_drawdown orders -- here
        # ~3x4=12 simulation runs), this is a SECOND PASS over only the
        # winning accumulation strategy from pass 1: N_accum + (N_drawdown-1)
        # extra runs (~3+3=6). Growth is additive in the number of
        # decumulation candidates, not multiplicative in both dimensions.
        # Gated by discover_drawdown_orders (Issue #303's exact gating
        # pattern): a horizon that never reaches retirement gets back a
        # single 'configured' candidate and pass 2 costs nothing extra.
        from scenario_discovery import discover_drawdown_orders
        drawdown_candidates = discover_drawdown_orders(cfg)
        if results and len(drawdown_candidates) > 1:
            winner = results[0]
            winner_strategy = discovered.get(winner['strategy'])
            # DP#32: absence (key missing / None) falls back to the engine's
            # own default (simulation.py); an explicitly configured order
            # (including an unusual empty list) is not silently overridden.
            configured_order = (config.retirement_data or {}).get('drawdown_order')
            if configured_order is None:
                configured_order = ['tfsa', 'non_reg', 'rrsp']
            if winner_strategy is not None:
                for candidate in drawdown_candidates:
                    if candidate['order'] == configured_order:
                        # Already evaluated as `winner` in pass 1 -- the
                        # configured/default order IS this candidate.
                        continue
                    variant_retirement = dict(config.retirement_data or {})
                    variant_retirement['drawdown_order'] = candidate['order']
                    variant_config = replace(config, retirement_data=variant_retirement)
                    # issue #735: pass 2 varies ONLY decumulation order --
                    # it must reuse pass 1's winning ACCUMULATION decision,
                    # including how much of the facility that winner drew,
                    # not silently re-derive a different lump sum.
                    winner_draw_fraction = winner.get('draw_fraction', 0.0)
                    variant_result = evaluate_strategy_with_simulation(
                        name=f"{winner['strategy']} + {candidate['label']}",
                        strategy=winner_strategy,
                        config=variant_config,
                        rate_path=rate_path,
                        use_readvanceable=winner.get('readvanceable_mortgage', False),
                        deduct_later=winner.get('deduct_later', False),
                        lump_sum=config.margin_available * winner_draw_fraction + cashout,
                        objective=objective,
                        include_year_by_year=include_year_by_year,
                    )
                    variant_result['drawdown_order_id'] = candidate['id']
                    variant_result['draw_fraction'] = winner_draw_fraction
                    results.append(variant_result)

            results.sort(key=lambda r: ranking_key(r, r.get('objective_score', r.get('net_benefit', 0))), reverse=True)

        return results

    else:
        # ── Old format fallback ──
        strategies = generate_strategies(cfg)
        return [evaluate_strategy(s['name'], cfg, s) for s in strategies]


def simulated_deduct_timing(cfg: Dict, input_path: str = "input.json",
                            objective: ObjectiveFunction = None) -> Dict:
    """Compare deduct-now vs deduct-later using the *simulation* (issue #251).

    The simulation is the single source of truth for deduct-timing. Rather than
    a misleading standalone closed-form (which compared a one-year full-room
    deduction against a 10-year spread — apples to oranges), this runs the
    engine for each discovered strategy both ways (``deduct_later=False`` and
    ``deduct_later=True``), mirroring how ``simulate.py`` enumerates the
    ``deduct_later_options`` anchor. It reports the best of each bucket.

    Returns a dict with ``deduct_now`` / ``deduct_later`` objective scores
    (best in each bucket), the advantage of deducting later (later - now), and
    the representative strategy names, or ``None`` if the new input format /
    property data is unavailable.
    """
    from dataclasses import replace

    if objective is None:
        objective = MAX_NET_BENEFIT

    use_new_format = 'family' in cfg and 'members' in cfg.get('family', {})
    if not use_new_format:
        return None

    brackets = default_tax_provider().get_combined_brackets()
    config = SimulationConfig.from_dict(cfg)
    primary = config.member_by_role('primary', {})  # #699 seam
    spouse_mem = config.member_by_role('spouse', {})
    income = primary.get('gross_income', 0)
    spouse_income = spouse_mem.get('gross_income', 0)

    state = FamilyState(
        primary_income=income,
        spouse_income=spouse_income,
        primary_marginal_rate=marginal_rate(income, brackets),
        spouse_marginal_rate=marginal_rate(spouse_income, brackets) if spouse_income > 0 else 0,
        primary_rrsp_room=primary.get('rrsp_room_accumulated', 0),
        spouse_rrsp_room=spouse_mem.get('rrsp_room_accumulated', 0),
        primary_tfsa_room=primary.get('tfsa_room_accumulated', 0),
        spouse_tfsa_room=spouse_mem.get('tfsa_room_accumulated', 0),
    )
    # Issue #677: see run_optimization() -- the HELOC's own declared rate,
    # never the mortgage's, resolved via the one canonical helper.
    opt_heloc_rate = resolve_heloc_rate(cfg, default=0.0)
    opt_investment_return = resolve_return_rate(cfg)
    # issue #663: see run_optimization() -- mortgage_cfg must be the
    # property block, not the full cfg (discover_strategies reads
    # mortgage_cfg['heloc_readvance'] as a flat key).
    # issue #713: see run_optimization() -- the household's authored strategies
    # reach this search space too, or the two entry points would rank different
    # things from the same document.
    discovered = discover_strategies(
        state, cfg.get('property', {}),
        investment_return=opt_investment_return,
        heloc_rate=opt_heloc_rate,
        custom_strategies=cfg.get('strategies'),
    )
    # issue #663/DP#32: same hard gate as run_optimization() -- never rank a
    # readvanceable strategy this household has no facility for.
    if not has_readvanceable_facility(cfg):
        discovered = {
            name: strat for name, strat in discovered.items()
            if not strat.prioritize_readvanceable
        }
    if not discovered:
        return None

    rate_path = build_rate_path(
        name="Current variable",
        initial_rate=config.mortgage_rate,
        term_years=config.projection_years,
        rate_type='variable',
        renewal_rates=[config.mortgage_rate],
    )
    cashout = max(0, config.house_value * config.ltv_max - config.mortgage_balance)
    # issue #735: undrawn by default, same fix as run_optimization/
    # GridOptimizer -- this A/B (deduct-now vs. deduct-later) comparison
    # does not itself sweep a draw-fraction dimension (it is not ranking
    # structures, only comparing two deduction TIMINGS on the SAME
    # accumulation decision), so it fixes the bug at the correct default
    # (0.0) rather than adding a sweep loop nothing here would consume.
    lump_sum = config.margin_available * 0.0 + cashout

    # Apples-to-apples: for each strategy, simulate it BOTH ways (the only
    # honest deduct-timing comparison — same allocation, only the deduction
    # timing differs). The previous closed-form compared a one-year lump
    # deduction against a 10-year spread, which is what produced the bogus
    # ~$43k "advantage" (issue #251).
    pairs = []  # list of (name, score_now, score_later)
    for name, strategy in discovered.items():
        scores = {}
        for deduct_later in (False, True):
            strat = replace(strategy, deduct_later=deduct_later)
            res = evaluate_strategy_with_simulation(
                name=name,
                strategy=strat,
                config=config,
                rate_path=rate_path,
                use_readvanceable=strat.prioritize_readvanceable,
                deduct_later=deduct_later,
                lump_sum=lump_sum,
                objective=objective,
                include_year_by_year=False,  # score-only caller — skip year_by_year serialization, #1058
            )
            scores[deduct_later] = res.get('objective_score', res.get('net_benefit', 0))
        pairs.append((name, scores[False], scores[True]))

    if not pairs:
        return None

    # The optimizer's recommended strategy is the top-ranked one; report its own
    # (apples-to-apples) deduct-now vs deduct-later delta so the figure agrees
    # with the simulated ranking. If that strategy is timing-neutral, surface the
    # most deduct-timing-sensitive strategy instead so the user sees the real
    # signal (still simulated, never the bogus closed-form $43k).
    top_ranked = max(pairs, key=lambda p: max(p[1], p[2]))
    if abs(top_ranked[2] - top_ranked[1]) < 1.0:
        chosen = max(pairs, key=lambda p: abs(p[2] - p[1]))
    else:
        chosen = top_ranked
    name, score_now, score_later = chosen

    return {
        'strategy': name,
        'deduct_now': score_now,
        'deduct_later': score_later,
        'advantage_later': score_later - score_now,
        'objective_name': objective.name,
    }


def _has_property_data(cfg: Dict) -> bool:
    """Check if cfg has enough property data for cash-out and LTV analysis."""
    prop = cfg.get('property', {})
    return (prop.get('house_value', 0) > 0
            and prop.get('mortgage_balance', 0) > 0
            and prop.get('margin_available', 0) > 0)


def _has_family_data(cfg: Dict) -> bool:
    """Check if cfg has family member data."""
    return 'family' in cfg and 'members' in cfg.get('family', {})


# ── Anchor & Overlay Presets (DP#5: anchors are decisions, overlays are sensitivities) ─

# DP#5: Anchor decisions represent actual choices the user is weighing
# (e.g., which mortgage rate to accept). These are NOT sensitivity parameters.
ANCHOR_PRESETS = {
    'renew_current': {
        'mortgage_rate': 0.05,  # DP#13: round-number fallback
        'label': 'Renew at current rate (5%)',
    },
    'renew_low': {
        'mortgage_rate': 0.035,  # DP#13: round-number fallback
        'label': 'Renew at low rate (3.5%)',
    },
    'refinance_fixed': {
        'mortgage_rate': 0.04,  # DP#13: round-number fallback
        'label': 'Refinance to fixed (4%)',
    },
}


def discover_rate_anchors(cfg: Dict) -> Dict[str, Dict]:
    """Auto-discover rate anchors from input.json refinance_options and renewal_options.

    DP#5: Anchors represent actual decisions the user is weighing.
    This function reads the user's actual rate offers from their input data
    and generates anchor presets dynamically, replacing hardcoded fallbacks.

    Returns a dict of anchor_name -> {mortgage_rate, label, source, heloc_rate, ...}
    """
    anchors = {}

    # 1. Current rate anchor (always available)
    current_rate = cfg.get('property', {}).get('mortgage_rate', 0.05)
    current_label = f'Renew at current rate ({current_rate*100:.2f}%)'
    anchors['renew_current'] = {
        'mortgage_rate': current_rate,
        'heloc_rate': current_rate,  # Same rate for HELOC and mortgage
        'label': current_label,
        'source': 'current',
    }

    # 2. Renewal options from property.renewal_options
    for opt in cfg.get('property', {}).get('renewal_options', []):
        name = opt.get('name', f"renew_{opt.get('type', 'unknown')}")
        # Create a clean anchor key from the name
        key = name.lower().replace(' ', '_').replace('-', '_')
        # Remove duplicate underscores
        while '__' in key:
            key = key.replace('__', '_')
        anchors[key] = {
            'mortgage_rate': opt.get('rate', 0.05),
            'heloc_rate': opt.get('rate', current_rate),
            'label': opt.get('name', key),
            'source': 'renewal',
            'term_years': opt.get('term_years'),
            'type': opt.get('type'),
            'renewal_rate_assumption': opt.get('renewal_rate_assumption'),
        }

    # 3. Refinance options from property.refinance_options
    for opt in cfg.get('property', {}).get('refinance_options', []):
        name = opt.get('name', f"refi_{opt.get('type', 'unknown')}")
        key = name.lower().replace(' ', '_').replace('-', '_')
        while '__' in key:
            key = key.replace('__', '_')
        anchors[key] = {
            'mortgage_rate': opt.get('rate', 0.05),
            'heloc_rate': opt.get('rate', current_rate),
            'label': opt.get('name', key),
            'source': 'refinance',
            'term_years': opt.get('term_years'),
            'type': opt.get('type'),
            'renewal_rate_assumption': opt.get('renewal_rate_assumption'),
        }

    return anchors

# DP#5: Sensitivity overlays are uncertain parameters that layer on top
# of anchor decisions. The same overlay applies across all anchors.
#
# Issue #285: These named overlay presets are data, not code. They are sourced
# from the user's input config under the `sensitivity_overlay_presets` key (see
# get_sensitivity_overlays). DEFAULT_SENSITIVITY_OVERLAYS holds the built-in
# fallback used when the config does not define any — this preserves identical
# behavior for existing scenarios that have no `sensitivity_overlay_presets` key.
DEFAULT_SENSITIVITY_OVERLAYS = {
    'conservative': {
        'investment_return': 0.05,
        'salary_growth': 0.01,
        'inflation': 0.03,
        'label': 'Conservative (5% return, 1% salary growth, 3% inflation)',
    },
    'moderate': {
        'investment_return': 0.07,
        'salary_growth': 0.02,
        'inflation': 0.025,
        'label': 'Moderate (7% return, 2% salary growth, 2.5% inflation)',
    },
    'aggressive': {
        'investment_return': 0.09,
        'salary_growth': 0.04,
        'inflation': 0.02,
        'label': 'Aggressive (9% return, 4% salary growth, 2% inflation)',
    },
}

# Backward-compatible alias: SENSITIVITY_OVERLAYS remains importable and equals
# the built-in defaults. Runtime code should prefer get_sensitivity_overlays(cfg)
# so user-defined presets in input config take effect (DP#5, issue #285).
SENSITIVITY_OVERLAYS = DEFAULT_SENSITIVITY_OVERLAYS


def get_sensitivity_overlays(cfg: Dict) -> Dict[str, Dict]:
    """Resolve named sensitivity overlay presets from the input config (DP#5).

    Issue #285: Overlay presets are data the user controls, not hardcoded source.
    Reads the `sensitivity_overlay_presets` key from the config; when absent,
    falls back to DEFAULT_SENSITIVITY_OVERLAYS so existing scenarios behave
    identically. Mirrors the discover_rate_anchors() config-sourcing pattern.

    Args:
        cfg: Loaded input configuration dict.

    Returns:
        Mapping of overlay name -> overlay parameter dict.
    """
    overlays = (cfg or {}).get('sensitivity_overlay_presets')
    if not overlays:
        return DEFAULT_SENSITIVITY_OVERLAYS
    return overlays

# ── Forecast Presets (composes ANCHOR + OVERLAY) ────────────────────────────
# Use ANCHOR_PRESETS and SENSITIVITY_OVERLAYS separately for fine-grained control.
# FORECAST_PRESETS bundles anchor + overlay for convenience.
FORECAST_PRESETS = {
    'conservative': {
        'investment_return': 0.05,
        'mortgage_rate': 0.055,
        'salary_growth': 0.01,
        'inflation': 0.03,
        'label': 'Conservative (5% return, 5.5% mortgage, 3% inflation)',
    },
    'moderate': {
        'investment_return': 0.07,
        'mortgage_rate': 0.05,  # DP#13: round-number placeholder
        'salary_growth': 0.02,
        'inflation': 0.025,
        'label': 'Moderate (7% return, 4.95% mortgage, 2.5% inflation)',
    },
    'aggressive': {
        'investment_return': 0.09,
        'mortgage_rate': 0.035,
        'salary_growth': 0.04,
        'inflation': 0.02,
        'label': 'Aggressive (9% return, 3.5% mortgage, 2% inflation)',
    },
}


def apply_preset(cfg: Dict, preset_name: str) -> Dict:
    """Apply a forecast preset overlay to the config.
    
    DP#5/D#21: presets are overlays on the base config, not replacements.
    Only override the variables that the preset specifies.
    
    DEPRECATED: Use apply_anchor_preset() + apply_sensitivity_overlay() instead
    to properly separate anchor decisions from sensitivity overlays (DP#5).
    """
    preset = FORECAST_PRESETS.get(preset_name)
    if not preset:
        return cfg
    
    cfg = deepcopy(cfg)
    if 'investment_return' in preset:
        # DP#21/#591: route through the one helper that targets return_model,
        # the engine's single source of truth. Writing assumptions.investment_return
        # directly is a silent no-op whenever a return_model block is present.
        set_return_rate(cfg, preset['investment_return'])
    if 'mortgage_rate' in preset and 'property' in cfg:
        cfg['property']['mortgage_rate'] = preset['mortgage_rate']
    if 'salary_growth' in preset and 'assumptions' in cfg:
        cfg['assumptions']['salary_growth'] = preset['salary_growth']
    
    return cfg


def apply_anchor_preset(cfg: Dict, anchor_name: str, discovered_anchors: Dict = None) -> Dict:
    """Apply an anchor preset to the config (DP#5: anchor decisions).
    
    Anchors represent actual decisions the user is weighing,
    not uncertain parameters. Examples: mortgage rate, cash-out amount.
    
    If discovered_anchors is provided, uses auto-discovered anchors from
    input.json refinance_options/renewal_options. Otherwise falls back to
    hardcoded ANCHOR_PRESETS (DP#13).
    """
    # Try discovered anchors first, then fall back to hardcoded
    anchor = None
    if discovered_anchors and anchor_name in discovered_anchors:
        anchor = discovered_anchors[anchor_name]
    elif anchor_name in ANCHOR_PRESETS:
        anchor = ANCHOR_PRESETS[anchor_name]
    
    if not anchor:
        return cfg
    
    cfg = deepcopy(cfg)
    if 'mortgage_rate' in anchor:
        if 'property' in cfg:
            cfg['property']['mortgage_rate'] = anchor['mortgage_rate']
    # DP#9/#595-B (epic #603 Track C Phase 2): this used to also write
    # cfg['heloc']['rate'] -- the losing spelling of the HELOC-rate
    # duplicate. heloc.rate had no production reader even before the
    # heloc.* block was deleted outright; assumptions.heloc_rate (below) is
    # the one scenario_discovery.py's strategy-discovery heuristics
    # actually consume (via config_access.resolve_heloc_rate(), issue
    # #677's canonical resolver -- NOT the same thing as #654's
    # property.heloc_rate, which is the base declared rate the core
    # simulation.py pricing engine reads; resolve_heloc_rate() checks
    # assumptions.heloc_rate FIRST specifically so this anchor override
    # wins over -- rather than being shadowed by -- a household's own
    # already-declared property.heloc_rate).
    if 'heloc_rate' in anchor:
        # Discovered anchors may specify a different HELOC rate
        if 'assumptions' in cfg:
            cfg['assumptions']['heloc_rate'] = anchor['heloc_rate']
    elif 'mortgage_rate' in anchor:
        # For hardcoded presets, HELOC rate = mortgage rate. DP#5: this is
        # the anchor's own explicit, labelled modeling choice for a
        # hypothetical renewal scenario ("assume the HELOC also reprices"),
        # not the #654/#677 bug pattern of silently substituting the
        # mortgage rate for an ALREADY-DECLARED, currently-true HELOC rate
        # -- ANCHOR_PRESETS never declares its own heloc_rate, so there is
        # no declared value being silently overridden here.
        if 'assumptions' in cfg:
            cfg['assumptions']['heloc_rate'] = anchor['mortgage_rate']

    return cfg


def apply_sensitivity_overlay(cfg: Dict, overlay_name: str,
                              overlays: Dict = None) -> Dict:
    """Apply a sensitivity overlay to the config (DP#5: sensitivity overlays).

    Overlays are uncertain parameters that layer on top of anchor decisions.
    The same overlay can be applied across all anchors.

    The overlay definitions are sourced from the input config (issue #285): they
    default to those declared under `sensitivity_overlay_presets` in `cfg`,
    falling back to DEFAULT_SENSITIVITY_OVERLAYS. Callers may pass `overlays`
    explicitly to override resolution.
    """
    if overlays is None:
        overlays = get_sensitivity_overlays(cfg)
    overlay = overlays.get(overlay_name)
    if not overlay:
        return cfg
    
    cfg = deepcopy(cfg)
    if 'investment_return' in overlay:
        # DP#21/#591: route through the one helper that targets return_model,
        # the engine's single source of truth. Writing assumptions.investment_return
        # directly is a silent no-op whenever a return_model block is present.
        set_return_rate(cfg, overlay['investment_return'])
    if 'salary_growth' in overlay and 'assumptions' in cfg:
        cfg['assumptions']['salary_growth'] = overlay['salary_growth']
    if 'inflation' in overlay:
        # issue #591: sensitivity_overlay_presets.*.inflation is declared in
        # the schema and was never applied.
        cfg.setdefault('assumptions', {})['inflation'] = overlay['inflation']

    return cfg


def compose_preset(cfg: Dict, anchor_name: str = None, overlay_name: str = None,
                    discovered_anchors: Dict = None, overlays: Dict = None) -> Dict:
    """Compose an anchor + overlay on the config (DP#5).
    
    This is the recommended way to apply presets: choose an anchor decision
    and a sensitivity overlay separately.
    
    Args:
        cfg: Base configuration dict
        anchor_name: Name from ANCHOR_PRESETS or discovered anchors
        overlay_name: Name of a sensitivity overlay preset (e.g., 'moderate')
        discovered_anchors: Auto-discovered anchors from input.json
        overlays: Sensitivity overlay presets; defaults to those resolved from
            cfg via get_sensitivity_overlays() (issue #285)
    
    Returns:
        Modified config dict (deep copy)
    """
    # Always start from a copy to avoid mutating the original
    cfg = deepcopy(cfg)
    if anchor_name:
        cfg = apply_anchor_preset(cfg, anchor_name, discovered_anchors)
    if overlay_name:
        cfg = apply_sensitivity_overlay(cfg, overlay_name, overlays=overlays)
    return cfg


def run_rate_comparison(cfg: Dict, input_path: str, discovered_anchors: Dict) -> List[Dict]:
    """Run optimization across all discovered rate anchors and return comparison.
    
    For each rate anchor (renewal option, refinance option), runs the
    full optimization at the best LTV level and produces a comparison table.
    
    Returns list of result dicts with rate anchor info added.
    """
    results = []
    brackets = default_tax_provider().get_combined_brackets()
    income = next((m['gross_income'] for m in cfg.get('family', {}).get('members', [])
                   if m.get('role') == 'primary'),
                  cfg.get('person', {}).get('annual_income', 0))
    
    for anchor_name, anchor in discovered_anchors.items():
        cfg_anchor = compose_preset(cfg, anchor_name=anchor_name,
                                     discovered_anchors=discovered_anchors)

        # issue #259: refinance to ltv_max through the authoritative overlay path
        # (same as the headline), so cash-out is recorded as debt and invested.
        anchor_ltv = cfg_anchor.get('property', {}).get('ltv_max', 0.80)
        anchor_overlay = _scenario_overlay(cfg_anchor, anchor_ltv,
                                           label=anchor.get('label', anchor_name))
        anchor_results = run_optimization(cfg_anchor, input_path,
                                          overlay=anchor_overlay)
        
        # Find best result
        if anchor_results:
            best = max(anchor_results, key=lambda r: r.get('net_benefit', 0))
            best['anchor_name'] = anchor_name
            best['anchor_label'] = anchor.get('label', anchor_name)
            best['mortgage_rate'] = anchor.get('mortgage_rate', 0)
            best['heloc_rate'] = anchor.get('heloc_rate', anchor.get('mortgage_rate', 0))
            best['source'] = anchor.get('source', 'unknown')
            
            # Compute after-tax HELOC cost
            rate = marginal_rate(income, brackets)
            heloc_rate = best['heloc_rate']
            best['after_tax_heloc_cost'] = heloc_rate * (1 - rate)
            best['net_spread'] = resolve_return_rate(cfg_anchor) - best['after_tax_heloc_cost']
            
            results.append(best)
    
    return results


def print_rate_comparison(results: List[Dict]) -> None:
    """Print formatted rate comparison table."""
    if not results:
        return
    
    print(f"\n{'=' * 120}")
    print(f"  💹 RATE COMPARISON — Best strategy at each rate option")
    print(f"{'=' * 120}")
    print(f"\n  {'Rate Option':<30s} {'Rate':>6s} {'After-tax':>10s} {'Spread':>8s} {'Net Benefit':>12s} {'Total Debt':>12s}")
    print(f"  {'-' * 80}")
    
    for r in sorted(results, key=lambda x: ranking_key(x, x.get('net_benefit', 0)), reverse=True):
        rate_pct = f"{r.get('mortgage_rate', 0)*100:.2f}%"
        at_cost = f"{r.get('after_tax_heloc_cost', 0)*100:.2f}%"
        spread = f"{r.get('net_spread', 0)*100:.2f}%"
        net = r.get('net_benefit', 0)
        debt = r.get('total_debt', 0)
        label = r.get('anchor_label', r.get('anchor_name', '?'))
        marker = ' ★' if r == max(results, key=lambda x: x.get('net_benefit', 0)) else ''
        print(f"  {label:<30s} {rate_pct:>6s} {at_cost:>10s} {spread:>8s} ${net:>10,.0f} ${debt:>10,.0f}{marker}")
    
    # Print recommendation
    best = max(results, key=lambda x: x.get('net_benefit', 0))
    print(f"\n  💡 Best rate option: {best.get('anchor_label', best.get('anchor_name', '?'))} "
          f"at {best.get('mortgage_rate', 0)*100:.2f}% "
          f"→ Net benefit ${best.get('net_benefit', 0):,.0f}")


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, extracted from main() so a test can call
    ``format_help()`` on it directly (issue #658: a bare ``%`` in a help
    string crashed ``--help`` via argparse's ``%``-interpolation; the guard
    invokes this rather than spawning a subprocess -- faster and cleaner).
    """
    parser = argparse.ArgumentParser(description="Strategy Optimizer")
    parser.add_argument("--input", default="input.json", help="Path to input JSON file")
    parser.add_argument("--export-csv", action="store_true")
    parser.add_argument("--export-config", nargs='?', const='-',
                        help='Export current config as JSON (to file or stdout with -). DP#24: config round-trips.')
    parser.add_argument("--html", nargs='?', const="optimizer_report.html",
                       help="Generate standalone HTML report")
    parser.add_argument("--json", nargs='?', const="optimizer_report.json",
                       help="Export machine-readable JSON report")
    parser.add_argument("--txt", nargs='?', const="optimizer_report.txt",
                       help="Export plain-text report to file")
    parser.add_argument("--md", nargs='?', const="optimizer_report.md",
                       help="Export GitHub-flavored Markdown report to file")
    parser.add_argument("--preset", choices=['conservative', 'moderate', 'aggressive'],
                        # Issue #658: argparse runs every help string through
                        # %-interpolation, so a literal percent MUST be escaped
                        # as %% or --help crashes with "not enough arguments for
                        # format string". The rendered output shows a single %.
                        help='Forecast preset: conservative(5%%r/5.5%%m), moderate(7%%/4.95%%), aggressive(9%%/3.5%%). DEPRECATED: use --anchor and --overlay separately (DP#5).')
    parser.add_argument("--anchor", choices=None,
                        help='Anchor decision preset (DP#5): which mortgage/refinance scenario to model. Use --list-anchors to see available options.')
    parser.add_argument("--overlay",
                        help='Sensitivity overlay preset (DP#5): which return/growth/inflation assumptions. '
                             'Names come from `sensitivity_overlay_presets` in input.json '
                             f'(defaults: {", ".join(DEFAULT_SENSITIVITY_OVERLAYS)})')
    parser.add_argument("--compare-rates", action='store_true',
                        help='Run optimization across all rate anchors from input.json and show comparison table')
    parser.add_argument("--list-anchors", action='store_true',
                        help='List all available anchor presets (hardcoded + discovered from input.json)')
    parser.add_argument("--template", nargs='?', const='-',
                        help='Output config template')
    parser.add_argument("--save-session", nargs='?', const='',
                        help='Append this optimization run to a JSONL session file '
                             '(issue #239). Default: optimisations.jsonl next to '
                             'input.json. Pass a path to override. Records '
                             'include the full input, year-by-year actions, '
                             'returns, balances, git commit id, and timestamp.')
    parser.add_argument("--runway-sweep", action='store_true',
                        help='Issue #758: sweep the income-shock DATE and show the '
                             'runway curve (now vs +12mo vs +24mo). Re-runs the '
                             'optimizer once per shock date against the household\'s '
                             'ONE authored decisions.income[] shock scenario, so it '
                             'is OPT-IN (it multiplies the run) -- not on by default.')
    # Issue #862 (DP#22): the objective is data; --objective picks which one the
    # ranking is scored under, overriding a contract's decisions.objective for
    # ad-hoc comparison. No argparse `choices=` (which would emit its own terse
    # error): resolve_objective refuses an unknown name loudly (DP#32), naming
    # the value and listing the valid names -- and --list-objectives prints the
    # menu with descriptions.
    parser.add_argument("--objective", default=None,
                        help='Optimization objective to rank strategies under '
                             '(issue #862). Overrides the contract\'s '
                             'decisions.objective. Default: max_net_benefit. '
                             'See --list-objectives for the full menu.')
    parser.add_argument("--list-objectives", action='store_true',
                        help='List the available optimization objectives '
                             '(name + description) and exit.')
    parser.add_argument("--workers", type=int, default=None,
                        help='Number of worker processes for the scenario '
                             'sweep (perf). Each strategy candidate is an '
                             'independent pure fold, dispatched across cores. '
                             '1 = serial (identical to the pre-parallel path); '
                             'omitted = OPTIMIZE_WORKERS env or '
                             '(cpu_count - 1). Results are collected in a fixed '
                             'order, so the ranking is deterministic regardless '
                             'of worker count.')
    return parser


def _record_asset_location(cfg: Dict, input_path: str,
                           objective: ObjectiveFunction) -> Optional[Dict]:
    """Issue #473: compute + surface the tax-efficient asset-location placement.

    DP#16: gated inside ``recommend_asset_location`` on the household actually
    declaring a foreign sleeve on its registered accounts -- no CLI flag, and no
    extra optimizer pass for the households (the golden included) that have not
    asked this question, for which this returns ``None`` and prints nothing.

    When a recommendation exists it is recorded onto ``cfg['assumptions']``
    (mirroring runway/decumulation_shortfall) so the JSON/HTML/TXT surfaces read
    the SAME recommendation the console prints (DP#9), and returned.
    """
    from asset_location_optimize import (
        recommend_asset_location, format_asset_location)
    asset_location = recommend_asset_location(cfg, input_path, objective=objective)
    if asset_location is None:
        return None
    cfg.setdefault('assumptions', {})['asset_location'] = asset_location
    print()
    print(format_asset_location(asset_location))
    return asset_location


def _record_cross_member_asset_location(cfg: Dict) -> Optional[Dict]:
    """Issue #859 Part B: compute + surface the cross-member asset-location
    placement (which MEMBER's registered account shelters the foreign sleeve for
    the best FAMILY after-tax outcome, #861).

    DP#16/DP#32: gated inside ``recommend_cross_member_location`` on the family
    actually declaring an ``asset_location.cross_member_sleeve`` -- no CLI flag,
    and nothing printed for the households (the golden included) that have not
    asked this question, for which this returns ``None``. The module never
    touches the simulation, so surfacing it cannot move the golden trajectory.

    Real-contract reach is gated on #917 (the adapter threads only ``non_reg``
    composition today); this is reachable from an internally-built multi-member
    config. When a recommendation exists it is recorded onto ``cfg['assumptions']``
    (mirroring #473's asset_location, DP#9) and returned.
    """
    from asset_location_optimize import (
        recommend_cross_member_location, format_cross_member_location)
    rec = recommend_cross_member_location(cfg)
    if rec is None:
        return None
    cfg.setdefault('assumptions', {})['cross_member_asset_location'] = rec
    print()
    print(format_cross_member_location(rec))
    return rec


def _record_risk_allocation(cfg: Dict) -> Optional[Dict]:
    """Issue #474: compute + surface the risk-aware equity/fixed-income mix.

    DP#16/DP#32: gated inside ``recommend_allocation`` on the household actually
    declaring ``portfolio.risk_tolerance`` -- no CLI flag, and nothing printed
    for the households (the golden included) that have not asked this question,
    for which this returns ``None``. The module never touches the simulation, so
    surfacing it cannot move the golden trajectory.

    When a recommendation exists it is recorded onto ``cfg['assumptions']``
    (mirroring #473's asset_location) so the JSON/HTML/TXT surfaces read the SAME
    recommendation the console prints (DP#9), and returned.
    """
    from risk_allocation import recommend_allocation, format_allocation
    risk_allocation = recommend_allocation(cfg)
    if risk_allocation is None:
        return None
    cfg.setdefault('assumptions', {})['risk_allocation'] = risk_allocation
    print()
    print(format_allocation(risk_allocation))
    return risk_allocation


def main():
    parser = build_parser()
    args = parser.parse_args()

    # Perf: fix the scenario-sweep worker count for this run. An explicit
    # --workers wins over the OPTIMIZE_WORKERS env / default; --workers 1 forces
    # the serial path (provably identical to the pre-parallel version).
    if args.workers is not None:
        set_workers(args.workers)

    # Issue #862 (DP#22): the objective menu -- no input document needed, since
    # the objectives are a static registry, not derived from the household.
    if args.list_objectives:
        print(f"\n{'=' * 80}")
        print(f"  🎯 AVAILABLE OPTIMIZATION OBJECTIVES")
        print(f"{'=' * 80}")
        print(f"\n  The optimizer RANKS; you CHOOSE (DP#22). Select one with")
        print(f"  --objective <name> or a contract's decisions.objective.")
        print(f"  Default (nothing declared): max_net_benefit\n")
        for line in _list_objectives_text():
            print(line)
        return

    if args.template is not None:
        # Epic #603 Track C Phase 2b: the template is now the input contract's
        # own populated example (schema/example.json), the only committed
        # document that validates against schema/input_schema.json +
        # schema/countries/canada/input_schema.json -- the deleted root
        # input_schema.json was an example-instance file of the retired
        # legacy shape, not the contract itself (DP#9).
        import shutil
        schema_path = contract_schema.EXAMPLE_PATH
        if args.template == '-':
            with open(schema_path) as f:
                print(f.read())
        else:
            shutil.copy2(schema_path, args.template)
            print(f'Template written to {args.template}')
        return

    # Epic #603 Track C Phase 2b: args.input is a contract document (the
    # sole wire format) -- validate + map to the internal shape this CLI's
    # discovery/optimization pipeline (discover_rate_anchors,
    # get_sensitivity_overlays, build_all_overlays, ...) operates on.
    import input_contract
    cfg = input_contract.load_and_map(args.input)
    # Issue #239: keep the verbatim input config (before preset overlays) so
    # the session record stores exactly what the user provided.
    raw_cfg = deepcopy(cfg)

    # Issue #862 (DP#22/DP#5): resolve the objective ONCE, here, off the
    # freshly-loaded config -- before any --anchor/--overlay/--preset rebuild of
    # cfg -- so the choice survives regardless of what the overlay path does to
    # the internal shape, and every scoring pass below is handed the SAME
    # ObjectiveFunction. CLI --objective wins over the contract's
    # decisions.objective, which wins over the max_net_benefit default. An
    # unknown name is refused loudly (DP#32) rather than silently scored under
    # the default.
    try:
        objective = resolve_objective(args.objective, cfg)
    except ObjectiveSelectionError as e:
        print(f"Error: {e}")
        return
    # DP#5 + DP#24: Auto-discover rate anchors from input.json
    discovered_anchors = discover_rate_anchors(cfg)
    # DP#5 + issue #285: Sensitivity overlay presets are sourced from input config
    sensitivity_overlays = get_sensitivity_overlays(cfg)

    # Merge discovered anchors with hardcoded presets (discovered takes priority)
    all_anchors = {**ANCHOR_PRESETS, **discovered_anchors}
    
    # --list-anchors: show all available anchor presets and exit
    if args.list_anchors:
        print(f"\n{'=' * 80}")
        print(f"  💹 AVAILABLE RATE ANCHORS")
        print(f"{'=' * 80}")
        print(f"\n  Hardcoded presets (fallback):")
        for name, preset in ANCHOR_PRESETS.items():
            marker = ' (overridden by input.json)' if name in discovered_anchors else ''
            print(f"    {name}: {preset['label']} — {preset['mortgage_rate']*100:.2f}%{marker}")
        print(f"\n  Discovered from input.json:")
        for name, preset in discovered_anchors.items():
            print(f"    {name}: {preset.get('label', name)} — {preset.get('mortgage_rate', 0)*100:.2f}% ({preset.get('source', 'unknown')})")
        if not discovered_anchors:
            print(f"    (no renewal_options or refinance_options found in input.json)")
        return
    
    # Validate --anchor against all available anchors
    if args.anchor and args.anchor not in all_anchors:
        print(f"Error: Unknown anchor '{args.anchor}'. Available anchors:")
        for name in all_anchors:
            print(f"  {name}")
        return

    # Validate --overlay against config-sourced overlays (issue #285)
    if args.overlay and args.overlay not in sensitivity_overlays:
        print(f"Error: Unknown overlay '{args.overlay}'. Available overlays:")
        for name in sensitivity_overlays:
            print(f"  {name}")
        return
    
    # --compare-rates: run across all rate anchors and show comparison
    if args.compare_rates:
        rate_results = run_rate_comparison(cfg, args.input, discovered_anchors)
        if rate_results:
            print_rate_comparison(rate_results)
        else:
            print("No rate anchors found. Add renewal_options or refinance_options to input.json.")
        return
    
    # DP#5: apply anchor + overlay separately (preferred), or legacy preset
    if args.anchor or args.overlay:
        cfg = compose_preset(cfg, anchor_name=args.anchor, overlay_name=args.overlay,
                             discovered_anchors=discovered_anchors,
                             overlays=sensitivity_overlays)
    elif args.preset:
        cfg = apply_preset(cfg, args.preset)
    
    # DP#24: --export-config round-trips SimulationConfig's INTERNAL dict
    # shape (unchanged by epic #603 Track C Phase 2b -- see
    # simulation_config.py's from_dict docstring). NOTE (honest limitation,
    # not silently broken): this is not a contract document -- feeding this
    # export back in via --input will fail validate_contract, because
    # to_dict() was never rewritten to emit the contract shape. That reverse
    # mapping (internal shape -> contract document) is real, separate work,
    # not part of this phase's ingestion-boundary rewrite.
    if args.export_config is not None:
        config = SimulationConfig.from_dict(cfg)
        exported = config.to_dict()
        if args.export_config == '-':
            print(json.dumps(exported, indent=2))
        else:
            with open(args.export_config, 'w') as f:
                json.dump(exported, f, indent=2)
            print(f"Config exported to {args.export_config}")
        return

    brackets = default_tax_provider().get_combined_brackets()
    use_new_format = _has_family_data(cfg)

    if use_new_format:
        primary = find_member_by_role(cfg['family']['members'], 'primary', {})  # #699 seam
        spouse_mem = find_member_by_role(cfg['family']['members'], 'spouse', {})
        income = primary.get('gross_income', 0)  # DP#13: personal data, not a default
        spouse_income = spouse_mem.get('gross_income', 0)
    else:
        income = cfg['person']['annual_income']
        spouse_income = 0

    # issue #663/DP#32: "no HELOC" is a first-class state, not a missing key
    # or a $0 margin_available. A contract with no kind=heloc liability has
    # no margin_available key at all (input_contract.py deliberately omits
    # it); has_readvanceable_facility() is the one predicate every consumer
    # asks instead of indexing/defaulting the key directly.
    has_heloc = has_readvanceable_facility(cfg)
    mortgage_balance = cfg.get('property', {}).get('mortgage_balance', 0)
    house_value = cfg.get('property', {}).get('house_value', 0)
    ltv_max = cfg.get('property', {}).get('ltv_max', 0.80)
    cash_out = max(0, house_value * ltv_max - mortgage_balance)
    # DP#13: a missing investment_return is missing data, not a 7% opinion.
    # Fall back to a round placeholder that makes the absence obvious.
    # DP#21: resolved from return_model (issue #591), not the deprecated
    # assumptions.investment_return scalar, so this reflects any overlay
    # (--overlay/--preset) actually applied to cfg above.
    ret = resolve_return_rate(cfg, default=0)

    rate = marginal_rate(income, brackets)

    print("\n" + "=" * 120)
    print("🏦 STRATEGY OPTIMIZER")
    print("=" * 120)
    print(f"\n  📋 INPUTS (from {args.input})")
    if args.preset:
        print(f"     ⚡ Preset: {args.preset} — {FORECAST_PRESETS[args.preset]['label']}")
    if args.anchor:
        anchor_info = all_anchors.get(args.anchor, {})
        print(f"     ⚓ Anchor: {args.anchor} — {anchor_info.get('label', 'unknown')} ({anchor_info.get('mortgage_rate', 0)*100:.2f}%)")
    if args.overlay:
        overlay_info = sensitivity_overlays.get(args.overlay, {})
        print(f"     🌦️ Overlay: {args.overlay} — {overlay_info.get('label', 'unknown')}")
    print(f"     Income: ${income:,.0f} → Marginal rate: {rate*100:.2f}%")
    if spouse_income:
        print(f"     Spouse income: ${spouse_income:,.0f}")
    print(f"     House: ${house_value:,.0f} | Mortgage: ${mortgage_balance:,.0f}")
    if has_heloc:
        margin = cfg.get('property', {}).get('margin_available', 0)
        # Issue #677: this used to read property.mortgage_rate as a
        # stand-in for the HELOC's own rate -- the exact aliasing bug the
        # comment in the `else` branch below already calls out as #654's,
        # still live right here. Resolved via the canonical helper instead.
        heloc_rate = resolve_heloc_rate(cfg, default=0.0)
        sm_cost = heloc_rate * (1 - rate)
        print(f"     Margin: ${margin:,.0f} | Cash-out: ${cash_out:,.0f} | Total: ${margin + cash_out:,.0f}")
        print(f"     HELOC: {heloc_rate*100:.2f}% → After-tax (SM): {sm_cost*100:.2f}%")
        print(f"     Net spread: {ret*100:.0f}% - {sm_cost*100:.2f}% = {(ret - sm_cost)*100:.2f}%")
    else:
        # Symptom 2 (#663): there is no HELOC on this contract -- do not
        # print a HELOC rate, an after-tax SM cost, or a net spread for a
        # strategy this household structurally cannot execute. The
        # mortgage rate is NOT a stand-in for a HELOC rate (that aliasing
        # is #654's bug); simply don't quote one.
        n_sm_strategies = len({s.name for s in STRATEGIES.values() if s.prioritize_readvanceable})
        print(f"     Cash-out: ${cash_out:,.0f}")
        print(f"     Smith Manoeuvre unavailable: no readvanceable line on this "
              f"contract ({n_sm_strategies} strateg{'y' if n_sm_strategies == 1 else 'ies'} skipped).")

    # Model fidelity (issue #585 / DP#32): units + any approximation that
    # biases a headline figure for this run — printed unconditionally, not
    # buried in a docstring only the source reader ever sees. The objective is
    # named because several caveats are objective-specific (a pre-tax
    # terminal-wealth caveat must not fire for an after-tax objective); issue
    # #862 makes it the RESOLVED objective (CLI/contract/default), so the
    # caveats reflect the objective the ranking below is actually scored on.
    print(f"\n  🔍 MODEL FIDELITY")
    for line in model_fidelity.render_text(cfg, objective.name):
        print(f"     {line}")

    # RRSP optimization summary
    rrsp_room_primary = primary.get('rrsp_room_accumulated', 0) if use_new_format else cfg.get('accounts', {}).get('rrsp_room', 0)
    rrsp_room_spouse = spouse_mem.get('rrsp_room_accumulated', 0) if use_new_format else cfg.get('accounts', {}).get('spouse_rrsp_room', 0)
    rrsp_room = rrsp_room_primary + rrsp_room_spouse
    if rrsp_room > 0:
        # Issue #251: the deduct-timing comparison is DERIVED FROM THE SIMULATION
        # (the single source of truth), not a standalone closed-form. We run each
        # discovered strategy both ways (deduct_later False/True) and report the
        # best of each bucket — mirroring simulate.py's deduct_later_options anchor.
        timing = simulated_deduct_timing(cfg, args.input, objective=objective)
        if timing is not None:
            advantage = timing['advantage_later']
            print(f"\n  🎯 RRSP DEDUCTION TIMING (Room: ${rrsp_room:,.0f}) — simulated [{timing['strategy']}]")
            print(f"     Deduct now:   ${timing['deduct_now']:,.0f}")
            print(f"     Deduct later: ${timing['deduct_later']:,.0f}")
            if advantage > 0:
                print(f"     Advantage of deduct later: ${advantage:,.0f}")
            elif advantage < 0:
                print(f"     Advantage of deduct now:   ${-advantage:,.0f}")
            else:
                print("     Deduct timing is neutral for this strategy.")

    # ── Auto: Minimum extraction analysis (when property data available) ──
    if _has_property_data(cfg):
        plan = compute_min_extraction(cfg, brackets, investment_return=resolve_return_rate(cfg))
        print_cashout_report(plan)

    # ── Auto: LTV exploration (when property data available) ──
    if _has_property_data(cfg):
        ltv_results = run_ltv_exploration(cfg, args.input, objective=objective)
        _print_ltv_exploration(ltv_results)
    else:
        ltv_results = None

    # ── Strategy ranking at current LTV, across every declared income scenario ──
    # issue #259: the headline refinances to ltv_max through the authoritative
    # overlay path (same as simulate.py and the LTV-exploration 80% row), so the
    # cash-out is recorded as debt and invested — not silently dropped.
    # year_by_year is always threaded through (issue #248); the JSONL session
    # logger (issue #239) persists it via --save-session below.
    # issue #665: decisions.income[] ("stay at current job" / "salary cut" /
    # "job loss, EI only") is run for real here — one full optimization pass
    # PER declared income scenario, not just the base config's income.
    results = run_income_scenario_exploration(cfg, args.input, ltv_max=ltv_max,
                                              objective=objective)
    n_income_scenarios = len(set(r['income_scenario_id'] for r in results))

    # Issue #707: record the worst-case decumulation shortfall onto cfg BEFORE
    # any surface renders, so the model_fidelity caveat (which reads
    # assumptions.decumulation_shortfall) fires identically in TXT/JSON/HTML,
    # and the dedicated console section below names the same year and gap.
    # Stored under `assumptions` (mirroring #685's assumptions.rate_path_conflicts)
    # because a top-level key would be rejected by _validate_internal_shape
    # (build_overlay_config -> from_dict is on every HtmlReport path).
    cfg.setdefault('assumptions', {})['decumulation_shortfall'] = worst_drawdown_shortfall(results)
    # Issue #170: record whether any ranked scenario refused a declared RRSP
    # contribution (over-room slice clipped to $0) onto cfg BEFORE any surface
    # renders, so the model_fidelity caveat (which reads
    # assumptions.rrsp_contribution_refused) fires identically in TXT/JSON/
    # HTML. Same bridge, same spelling, as decumulation_shortfall above
    # (#707/DP#9); the worst-across-scenarios reduction is the pure
    # ``worst_rrsp_refusal`` beside the summarizer.
    from rules_contributions import worst_rrsp_refusal
    cfg.setdefault('assumptions', {})['rrsp_contribution_refused'] = \
        worst_rrsp_refusal([r.get('rrsp_refusal') for r in results
                            if isinstance(r, dict)])
    # Issue #758: record the worst-case (shortest-runway) scenario's verdict
    # onto cfg BEFORE any surface renders, so the model_fidelity runway caveats
    # (which read assumptions.runway) fire identically in TXT/JSON/HTML and the
    # console runway section below names the same scenario. Same bridge, same
    # spelling, as decumulation_shortfall above (DP#9).
    from runway import worst_runway_summary
    cfg.setdefault('assumptions', {})['runway'] = worst_runway_summary(results)
    _print_decumulation_shortfall_report(results, cfg)

    # Issue #758: the shock-date sweep -- OPT-IN (--runway-sweep), because it
    # re-runs the optimizer once per shock date. Shows runway for a shock now
    # vs in 12 vs 24 months: the curve, not a point. Skipped silently when the
    # household authored no dated income shock (run_runway_sweep returns []).
    runway_sweep_curve = _record_runway_sweep(args, cfg, args.input, ltv_max,
                                              objective=objective)

    # DP#8/DP#25: the ranked-strategies table is pure presentation over
    # `results`, which are already plain Python — it never needed pandas and
    # works identically in every environment.
    # Issue #862 (DP#22): rank on the RESOLVED objective's score, not a
    # hardcoded net_benefit, so a chosen --objective / decisions.objective
    # actually reorders the headline. objective_score == net_benefit for the
    # default max_net_benefit (same underlying compute_net_benefit), so the
    # default run's ordering is unchanged; a non-default objective ranks by
    # what it measures.
    results_sorted = sorted(
        results,
        key=lambda r: ranking_key(r, r.get('objective_score', r.get('net_benefit', 0))),
        reverse=True)
    _print_headline_basis(cfg, ltv_max)
    if n_income_scenarios > 1:
        print(f"  ({n_income_scenarios} income scenarios from decisions.income[] — see "
              f"RECOMMENDATION BY INCOME SCENARIO below)")
        print(f"\n  {'#':<3} {'Strategy':<40} {'Income scenario':<20} {'Net':>9}")
        print(f"  {'-'*85}")
        for i, r in enumerate(results_sorted):
            name = r.get('strategy', '?')
            if r.get('deduct_later'):
                name += " 📋"
            print(f"  {i+1:<3} {name:<40} {r.get('income_scenario_label', '?'):<20} "
                  f"${r.get('net_benefit', 0)/1000:>7.0f}k")
    else:
        print(f"\n  {'#':<3} {'Strategy':<55} {'Net':>9}")
        print(f"  {'-'*70}")
        for i, r in enumerate(results_sorted):
            name = r.get('strategy', '?')
            if r.get('deduct_later'):
                name += " 📋"
            print(f"  {i+1:<3} {name:<55} ${r.get('net_benefit', 0)/1000:>7.0f}k")

    # Issue #672: a household that declared its estate elections (`estate` is
    # a required schema key, so every contract-sourced run does) has told the
    # tool who dies first and what the rollover election is -- it has asked
    # to be scored on it. net_benefit above is EXACTLY insensitive to those
    # elections (#661's VOI sweep, measured, not theorised); print the same
    # strategies ranked by max_after_tax_estate side by side, rather than
    # leaving that $0 sensitivity invisible.
    if estate_is_declared(cfg):
        _print_estate_ranking(results_sorted, results)

    _print_income_scenario_report(results)

    # Issue #473: asset-location placement as an optimizable dimension.
    _record_asset_location(cfg, args.input, objective)

    # Issue #859 Part B: asset location ACROSS family members.
    _record_cross_member_asset_location(cfg)

    # Issue #474: risk-aware equity/fixed-income allocation recommendation.
    _record_risk_allocation(cfg)

    # Issue #687: mortgage-STRUCTURE ranking (all-mortgage vs. readvanceable
    # vs. mortgage+revolving-line), per declared income scenario. DP#16:
    # gated on the household actually having declared
    # decisions.mortgage.structure_options -- no CLI flag, no opt-in, and no
    # extra optimizer pass for the households that have not asked this
    # question yet.
    #
    # Issue #845: the cross is composed ONCE here and handed to both the
    # scorer and the reporter, so the tables and the "NOT SCORED" notice
    # cannot disagree about which cells existed (DP#9).
    if cfg.get('property', {}).get('structure_options'):
        structure_cells = structure_refinance_cells(cfg)
        structure_results = run_mortgage_structure_exploration(
            cfg, args.input, cells=structure_cells, objective=objective)
        _print_structure_report(structure_results, cells=structure_cells)

    # Issue #1011: property-purchase FUNDING ranking (all-cash vs. down-
    # payment+mortgage at declared LTV), per declared income scenario. DP#16:
    # gated on the household actually having declared
    # ``purchase.funding_options`` on a dated purchase -- no CLI flag, no
    # opt-in, and no extra optimizer pass for the households that have not
    # asked this question yet. The gate reads the SAME cells the scorer and
    # reporter consume (DP#9), so a household with no funding_options never
    # reaches the exploration and the golden trajectory is byte-identical
    # to #967/#696 (DP#32).
    from scenario_discovery import discover_property_funding_cells
    if discover_property_funding_cells(cfg):
        funding_results = run_property_funding_exploration(
            cfg, args.input, objective=objective)
        _print_property_funding_report(funding_results)

    # Issue #1036: borrow-to-invest ranking (draw $X against a declared HELOC
    # at year 0, invest in non-reg, deduct the interest under ITA s.20(1)(c)).
    # DP#16: gated on the household actually having declared
    # ``decisions.borrow_to_invest`` -- no CLI flag, no opt-in, and no extra
    # optimizer pass for the households that have not asked this question yet.
    # A mortgage-free household with a HELOC can express leverage this way; a
    # household that declares none never reaches the exploration and the golden
    # trajectory is byte-identical (DP#32).
    if cfg.get('borrow_to_invest_options'):
        btv_results = run_borrow_to_invest_exploration(
            cfg, args.input, objective=objective)
        _print_borrow_to_invest_report(btv_results)

    # ── Export ── (DP#15: output files go to ~/.cache, not repo root)
    if args.export_csv:
        _export_ranked_csv(results, ltv_results)

    # Pluggable output formats work without pandas (DP#8): HTML/JSON/TXT
    # and the year-by-year long-format CSV (issue #248).
    _export_reports(args, results, cfg)
    _save_session_if_requested(args, results, raw_cfg, all_anchors)

    print()


def _export_ranked_csv(results: List[Dict], ltv_results: Optional[List[Dict]]) -> None:
    """Write the flat ranked-summary CSV(s) requested via ``--export-csv``.

    Issue #367: pandas is an optional runtime dependency (see pyproject.toml)
    used *only* here — the ranked-strategies table and every other output
    format (HTML/JSON/TXT, year-by-year CSV) are plain Python and work
    without it. When pandas is missing, this used to silently skip writing
    ``strategy_results.csv`` / ``ltv_exploration_results.csv`` with no error
    and no message: a user who explicitly asked for ``--export-csv`` would
    quietly get one file instead of two (or none) and never know why. Same
    failure class as the rest of epic #603 — absence must fail loudly, not
    substitute a quieter, wrong-er result.
    """
    try:
        import pandas as pd
    except ImportError as exc:
        raise ModuleNotFoundError(
            "--export-csv requires pandas, which is not installed. "
            "Install it with `pip install pandas` or `pip install -e '.[dev]'`."
        ) from exc

    from output_paths import output_path

    df = pd.DataFrame(results).sort_values('net_benefit', ascending=False)
    csv_path = output_path("strategy_results.csv")
    # Drop the nested per-year series column from the flat summary CSV;
    # it is exported separately as a tidy long-format file (issue #248).
    df.drop(columns=['year_by_year'], errors='ignore').to_csv(csv_path, index=False)
    print(f"\n  📁 Strategy results exported to {csv_path}")
    if ltv_results:
        ltv_path = output_path("ltv_exploration_results.csv")
        pd.DataFrame(ltv_results).to_csv(ltv_path, index=False)
        print(f"  📁 LTV results exported to {ltv_path}")


def _save_session_if_requested(args, results: List[Dict], raw_cfg: Dict,
                              all_anchors: Dict) -> None:
    """Append this optimization run to a JSONL session file (issue #239).

    No-op when ``--save-session`` was not passed. Otherwise appends one JSON
    line to ``optimisations.jsonl`` next to ``input.json`` (or the path given
    to ``--save-session``), capturing the verbatim input, the per-scenario
    year-by-year series, git commit id, and timestamp so successive runs can
    be compared as input.json and code change.
    """
    if args.save_session is None:
        return
    from session_log import (
        build_session_record, save_session as write_session, default_session_path,
    )
    anchor_info = all_anchors.get(args.anchor, {}) if args.anchor else None
    session_path = args.save_session if args.save_session else default_session_path(args.input)
    record = build_session_record(
        input_path=args.input,
        input_cfg=raw_cfg,
        results=results,
        objective_name=(results[0].get('objective_name', '') if results else ''),
        extra={
            'anchor': args.anchor,
            'anchor_label': anchor_info.get('label') if anchor_info else None,
            'overlay': args.overlay,
            'preset': args.preset,
        },
    )
    written = write_session(record, session_path)
    commit = record.get('git_commit') or 'unknown'
    dirty = ' (dirty)' if record.get('git_dirty') else ''
    print(f"  🗂️  Session appended to {written} (commit {commit}{dirty})")


def _export_reports(args, results: List[Dict], cfg: Dict) -> None:
    """Write pluggable output formats and the year-by-year CSV (issue #248).

    DP#8/DP#25: pure presentation. Works without pandas so the per-year
    breakdown is reachable in every environment.
    """
    from output_plugins import (
        HtmlReport, JsonReport, TextReport, MarkdownReport,
        write_year_by_year_csv,
    )

    if args.export_csv:
        from output_paths import output_path
        yby_path = output_path("strategy_results_year_by_year.csv")
        n_rows = write_year_by_year_csv(results, yby_path)
        if n_rows:
            print(f"  📁 Year-by-year results exported to {yby_path}")
    if args.html:
        HtmlReport(results, cfg, title="Strategy Optimizer Results").write(args.html)
        print(f"  🌐 HTML: {args.html}")
    if args.json:
        JsonReport(results, cfg, title="Strategy Optimizer Results").write(args.json)
        print(f"  📋 JSON: {args.json}")
    if args.txt:
        TextReport(results, cfg, title="Strategy Optimizer Results").write(args.txt)
        print(f"  📄 Text: {args.txt}")
    if args.md:
        MarkdownReport(results, cfg, title="Strategy Optimizer Results").write(args.md)
        print(f"  📝 Markdown: {args.md}")


if __name__ == "__main__":
    main()